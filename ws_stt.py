"""Streaming speech-to-text frames (audio, stt_start/chunk/stop) for a WebSocket session.

A mixin composed into ``websocket_session.WebSocketSessionHandler``; the
session state lives on ``self.state`` (see ``websocket_session``).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


VAD_SILENCE_SECONDS = 1.5  # auto-stop after this much silence post-speech
STT_FINAL_WAIT_S = 3.0  # longest wait at stop for an in-flight Whisper run


@dataclass
class StreamingSTTState:
    """Per-session state for streaming speech-to-text."""
    active: bool = False
    pcm_buffer: bytes = b""
    last_text: str = ""
    last_partial: str = ""
    speech_detected: bool = False
    silence_start: float | None = None
    # Bytes of pcm_buffer that last_text covers, and the buffer length at the end of
    # the last chunk VAD heard speech in: speech past last_len is not in last_text yet.
    last_len: int = 0
    speech_end: int = 0
    # Bumped on every reset, so a transcription started for one utterance cannot
    # write into the next one or send a partial after its transcript.
    generation: int = 0
    _transcribe_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reset(self):
        self.active = False
        self.pcm_buffer = b""
        self.last_text = ""
        self.last_partial = ""
        self.speech_detected = False
        self.silence_start = None
        self.last_len = 0
        self.speech_end = 0
        self.generation += 1


async def _acquire_and_release(lock: asyncio.Lock) -> None:
    async with lock:
        pass


class STTMixin:
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

        # Starting a new capture means the user is speaking now — cancel any
        # response still in flight so a "barge-in" actually interrupts it (the
        # client clears its own audio queue in parallel). In the normal flow the
        # turn is already finished by the time the mic reopens, so this is a
        # no-op; it only bites when the user talks over a still-streaming reply.
        turn = self.state.turn_task
        pending = self.state.pending_confirm
        # A turn waiting on a confirmation is not talking: Dave is opening the mic
        # to answer it, and his transcript resolves it in _spawn_turn.
        if turn and not turn.done() and (pending is None or pending.done()):
            turn.cancel()
            try:
                await turn
            except asyncio.CancelledError:
                pass
            except Exception:
                log.debug("In-flight turn ended during barge-in", exc_info=True)

        self.state.stt_stream.reset()
        self.state.stt_stream.active = True
        # Dave is speaking: warm pi's cache while he does (never blocks the capture).
        self._request_pi_warm("stt_start", start=True)
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
                stream.speech_end = len(stream.pcm_buffer)
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
            generation = stream.generation
            try:
                text = await transcribe_pcm(buf)
            except Exception:
                log.debug("Streaming STT chunk transcription failed", exc_info=True)
                return
            if not text or stream.generation != generation:
                return  # empty, or the utterance was reset while Whisper ran
            stream.last_text, stream.last_len = text, len(buf)
            # Once the capture has stopped, handle_stt_stop owns the text: a partial
            # sent now would land after the transcript and show as a second line.
            if stream.active and text != stream.last_partial:
                stream.last_partial = text
                await self.send_payload({"type": "transcript_partial", "text": text})


    async def handle_stt_stop(self, _data: dict):
        """Finalize streaming transcription and start agent turn.

        The latest partial usually lags the audio: Whisper was still working on a
        newer buffer when the capture stopped, and that run is the one that heard
        the last words. So wait (briefly) for an in-flight run instead of using a
        stale partial, then transcribe once more only if VAD heard speech past
        what the text covers. Nothing is sent as a partial after this point.
        """
        from stt import transcribe_pcm

        stream = self.state.stt_stream
        stream.active = False
        generation = stream.generation

        if stream._transcribe_lock.locked():
            try:
                await asyncio.wait_for(_acquire_and_release(stream._transcribe_lock), STT_FINAL_WAIT_S)
            except asyncio.TimeoutError:
                log.info("STT: in-flight transcription still running after %.1fs; using what we have", STT_FINAL_WAIT_S)
        if stream.generation != generation:
            return  # a new capture started while we waited

        last_text = stream.last_text
        buf = stream.pcm_buffer
        if (not last_text or stream.speech_end > stream.last_len) and len(buf) >= 1600 * 4:
            async with stream._transcribe_lock:
                if not last_text:
                    await self.send_json("status", "Transcribing...")
                try:
                    text = await transcribe_pcm(buf)
                except Exception as exc:
                    log.exception("Streaming STT final transcription failed")
                    if not last_text:
                        await self.send_json("status", f"Transcription failed: {exc}")
                        stream.reset()
                        return
                    text = ""
                if stream.generation != generation:
                    return
                last_text = text or last_text
        stream.reset()

        if not last_text:
            await self.send_json("status", "Couldn't hear anything. Try again.")
            return

        await self.send_json("transcript", last_text)
        self._spawn_turn(last_text, source="voice")
