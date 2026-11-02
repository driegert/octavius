"""The agent's model menu: `chat_model` WS frame, re-applied as the conversation changes."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from settings import settings
from test_reader_chat import _WS, _Fixture, _no_summary, _no_tags
from websocket_session import WebSocketSessionHandler, chat_model_choice, chat_models_payload


@patch("history.generate_summary_async", _no_summary)
@patch("history.generate_tags_async", _no_tags)
class ChatModelTests(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture(self)
        self.prefs = []
        self.warms = []

    def _handler(self, pi=True):
        h = WebSocketSessionHandler(_WS(self.fx.recorder))
        h.state.history_session = self.fx.recorder.start_conversation()
        if pi:
            h.ws.app.state.pi_brain = SimpleNamespace(
                prefer=lambda conv, model, thinking=None: self.prefs.append((conv, model, thinking)),
                request_warm=lambda conv, **k: self.warms.append((conv, k.get("reason"))),
                discard=mock.AsyncMock(),
                rekey=lambda old, new: None,
            )
        return h

    def _last(self, h, type_):
        return [m for m in h.ws.sent if m.get("type") == type_][-1]

    def test_menu_default_is_auto_on_the_shared_chain(self):
        self.assertEqual(chat_models_payload()["default"], "auto")
        self.assertIsNone(chat_model_choice("auto")["model"])
        self.assertEqual(chat_model_choice("no-such-model")["id"], "auto")

    def test_choice_is_applied_and_acknowledged(self):
        async def run():
            h = self._handler()
            conv = h.state.history_session.conv_id
            with patch.object(type(settings.pi_brain), "enabled", new_callable=mock.PropertyMock, return_value=True):
                await h.handle_chat_model({"model": "qwen3.8-27b"})
            ack = self._last(h, "chat_model")
            self.assertEqual((ack["model"], ack["label"]), ("qwen3.8-27b", "Qwen3.8 27B"))
            self.assertEqual(ack["default"], "auto")
            self.assertEqual(self.prefs[-1], (conv, "lrv-p01/qwen3.8-27b", None))
            self.assertEqual(self.warms[-1], (conv, "chat_model"))
            # Back to Auto clears the preference (prefer with no model).
            await h.handle_chat_model({"model": "auto"})
            self.assertEqual(self.prefs[-1], (conv, None, None))
            # Unknown ids fall back to the default rather than erroring.
            await h.handle_chat_model({"model": "bogus"})
            self.assertEqual(self._last(h, "chat_model")["model"], "auto")

        asyncio.run(run())

    def test_choice_carries_over_new_chat_and_load(self):
        async def run():
            h = self._handler()
            await h.handle_chat_model({"model": "gemma4-12b"})
            old = h.state.history_session.conv_id
            h.state.history_session.add_message("user", "hello")
            h.state.history_session.add_message("assistant", "hi")

            await h.handle_reset({})
            fresh = h.state.history_session.conv_id
            self.assertNotEqual(fresh, old)
            self.assertEqual(self.prefs[-1], (fresh, "wh-vllm/gemma4-12b", "off"))

            await h.handle_load_conversation({"conversation_id": old})
            loaded = h.state.history_session.conv_id
            self.assertEqual(self.prefs[-1], (loaded, "wh-vllm/gemma4-12b", "off"))
            # Applied before the load's warm, so the warm is on the chosen model.
            self.assertEqual(self.warms[-1], (loaded, "load_conversation"))

        asyncio.run(run())

    def test_clients_that_never_choose_are_untouched(self):
        async def run():
            h = self._handler()
            await h.handle_reset({})
            h._apply_chat_model()
            self.assertEqual(self.prefs, [])

        asyncio.run(run())

    def test_a_reader_chat_keeps_its_own_choice(self):
        async def run():
            h = self._handler()
            h.state.doc_chat_id = 7
            await h.handle_chat_model({"model": "qwen3.8-27b"})
            self.assertEqual(self.prefs, [])
            self.assertEqual(self._last(h, "chat_model")["model"], "qwen3.8-27b")

        asyncio.run(run())

    def test_native_brain_stores_the_choice_without_a_pi_manager(self):
        async def run():
            h = self._handler(pi=False)
            await h.handle_chat_model({"model": "qwen3.8-27b"})
            self.assertEqual(h.state.chat_model, "qwen3.8-27b")
            self.assertIs(self._last(h, "chat_model")["enabled"], chat_models_payload()["enabled"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
