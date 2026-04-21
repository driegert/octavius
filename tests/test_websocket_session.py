import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from websocket_session import build_item_chat_context, create_item_conversation
from websocket_session import WebSocketSessionHandler
from settings import settings


class _FakeHistorySession:
    def __init__(self, conv_id):
        self.conv_id = conv_id
        self.ended = False

    async def end_async(self):
        self.ended = True


class _FakeHistory:
    def __init__(self):
        self.started = []
        self.sessions = []

    def start_conversation(self, **kwargs):
        self.started.append(kwargs)
        session = _FakeHistorySession(conv_id=100 + len(self.sessions))
        self.sessions.append(session)
        return session

    def connect(self):
        class _ConnCtx:
            def __enter__(self_inner):
                return object()

            def __exit__(self_inner, exc_type, exc, tb):
                return False

        return _ConnCtx()


class _FakeDispatcher:
    def snapshot(self):
        return {}

    def fallback_url(self):
        return None


class _FakeWS:
    def __init__(self):
        self.sent = []
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                history=_FakeHistory(),
                mcp_manager=object(),
                subagent_dispatcher=_FakeDispatcher(),
            )
        )

    async def send_text(self, text):
        self.sent.append(text)


class WebSocketSessionTests(unittest.TestCase):
    def test_build_item_chat_context_includes_preview_and_id(self):
        item = {
            "title": "Paper",
            "item_type": "article",
            "content": "A" * 600,
        }
        context = build_item_chat_context(item, 42)
        self.assertIn("Title: Paper", context)
        self.assertIn("Type: article", context)
        self.assertIn("The item ID is 42.", context)
        self.assertIn("...", context)

    def test_create_item_conversation_injects_context(self):
        item = {
            "title": "Note",
            "item_type": "note",
            "content": "hello",
        }
        conversation = create_item_conversation(item, 7)
        self.assertEqual(conversation.get_messages()[0]["role"], "system")
        self.assertIn("Title: Note", conversation.get_messages()[0]["content"])

    def test_handle_reset_uses_settings_llm_chain_model(self):
        async def run():
            handler = WebSocketSessionHandler(_FakeWS())
            old_session = _FakeHistorySession(conv_id=1)
            handler.state.history_session = old_session

            await handler.handle_reset({})

            self.assertTrue(old_session.ended)
            self.assertEqual(
                handler.state.history.started[-1],
                {"source": "voice", "model": settings.llm_chain[0]["model"]},
            )

        asyncio.run(run())

    def test_handle_load_conversation_uses_settings_llm_chain_model(self):
        async def run():
            handler = WebSocketSessionHandler(_FakeWS())
            old_session = _FakeHistorySession(conv_id=1)
            handler.state.history_session = old_session

            with patch(
                "history.get_conversation_messages",
                return_value=[
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
            ):
                await handler.handle_load_conversation({"conversation_id": 42})

            self.assertTrue(old_session.ended)
            self.assertEqual(
                handler.state.history.started[-1],
                {"source": "voice", "model": settings.llm_chain[0]["model"]},
            )

        asyncio.run(run())

    def test_handle_item_chat_load_updates_saved_pointer_for_existing_chat(self):
        async def run():
            handler = WebSocketSessionHandler(_FakeWS())

            with (
                patch("history.get_saved_item", return_value={"title": "Note", "item_type": "note", "content": "hello"}),
                patch("history.get_item_chat_conversation_id", return_value=42),
                patch(
                    "history.get_conversation_messages",
                    return_value=[
                        {"role": "user", "content": "hello"},
                        {"role": "assistant", "content": "hi"},
                    ],
                ),
                patch("history.set_item_chat_conversation") as set_chat,
            ):
                await handler.handle_item_chat_load({"item_id": 7})

            set_chat.assert_called_once_with(unittest.mock.ANY, 7, 100)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
