import unittest
from dataclasses import replace
from unittest.mock import patch

import conversation
from conversation import Conversation


class ConversationTests(unittest.TestCase):
    def test_trim_keeps_system_and_latest_messages(self):
        with patch.object(
            conversation,
            "settings",
            replace(conversation.settings, max_conversation_messages=3),
        ):
            conv = Conversation()
            for index in range(5):
                conv.add_user(f"user-{index}")
            conv.trim()
            messages = conv.get_messages()
            self.assertEqual(messages[0]["role"], "system")
            self.assertEqual([msg["content"] for msg in messages[1:]], ["user-2", "user-3", "user-4"])

    def test_add_user_accepts_multimodal_content_array(self):
        conv = Conversation()
        content = [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        conv.add_user(content)
        messages = conv.get_messages()
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], content)

    def test_replace_last_user_content_downgrades_to_text(self):
        conv = Conversation()
        conv.add_user("first")
        conv.add_assistant("reply")
        content = [{"type": "text", "text": "x"}, {"type": "image_url", "image_url": {"url": "data:...;base64,AAAA"}}]
        conv.add_user(content)

        conv.replace_last_user_content("[image: cat.png]")

        messages = conv.get_messages()
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-1]["content"], "[image: cat.png]")
        # Earlier messages untouched.
        self.assertEqual(messages[1]["content"], "first")
        self.assertEqual(messages[2]["content"], "reply")

    def test_replace_last_user_content_is_noop_without_user_message(self):
        conv = Conversation()
        # Only the system message exists — nothing to downgrade.
        conv.replace_last_user_content("placeholder")
        messages = conv.get_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "system")

    def test_load_from_history_skips_tool_messages(self):
        conv = Conversation()
        conv.load_from_history(
            [
                {"role": "user", "content": "hello"},
                {"role": "tool", "content": "internal"},
                {"role": "assistant", "content": "hi"},
            ]
        )
        messages = conv.get_messages()
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[1]["content"], "hello")
        self.assertEqual(messages[2]["content"], "hi")


if __name__ == "__main__":
    unittest.main()
