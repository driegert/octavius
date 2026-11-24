from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from conversation import Conversation
from reader_playback import ReaderPacer
from settings import settings
from ws_common import WebSocket, WebSocketDisconnect  # noqa: F401  (re-exported)
from ws_delegation import DelegationMixin, DelegationRecord, ProactiveMessage  # noqa: F401
from ws_doc_chat import (  # noqa: F401  (re-exported: main.py and tests import these from here)
    DocChatMixin,
    chat_model_choice,
    chat_model_label,
    chat_models_payload,
    reader_model_label,
)
from ws_item_chat import ItemChatMixin, build_item_chat_context, create_item_conversation  # noqa: F401
from ws_media import MediaTurnsMixin
from ws_reader import ReaderPlaybackMixin
from ws_stt import STT_FINAL_WAIT_S, VAD_SILENCE_SECONDS, STTMixin, StreamingSTTState  # noqa: F401

log = logging.getLogger(__name__)


@dataclass
class WebSocketSessionState:
    ws: Any
    history: object
    mcp_manager: object
    subagent_dispatcher: object
    conversation: Conversation = field(default_factory=Conversation)
    voice: str = settings.tts.agent_voice
    tts_enabled: bool = True
    reader_task: asyncio.Task | None = None
    reader_seq: int | None = None
    reader_pacer: ReaderPacer | None = None
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
    renders_math: bool = True  # False on a Matrix thread, which shows a reply's LaTeX raw
    # Set while a pi turn waits on Dave to answer a confirmation (pi_brain); his
    # next transcript or text input resolves it instead of starting a turn.
    pending_confirm: asyncio.Future | None = None
    # The Reader chat panel: the document this connection's conversation is about, and
    # the place Dave was hearing when he last asked (doc_chat_position).
    doc_chat_id: int | None = None
    doc_chat_position: dict | None = None
    # The pi model the current turn's attempt is on (pi_brain sets it); None before one starts.
    pi_hop: str | None = None
    # The agent model Dave picked (`chat_model`, a chat_models menu id). None: the client has
    # never sent one, and its conversations behave exactly as before the menu existed.
    chat_model: str | None = None


class WebSocketSessionHandler(
    STTMixin,
    ReaderPlaybackMixin,
    DocChatMixin,
    MediaTurnsMixin,
    ItemChatMixin,
    DelegationMixin,
):
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
                # A disconnect arrives as a message, not an exception — calling
                # receive() again after it raises RuntimeError (which cleanup
                # used to be reached through; the traceback then leaked into
                # every warning logged during cleanup via exception chaining).
                if message.get("type") == "websocket.disconnect":
                    break
                if "text" in message:
                    await self.handle_text_message(message["text"])
                    continue
                if "bytes" in message:
                    if self.state.stt_stream.active:
                        await self.handle_stt_chunk(message["bytes"])
                    else:
                        await self.handle_audio_message(message["bytes"])
        except (WebSocketDisconnect, RuntimeError):
            pass
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
            "attach_session": self.handle_attach_session,
            "reset": self.handle_reset,
            "load_conversation": self.handle_load_conversation,
            "settings": self.handle_settings,
            "text_input": self.handle_text_input,
            "image_input": self.handle_image_input,
            "file_input": self.handle_file_input,
            "reader_play": self.handle_reader_play,
            "reader_pause": self.handle_reader_pause,
            "reader_stop": self.handle_reader_stop,
            "reader_heard": self.handle_reader_heard,
            "item_chat_load": self.handle_item_chat_load,
            "item_chat": self.handle_item_chat,
            "item_chat_reset": self.handle_item_chat_reset,
            "doc_chat_open": self.handle_doc_chat_open,
            "doc_chat_position": self.handle_doc_chat_position,
            "doc_chat_model": self.handle_doc_chat_model,
            "chat_model": self.handle_chat_model,
            "stt_start": self.handle_stt_start,
            "stt_stop": self.handle_stt_stop,
            "typing_start": self.handle_typing_start,
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
        """A client reconnected: carry on the conversation it was in.

        The connect-time record is retired and the old one becomes this connection's
        record, so new turns append to it and the id the client holds never changes
        (it used to get a new id per reconnect, and a second reconnect then restored
        a record holding only the turns since the first). An id with no messages gets
        no reply, as before; a record that is not a voice one (see
        `HistoryRecorder.resume_conversation`) is only loaded into memory.
        """
        from history import get_conversation_messages
        old_conv_id = data.get("conversation_id")
        if not old_conv_id:
            return
        if old_conv_id == self.state.history_session.conv_id:
            await self.send_payload({"type": "session_id", "conversation_id": old_conv_id})
            return
        with self.state.history.connect() as conn:
            msgs = get_conversation_messages(conn, old_conv_id)
        if not msgs:
            return
        await self._abandon_turn()
        resumed = self.state.history.resume_conversation(old_conv_id)
        if resumed is not None:
            await self.state.history_session.end_async()  # retire the connect-time record
            self.state.history_session = resumed
        self.state.conversation.load_from_history(msgs)
        log.info(
            "Restored conversation %d on reconnect (%d messages, %s)",
            old_conv_id, len(msgs),
            "continuing it" if resumed is not None else "into a new record",
        )
        pi_brain = getattr(self.ws.app.state, "pi_brain", None)
        if pi_brain is not None:
            # A no-op when the record was resumed (same id); it still matters when not.
            pi_brain.rekey(old_conv_id, self.state.history_session.conv_id)
            self._apply_chat_model()
            # A live re-attached process has history the slot may have lost.
            self._request_pi_warm("restore_session")
        await self.send_payload(
            {
                "type": "session_id",
                "conversation_id": self.state.history_session.conv_id,
            }
        )

    async def handle_attach_session(self, data: dict):
        """Bind this connection to a durable conversation by client key.

        The client (e.g. the Matrix sidecar) supplies a stable `key` per logical
        thread. We retire the empty connect-time record, resume-or-create the
        keyed conversation, and reload its history so the agent continues with
        full context across idle drops and restarts.
        """
        from history import get_conversation_messages
        key = data.get("key")
        if not key:
            return
        await self.state.history_session.end_async()  # retire the empty connect-time record
        self.state.renders_math = False
        self.state.history_session = self.state.history.resume_or_start_conversation(
            key,
            source="matrix",
            model=settings.llm_chain[0]["model"],
        )
        conv_id = self.state.history_session.conv_id
        msgs = get_conversation_messages(self.state.history_session.conn, conv_id)
        if msgs:
            self.state.conversation.load_from_history(msgs)
            log.info("Attached conversation %d for key %s (%d messages)", conv_id, key[:16], len(msgs))
        else:
            log.info("Attached new conversation %d for key %s", conv_id, key[:16])
        await self.send_payload(
            {
                "type": "session_id",
                "conversation_id": conv_id,
            }
        )

    async def _abandon_turn(self) -> None:
        # Mirror handle_stt_start: switching conversations must actually kill any
        # in-flight turn, or it keeps streaming into (and its reply gets recorded under)
        # the next conversation. Cancelling BEFORE end_async() also means the interrupted
        # turn's partial reply is persisted into the conversation it belongs to.
        turn = self.state.turn_task
        if turn and not turn.done():
            turn.cancel()
            try:
                await turn
            except asyncio.CancelledError:
                pass
            except Exception:
                log.debug("In-flight turn ended during a conversation switch", exc_info=True)
        # It also abandons any open streaming-STT capture (e.g. the Android client
        # tearing down a live mic for New Chat) — don't leave the stream armed.
        self.state.stt_stream.reset()
        self.state.stt_stream.active = False

    async def handle_reset(self, _data: dict):
        await self._abandon_turn()
        self.state.doc_chat_id = None
        await self._discard_pi_process()
        await self.state.history_session.end_async()
        self.state.conversation.reset()
        self.state.history_session = self.state.history.start_conversation(
            source="voice",
            model=settings.llm_chain[0]["model"],
        )
        self._apply_chat_model()
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
        # An in-flight reply belongs to the conversation being left, not the loaded one.
        await self._abandon_turn()
        self.state.doc_chat_id = None
        await self._discard_pi_process()
        await self.state.history_session.end_async()
        self.state.conversation.load_from_history(msgs)
        # A voice conversation is continued in place: new turns append to it, so a
        # later restore_session of this id reads them. Anything else (a Matrix
        # thread, a Reader or item chat, another service's record) is only read:
        # new turns go to a new record, as they always did.
        self.state.history_session = (
            self.state.history.resume_conversation(conv_id)
            or self.state.history.start_conversation(
                source="voice",
                model=settings.llm_chain[0]["model"],
            )
        )
        self._apply_chat_model()  # before the warm, so it warms the chosen model
        # The loaded thread runs on a new process; start it now and warm the template.
        self._request_pi_warm("load_conversation", start=True)
        history_pairs = []
        for message in msgs:
            if message["role"] not in ("user", "assistant") or not message.get("content"):
                continue
            pair = {"role": message["role"], "content": message["content"]}
            # pi turns record the model that actually answered as "pi:<model>"; native
            # turns record the chain's first model whether or not it answered, so only
            # pi turns get a caption.
            model = message.get("model") or ""
            if message["role"] == "assistant" and model.startswith("pi:") and model[3:]:
                pair["answered_by"] = chat_model_label(model[3:])
            history_pairs.append(pair)
        await self.send_payload(
            {
                "type": "conversation_loaded",
                "conversation_id": conv_id,
                "messages": history_pairs,
            }
        )
        # The id new turns are recorded under, which is what a client must restore after
        # a reconnect. Clients persist whatever session_id says.
        await self.send_payload(
            {
                "type": "session_id",
                "conversation_id": self.state.history_session.conv_id,
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

    async def handle_typing_start(self, _data: dict):
        """A client saw Dave start typing: warm pi while he types (text turns)."""
        self._request_pi_warm("typing_start", start=True)

    def _request_pi_warm(self, reason: str, *, start: bool = False) -> None:
        """Fire-and-forget /warm for this conversation's pi process (pi brain only).

        Never raises and never awaits: warming must not delay or fail anything.
        Vision threads stay native, so they are not warmed.
        """
        try:
            pi_brain = getattr(self.ws.app.state, "pi_brain", None)
            if (
                pi_brain is None
                or self.state.history_session is None
                or getattr(self.state.conversation, "has_images", False)
            ):
                return
            pi_brain.request_warm(self.state.history_session.conv_id, start=start, reason=reason)
        except Exception:
            log.debug("pi warm request failed", exc_info=True)

    async def _discard_pi_process(self) -> None:
        """The conversation is ending (reset, load): stop its pi process, if any."""
        pi_brain = getattr(self.ws.app.state, "pi_brain", None)
        if pi_brain is not None and self.state.history_session is not None:
            await pi_brain.discard(self.state.history_session.conv_id)

    def _spawn_turn(
        self,
        user_text: str,
        source: str,
        user_content: list[dict] | None = None,
        attachment: dict | None = None,
    ) -> None:
        """Run a turn as a background task.

        The WebSocket receive loop is single-threaded: awaiting a turn
        inline blocks it from reading further frames, so heartbeat pings
        sent during a long turn pile up unanswered and the client's
        watchdog force-closes the socket. Spawning the turn keeps the
        receive loop free to answer pings while the agent streams.

        The client UI disables input until a turn finishes, so overlapping
        turns shouldn't happen; if one slips through we drop it rather than
        queue, since stale voice input is not worth replaying.

        ``user_content``: OpenAI-style multimodal content array for an
        image turn (see handle_image_input) — routes this turn through the
        vision chain. ``attachment``: optional ``{"type", "reference",
        "title"}`` recorded against the persisted user message via the
        ``attachments`` table (image_input / file_input turns only).
        """
        pending = self.state.pending_confirm
        if pending is not None and not pending.done():
            from pi_brain import is_yes
            pending.set_result(is_yes(user_text))
            if self.state.history_session:
                # The answer is a turn of its own in history, between the question and the result.
                self._confirm_answer_task = asyncio.create_task(
                    self.state.history_session.add_message_async(role="user", content=user_text))
            return
        if self.state.turn_task and not self.state.turn_task.done():
            log.warning("Turn already in flight; dropping new turn request")
            return
        self.state.turn_task = asyncio.create_task(
            self._run_turn_guarded(user_text, source, user_content, attachment)
        )

    async def _run_turn_guarded(
        self,
        user_text: str,
        source: str,
        user_content: list[dict] | None = None,
        attachment: dict | None = None,
    ) -> None:
        try:
            await self.run_turn(user_text, source, user_content, attachment)
        except (WebSocketDisconnect, RuntimeError):
            log.info("Turn aborted: client disconnected mid-turn")
        except Exception:
            log.exception("Turn task crashed")

    async def run_turn(
        self,
        user_text: str,
        source: str,
        user_content: list[dict] | None = None,
        attachment: dict | None = None,
    ):
        from agent import stream_agent_turn
        from pi_brain import stream_pi_turn, wants_pi
        from tts import speech_audio
        async with self.state.turn_lock:
            turn_start = time.monotonic()
            await self.send_json("status", "Thinking...")

            # OCTAVIUS_BRAIN=pi sends voice/text turns to pi (pi_brain.py).
            # Image turns route through the vision chain; everything else
            # (including file_input's synthetic instruction turns) uses the
            # default text chain unchanged.
            use_pi = wants_pi(source, user_content, self.state.conversation)
            turn_model = (
                f"pi:{settings.pi_brain.model}" if use_pi
                else settings.vision_llm_chain[0]["model"] if user_content is not None
                else settings.llm_chain[0]["model"]
            )

            if self.state.history_session:
                user_kwargs = {}
                if source == "voice":
                    user_kwargs["stt_model"] = "whisper"
                user_msg_id = await self.state.history_session.add_message_async(
                    role="user", content=user_text, **user_kwargs
                )
                if attachment:
                    self.state.history_session.add_attachment(
                        message_id=user_msg_id,
                        type=attachment["type"],
                        reference=attachment["reference"],
                        title=attachment.get("title"),
                    )

            async def status_cb(text: str):
                await self.send_json("status", text)

            full_reply_parts = []
            self.state.pi_hop = None
            # commit_reply_so_far (pi's confirm question) sends and clears these mid-turn.
            self._turn_reply = (full_reply_parts, turn_model)
            first_sentence = True
            agent_error: Exception | None = None
            if use_pi:
                self._apply_chat_model()
                stream = stream_pi_turn(self, user_text, source=source, status_callback=status_cb)
            else:
                stream = stream_agent_turn(
                    self.state.conversation,
                    self.state.mcp_manager,
                    user_text,
                    status_callback=status_cb,
                    history_session=self.state.history_session,
                    session=self,
                    user_content=user_content,
                    source=source,
                )
            try:
                # aclosing: a cancelled turn (barge-in, reset) must close the
                # generator now, so the pi path aborts pi before the next prompt.
                async with aclosing(stream) as sentences:
                    async for sentence in sentences:
                        full_reply_parts.append(sentence)
                        # Text-side streaming: emit each sentence as it lands so a
                        # client can render progressively instead of waiting for the
                        # final `response`. The Matrix sidecar edits its reply in
                        # place from these; the browser ignores unknown frame types.
                        # Sent before TTS so text isn't gated on audio synthesis.
                        await self.send_json("response_delta", sentence)
                        if self.state.tts_enabled:
                            if first_sentence:
                                await self.send_json("status", "Speaking...")
                                first_sentence = False
                                if use_pi and self.state.pi_hop:
                                    turn_model = f"pi:{self.state.pi_hop}"
                                log.info(
                                    "First sentence after %.2fs (%s, %s)",
                                    time.monotonic() - turn_start, source, turn_model,
                                )
                            try:
                                async for wav_bytes, is_math in speech_audio(sentence, voice=self.state.voice):
                                    if is_math:
                                        await self.send_payload({"type": "speech_clip", "math": True})
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
                if use_pi and self.state.pi_hop:
                    turn_model = f"pi:{self.state.pi_hop}"  # the model that actually answered
                if full_reply and agent_error is None:
                    if use_pi and self.state.doc_chat_id is not None and self.state.pi_hop:
                        await self.send_payload({"type": "doc_chat_answered_by", "model": self.state.pi_hop,
                                                 "label": reader_model_label(self.state.pi_hop)})
                    elif use_pi and self.state.chat_model is not None and self.state.pi_hop:
                        # Only for clients using the menu, so others see no new frame type.
                        await self.send_payload({"type": "answered_by", "model": self.state.pi_hop,
                                                 "label": chat_model_label(self.state.pi_hop)})
                    await self.send_json("response", full_reply)
                    if self.state.history_session:
                        latency_ms = int((time.monotonic() - turn_start) * 1000)
                        await self.state.history_session.add_message_async(
                            role="assistant",
                            content=full_reply,
                            model=turn_model,
                            latency_ms=latency_ms,
                            tts_model=settings.tts.model if self.state.tts_enabled else None,
                        )

                # Always signal turn-end so the client's audio queue drains
                # and continuous mode re-arms recording. This must run on
                # every path: success, empty reply, or exception.
                await self.send_json("status", "audio_done")

    async def commit_reply_so_far(self) -> None:
        """Send and record what this turn has said so far as a reply of its own.

        pi_brain calls it after speaking a confirm question, so the question shows (and is
        stored) before Dave's yes/no and the turn's final `response` holds only what came
        after, instead of repeating the question under his answer.
        """
        reply = getattr(self, "_turn_reply", None)
        if not reply:
            return
        parts, model = reply
        text = "".join(parts).strip()
        parts.clear()
        if not text:
            return
        await self.send_json("response", text)
        if self.state.history_session:
            await self.state.history_session.add_message_async(role="assistant", content=text, model=model)


async def handle_websocket_session(ws: Any):
    handler = WebSocketSessionHandler(ws)
    await handler.run()
