import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import local_tool_inbox


class FormatAgeTests(unittest.TestCase):
    def test_seconds_ago(self):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        self.assertTrue(local_tool_inbox._format_age(ts).endswith("s ago"))

    def test_minutes_ago(self):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.assertTrue(local_tool_inbox._format_age(ts).endswith("m ago"))

    def test_hours_ago(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self.assertTrue(local_tool_inbox._format_age(ts).endswith("h ago"))

    def test_days_ago(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        self.assertTrue(local_tool_inbox._format_age(ts).endswith("d ago"))

    def test_naive_timestamp_treated_as_utc(self):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=2)).replace(tzinfo=None).isoformat()
        self.assertTrue(local_tool_inbox._format_age(ts).endswith("m ago"))

    def test_missing_timestamp(self):
        self.assertEqual(local_tool_inbox._format_age(None), "?")
        self.assertEqual(local_tool_inbox._format_age(""), "?")


class ContentSnippetTests(unittest.TestCase):
    def test_short_content_unchanged(self):
        self.assertEqual(local_tool_inbox._content_snippet("hello world"), "hello world")

    def test_long_content_truncated_with_ellipsis(self):
        content = "a" * 500
        snippet = local_tool_inbox._content_snippet(content, length=50)
        self.assertTrue(snippet.endswith("…"))
        self.assertLessEqual(len(snippet), 51)

    def test_whitespace_collapsed(self):
        self.assertEqual(
            local_tool_inbox._content_snippet("foo\n\n  bar\t\tbaz"),
            "foo bar baz",
        )


class SaveToStashTests(unittest.TestCase):
    def test_requires_title_and_content(self):
        session = SimpleNamespace(conn=object(), conv_id=1)
        self.assertEqual(
            local_tool_inbox.save_to_stash({"title": "", "content": ""}, session=session),
            "Error: title and content are required.",
        )

    def test_requires_session_connection(self):
        self.assertEqual(
            local_tool_inbox.save_to_stash({"title": "t", "content": "c"}, session=None),
            "Error: no database connection available.",
        )

    def test_success_returns_stash_phrasing(self):
        session = SimpleNamespace(conn=object(), conv_id=42)
        with patch("history.save_item", return_value=99):
            result = local_tool_inbox.save_to_stash(
                {"title": "A Note", "content": "body", "item_type": "note"},
                session=session,
            )
        self.assertEqual(result, "Saved to stash (item #99): A Note")


class ListStashItemsTests(unittest.TestCase):
    def test_requires_session_connection(self):
        self.assertEqual(
            local_tool_inbox.list_stash_items({}, session=None),
            "Error: no database connection available.",
        )

    def test_defaults_to_pending(self):
        fake_conn = object()
        session = SimpleNamespace(conn=fake_conn, conv_id=1)
        with patch("history_store.list_saved_items", return_value=[]) as mock_list:
            result = local_tool_inbox.list_stash_items({}, session=session)
        mock_list.assert_called_once_with(fake_conn, status="pending", item_type=None, limit=20)
        self.assertEqual(result, "No stash items found (status=pending).")

    def test_all_status_drops_filter(self):
        fake_conn = object()
        session = SimpleNamespace(conn=fake_conn, conv_id=1)
        with patch("history_store.list_saved_items", return_value=[]) as mock_list:
            local_tool_inbox.list_stash_items({"status": "all"}, session=session)
        mock_list.assert_called_once_with(fake_conn, status=None, item_type=None, limit=20)

    def test_formats_items_with_snippet(self):
        recent = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        session = SimpleNamespace(conn=object(), conv_id=1)
        rows = [
            {
                "id": 7,
                "title": "Paper summary",
                "item_type": "search_summary",
                "status": "pending",
                "content": "Two promising papers on graph drawing and education found via OpenAlex.",
                "created_at": recent,
            }
        ]
        with patch("history_store.list_saved_items", return_value=rows):
            result = local_tool_inbox.list_stash_items({}, session=session)
        self.assertIn("Stash items (status=pending)", result)
        self.assertIn("#7 [pending/search_summary]", result)
        self.assertIn("Paper summary", result)
        self.assertIn("Two promising papers", result)

    def test_limit_clamped(self):
        fake_conn = object()
        session = SimpleNamespace(conn=fake_conn, conv_id=1)
        with patch("history_store.list_saved_items", return_value=[]) as mock_list:
            local_tool_inbox.list_stash_items({"limit": 9999}, session=session)
        _, kwargs = mock_list.call_args
        self.assertEqual(kwargs["limit"], 50)


if __name__ == "__main__":
    unittest.main()
