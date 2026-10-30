"""Paced reader playback: the stream stays READER_AHEAD sentences ahead of what the client hears."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import reader_playback  # noqa: E402
from reader_playback import ReaderPacer  # noqa: E402
from tests.test_reader_progress import _db_with_doc, _row  # noqa: E402

# tests.test_reader_progress.SPEECH: globals 0-4 over three chunks; global 3 is blank.


async def _no_tts(*_a, **_k):
    return b"wav"


class _WS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        frame = json.loads(text)
        self.sent.append(frame.get("sentence_global", frame["type"]))

    async def send_bytes(self, _data):
        pass


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


class PacerTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_releases_on_report_and_ignores_other_plays(self):
        pacer = ReaderPacer(seq=4, ahead=2)
        pacer.start(10)
        await asyncio.wait_for(pacer.wait_until(11), 1)  # 11 - 9 = 2: free
        waiter = asyncio.create_task(pacer.wait_until(12))
        await _settle()
        self.assertFalse(waiter.done())
        self.assertFalse(pacer.report(3, 10))      # an earlier play's frame
        self.assertFalse(pacer.report(4, "10"))    # malformed
        await _settle()
        self.assertFalse(waiter.done())
        self.assertTrue(pacer.report(4, 10))
        await asyncio.wait_for(waiter, 1)
        pacer.report(4, 5)                          # never moves backwards
        self.assertEqual(pacer.heard, 10)


class PacedStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_waits_for_the_client_and_finishes_when_heard(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp, last_chunk=1, last_sentence=1)
            ws, pacer = _WS(), ReaderPacer(seq=1)
            with patch.object(reader_playback, "synthesize", _no_tts):
                task = asyncio.create_task(reader_playback.stream_reader_audio(ws, 1, db_path, seq=1, pacer=pacer))
                await _settle()
                self.assertEqual(ws.sent, [0, 1, 2])          # three ahead of nothing heard
                # A paced stream leaves the place to the client's reports.
                self.assertEqual(_row(db_path), (1, 1, None, None))
                pacer.report(1, 0)
                await _settle()
                self.assertEqual(ws.sent, [0, 1, 2])          # 4 - 0 > 3 (the blank still counts)
                pacer.report(1, 1)
                await _settle()
                self.assertEqual(ws.sent, [0, 1, 2, 4])       # all sent, but not yet heard
                pacer.report(1, 2)
                await _settle()
                self.assertFalse(task.done())
                pacer.report(1, 4)
                await asyncio.wait_for(task, 1)
            self.assertEqual(ws.sent[-1], "reader_audio_done")
            self.assertEqual(_row(db_path), (0, 0, None, 5))

    async def test_cancelled_paced_stream_writes_no_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp, last_chunk=0, last_sentence=1)
            with patch.object(reader_playback, "synthesize", _no_tts):
                task = asyncio.create_task(
                    reader_playback.stream_reader_audio(_WS(), 1, db_path, seq=1, pacer=ReaderPacer(1)))
                await _settle()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(_row(db_path), (0, 1, None, None))


class ReaderHeardWiringTests(unittest.IsolatedAsyncioTestCase):
    def _handler(self):
        from tests.test_websocket_session import _FakeWS
        from websocket_session import WebSocketSessionHandler
        handler = WebSocketSessionHandler(_FakeWS())
        handler.state.history = SimpleNamespace(db_path="/x.db")
        return handler

    async def test_play_with_paced_creates_a_pacer_and_heard_feeds_it(self):
        handler = self._handler()
        with patch("websocket_session.stream_reader_audio", side_effect=lambda *a, **k: asyncio.sleep(10)) as stream:
            await handler.handle_reader_play({"doc_id": 1, "seq": 7, "paced": True})
        pacer = stream.call_args.kwargs["pacer"]
        self.assertIsInstance(pacer, ReaderPacer)
        pacer.start(0)
        saves = []
        with patch("websocket_session.save_heard_position", side_effect=lambda *a: saves.append(a[1:])):
            await handler.handle_reader_heard({"seq": 6, "doc_id": 1, "chunk_index": 0, "sentence_index": 9,
                                               "sentence_global": 9})   # stale play: ignored
            await handler.handle_reader_heard({"seq": 7, "doc_id": 1, "chunk_index": 0, "sentence_index": 2,
                                               "sentence_global": 2})
        self.assertEqual(saves, [(1, 0, 2)])
        self.assertEqual(pacer.heard, 2)
        await handler.cancel_reader_task()
        with patch("websocket_session.save_heard_position", side_effect=lambda *a: saves.append(a[1:])):
            await handler.handle_reader_heard({"seq": 7, "doc_id": 1, "chunk_index": 0, "sentence_index": 3,
                                               "sentence_global": 3})   # stream over: ignored
        self.assertEqual(saves, [(1, 0, 2)])

    async def test_play_without_paced_is_unpaced(self):
        handler = self._handler()
        with patch("websocket_session.stream_reader_audio", side_effect=lambda *a, **k: asyncio.sleep(10)) as stream:
            await handler.handle_reader_play({"doc_id": 1, "seq": 1})
        self.assertIsNone(stream.call_args.kwargs["pacer"])
        with patch("websocket_session.save_heard_position") as save:
            await handler.handle_reader_heard({"seq": 1, "doc_id": 1, "chunk_index": 0, "sentence_index": 3})
        save.assert_not_called()
        await handler.cancel_reader_task()


if __name__ == "__main__":
    unittest.main()
