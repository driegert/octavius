import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import local_tool_history


class SearchConversationHistoryTests(unittest.TestCase):
    def test_requires_query_or_filter(self):
        session = SimpleNamespace(conn=object(), conv_id=1)
        result = local_tool_history.search_conversation_history(
            {"query": ""}, session=session
        )
        self.assertTrue(result.startswith("Error:"))
        self.assertIn("query", result)

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
            fake_conn, "gutters", service="octavius", limit=5, source=None, since=None
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


class SearchFiltersTests(unittest.TestCase):
    def test_rejects_unknown_source(self):
        session = SimpleNamespace(conn=object(), conv_id=1)
        result = local_tool_history.search_conversation_history(
            {"query": "x", "source": "carrier-pigeon"}, session=session
        )
        self.assertIn("source must be one of", result)

    def test_rejects_bad_since(self):
        session = SimpleNamespace(conn=object(), conv_id=1)
        result = local_tool_history.search_conversation_history(
            {"query": "x", "since": "yesterday-ish"}, session=session
        )
        self.assertIn("since must be", result)

    def test_source_and_since_passed_to_search(self):
        fake_conn = object()
        session = SimpleNamespace(conn=fake_conn, conv_id=1)
        with patch(
            "history_store.search_conversations", return_value=[]
        ) as mock_search:
            local_tool_history.search_conversation_history(
                {"query": "x", "source": "voice", "since": "2026-07-20"},
                session=session,
            )
        _, kwargs = mock_search.call_args
        self.assertEqual(kwargs["source"], "voice")
        # bare date normalizes to a UTC ISO timestamp (local midnight)
        since = datetime.fromisoformat(kwargs["since"])
        self.assertIsNotNone(since.tzinfo)

    def test_queryless_listing_uses_list_conversations(self):
        fake_conn = object()
        session = SimpleNamespace(conn=fake_conn, conv_id=1)
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        rows = [
            {
                "conversation_id": 9,
                "session_id": "cccccccc",
                "started_at": recent,
                "source": "voice",
                "summary": "Voice chat about multitaper deadlines.",
                "tags": [],
            }
        ]
        with patch(
            "history_store.list_conversations", return_value=rows
        ) as mock_list, patch(
            "history_store.search_conversations"
        ) as mock_search:
            result = local_tool_history.search_conversation_history(
                {"source": "voice"}, session=session
            )
        mock_search.assert_not_called()
        _, kwargs = mock_list.call_args
        self.assertEqual(kwargs["source"], "voice")
        self.assertIn("Recent conversations", result)
        self.assertIn("#9", result)
        self.assertIn("read_conversation", result)

    def test_result_lines_include_source_and_timestamp(self):
        session = SimpleNamespace(conn=object(), conv_id=1)
        ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        rows = [
            {
                "conversation_id": 7,
                "session_id": "abcd1234",
                "started_at": ts,
                "source": "voice",
                "summary": "Gutter planning.",
                "tags": [],
            }
        ]
        with patch("history_store.search_conversations", return_value=rows):
            result = local_tool_history.search_conversation_history(
                {"query": "gutters"}, session=session
            )
        self.assertIn("voice", result)
        expected_ts = local_tool_history._format_local(ts)
        self.assertIn(expected_ts, result)


class ReadConversationTests(unittest.TestCase):
    @staticmethod
    def _session():
        return SimpleNamespace(conn=object(), conv_id=99)

    @staticmethod
    def _meta(**over):
        meta = {
            "conversation_id": 7,
            "session_id": "abcd1234",
            "started_at": "2026-07-20T13:14:00+00:00",
            "ended_at": None,
            "service": "octavius",
            "source": "voice",
            "summary": "Talked about gutters.",
            "model": None,
            "message_count": 3,
        }
        meta.update(over)
        return meta

    def test_requires_conversation_id(self):
        result = local_tool_history.read_conversation({}, session=self._session())
        self.assertIn("conversation_id is required", result)

    def test_requires_session_connection(self):
        result = local_tool_history.read_conversation(
            {"conversation_id": 7}, session=None
        )
        self.assertIn("no database connection", result)

    def test_rejects_current_conversation(self):
        result = local_tool_history.read_conversation(
            {"conversation_id": 99}, session=self._session()
        )
        self.assertIn("current conversation", result)

    def test_unknown_conversation(self):
        with patch("history_store.get_conversation", return_value=None):
            result = local_tool_history.read_conversation(
                {"conversation_id": 7}, session=self._session()
            )
        self.assertIn("no conversation #7", result)

    def test_formats_transcript_and_skips_tool_rows(self):
        messages = [
            {"role": "user", "content": "Hi there", "created_at": "2026-07-20T13:14:00+00:00"},
            {"role": "tool", "content": "raw tool blob", "created_at": "2026-07-20T13:14:05+00:00"},
            {"role": "assistant", "content": "Hello Dave", "created_at": "2026-07-20T13:14:10+00:00"},
        ]
        with patch(
            "history_store.get_conversation", return_value=self._meta()
        ), patch(
            "history_store.get_conversation_messages", return_value=messages
        ):
            result = local_tool_history.read_conversation(
                {"conversation_id": 7}, session=self._session()
            )
        self.assertIn("Conversation #7 (voice)", result)
        self.assertIn("Summary: Talked about gutters.", result)
        self.assertIn("Dave: Hi there", result)
        self.assertIn("Octavius: Hello Dave", result)
        self.assertNotIn("raw tool blob", result)
        self.assertIn("page 1 of 1", result)

    def test_pagination_most_recent_first(self):
        big = "x" * 1200
        messages = [
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"msg{i} {big}",
                "created_at": f"2026-07-20T13:{i:02d}:00+00:00",
            }
            for i in range(8)
        ]
        with patch(
            "history_store.get_conversation", return_value=self._meta(message_count=8)
        ), patch(
            "history_store.get_conversation_messages", return_value=messages
        ):
            page1 = local_tool_history.read_conversation(
                {"conversation_id": 7}, session=self._session()
            )
            page2 = local_tool_history.read_conversation(
                {"conversation_id": 7, "page": 2}, session=self._session()
            )
            too_far = local_tool_history.read_conversation(
                {"conversation_id": 7, "page": 99}, session=self._session()
            )
        self.assertIn("msg7", page1)
        self.assertNotIn("msg0", page1)
        self.assertIn("page=2", page1)
        self.assertNotIn("msg7", page2)
        self.assertIn("higher pages are earlier", page1)
        self.assertIn("only", too_far)

    def test_long_message_truncated(self):
        messages = [
            {
                "role": "user",
                "content": "y" * 5000,
                "created_at": "2026-07-20T13:14:00+00:00",
            }
        ]
        with patch(
            "history_store.get_conversation", return_value=self._meta(message_count=1)
        ), patch(
            "history_store.get_conversation_messages", return_value=messages
        ):
            result = local_tool_history.read_conversation(
                {"conversation_id": 7}, session=self._session()
            )
        self.assertIn("[message truncated]", result)
        self.assertLess(len(result), 3000)


class FormatAgeTests(unittest.TestCase):
    def test_minutes(self):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat()
        self.assertTrue(local_tool_history._format_age(ts).endswith("m ago"))

    def test_missing(self):
        self.assertEqual(local_tool_history._format_age(None), "?")


if __name__ == "__main__":
    unittest.main()
