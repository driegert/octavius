import sqlite3
import tempfile
import unittest
from pathlib import Path

import history_store as store

# Conversation and inbox search run through the hybrid-corpus library now, so
# the tests that cover them build the library's sidecars in a throwaway
# database and drive them with FakeEmbedder — deterministic, unit-norm, and
# offline, so the suite never reaches an embedding endpoint. This mirrors
# hybrid-corpus's own tests/test_corpora_history.py.

# The app-owned tables the two adapters read, copied down from the live DDL.
# The token/duration columns matter: history_summaries' hydrate_sql selects
# them, so a fixture without them fails to hydrate rather than failing to rank.
_APP_DDL = """
CREATE TABLE conversations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT    NOT NULL UNIQUE,
    started_at          TEXT    NOT NULL,
    ended_at            TEXT,
    service             TEXT    NOT NULL,
    source              TEXT    NOT NULL,
    summary             TEXT,
    model               TEXT,
    message_count       INTEGER DEFAULT 0,
    total_input_tokens  INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_duration_ms   INTEGER DEFAULT 0
);
CREATE TABLE saved_items (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id      INTEGER,
    item_type            TEXT    NOT NULL,
    title                TEXT    NOT NULL,
    content              TEXT    NOT NULL,
    source_url           TEXT,
    metadata             TEXT,
    status               TEXT    NOT NULL DEFAULT 'pending',
    chat_conversation_id INTEGER,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT
);
CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE conversation_tags (
    conversation_id INTEGER NOT NULL,
    tag_id          INTEGER NOT NULL,
    PRIMARY KEY (conversation_id, tag_id)
);
"""


class LibraryBackedCase(unittest.TestCase):
    """A throwaway DB with sqlite_vec loaded, the app tables, and the library's
    sidecars, with history_store pointed at a FakeEmbedder."""

    fail_embedder = False

    def setUp(self):
        from hybrid_corpus.corpora.history import (
            HistorySavedItemsAdapter,
            HistorySummariesAdapter,
        )
        from hybrid_corpus.db import connect
        from hybrid_corpus.embed import FakeEmbedder

        self._tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self._tmp.name) / "history.db")
        self.conn.executescript(_APP_DDL)
        self.conn.commit()
        self.embedder = FakeEmbedder(fail=self.fail_embedder)
        self.adapters = {
            a.collection: a
            for a in (HistorySummariesAdapter(), HistorySavedItemsAdapter())
        }
        self.embedders = dict.fromkeys(self.adapters, self.embedder)
        store.set_search_backend(self.adapters, self.embedders)

    def tearDown(self):
        store.set_search_backend(None)
        self.conn.close()
        self._tmp.cleanup()

    def index_all(self):
        """Build/refresh both collections' sidecars from the app tables."""
        from hybrid_corpus.index import Indexer

        for adapter in self.adapters.values():
            indexer = Indexer(self.conn, adapter, self.embedder)
            indexer.ensure()
            indexer.sync()


class SaveItemTests(LibraryBackedCase):
    def test_save_and_get_saved_item_round_trip(self):
        item_id = store.save_item(
            self.conn, item_type="note", title="Title", content="Body",
            metadata={"a": 1},
        )
        item = store.get_saved_item(self.conn, item_id)
        self.assertEqual(item["title"], "Title")
        self.assertEqual(item["metadata"], {"a": 1})

    def test_save_item_indexes_the_new_row_immediately(self):
        """save_item indexes through the library's Indexer, so a just-saved item
        is searchable without waiting for history-index.timer."""
        item_id = store.save_item(
            self.conn, item_type="note", title="Multitaper",
            content="Thomson 1982 harmonic analysis",
        )
        units = self.conn.execute(
            "SELECT COUNT(*) FROM history_saved_items_units WHERE source_id = ?",
            (str(item_id),),
        ).fetchone()[0]
        self.assertEqual(units, 1)
        pending = self.conn.execute(
            "SELECT embed_pending FROM history_saved_items_source_state"
            " WHERE source_id = ?", (str(item_id),),
        ).fetchone()[0]
        self.assertEqual(pending, 0)


class SaveItemEmbedOutageTests(LibraryBackedCase):
    """An embedding outage must never fail a save. The row lands and stays
    keyword-searchable; only the vector is owed, and the timer collects it."""

    fail_embedder = True

    def test_save_survives_the_embedder_being_down(self):
        item_id = store.save_item(
            self.conn, item_type="note", title="Title", content="Body",
        )
        self.assertIsNotNone(item_id)
        self.assertEqual(store.get_saved_item(self.conn, item_id)["title"], "Title")

    def test_index_saved_item_reports_failure_without_raising(self):
        item_id = store.save_item(
            self.conn, item_type="note", title="Title", content="Body",
        )
        self.assertFalse(store.index_saved_item(self.conn, item_id))


class HistoryStoreTests(unittest.TestCase):

    @staticmethod
    def _messages_conn():
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE messages (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id   INTEGER NOT NULL,
                role              TEXT    NOT NULL,
                content           TEXT    NOT NULL,
                created_at        TEXT    NOT NULL,
                model             TEXT,
                input_tokens      INTEGER,
                output_tokens     INTEGER,
                latency_ms        INTEGER,
                stt_model         TEXT,
                stt_confidence    REAL,
                audio_duration_ms INTEGER,
                tts_model         TEXT,
                error             TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE tool_calls (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id      INTEGER NOT NULL,
                tool_name       TEXT    NOT NULL,
                server_name     TEXT,
                arguments       TEXT,
                status          TEXT    NOT NULL DEFAULT 'success',
                result_summary  TEXT,
                result_size     INTEGER,
                duration_ms     INTEGER,
                created_at      TEXT    NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE attachments (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id      INTEGER NOT NULL,
                type            TEXT    NOT NULL,
                reference       TEXT    NOT NULL,
                title           TEXT,
                created_at      TEXT    NOT NULL
            )"""
        )
        return conn

    def test_get_conversation_messages_includes_attachments_when_present(self):
        conn = self._messages_conn()
        conn.execute(
            """INSERT INTO messages (id, conversation_id, role, content, created_at)
               VALUES (1, 1, 'user', '[image: cat.png]', 't1')"""
        )
        conn.execute(
            """INSERT INTO attachments (message_id, type, reference, title, created_at)
               VALUES (1, 'image', '/spool/cat.png', 'cat.png', 't1')"""
        )
        conn.commit()

        messages = store.get_conversation_messages(conn, 1)

        self.assertEqual(len(messages), 1)
        self.assertEqual(
            messages[0]["attachments"],
            [{"type": "image", "reference": "/spool/cat.png", "title": "cat.png"}],
        )

    def test_get_conversation_messages_omits_attachments_key_when_absent(self):
        conn = self._messages_conn()
        conn.execute(
            """INSERT INTO messages (id, conversation_id, role, content, created_at)
               VALUES (1, 1, 'user', 'hello', 't1')"""
        )
        conn.commit()

        messages = store.get_conversation_messages(conn, 1)

        self.assertEqual(len(messages), 1)
        self.assertNotIn("attachments", messages[0])

    def test_update_saved_item_status_returns_true_for_existing_item(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE saved_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER,
                item_type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source_url TEXT,
                metadata TEXT,
                status TEXT NOT NULL,
                chat_conversation_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO saved_items
               (item_type, title, content, status, created_at)
               VALUES ('note', 'T', 'C', 'pending', 'now')"""
        )
        conn.commit()
        self.assertTrue(store.update_saved_item_status(conn, 1, "done"))
        self.assertEqual(conn.execute("SELECT status FROM saved_items WHERE id = 1").fetchone()[0], "done")


class ConversationLookupTests(unittest.TestCase):
    @staticmethod
    def _conversations_conn():
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE conversations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    TEXT NOT NULL UNIQUE,
                started_at    TEXT NOT NULL,
                ended_at      TEXT,
                service       TEXT NOT NULL,
                source        TEXT NOT NULL,
                summary       TEXT,
                model         TEXT,
                message_count INTEGER DEFAULT 0
            )"""
        )
        conn.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute(
            """CREATE TABLE conversation_tags (
                conversation_id INTEGER, tag_id INTEGER
            )"""
        )
        rows = [
            ("aaaa1111", "2026-07-19T12:00:00+00:00", "octavius", "voice", "Gutter talk"),
            ("bbbb2222", "2026-07-20T13:00:00+00:00", "octavius", "voice", "Multitaper chat"),
            ("cccc3333", "2026-07-20T14:00:00+00:00", "octavius", "matrix", "Matrix thread"),
            ("dddd4444", "2026-07-20T15:00:00+00:00", "claude-code", "cli", "Other service"),
        ]
        for session_id, started, service, source, summary in rows:
            conn.execute(
                """INSERT INTO conversations
                   (session_id, started_at, service, source, summary, message_count)
                   VALUES (?, ?, ?, ?, ?, 2)""",
                (session_id, started, service, source, summary),
            )
        # orphan row left by a WS connect that attached elsewhere / never spoke
        conn.execute(
            """INSERT INTO conversations
               (session_id, started_at, service, source, summary, message_count)
               VALUES ('eeee5555', '2026-07-20T16:00:00+00:00', 'octavius', 'voice',
                       NULL, 0)"""
        )
        conn.commit()
        return conn

    def test_list_conversations_orders_and_filters(self):
        conn = self._conversations_conn()
        results = store.list_conversations(conn, service="octavius")
        self.assertEqual(
            [r["summary"] for r in results],
            ["Matrix thread", "Multitaper chat", "Gutter talk"],
        )
        voice_only = store.list_conversations(conn, service="octavius", source="voice")
        self.assertEqual(
            [r["summary"] for r in voice_only], ["Multitaper chat", "Gutter talk"]
        )
        today = store.list_conversations(
            conn, service="octavius", since="2026-07-20T00:00:00+00:00"
        )
        self.assertEqual(
            [r["summary"] for r in today], ["Matrix thread", "Multitaper chat"]
        )

    def test_get_conversation_round_trip_and_missing(self):
        conn = self._conversations_conn()
        meta = store.get_conversation(conn, 2)
        self.assertEqual(meta["summary"], "Multitaper chat")
        self.assertEqual(meta["source"], "voice")
        self.assertEqual(meta["session_id"], "bbbb2222"[:8])
        self.assertIsNone(store.get_conversation(conn, 999))



class MemoryWatermarkTests(unittest.TestCase):
    """The watermark helpers moved here from memory.store when the memory
    service was extracted to the agent-memory repo — history.py's push path
    imports them from history_store, so they must exist and behave."""

    @staticmethod
    def _conn():
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                last_extracted_message_id INTEGER
            )"""
        )
        conn.execute(
            """CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL
            )"""
        )
        conn.execute("INSERT INTO conversations (last_extracted_message_id) VALUES (NULL)")
        for role, content in [
            ("user", "hi"),
            ("assistant", "hello"),
            ("tool", "SECRET tool payload"),
            ("user", "bye"),
        ]:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (1, ?, ?)",
                (role, content),
            )
        conn.commit()
        return conn

    def test_watermark_defaults_to_zero_and_round_trips(self):
        conn = self._conn()
        self.assertEqual(store.get_memory_watermark(conn, 1), 0)
        self.assertEqual(store.get_memory_watermark(conn, 999), 0)  # unknown conv
        store.set_memory_watermark(conn, 1, 4)
        self.assertEqual(store.get_memory_watermark(conn, 1), 4)

    def test_messages_after_watermark_excludes_tool_turns(self):
        conn = self._conn()
        msgs, max_id = store.messages_after_watermark(conn, 1, 0)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(max_id, 4)
        self.assertNotIn("SECRET", " ".join(m["content"] for m in msgs))

    def test_messages_after_watermark_respects_watermark(self):
        conn = self._conn()
        msgs, max_id = store.messages_after_watermark(conn, 1, 2)
        self.assertEqual([m["content"] for m in msgs], ["bye"])
        self.assertEqual(max_id, 4)
        # Nothing new past the watermark: max_id stays at the watermark.
        msgs, max_id = store.messages_after_watermark(conn, 1, 4)
        self.assertEqual(msgs, [])
        self.assertEqual(max_id, 4)


if __name__ == "__main__":
    unittest.main()


_CONVERSATIONS = [
    ("aaaa1111", "2026-07-19T12:00:00+00:00", "octavius", "voice", "Gutter talk about eavestroughs"),
    ("bbbb2222", "2026-07-20T13:00:00+00:00", "octavius", "voice", "Multitaper spectral estimation chat"),
    ("cccc3333", "2026-07-20T14:00:00+00:00", "octavius", "matrix", "Multitaper thread on Matrix"),
    ("dddd4444", "2026-07-20T15:00:00+00:00", "claude-code", "cli", "Multitaper work in another service"),
]


class SearchConversationsTests(LibraryBackedCase):
    """search_conversations over the history_summaries collection.

    The legacy version ranked by vec_distance_cosine over an L2-metric table of
    mixed-norm vectors, with a LIKE fallback. These assert the filters and the
    result shape its two callers (agent.py's episodic recall and the
    search_conversation_history local tool) actually consume.
    """

    def setUp(self):
        super().setUp()
        for session_id, started, service, source, summary in _CONVERSATIONS:
            self.conn.execute(
                "INSERT INTO conversations"
                " (session_id, started_at, service, source, summary, message_count)"
                " VALUES (?, ?, ?, ?, ?, 2)",
                (session_id, started, service, source, summary),
            )
        # An empty conversation with no summary: never indexed at all, because
        # the adapter only yields conversations whose summary is non-blank.
        self.conn.execute(
            "INSERT INTO conversations"
            " (session_id, started_at, service, source, summary, message_count)"
            " VALUES ('eeee5555', '2026-07-20T16:00:00+00:00', 'octavius', 'voice', NULL, 0)"
        )
        self.conn.execute("INSERT INTO tags (id, name) VALUES (1, 'statistics')")
        self.conn.execute(
            "INSERT INTO conversation_tags (conversation_id, tag_id) VALUES (2, 1)"
        )
        self.conn.commit()
        self.index_all()

    def _summaries(self, **kwargs):
        return [r["summary"] for r in
                store.search_conversations(self.conn, "multitaper", **kwargs)]

    def test_service_filter(self):
        hits = self._summaries(service="octavius")
        self.assertIn("Multitaper spectral estimation chat", hits)
        self.assertNotIn("Multitaper work in another service", hits)

    def test_source_filter(self):
        # Asserted as inclusion/exclusion rather than equality: FakeEmbedder's
        # similarity is deterministic but arbitrary by design, so the dense arm
        # legitimately surfaces any other row that passes the filter. What is
        # under test is the filter, and it must bite on BOTH arms.
        hits = self._summaries(service="octavius", source="voice")
        self.assertIn("Multitaper spectral estimation chat", hits)
        self.assertNotIn("Multitaper thread on Matrix", hits)
        self.assertNotIn("Multitaper work in another service", hits)

    def test_since_filter_excludes_earlier_conversations(self):
        hits = self._summaries(service="octavius",
                               since="2026-07-20T00:00:00+00:00")
        self.assertNotIn("Gutter talk about eavestroughs", hits)

    def test_combined_filters(self):
        hits = self._summaries(service="octavius", source="voice",
                               since="2026-07-20T00:00:00+00:00")
        self.assertEqual(hits, ["Multitaper spectral estimation chat"])

    def test_result_shape_matches_what_callers_read(self):
        """agent.py reads conversation_id/summary/distance; the local tool reads
        conversation_id/started_at/source/tags/summary."""
        hit = store.search_conversations(self.conn, "multitaper", service="octavius")[0]
        for key in ("conversation_id", "session_id", "started_at", "ended_at",
                    "service", "source", "summary", "model", "message_count",
                    "tags"):
            self.assertIn(key, hit)
        self.assertIsInstance(hit["tags"], list)
        self.assertLessEqual(len(hit["session_id"]), 8)
        # Additive, analogous to the legacy `distance` the shape already carried.
        self.assertIn("score", hit)
        self.assertIn("unit_id", hit)

    def test_tags_are_populated(self):
        hits = store.search_conversations(self.conn, "multitaper", service="octavius")
        by_id = {h["conversation_id"]: h for h in hits}
        self.assertEqual(by_id[2]["tags"], ["statistics"])

    def test_unsummarised_conversation_is_never_returned(self):
        hits = store.search_conversations(self.conn, "multitaper", service="octavius")
        self.assertNotIn(5, [h["conversation_id"] for h in hits])

    def test_limit_is_honoured(self):
        self.assertLessEqual(
            len(store.search_conversations(self.conn, "multitaper", limit=1)), 1
        )


class SearchConversationsDegradedTests(LibraryBackedCase):
    """The embedding chain being down must degrade to lexical-only: never raise,
    never silently return []. octavius did raise here before this change."""

    fail_embedder = True

    def setUp(self):
        super().setUp()
        for session_id, started, service, source, summary in _CONVERSATIONS:
            self.conn.execute(
                "INSERT INTO conversations"
                " (session_id, started_at, service, source, summary, message_count)"
                " VALUES (?, ?, ?, ?, ?, 2)",
                (session_id, started, service, source, summary),
            )
        self.conn.commit()
        # Index with a working embedder so there are vectors to NOT use; the
        # outage is on the query side.
        from hybrid_corpus.embed import FakeEmbedder
        from hybrid_corpus.index import Indexer
        good = FakeEmbedder()
        for adapter in self.adapters.values():
            indexer = Indexer(self.conn, adapter, good)
            indexer.ensure()
            indexer.sync()

    def test_lexical_results_still_come_back(self):
        hits = store.search_conversations(self.conn, "multitaper", service="octavius")
        self.assertTrue(hits, "an embedder outage must not empty the result set")
        self.assertIn("Multitaper", hits[0]["summary"])


class SearchSavedItemsTests(LibraryBackedCase):
    def setUp(self):
        super().setUp()
        rows = [
            (1, "note", "Multitaper reading list", "Thomson 1982 and Percival & Walden", "pending"),
            (None, "email_draft", "Reply to Melanie", "Thank you for reaching out", "done"),
            (None, "note", "Multitaper scratch", "dismissed multitaper note", "dismissed"),
        ]
        for conv_id, item_type, title, content, status in rows:
            self.conn.execute(
                "INSERT INTO saved_items"
                " (conversation_id, item_type, title, content, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, '2026-07-20T12:00:00+00:00')",
                (conv_id, item_type, title, content, status),
            )
        self.conn.commit()
        self.index_all()

    def test_result_shape_matches_the_inbox_api(self):
        hit = store.search_saved_items(self.conn, "multitaper")[0]
        for key in ("id", "conversation_id", "item_type", "title", "content",
                    "source_url", "metadata", "status", "created_at"):
            self.assertIn(key, hit)

    def test_conversation_id_is_preserved(self):
        """The shared adapter's shape has no conversation_id (the MCP tool never
        had one); octavius's API response always did, so it is restored."""
        hits = {h["id"]: h for h in store.search_saved_items(self.conn, "multitaper")}
        self.assertEqual(hits[1]["conversation_id"], 1)

    def test_dismissed_items_are_excluded_by_default(self):
        ids = [h["id"] for h in store.search_saved_items(self.conn, "multitaper")]
        self.assertNotIn(3, ids)

    def test_content_is_clipped_to_the_app_snippet_length(self):
        for hit in store.search_saved_items(self.conn, "multitaper"):
            self.assertLessEqual(len(hit["content"]), store.SAVED_ITEM_SNIPPET)


class SearchSavedItemsDegradedTests(LibraryBackedCase):
    fail_embedder = True

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO saved_items"
            " (item_type, title, content, status, created_at)"
            " VALUES ('note', 'Multitaper reading list', 'Thomson 1982',"
            " 'pending', '2026-07-20T12:00:00+00:00')"
        )
        self.conn.commit()
        from hybrid_corpus.embed import FakeEmbedder
        from hybrid_corpus.index import Indexer
        good = FakeEmbedder()
        for adapter in self.adapters.values():
            indexer = Indexer(self.conn, adapter, good)
            indexer.ensure()
            indexer.sync()

    def test_lexical_results_still_come_back(self):
        hits = store.search_saved_items(self.conn, "multitaper")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["title"], "Multitaper reading list")
