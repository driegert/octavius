import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import local_tool_history


class SearchConversationHistoryTests(unittest.TestCase):
    def test_requires_query(self):
        session = SimpleNamespace(conn=object(), conv_id=1)
        self.assertEqual(
            local_tool_history.search_conversation_history({"query": ""}, session=session),
            "Error: query is required.",
        )

    def test_requires_session_connection(self):
        self.assertEqual(
            local_tool_history.search_conversation_history({"query": "x"}, session=None),
            "Error: no database connection available.",
        )

    def test_filters_to_octavius_service(self):
        fake_conn = object()
        session = SimpleNamespace(conn=fake_conn, conv_id=42)
        with patch(
            "history_store.search_conversations", return_value=[]
        ) as mock_search:
            local_tool_history.search_conversation_history(
                {"query": "gutters"}, session=session
            )
        mock_search.assert_called_once_with(
            fake_conn, "gutters", service="octavius", limit=5
        )

    def test_limit_clamped(self):
        fake_conn = object()
        session = SimpleNamespace(conn=fake_conn, conv_id=1)
        with patch(
            "history_store.search_conversations", return_value=[]
        ) as mock_search:
            local_tool_history.search_conversation_history(
                {"query": "x", "limit": 999}, session=session
            )
        _, kwargs = mock_search.call_args
        self.assertEqual(kwargs["limit"], 20)

    def test_empty_results_explains_indexing(self):
        session = SimpleNamespace(conn=object(), conv_id=1)
        with patch("history_store.search_conversations", return_value=[]):
            result = local_tool_history.search_conversation_history(
                {"query": "no match"}, session=session
            )
        self.assertIn("No prior conversations matched", result)
        self.assertIn("not indexed", result)

    def test_current_conversation_excluded(self):
        session = SimpleNamespace(conn=object(), conv_id=42)
        recent = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        rows = [
            {
                "conversation_id": 42,  # the current session — should be filtered out
                "session_id": "aaaaaaaa",
                "started_at": recent,
                "summary": "Current session in progress.",
                "tags": [],
            },
            {
                "conversation_id": 41,
                "session_id": "bbbbbbbb",
                "started_at": recent,
                "summary": "Earlier discussion of gutters.",
                "tags": ["home-maintenance"],
            },
        ]
        with patch("history_store.search_conversations", return_value=rows):
            result = local_tool_history.search_conversation_history(
                {"query": "gutters"}, session=session
            )
        self.assertNotIn("#42", result)
        self.assertIn("#41", result)
        self.assertIn("Earlier discussion of gutters", result)
        self.assertIn("home-maintenance", result)

    def test_formats_results_with_age_and_tags(self):
        session = SimpleNamespace(conn=object(), conv_id=1)
        ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        rows = [
            {
                "conversation_id": 7,
                "session_id": "abcd1234",
                "started_at": ts,
                "summary": "Designed conversation-history search tool.",
                "tags": ["design", "history"],
            }
        ]
        with patch("history_store.search_conversations", return_value=rows):
            result = local_tool_history.search_conversation_history(
                {"query": "search"}, session=session
            )
        self.assertIn("Prior conversations matching 'search'", result)
        self.assertIn("#7", result)
        self.assertIn("h ago", result)
        self.assertIn("design, history", result)
        self.assertIn("Designed conversation-history search tool.", result)


class FormatAgeTests(unittest.TestCase):
    def test_minutes(self):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat()
        self.assertTrue(local_tool_history._format_age(ts).endswith("m ago"))

    def test_missing(self):
        self.assertEqual(local_tool_history._format_age(None), "?")


if __name__ == "__main__":
    unittest.main()
