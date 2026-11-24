"""Item chat (ws_item_chat), split out of test_websocket_session."""
import unittest
import asyncio
from unittest.mock import patch

from tests.test_websocket_session import _FakeWS
from ws_item_chat import build_item_chat_context, create_item_conversation
from websocket_session import WebSocketSessionHandler


class ItemChatTests(unittest.TestCase):
    def test_build_item_chat_context_includes_content_and_id(self):
        item = {
            "title": "Paper",
            "item_type": "article",
            "content": "A" * 600,
        }
        context = build_item_chat_context(item, 42)
        self.assertIn("Title: Paper", context)
        self.assertIn("Type: article", context)
        self.assertIn("42", context)
        self.assertIn("A" * 600, context)
        self.assertNotIn("read_item_content", context)

    def test_create_item_conversation_injects_context(self):
        item = {
            "title": "Note",
            "item_type": "note",
            "content": "hello",
        }
        conversation = create_item_conversation(item, 7)
        self.assertEqual(conversation.get_messages()[0]["role"], "system")
        self.assertIn("Title: Note", conversation.get_messages()[0]["content"])

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
