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
