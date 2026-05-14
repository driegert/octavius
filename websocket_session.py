from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

try:
    from fastapi import WebSocket, WebSocketDisconnect
except ModuleNotFoundError:
    WebSocket = Any

    class WebSocketDisconnect(RuntimeError):
        pass
from conversation import Conversation
from reader_playback import stream_reader_audio
from settings import settings
from subagent import run_subagent
from subagent_dispatcher import SubagentTicket

log = logging.getLogger(__name__)


def build_item_chat_context(item: dict, item_id: int) -> str:
    preview = item["content"][:500] + ("..." if len(item["content"]) > 500 else "")
    return (
        "\n\nYou are discussing a saved inbox item with Dave.\n"
        f"Title: {item['title']}\nType: {item['item_type']}\n"
        f"Preview: {preview}\n\n"
        "Use the read_item_content tool to fetch the full content "
        f"or specific sections when you need more detail. The item ID is {item_id}."
    )


def create_item_conversation(item: dict, item_id: int) -> Conversation:
    conversation = Conversation()
    conversation._messages[0]["content"] += build_item_chat_context(item, item_id)
    return conversation


VAD_SILENCE_SECONDS = 1.5  # auto-stop after this much silence post-speech

# Max chars in the per-delegation preview line shown in the UI badge.
_DELEGATION_PREVIEW_CHARS = 180


def _build_preview(text: str) -> str:
    """Build a single-line preview of a delegation result for the UI badge."""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if len(first_line) <= _DELEGATION_PREVIEW_CHARS:
        return first_line
    cut = first_line[:_DELEGATION_PREVIEW_CHARS].rsplit(" ", 1)[0]
    return cut + "..."


@dataclass
class StreamingSTTState:
    """Per-session state for streaming speech-to-text."""
    active: bool = False
    pcm_buffer: bytes = b""
    last_text: str = ""
    speech_detected: bool = False
    silence_start: float | None = None
    _transcribe_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reset(self):
        self.active = False
        self.pcm_buffer = b""
        self.last_text = ""
        self.speech_detected = False
        self.silence_start = None


@dataclass
class DelegationRecord:
    handle: str
    domain: str
    submitted_task: str
    ticket: SubagentTicket
    created_at: datetime
    task: asyncio.Task | None = None
    status: str = "running"  # 'running' | 'ready' | 'failed'
    result: str | None = None  # full result text including TOOL DATA, parked for pull
    preview: str | None = None  # short one-line preview for the UI badge
    error: str | None = None


@dataclass
class ProactiveMessage:
    handle: str
    domain: str
    text: str


@dataclass
class WebSocketSessionState:
    ws: Any
    history: object
    mcp_manager: object
    subagent_dispatcher: object
    conversation: Conversation = field(default_factory=Conversation)
    voice: str = settings.tts.voice
    tts_enabled: bool = True
    reader_task: asyncio.Task | None = None
    item_conversations: dict[int, Conversation] = field(default_factory=dict)
    item_history_sessions: dict[int, object] = field(default_factory=dict)
    history_session: object | None = None
    stt_stream: StreamingSTTState = field(default_factory=StreamingSTTState)
    vad: object | None = None  # SileroVAD instance, created on first stt_start
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_task: asyncio.Task | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    delegations: dict[str, DelegationRecord] = field(default_factory=dict)
    proactive_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    proactive_worker_task: asyncio.Task | None = None
    # When False (default), finished delegations park their results on the
    # record and the UI shows a non-intrusive badge instead of speaking
    # results aloud. Flip to True to restore the old interrupt-and-speak path.
    proactive_speak_enabled: bool = False


class WebSocketSessionHandler:
    def __init__(self, ws: Any):
        self.state = WebSocketSessionState(
            ws=ws,
            history=ws.app.state.history,
            mcp_manager=ws.app.state.mcp_manager,
            subagent_dispatcher=ws.app.state.subagent_dispatcher,
        )

    @property
    def ws(self) -> Any:
        return self.state.ws

    async def send_json(self, msg_type: str, text: str):
        async with self.state.send_lock:
            await self.ws.send_text(json.dumps({"type": msg_type, "text": text}))

    async def send_payload(self, payload: dict):
        async with self.state.send_lock:
            await self.ws.send_text(json.dumps(payload))

    async def send_bytes(self, data: bytes):
        async with self.state.send_lock:
            await self.ws.send_bytes(data)

    async def start(self):
        await self.ws.accept()
        log.info("WebSocket connected")
        self.state.history_session = self.state.history.start_conversation(
            service="octavius",
            source="voice",
            model=settings.llm_chain[0]["model"],
        )
        self.state.proactive_worker_task = asyncio.create_task(self._proactive_worker())
        await self.send_payload(
            {
                "type": "session_id",
                "conversation_id": self.state.history_session.conv_id,
            }
        )

    async def cleanup(self):
        if self.state.turn_task and not self.state.turn_task.done():
            self.state.turn_task.cancel()
        if self.state.reader_task and not self.state.reader_task.done():
            self.state.reader_task.cancel()
        for record in list(self.state.delegations.values()):
            if record.task and not record.task.done():
                record.task.cancel()
        if self.state.proactive_worker_task and not self.state.proactive_worker_task.done():
            self.state.proactive_worker_task.cancel()
        for history_session in self.state.item_history_sessions.values():
            await history_session.end_async()
        if self.state.history_session:
            await self.state.history_session.end_async()

    async def run(self):
        await self.start()
        try:
            while True:
                message = await self.ws.receive()
                if "text" in message:
                    await self.handle_text_message(message["text"])
                    continue
                if "bytes" in message:
                    if self.state.stt_stream.active:
                        await self.handle_stt_chunk(message["bytes"])
                    else:
                        await self.handle_audio_message(message["bytes"])
        except (WebSocketDisconnect, RuntimeError):
            log.info("WebSocket disconnected")
            await self.cleanup()

    async def handle_text_message(self, text: str):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type")
        handlers = {
            "ping": self.handle_ping,
            "restore_session": self.handle_restore_session,
            "reset": self.handle_reset,
            "load_conversation": self.handle_load_conversation,
            "settings": self.handle_settings,
            "text_input": self.handle_text_input,
            "reader_play": self.handle_reader_play,
            "reader_pause": self.handle_reader_pause,
            "reader_stop": self.handle_reader_stop,
            "item_chat_load": self.handle_item_chat_load,
            "item_chat": self.handle_item_chat,
            "item_chat_reset": self.handle_item_chat_reset,
            "stt_start": self.handle_stt_start,
            "stt_stop": self.handle_stt_stop,
            "delegation_list": self.handle_delegation_list,
            "delegation_pull": self.handle_delegation_pull,
            "delegation_dismiss": self.handle_delegation_dismiss,
        }
        handler = handlers.get(msg_type)
        if handler:
            await handler(data)

    async def handle_ping(self, _data: dict):
        await self.send_payload({"type": "pong"})

    async def handle_restore_session(self, data: dict):
        from history import get_conversation_messages
        old_conv_id = data.get("conversation_id")
        if not old_conv_id:
            return
        with self.state.history.connect() as conn:
            msgs = get_conversation_messages(conn, old_conv_id)
        if msgs:
            self.state.conversation.load_from_history(msgs)
            log.info("Restored conversation %d on reconnect (%d messages)", old_conv_id, len(msgs))
            await self.send_payload(
                {
                    "type": "session_id",
                    "conversation_id": self.state.history_session.conv_id,
                }
            )

    async def handle_reset(self, _data: dict):
        await self.state.history_session.end_async()
        self.state.conversation.reset()
        self.state.history_session = self.state.history.start_conversation(
            source="voice",
            model=settings.llm_chain[0]["model"],
        )
        await self.send_payload(
            {
                "type": "session_id",
                "conversation_id": self.state.history_session.conv_id,
            }
        )
        await self.send_json("status", "Conversation reset.")
        log.info("Conversation reset by client")

    async def handle_load_conversation(self, data: dict):
        from history import get_conversation_messages
        conv_id = data.get("conversation_id")
        if not conv_id:
            return
        with self.state.history.connect() as conn:
            msgs = get_conversation_messages(conn, conv_id)
        if not msgs:
            await self.send_json("status", "Conversation not found.")
            return
        await self.state.history_session.end_async()
        self.state.conversation.load_from_history(msgs)
        self.state.history_session = self.state.history.start_conversation(
            source="voice",
            model=settings.llm_chain[0]["model"],
        )
        history_pairs = [
            {"role": message["role"], "content": message["content"]}
            for message in msgs
            if message["role"] in ("user", "assistant") and message.get("content")
        ]
        await self.send_payload(
            {
                "type": "conversation_loaded",
                "conversation_id": conv_id,
                "messages": history_pairs,
            }
        )
        log.info("Loaded conversation %d (%d messages)", conv_id, len(msgs))

    async def handle_settings(self, data: dict):
        if "voice" in data:
            self.state.voice = data["voice"]
            log.info("Voice set to %s", self.state.voice)
        if "tts" in data:
            self.state.tts_enabled = data["tts"]
            log.info("TTS %s", "enabled" if self.state.tts_enabled else "disabled")
        if "proactive_speak" in data:
            self.state.proactive_speak_enabled = bool(data["proactive_speak"])
            log.info(
                "Proactive speak %s",
                "enabled" if self.state.proactive_speak_enabled else "disabled",
            )

    async def handle_text_input(self, data: dict):
        user_text = data.get("text", "").strip()
        if not user_text:
            return
        await self.send_json("transcript", user_text)
        self._spawn_turn(user_text, source="text")

    async def handle_reader_play(self, data: dict):
        await self.cancel_reader_task()
        self.state.reader_task = asyncio.create_task(
            stream_reader_audio(
                self.ws,
                data["doc_id"],
                self.state.history.db_path,
                chunk_index=data.get("chunk_index", 0),
                sentence_index=data.get("sentence_index", 0),
                voice=data.get("voice", self.state.voice),
                seq=data.get("seq", 0),
            )
        )

    async def handle_reader_pause(self, _data: dict):
        await self.cancel_reader_task()

    async def handle_reader_stop(self, _data: dict):
        await self.cancel_reader_task()

    async def handle_item_chat_load(self, data: dict):
        from history import (
            get_conversation_messages,
            get_item_chat_conversation_id,
            get_saved_item,
            set_item_chat_conversation,
        )
        item_id = data.get("item_id")
        with self.state.history.connect() as conn:
            item = get_saved_item(conn, item_id)
            chat_conv_id = get_item_chat_conversation_id(conn, item_id)
            msgs = get_conversation_messages(conn, chat_conv_id) if chat_conv_id else []

        if not item:
            await self.send_payload(
                {
                    "type": "item_chat_status",
                    "item_id": item_id,
                    "text": "Item not found.",
                }
            )
            return

        if chat_conv_id:
            conversation = create_item_conversation(item, item_id)
            for message in msgs:
                if message["role"] in ("user", "assistant") and message.get("content"):
                    conversation._messages.append({"role": message["role"], "content": message["content"]})
            conversation.trim()
            self.state.item_conversations[item_id] = conversation
            history_session = self.state.history.start_conversation(
                service="octavius",
                source="inbox_chat",
                model=settings.llm_chain[0]["model"],
            )
            self.state.item_history_sessions[item_id] = history_session
            with self.state.history.connect() as conn:
                set_item_chat_conversation(conn, item_id, history_session.conv_id)
            history_pairs = [
                {"role": message["role"], "content": message["content"]}
                for message in msgs
                if message["role"] in ("user", "assistant") and message.get("content")
            ]
            await self.send_payload(
                {
                    "type": "item_chat_loaded",
                    "item_id": item_id,
                    "messages": history_pairs,
                }
            )
            return

        self.state.item_conversations[item_id] = create_item_conversation(item, item_id)
        history_session = self.state.history.start_conversation(
            service="octavius",
            source="inbox_chat",
            model=settings.llm_chain[0]["model"],
        )
        self.state.item_history_sessions[item_id] = history_session
        with self.state.history.connect() as conn:
            set_item_chat_conversation(conn, item_id, history_session.conv_id)
        await self.send_payload({"type": "item_chat_loaded", "item_id": item_id, "messages": []})

    async def handle_item_chat(self, data: dict):
        from agent import stream_agent_turn
        item_id = data.get("item_id")
        user_text = data.get("text", "").strip()
        if not user_text or item_id not in self.state.item_conversations:
            return

        conversation = self.state.item_conversations[item_id]
        history_session = self.state.item_history_sessions.get(item_id)
        await self.send_payload({"type": "item_chat_status", "item_id": item_id, "text": "Thinking..."})
        if history_session:
            await history_session.add_message_async(role="user", content=user_text)

        turn_start = time.monotonic()

        async def item_status_cb(text: str):
            await self.send_payload({"type": "item_chat_status", "item_id": item_id, "text": text})

        full_parts = []
        try:
            async for sentence in stream_agent_turn(
                conversation,
                self.state.mcp_manager,
                user_text,
                status_callback=item_status_cb,
                history_session=history_session,
                session=self,
            ):
                full_parts.append(sentence)
        except Exception as exc:
            log.exception("Item chat agent failed")
            await self.send_payload(
                {
                    "type": "item_chat_response",
                    "item_id": item_id,
                    "text": f"Error: {exc}",
                }
            )
            return

        full_reply = "".join(full_parts).strip()
        if history_session and full_reply:
            latency_ms = int((time.monotonic() - turn_start) * 1000)
            await history_session.add_message_async(
                role="assistant",
                content=full_reply,
                model=settings.llm_chain[0]["model"],
                latency_ms=latency_ms,
            )

        await self.send_payload(
            {
                "type": "item_chat_response",
                "item_id": item_id,
                "text": full_reply or "I'm not sure how to respond to that.",
            }
        )

    async def handle_item_chat_reset(self, data: dict):
        from history import get_saved_item, set_item_chat_conversation
        item_id = data.get("item_id")
        old_session = self.state.item_history_sessions.pop(item_id, None)
        if old_session:
            await old_session.end_async()
        self.state.item_conversations.pop(item_id, None)

        with self.state.history.connect() as conn:
            item = get_saved_item(conn, item_id)
        if item:
            conversation = create_item_conversation(item, item_id)
            self.state.item_conversations[item_id] = conversation
            history_session = self.state.history.start_conversation(
                service="octavius",
                source="inbox_chat",
                model=settings.llm_chain[0]["model"],
            )
            self.state.item_history_sessions[item_id] = history_session
            with self.state.history.connect() as conn:
                set_item_chat_conversation(conn, item_id, history_session.conv_id)

        await self.send_payload({"type": "item_chat_loaded", "item_id": item_id, "messages": []})

    async def handle_audio_message(self, audio_bytes: bytes):
        from stt import transcribe

        # WebM/Opus blobs always start with the EBML magic header.
        # When VAD auto-stops the streaming session, the browser's audio
        # capture keeps producing PCM chunks for a brief window; those
        # arrive after stt_stream.active flips False and would otherwise
        # land here, get POSTed as audio/webm, and 500 on Whisper. Drop
        # silently — the streaming path already produced the transcript
        # for this turn.
        if not audio_bytes.startswith(b"\x1A\x45\xDF\xA3"):
            log.debug(
                "Discarding %d bytes of non-WebM data on non-streaming "
                "audio path (likely trailing PCM after VAD auto-stop)",
                len(audio_bytes),
            )
            return

        await self.send_json("status", "Transcribing...")
        try:
            user_text = await transcribe(audio_bytes)
        except Exception as exc:
            log.exception("STT failed")
            await self.send_json("status", f"Transcription failed: {exc}")
            return

        if not user_text:
            await self.send_json("status", "Couldn't hear anything. Try again.")
            return

        await self.send_json("transcript", user_text)
        self._spawn_turn(user_text, source="voice")

    # --- Streaming STT ---

    async def handle_stt_start(self, _data: dict):
        from vad import SileroVAD

        self.state.stt_stream.reset()
        self.state.stt_stream.active = True
        if self.state.vad is None:
            self.state.vad = SileroVAD()
        else:
            self.state.vad.reset()
        log.debug("Streaming STT started")

    async def handle_stt_chunk(self, pcm_bytes: bytes):
        """Accumulate PCM audio, run VAD, and kick off background transcription."""
        stream = self.state.stt_stream
        stream.pcm_buffer += pcm_bytes

        # Run VAD to detect speech/silence
        vad = self.state.vad
        if vad is not None:
            probs = vad.process_chunk(pcm_bytes)
            max_prob = max(probs) if probs else 0.0
            has_speech = max_prob >= vad.threshold
            if has_speech:
                stream.speech_detected = True
                stream.silence_start = None
            elif stream.speech_detected:
                # Silence after speech — track duration
                now = time.monotonic()
                if stream.silence_start is None:
                    stream.silence_start = now
                elif (now - stream.silence_start) >= VAD_SILENCE_SECONDS:
                    # End of turn detected — auto-stop
                    log.info("VAD: end of speech detected (%.1fs silence)", now - stream.silence_start)
                    await self.send_payload({"type": "stt_auto_stop"})
                    await self.handle_stt_stop({})
                    return

        # Skip transcription if a transcription is already running or buffer too short
        if stream._transcribe_lock.locked():
            return
        if len(stream.pcm_buffer) < 16000 * 4:  # need at least ~1s
            return

        # Run transcription in background so the message loop isn't blocked
        asyncio.create_task(self._transcribe_streaming_buffer())

    async def _transcribe_streaming_buffer(self):
        """Background task: transcribe the current PCM buffer and send a partial."""
        from stt import transcribe_pcm

        stream = self.state.stt_stream
        if not stream.active:
            return
        async with stream._transcribe_lock:
            if not stream.active:
                return
            buf = stream.pcm_buffer
            try:
                text = await transcribe_pcm(buf)
                if text and text != stream.last_text:
                    stream.last_text = text
                    await self.send_payload({"type": "transcript_partial", "text": text})
            except Exception:
                log.debug("Streaming STT chunk transcription failed", exc_info=True)

    async def handle_stt_stop(self, _data: dict):
        """Finalize streaming transcription and start agent turn.

        If we already have a partial transcription, use it immediately without
        waiting for any in-flight Whisper call (which is likely just processing
        silence). Only waits/re-transcribes if no partial was captured yet.
        """
        from stt import transcribe_pcm

        stream = self.state.stt_stream
        stream.active = False
        last_text = stream.last_text

        if last_text:
            # Already have good text from partials — use it immediately
            stream.reset()
        else:
            # No partials yet (very short recording) — wait and transcribe
            buf = stream.pcm_buffer
            async with stream._transcribe_lock:
                last_text = stream.last_text
                if not last_text and len(buf) >= 1600 * 4:
                    await self.send_json("status", "Transcribing...")
                    try:
                        last_text = await transcribe_pcm(buf)
                    except Exception as exc:
                        log.exception("Streaming STT final transcription failed")
                        await self.send_json("status", f"Transcription failed: {exc}")
                        stream.reset()
                        return
            stream.reset()

        if not last_text:
            await self.send_json("status", "Couldn't hear anything. Try again.")
            return

        await self.send_json("transcript", last_text)
        self._spawn_turn(last_text, source="voice")

    def _spawn_turn(self, user_text: str, source: str) -> None:
        """Run a turn as a background task.

        The WebSocket receive loop is single-threaded: awaiting a turn
        inline blocks it from reading further frames, so heartbeat pings
        sent during a long turn pile up unanswered and the client's
        watchdog force-closes the socket. Spawning the turn keeps the
        receive loop free to answer pings while the agent streams.

        The client UI disables input until a turn finishes, so overlapping
        turns shouldn't happen; if one slips through we drop it rather than
        queue, since stale voice input is not worth replaying.
        """
        if self.state.turn_task and not self.state.turn_task.done():
            log.warning("Turn already in flight; dropping new turn request")
            return
        self.state.turn_task = asyncio.create_task(
            self._run_turn_guarded(user_text, source)
        )

    async def _run_turn_guarded(self, user_text: str, source: str) -> None:
        try:
            await self.run_turn(user_text, source)
        except (WebSocketDisconnect, RuntimeError):
            log.info("Turn aborted: client disconnected mid-turn")
        except Exception:
            log.exception("Turn task crashed")

    async def run_turn(self, user_text: str, source: str):
        from agent import stream_agent_turn
        from tts import synthesize
        async with self.state.turn_lock:
            turn_start = time.monotonic()
            await self.send_json("status", "Thinking...")

            if self.state.history_session:
                user_kwargs = {}
                if source == "voice":
                    user_kwargs["stt_model"] = "whisper"
                await self.state.history_session.add_message_async(role="user", content=user_text, **user_kwargs)

            async def status_cb(text: str):
                await self.send_json("status", text)

            full_reply_parts = []
            first_sentence = True
            agent_error: Exception | None = None
            try:
                async for sentence in stream_agent_turn(
                    self.state.conversation,
                    self.state.mcp_manager,
                    user_text,
                    status_callback=status_cb,
                    history_session=self.state.history_session,
                    session=self,
                ):
                    full_reply_parts.append(sentence)
                    if self.state.tts_enabled:
                        if first_sentence:
                            await self.send_json("status", "Speaking...")
                            first_sentence = False
                        try:
                            wav_bytes = await synthesize(sentence, voice=self.state.voice)
                            await self.send_bytes(wav_bytes)
                        except Exception:
                            log.exception("TTS failed for chunk")
            except Exception as exc:
                # Capture but do NOT early-return — the finally block must
                # send audio_done so continuous mode re-arms. Without it,
                # the browser sits in "Speaking..." forever.
                agent_error = exc
                log.exception("Agent failed")
                await self.send_json("status", f"Agent error: {exc}")
                if self.state.history_session:
                    await self.state.history_session.add_message_async(
                        role="assistant",
                        content=f"Error: {exc}",
                        error=str(exc),
                    )
            finally:
                full_reply = "".join(full_reply_parts).strip()

                # On the happy path, send the response and persist it. On the
                # error path, agent_error is set and we skip these — the
                # history record was already written above.
                if full_reply and agent_error is None:
                    await self.send_json("response", full_reply)
                    if self.state.history_session:
                        latency_ms = int((time.monotonic() - turn_start) * 1000)
                        await self.state.history_session.add_message_async(
                            role="assistant",
                            content=full_reply,
                            model=settings.llm_chain[0]["model"],
                            latency_ms=latency_ms,
                            tts_model=settings.tts.model if self.state.tts_enabled else None,
                        )

                # Always signal turn-end so the client's audio queue drains
                # and continuous mode re-arms recording. This must run on
                # every path: success, empty reply, or exception.
                await self.send_json("status", "audio_done")

    async def cancel_reader_task(self):
        if self.state.reader_task and not self.state.reader_task.done():
            self.state.reader_task.cancel()
            try:
                await self.state.reader_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _proactive_worker(self) -> None:
        try:
            while True:
                msg = await self.state.proactive_queue.get()
                try:
                    async with self.state.turn_lock:
                        await self._proactive_speak(msg)
                except Exception:
                    log.exception("Proactive speak failed for %s", msg.handle)
        except asyncio.CancelledError:
            pass

    async def _proactive_speak(self, msg: ProactiveMessage) -> None:
        from agent import SENTENCE_END
        from tts import synthesize

        spoken = msg.text.split("\n\n===TOOL DATA", 1)[0].strip()
        if not spoken:
            return

        prelude = f"(from {msg.domain}) "
        full = prelude + spoken
        self.state.conversation.add_assistant(full)
        if self.state.history_session:
            await self.state.history_session.add_message_async(
                role="assistant",
                content=full,
                model=settings.subagent_llm_chain[0]["model"],
            )

        await self.send_payload({
            "type": "subagent_done",
            "handle": msg.handle,
            "domain": msg.domain,
            "text": full,
        })

        if not self.state.tts_enabled:
            return

        await self.send_json("status", "Speaking...")
        parts = SENTENCE_END.split(spoken)
        sentences = [prelude + parts[0]] + parts[1:] if parts else [full]
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            try:
                wav_bytes = await synthesize(sentence, voice=self.state.voice)
                await self.send_bytes(wav_bytes)
            except Exception:
                log.exception("TTS failed for proactive chunk")
        await self.send_json("status", "audio_done")

    async def spawn_delegation(self, domain: str, task: str) -> dict:
        """Reserve an endpoint, create a background asyncio.Task running the
        subagent, and register it. Returns a summary dict for the tool response.
        """
        ticket = await self.state.subagent_dispatcher.reserve()
        handle = f"dlg_{uuid.uuid4().hex[:12]}"
        record = DelegationRecord(
            handle=handle,
            domain=domain,
            submitted_task=task,
            ticket=ticket,
            created_at=datetime.now(),
        )
        record.task = asyncio.create_task(self._run_and_announce(record))
        self.state.delegations[handle] = record
        log.info(
            "Spawned delegation %s (domain=%s, dispatcher=%s)",
            handle, domain, self.state.subagent_dispatcher.snapshot(),
        )
        return {
            "handle": handle,
            "domain": domain,
            "status": "started",
        }

    async def cancel_delegation(self, handle: str) -> dict:
        record = self.state.delegations.get(handle)
        if record is None:
            return {"cancelled": False, "reason": "unknown handle", "handle": handle}
        if record.ticket.assigned_url is None:
            await record.ticket.cancel_pending()
        if record.task and not record.task.done():
            record.task.cancel()
        self.state.delegations.pop(handle, None)
        return {"cancelled": True, "handle": handle}

    async def _emit_delegation_update(self, record: DelegationRecord) -> None:
        await self.send_payload({
            "type": "delegation_update",
            "handle": record.handle,
            "domain": record.domain,
            "submitted_task": record.submitted_task,
            "status": record.status,
            "preview": record.preview,
            "error": record.error,
        })

    async def _run_and_announce(self, record: DelegationRecord) -> None:
        assigned_url: str | None = None
        try:
            await self._emit_delegation_update(record)
            assigned_url = await record.ticket.acquire()
            fallback_url = self.state.subagent_dispatcher.fallback_url()

            async def status_cb(text: str):
                label = settings.tool_labels.get(text, text)
                await self.send_payload({
                    "type": "subagent_progress",
                    "handle": record.handle,
                    "domain": record.domain,
                    "text": label,
                })

            result = await run_subagent(
                record.submitted_task,
                record.domain,
                self.state.mcp_manager,
                assigned_url=assigned_url,
                fallback_url=fallback_url,
                status_callback=status_cb,
            )
            spoken = result.split("\n\n===TOOL DATA", 1)[0].strip()
            record.result = result
            record.preview = _build_preview(spoken)
            record.status = "ready"
            await self._emit_delegation_update(record)
            if self.state.proactive_speak_enabled:
                await self.state.proactive_queue.put(
                    ProactiveMessage(handle=record.handle, domain=record.domain, text=result)
                )
        except asyncio.CancelledError:
            log.info("Delegation %s cancelled", record.handle)
            # Cancelled records are removed by cancel_delegation already.
            raise
        except Exception as exc:
            log.exception("Delegation %s crashed", record.handle)
            record.status = "failed"
            record.error = str(exc)
            await self._emit_delegation_update(record)
        finally:
            await record.ticket.release()
            # Note: we intentionally do NOT remove the record here. Ready and
            # failed records stay parked until the user pulls or dismisses
            # them (or the WebSocket disconnects, at which point cleanup
            # cancels everything).

    async def handle_delegation_list(self, _data: dict):
        """Reply with the current snapshot. Used by the UI on connect/reconnect."""
        for record in list(self.state.delegations.values()):
            await self._emit_delegation_update(record)

    async def handle_delegation_dismiss(self, data: dict):
        handle = data.get("handle", "")
        record = self.state.delegations.get(handle)
        if record is None:
            return
        if record.status == "running":
            # Treat dismiss-while-running as cancel.
            await self.cancel_delegation(handle)
        else:
            self.state.delegations.pop(handle, None)
        await self.send_payload({"type": "delegation_removed", "handle": handle})

    async def handle_delegation_pull(self, data: dict):
        handle = data.get("handle", "")
        mode = data.get("mode", "merge")
        await self.pull_delegation(handle=handle, mode=mode, via="ui")

    async def pull_delegation(self, handle: str, mode: str, via: str = "ui") -> str:
        """Bring a parked delegation result into the conversation.

        mode='merge' injects the result into the running conversation. When
        called via UI, runs a fresh agent turn so Octavius summarizes the
        result naturally; when called via the agent's pull_delegation tool
        (via='voice'), returns the result text as the tool observation so
        the in-flight agent turn can incorporate it.

        mode='new' (UI only) creates a fresh history session seeded with the
        original task and result, and tells the UI to switch to it. The
        previous conversation is left intact.
        """
        record = self.state.delegations.get(handle)
        if record is None:
            return f"No pending delegation found with handle {handle}."
        if record.status == "running":
            return (
                f"Delegation {handle} is still running. "
                "I'll pull the result once it's ready."
            )

        if record.status == "failed":
            err = record.error or "unknown error"
            self.state.delegations.pop(handle, None)
            await self.send_payload({"type": "delegation_removed", "handle": handle})
            return f"That delegation failed: {err}"

        spoken = (record.result or "").split("\n\n===TOOL DATA", 1)[0].strip()
        domain = record.domain
        submitted = record.submitted_task
        self.state.delegations.pop(handle, None)
        await self.send_payload({"type": "delegation_removed", "handle": handle})

        if mode == "new":
            await self._spawn_seeded_conversation(domain, submitted, spoken)
            return f"Started a new conversation seeded with the {domain} results."

        # mode == "merge"
        if via == "ui":
            prompt = (
                f"[I earlier delegated this {domain} task and the specialist has "
                f"now returned. Original task: {submitted}\n\nSpecialist reply:\n"
                f"{spoken}\n\nSummarize this for me conversationally.]"
            )
            await self.send_json("transcript", f"(reviewing {domain} results)")
            self._spawn_turn(prompt, source="text")
            return ""
        # via == "voice" — caller is the agent mid-turn; hand it the text.
        return spoken

    async def _spawn_seeded_conversation(
        self, domain: str, submitted_task: str, result_text: str,
    ) -> None:
        """End the current history session, start a new one seeded with the
        delegation exchange, and tell the UI to switch to it.
        """
        if self.state.history_session:
            await self.state.history_session.end_async()
        new_session = self.state.history.start_conversation(
            service="octavius",
            source="voice",
            model=settings.llm_chain[0]["model"],
        )
        seed_user = (
            f"[Earlier I delegated this {domain} task and the specialist has "
            f"returned. Original task: {submitted_task}]"
        )
        seed_assistant = result_text or "(no content)"
        await new_session.add_message_async(
            role="user", content=seed_user, model=None,
        )
        await new_session.add_message_async(
            role="assistant",
            content=seed_assistant,
            model=settings.subagent_llm_chain[0]["model"],
        )
        self.state.history_session = new_session
        self.state.conversation.reset()
        self.state.conversation.add_user(seed_user)
        self.state.conversation.add_assistant(seed_assistant)
        await self.send_payload({
            "type": "conversation_loaded",
            "conversation_id": new_session.conv_id,
            "messages": [
                {"role": "user", "content": seed_user},
                {"role": "assistant", "content": seed_assistant},
            ],
        })
        await self.send_payload({
            "type": "session_id",
            "conversation_id": new_session.conv_id,
        })


async def handle_websocket_session(ws: Any):
    handler = WebSocketSessionHandler(ws)
    await handler.run()
