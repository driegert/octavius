"""Ending a capture: the transcript is the freshest text, and no partial follows it."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_websocket_session import _FakeWS  # noqa: E402
from websocket_session import WebSocketSessionHandler  # noqa: E402

SECOND = 16000 * 4  # bytes of float32 PCM at 16 kHz


def _handler():
    handler = WebSocketSessionHandler(_FakeWS())
    handler.turns = []
    handler._spawn_turn = lambda text, source: handler.turns.append(text)
    stream = handler.state.stt_stream
    stream.active = True
    return handler, stream


def _frames(handler):
    return [json.loads(t) for t in handler.ws.sent]


class SttFinalizeTests(unittest.TestCase):
    def test_stop_waits_for_the_inflight_run_and_sends_no_late_partial(self):
        async def run():
            handler, stream = _handler()
            stream.pcm_buffer = b"\0" * (3 * SECOND)
            stream.last_text, stream.last_len = "how many roads", 2 * SECOND
            stream.speech_end = 3 * SECOND
            release = asyncio.Event()

            async def whisper(_buf):
                await release.wait()
                return "how many roads must a man walk down"

            with patch("stt.transcribe_pcm", side_effect=whisper):
                inflight = asyncio.create_task(handler._transcribe_streaming_buffer())
                await asyncio.sleep(0)
                stop = asyncio.create_task(handler.handle_stt_stop({}))
                await asyncio.sleep(0.01)
                self.assertFalse(stop.done())  # waiting on Whisper, not using the stale text
                release.set()
                await asyncio.gather(inflight, stop)
            frames = _frames(handler)
            self.assertEqual([f["type"] for f in frames], ["transcript"])
            self.assertEqual(frames[0]["text"], "how many roads must a man walk down")
            self.assertEqual(handler.turns, ["how many roads must a man walk down"])

        asyncio.run(run())

    def test_speech_after_the_last_text_is_transcribed_again(self):
        async def run():
            handler, stream = _handler()
            stream.pcm_buffer = b"\0" * (3 * SECOND)
            stream.last_text, stream.last_len = "what's the unicode for the", 2 * SECOND
            stream.speech_end = 3 * SECOND  # e.g. hold-to-talk released mid-sentence
            with patch("stt.transcribe_pcm", return_value="what's the unicode for the TM symbol?") as whisper:
                await handler.handle_stt_stop({})
            whisper.assert_called_once()
            self.assertEqual(handler.turns, ["what's the unicode for the TM symbol?"])

        asyncio.run(run())

    def test_up_to_date_text_is_used_without_another_call(self):
        async def run():
            handler, stream = _handler()
            stream.pcm_buffer = b"\0" * (3 * SECOND)
            stream.last_text, stream.last_len = "hello there", 3 * SECOND
            stream.speech_end = 2 * SECOND  # only silence since
            with patch("stt.transcribe_pcm") as whisper:
                await handler.handle_stt_stop({})
            whisper.assert_not_called()
            self.assertEqual(handler.turns, ["hello there"])

        asyncio.run(run())

    def test_failed_final_call_keeps_the_partial(self):
        async def run():
            handler, stream = _handler()
            stream.pcm_buffer = b"\0" * (3 * SECOND)
            stream.last_text, stream.last_len = "hello", SECOND
            stream.speech_end = 3 * SECOND
            with patch("stt.transcribe_pcm", side_effect=RuntimeError("down")):
                await handler.handle_stt_stop({})
            self.assertEqual(handler.turns, ["hello"])

        asyncio.run(run())

    def test_a_run_from_a_previous_capture_cannot_leak_into_the_next(self):
        async def run():
            handler, stream = _handler()
            stream.pcm_buffer = b"\0" * (2 * SECOND)
            release = asyncio.Event()

            async def whisper(_buf):
                await release.wait()
                return "old words"

            with patch("stt.transcribe_pcm", side_effect=whisper):
                inflight = asyncio.create_task(handler._transcribe_streaming_buffer())
                await asyncio.sleep(0)
                stream.reset()          # stt_start for the next utterance
                stream.active = True
                release.set()
                await inflight
            self.assertEqual(stream.last_text, "")
            self.assertEqual(handler.ws.sent, [])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
