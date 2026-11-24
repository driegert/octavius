"""Reader playback frames (ws_reader), split out of test_websocket_session."""
import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_websocket_session import _FakeWS
from websocket_session import WebSocketSessionHandler


class ReaderHeardPositionWiringTests(unittest.TestCase):
    def test_pause_and_stop_save_the_heard_place_after_cancelling(self):
        async def run():
            handler = WebSocketSessionHandler(_FakeWS())
            handler.state.history = SimpleNamespace(db_path="/x.db")
            order = []

            async def cancel():
                order.append("cancel")
            with patch.object(handler, "cancel_reader_task", cancel), \
                    patch("ws_reader.save_heard_position",
                          side_effect=lambda *a: order.append(("save",) + a[1:])):
                await handler.handle_reader_stop({"doc_id": 3, "chunk_index": 4, "sentence_index": 2})
                await handler.handle_reader_pause({"doc_id": 3, "chunk_index": 5, "sentence_index": 0})
                await handler.handle_reader_pause({})  # an older client: nothing to save
            self.assertEqual(order, ["cancel", ("save", 3, 4, 2), "cancel", ("save", 3, 5, 0), "cancel"])

        asyncio.run(run())
