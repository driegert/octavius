import unittest

from websocket_session import build_item_chat_context, create_item_conversation


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


if __name__ == "__main__":
    unittest.main()
