import base64
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import conversation
from conversation import Conversation, MAX_REHYDRATED_IMAGES


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

    def test_has_images_false_on_init(self):
        conv = Conversation()
        self.assertFalse(conv.has_images)

    def test_has_images_stays_false_for_text_only_turns(self):
        conv = Conversation()
        conv.add_user("hello")
        conv.add_user("hello again")
        self.assertFalse(conv.has_images)

    def test_add_user_with_list_content_sets_has_images_and_is_sticky(self):
        conv = Conversation()
        content = [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        conv.add_user(content)
        self.assertTrue(conv.has_images)
        # Stickiness: a later plain-text turn does not clear the flag.
        conv.add_user("a follow-up question")
        self.assertTrue(conv.has_images)

    def test_reset_clears_has_images(self):
        conv = Conversation()
        content = [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        conv.add_user(content)
        self.assertTrue(conv.has_images)
        conv.reset()
        self.assertFalse(conv.has_images)
        messages = conv.get_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "system")

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

    @staticmethod
    def _make_image_file(tmpdir, name="cat.png", data=b"\x89PNG\r\n\x1a\n"):
        path = Path(tmpdir) / name
        path.write_bytes(data)
        return path

    def test_load_from_history_rehydrates_image_attachment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = self._make_image_file(tmpdir)
            conv = Conversation()
            conv.load_from_history(
                [
                    {
                        "role": "user",
                        "content": "[image: cat.png]",
                        "attachments": [
                            {"type": "image", "reference": str(img_path), "title": "cat.png"},
                        ],
                    },
                    {"role": "assistant", "content": "It's a cat."},
                ]
            )
            messages = conv.get_messages()
            user_msg = next(m for m in messages if m["role"] == "user")
            self.assertIsInstance(user_msg["content"], list)
            text_part = next(p for p in user_msg["content"] if p["type"] == "text")
            image_part = next(p for p in user_msg["content"] if p["type"] == "image_url")
            self.assertEqual(text_part["text"], "[image: cat.png]")
            expected_b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
            self.assertEqual(
                image_part["image_url"]["url"], f"data:image/png;base64,{expected_b64}"
            )
            self.assertTrue(conv.has_images)

    def test_load_from_history_missing_file_degrades_silently(self):
        conv = Conversation()
        conv.load_from_history(
            [
                {
                    "role": "user",
                    "content": "[image: gone.png]",
                    "attachments": [
                        {
                            "type": "image",
                            "reference": "/nonexistent/path/gone.png",
                            "title": "gone.png",
                        },
                    ],
                },
            ]
        )
        messages = conv.get_messages()
        user_msg = next(m for m in messages if m["role"] == "user")
        self.assertEqual(user_msg["content"], "[image: gone.png]")
        self.assertIsInstance(user_msg["content"], str)
        self.assertFalse(conv.has_images)

    def test_load_from_history_non_image_mime_degrades_silently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "notes.txt"
            path.write_text("hello")
            conv = Conversation()
            conv.load_from_history(
                [
                    {
                        "role": "user",
                        "content": "[file: notes.txt]",
                        "attachments": [
                            {"type": "image", "reference": str(path), "title": "notes.txt"},
                        ],
                    },
                ]
            )
            messages = conv.get_messages()
            user_msg = next(m for m in messages if m["role"] == "user")
            self.assertEqual(user_msg["content"], "[file: notes.txt]")
            self.assertFalse(conv.has_images)

    def test_load_from_history_caps_rehydration_at_most_recent_three(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            history = []
            for i in range(5):
                img_path = self._make_image_file(
                    tmpdir, name=f"img{i}.png", data=f"data-{i}".encode()
                )
                history.append(
                    {
                        "role": "user",
                        "content": f"[image: img{i}.png]",
                        "attachments": [
                            {
                                "type": "image",
                                "reference": str(img_path),
                                "title": f"img{i}.png",
                            },
                        ],
                    }
                )
                history.append({"role": "assistant", "content": f"reply {i}"})

            conv = Conversation()
            conv.load_from_history(history)
            messages = conv.get_messages()
            user_msgs = [m for m in messages if m["role"] == "user"]
            self.assertEqual(len(user_msgs), 5)
            # Oldest (5 - MAX_REHYDRATED_IMAGES) stay text placeholders; only
            # the most recent MAX_REHYDRATED_IMAGES are rehydrated.
            stale_cutoff = 5 - MAX_REHYDRATED_IMAGES
            for msg in user_msgs[:stale_cutoff]:
                self.assertIsInstance(msg["content"], str)
            for msg in user_msgs[stale_cutoff:]:
                self.assertIsInstance(msg["content"], list)
            self.assertTrue(conv.has_images)

    def test_load_from_history_no_attachments_leaves_has_images_false(self):
        conv = Conversation()
        conv.load_from_history(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
        )
        self.assertFalse(conv.has_images)


if __name__ == "__main__":
    unittest.main()
