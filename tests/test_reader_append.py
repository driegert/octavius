"""Appending content to an existing reader document.

The scenario: a URL pull came back partial (sign-in pop-up, paywall teaser) and
Dave supplies the rest by hand. The new text must land at the END of the
existing document without disturbing the chunks already there — Dave's
playback position points into them — and a later retry (which replays the
original source) must fold the appended text back in rather than drop it.
"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import local_tool_reader
import reader_ingest_handlers as handlers
import reader_ingest_service as service
import reader_store
import reader_text

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import reader_api
except ImportError:  # pragma: no cover
    FastAPI = TestClient = reader_api = None


READER_DOCUMENTS_DDL = """CREATE TABLE reader_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_path TEXT,
    saved_item_id INTEGER,
    speech_file TEXT,
    original_md_file TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'processing',
    error TEXT,
    last_chunk INTEGER NOT NULL DEFAULT 0,
    last_sentence INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT
)"""


def _make_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(READER_DOCUMENTS_DDL)
    conn.commit()
    conn.close()


def _insert_doc(conn, *, title="Article", status="ready", speech_file=None,
                last_chunk=0, last_sentence=0, source_type="url", source_path="https://x/a"):
    cur = conn.execute(
        """INSERT INTO reader_documents
           (title, source_type, source_path, speech_file, status, last_chunk, last_sentence, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'now')""",
        (title, source_type, source_path, speech_file, status, last_chunk, last_sentence),
    )
    conn.commit()
    return cur.lastrowid


def _fake_settings(directory: str):
    return SimpleNamespace(reader=SimpleNamespace(directory=directory))


async def _identity_convert(chunks):
    """Stand-in for the math-conversion pass: speech text == chunk text."""
    return [c["text"] for c in chunks]


class AppendixStoreTests(unittest.TestCase):
    def test_appendices_round_trip_in_append_order(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)):
            self.assertEqual(reader_store.load_appendices(7), [])
            reader_store.save_appendix(7, "first")
            reader_store.save_appendix(7, "second")
            reader_store.save_appendix(7, "third")
            self.assertEqual(reader_store.load_appendices(7), ["first", "second", "third"])
            # Sequence numbers, not timestamps, order the blocks: two appends
            # within the same second must still come back in append order.
            names = sorted(p.name for p in reader_store.appendix_dir(7).glob("*.md"))
            self.assertTrue(names[0].startswith("001-"))
            self.assertTrue(names[2].startswith("003-"))

            reader_store.clear_appendices(7)
            self.assertEqual(reader_store.load_appendices(7), [])
            self.assertFalse(reader_store.appendix_dir(7).exists())

    def test_replace_writes_new_block_before_removing_old_and_sets_marker(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)):
            reader_store.save_appendix(3, "stale one")
            reader_store.save_appendix(3, "stale two")
            self.assertFalse(reader_store.is_replaced(3))

            path = reader_store.save_appendix(3, "fresh", replace=True)

            # Sequence keeps counting (003) so the new file could never have
            # collided with one it was about to remove.
            self.assertTrue(path.name.startswith("003-"))
            self.assertEqual(reader_store.load_appendices(3), ["fresh"])
            self.assertTrue(reader_store.is_replaced(3))
            # A later plain append lands after the replacement and keeps the marker.
            reader_store.save_appendix(3, "later")
            self.assertEqual(reader_store.load_appendices(3), ["fresh", "later"])
            self.assertTrue(reader_store.is_replaced(3))

    def test_non_numbered_files_in_appendix_dir_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)):
            reader_store.save_appendix(4, "real")
            (reader_store.appendix_dir(4) / "notes.md").write_text("stray")
            (reader_store.appendix_dir(4) / ".001-swap.md.swp").write_text("swap")
            self.assertEqual(reader_store.load_appendices(4), ["real"])
            reader_store.save_appendix(4, "second")
            self.assertEqual(reader_store.load_appendices(4), ["real", "second"])

    def test_claim_document_for_processing_is_conditional(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(READER_DOCUMENTS_DDL)
        doc_id = _insert_doc(conn, status="ready")
        conn.execute("UPDATE reader_documents SET error = 'old' WHERE id = ?", (doc_id,))
        self.assertTrue(reader_store.claim_document_for_processing(conn, doc_id))
        row = conn.execute("SELECT status, error FROM reader_documents WHERE id = ?", (doc_id,)).fetchone()
        self.assertEqual(row, ("processing", None))
        # Second claimant loses; a missing row also loses.
        self.assertFalse(reader_store.claim_document_for_processing(conn, doc_id))
        self.assertFalse(reader_store.claim_document_for_processing(conn, 999))

    def test_delete_document_removes_appendices(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)):
            conn = sqlite3.connect(":memory:")
            conn.execute(READER_DOCUMENTS_DDL)
            doc_id = _insert_doc(conn)
            reader_store.save_appendix(doc_id, "extra")
            self.assertTrue(reader_store.appendix_dir(doc_id).exists())

            self.assertTrue(reader_store.delete_document(conn, doc_id))
            self.assertFalse(reader_store.appendix_dir(doc_id).exists())


class AppendToDocumentTests(unittest.TestCase):
    """reader_text.append_to_document — the incremental speech-JSON update."""

    def _patches(self, tmp, convert=_identity_convert):
        return (
            patch.object(reader_text, "READER_PATH", Path(tmp)),
            patch.object(reader_store, "settings", _fake_settings(tmp)),
            patch.object(reader_text, "_convert_all_chunks", convert),
        )

    def _setup(self, tmp):
        conn = sqlite3.connect(":memory:")
        conn.execute(READER_DOCUMENTS_DDL)
        reader_dir = Path(tmp)
        speech_path = reader_dir / "1.json"
        existing = {
            "title": "Article",
            "total_sentences": 3,
            "chunks": [
                {"index": 0, "heading": "Intro", "speech_text": "One. Two.", "sentences": ["One.", "Two."]},
                {"index": 1, "heading": None, "speech_text": "Three.", "sentences": ["Three."]},
            ],
        }
        speech_path.write_text(json.dumps(existing))
        # Mid-chunk position (chunk 0, sentence 1): stronger than a chunk
        # boundary, since sentence indices within a chunk must also survive.
        doc_id = _insert_doc(conn, speech_file=str(speech_path), last_chunk=0, last_sentence=1)
        self.assertEqual(doc_id, 1)
        return conn, reader_dir, speech_path, existing

    def test_append_keeps_existing_chunks_and_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, reader_dir, speech_path, existing = self._setup(tmp)
            p1, p2, p3 = self._patches(tmp)
            with p1, p2, p3:
                asyncio.run(reader_text.append_to_document(conn, 1, "## More\n\nFour. Five.\n\nSix."))
                appendices, replaced = reader_store.load_appendices(1), reader_store.is_replaced(1)

            speech = json.loads(speech_path.read_text())
            # The first two chunks are byte-for-byte what was there before.
            self.assertEqual(speech["chunks"][:2], existing["chunks"])
            self.assertEqual([c["index"] for c in speech["chunks"]], [0, 1, 2])
            self.assertEqual(speech["chunks"][2]["heading"], "More")
            self.assertEqual(speech["chunks"][2]["sentences"], ["Four.", "Five.", "Six."])
            self.assertEqual(speech["total_sentences"], 6)

            row = conn.execute(
                "SELECT status, error, chunk_count, last_chunk, last_sentence FROM reader_documents WHERE id = 1"
            ).fetchone()
            self.assertEqual(row, ("ready", None, 3, 0, 1))
            # The appendix landed too, so a retry can reproduce it.
            self.assertEqual(appendices, ["## More\n\nFour. Five.\n\nSix."])
            self.assertFalse(replaced)

    def test_replace_discards_existing_and_resets_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, reader_dir, speech_path, _ = self._setup(tmp)
            p1, p2, p3 = self._patches(tmp)
            with p1, p2, p3:
                asyncio.run(reader_text.append_to_document(conn, 1, "Fresh start.", replace=True))
                replaced = reader_store.is_replaced(1)

            speech = json.loads(speech_path.read_text())
            self.assertEqual(len(speech["chunks"]), 1)
            self.assertEqual(speech["chunks"][0]["index"], 0)
            self.assertEqual(speech["chunks"][0]["sentences"], ["Fresh start."])
            row = conn.execute(
                "SELECT status, chunk_count, last_chunk, last_sentence FROM reader_documents WHERE id = 1"
            ).fetchone()
            self.assertEqual(row, ("ready", 1, 0, 0))
            self.assertTrue(replaced)

    def test_failed_document_without_speech_file_is_built_from_new_text(self):
        """A URL pull that hit a sign-in wall fails with no speech file; the
        appended text is then the whole document."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(":memory:")
            conn.execute(READER_DOCUMENTS_DDL)
            # Stale position from a life before the failure must not point
            # past the end of the rebuilt document.
            doc_id = _insert_doc(conn, status="failed", last_chunk=7, last_sentence=3)
            p1, p2, p3 = self._patches(tmp)
            with p1, p2, p3:
                asyncio.run(reader_text.append_to_document(conn, doc_id, "The real article.", prior_status="failed"))

            row = conn.execute(
                "SELECT status, error, speech_file, chunk_count, last_chunk, last_sentence "
                "FROM reader_documents WHERE id = ?", (doc_id,)
            ).fetchone()
            self.assertEqual(row[:2], ("ready", None))
            self.assertEqual(row[3:], (1, 0, 0))
            speech = json.loads(Path(row[2]).read_text())
            self.assertEqual(speech["chunks"][0]["sentences"], ["The real article."])

    def test_blank_text_restores_prior_status_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, reader_dir, speech_path, existing = self._setup(tmp)
            p1, p2, p3 = self._patches(tmp)
            with p1, p2, p3:
                asyncio.run(reader_text.append_to_document(conn, 1, "   \n  ", prior_status="ready"))
                appendices = reader_store.load_appendices(1)

            row = conn.execute("SELECT status, error FROM reader_documents WHERE id = 1").fetchone()
            self.assertEqual(row[0], "ready")
            self.assertIn("No content", row[1])
            self.assertEqual(json.loads(speech_path.read_text()), existing)
            self.assertEqual(appendices, [])

    def test_conversion_failure_leaves_ready_document_ready_with_no_orphan_appendix(self):
        """The likeliest real failure: the reader LLM is down mid-append. The
        document must stay playable, its speech file byte-for-byte intact, and
        no appendix may be left behind for a later retry to fold in."""
        async def boom(chunks):
            raise RuntimeError("LLM chain down")

        with tempfile.TemporaryDirectory() as tmp:
            conn, reader_dir, speech_path, existing = self._setup(tmp)
            before = speech_path.read_bytes()
            p1, p2, p3 = self._patches(tmp, convert=boom)
            with p1, p2, p3:
                asyncio.run(reader_text.append_to_document(conn, 1, "More.", prior_status="ready"))
                appendices, dir_exists = reader_store.load_appendices(1), reader_store.appendix_dir(1).exists()

            row = conn.execute(
                "SELECT status, error, last_chunk, last_sentence FROM reader_documents WHERE id = 1"
            ).fetchone()
            self.assertEqual(row, ("ready", "Append failed: LLM chain down", 0, 1))
            self.assertEqual(speech_path.read_bytes(), before)
            self.assertEqual(appendices, [])
            self.assertFalse(dir_exists)

    def test_conversion_failure_on_failed_document_stays_failed(self):
        async def boom(chunks):
            raise RuntimeError("LLM chain down")

        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(":memory:")
            conn.execute(READER_DOCUMENTS_DDL)
            doc_id = _insert_doc(conn, status="failed")
            p1, p2, p3 = self._patches(tmp, convert=boom)
            with p1, p2, p3:
                asyncio.run(reader_text.append_to_document(conn, doc_id, "More.", prior_status="failed"))
            row = conn.execute("SELECT status FROM reader_documents WHERE id = ?", (doc_id,)).fetchone()
            self.assertEqual(row, ("failed",))

    def test_appendix_persistence_failure_fails_the_append_before_the_speech_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, reader_dir, speech_path, existing = self._setup(tmp)
            p1, p2, p3 = self._patches(tmp)
            with p1, p2, p3, patch.object(reader_text, "save_appendix", side_effect=OSError("disk full")):
                asyncio.run(reader_text.append_to_document(conn, 1, "More.", prior_status="ready"))
            row = conn.execute("SELECT status, error FROM reader_documents WHERE id = 1").fetchone()
            self.assertEqual(row, ("ready", "Append failed: disk full"))
            self.assertEqual(json.loads(speech_path.read_text()), existing)

    def test_missing_document_marks_nothing_and_logs(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(READER_DOCUMENTS_DDL)
        with patch.object(reader_text, "_convert_all_chunks", _identity_convert):
            asyncio.run(reader_text.append_to_document(conn, 99, "text"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM reader_documents").fetchone()[0], 0)

    def test_speech_file_write_is_atomic(self):
        """A failure between temp write and rename must leave the old file intact
        and no temp file behind."""
        with tempfile.TemporaryDirectory() as tmp:
            conn, reader_dir, speech_path, existing = self._setup(tmp)
            with patch.object(reader_text, "READER_PATH", reader_dir), \
                 patch.object(reader_text.os, "replace", side_effect=OSError("rename failed")):
                with self.assertRaises(OSError):
                    reader_text._write_speech(conn, 1, "Article", existing["chunks"])
            self.assertEqual(json.loads(speech_path.read_text()), existing)
            self.assertEqual([p.name for p in reader_dir.glob("*.tmp")], [])


class RetryFoldsAppendicesTests(unittest.TestCase):
    """ingest_document (the tail of every retry path) appends saved appendices."""

    def test_ingest_document_appends_saved_appendices_after_base_text(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)), \
             patch.object(reader_text, "READER_PATH", Path(tmp)), \
             patch.object(reader_text, "_convert_all_chunks", _identity_convert):
            conn = sqlite3.connect(":memory:")
            conn.execute(READER_DOCUMENTS_DDL)
            doc_id = _insert_doc(conn, status="processing")
            reader_store.save_appendix(doc_id, "Appended one.")
            reader_store.save_appendix(doc_id, "Appended two.")

            asyncio.run(reader_text.ingest_document(conn, doc_id, "Base text.\n", "Article"))

            row = conn.execute("SELECT status, speech_file FROM reader_documents WHERE id = ?", (doc_id,)).fetchone()
            self.assertEqual(row[0], "ready")
            speech = json.loads(Path(row[1]).read_text())
            all_sentences = [s for c in speech["chunks"] for s in c["sentences"]]
            self.assertEqual(all_sentences, ["Base text.", "Appended one.", "Appended two."])

    def test_replaced_document_drops_original_source_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)), \
             patch.object(reader_text, "READER_PATH", Path(tmp)), \
             patch.object(reader_text, "_convert_all_chunks", _identity_convert):
            conn = sqlite3.connect(":memory:")
            conn.execute(READER_DOCUMENTS_DDL)
            doc_id = _insert_doc(conn, status="processing")
            reader_store.save_appendix(doc_id, "Please sign in.")
            reader_store.save_appendix(doc_id, "The real article.", replace=True)
            reader_store.save_appendix(doc_id, "And a later addition.")

            self.assertEqual(
                reader_text.with_appendices(doc_id, "Please sign in to continue."),
                "The real article.\n\nAnd a later addition.",
            )
            asyncio.run(reader_text.ingest_document(conn, doc_id, "Please sign in to continue.", "Article"))
            row = conn.execute("SELECT status, speech_file FROM reader_documents WHERE id = ?", (doc_id,)).fetchone()
            speech = json.loads(Path(row[1]).read_text())
            all_sentences = [s for c in speech["chunks"] for s in c["sentences"]]
            self.assertEqual(all_sentences, ["The real article.", "And a later addition."])

    def test_start_retry_task_skips_source_replay_for_replaced_document(self):
        """For a URL the original source is the sign-in wall: replaying it would
        fail again before ingest_document ever folded the real text in."""
        created = []

        def fake_create_task(coro):
            created.append(coro.cr_frame.f_locals.copy())
            coro.close()

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)), \
             patch.object(handlers.asyncio, "create_task", side_effect=fake_create_task), \
             patch.object(handlers, "ingest_url_document") as url_ingest:
            reader_store.save_appendix(5, "The article.", replace=True)
            doc = {"id": 5, "title": "T", "source_type": "url", "source_path": "https://x/a", "saved_item_id": None}
            handlers.start_retry_task(Path(tmp) / "h.db", object(), doc, service.ReaderIngestError)

        url_ingest.assert_not_called()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["markdown"], "")
        self.assertEqual(created[0]["doc_id"], 5)

    def test_with_appendices_is_identity_when_nothing_was_appended(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)):
            self.assertEqual(reader_text.with_appendices(5, "Base."), "Base.")


class StartAppendIngestTests(unittest.TestCase):
    """reader_ingest_handlers.start_append_ingest — status rules and task launch."""

    def _run(self, db_path, doc_id, text, *, replace=False):
        created = []

        def fake_create_task(coro):
            created.append(coro.cr_frame.f_locals.copy())
            coro.close()

        with patch.object(handlers.asyncio, "create_task", side_effect=fake_create_task):
            result = asyncio.run(
                handlers.start_append_ingest(db_path, doc_id, text, service.ReaderIngestError, replace=replace)
            )
        return result, created

    def test_rejects_missing_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with self.assertRaises(service.ReaderIngestError) as ctx:
                self._run(db_path, 42, "more")
            self.assertEqual(ctx.exception.status_code, 404)

    def test_rejects_document_still_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn, status="processing")
            with self.assertRaises(service.ReaderIngestError) as ctx:
                self._run(db_path, doc_id, "more")
            self.assertEqual(ctx.exception.status_code, 409)

    def test_rejects_blank_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn)
            with self.assertRaises(service.ReaderIngestError):
                self._run(db_path, doc_id, "  ")

    def test_ready_document_is_claimed_and_task_carries_prior_status(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)):
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn, title="Article", status="ready")
                conn.execute("UPDATE reader_documents SET error = 'old' WHERE id = ?", (doc_id,))

            result, created = self._run(db_path, doc_id, "More text.")

            self.assertEqual(result, {"id": doc_id, "status": "processing", "title": "Article", "replace": False})
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0]["prior_status"], "ready")
            self.assertEqual(created[0]["text"], "More text.")
            # Nothing is persisted until the task's conversion succeeds.
            self.assertEqual(reader_store.load_appendices(doc_id), [])
            with sqlite3.connect(db_path) as conn:
                row = conn.execute("SELECT status, error FROM reader_documents WHERE id = ?", (doc_id,)).fetchone()
            self.assertEqual(row, ("processing", None))

    def test_failed_document_is_allowed(self):
        """The sign-in-wall case: the pull failed, and hand-supplied text is the fix."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn, status="failed")
            result, created = self._run(db_path, doc_id, "The article.")
            self.assertEqual(result["status"], "processing")
            self.assertEqual(created[0]["prior_status"], "failed")

    def test_second_concurrent_append_is_rejected_by_the_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn)
            self._run(db_path, doc_id, "first")
            with self.assertRaises(service.ReaderIngestError) as ctx:
                self._run(db_path, doc_id, "second")
            self.assertEqual(ctx.exception.status_code, 409)

    def test_end_to_end_append_through_the_background_task(self):
        """Let the task actually run: status goes processing → ready, the speech
        file gains the new chunk, and the appendix is on disk."""
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)), \
             patch.object(reader_text, "READER_PATH", Path(tmp)), \
             patch.object(reader_text, "_convert_all_chunks", _identity_convert):
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            speech_path = Path(tmp) / "1.json"
            speech_path.write_text(json.dumps({
                "title": "Article", "total_sentences": 1,
                "chunks": [{"index": 0, "heading": None, "speech_text": "One.", "sentences": ["One."]}],
            }))
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn, speech_file=str(speech_path), status="ready")

            async def run():
                result = await handlers.start_append_ingest(
                    db_path, doc_id, "Two.", service.ReaderIngestError
                )
                pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                await asyncio.gather(*pending)
                return result

            result = asyncio.run(run())

            self.assertEqual(result["status"], "processing")
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT status, error, chunk_count FROM reader_documents WHERE id = ?", (doc_id,)
                ).fetchone()
            self.assertEqual(row, ("ready", None, 2))
            speech = json.loads(speech_path.read_text())
            self.assertEqual([c["sentences"] for c in speech["chunks"]], [["One."], ["Two."]])
            self.assertEqual(reader_store.load_appendices(doc_id), ["Two."])


class ResolveAppendTextTests(unittest.TestCase):
    def test_text_wins_over_path(self):
        self.assertEqual(handlers.resolve_append_text("body", "/nope", service.ReaderIngestError), "body")

    def test_requires_text_or_path(self):
        with self.assertRaises(service.ReaderIngestError):
            handlers.resolve_append_text(None, None, service.ReaderIngestError)

    def test_missing_path_is_404(self):
        with self.assertRaises(service.ReaderIngestError) as ctx:
            handlers.resolve_append_text("", "/definitely/missing.md", service.ReaderIngestError)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_directory_path_is_a_clean_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(service.ReaderIngestError) as ctx:
                handlers.resolve_append_text(None, tmp, service.ReaderIngestError)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("regular file", ctx.exception.message)

    def test_non_string_text_is_rejected(self):
        with self.assertRaises(service.ReaderIngestError):
            handlers.resolve_append_text(["not", "a", "string"], None, service.ReaderIngestError)

    def test_markdown_file_is_read_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rest.md"
            path.write_text("# Rest\n\nof the article.\n")
            self.assertEqual(
                handlers.resolve_append_text(None, str(path), service.ReaderIngestError),
                "# Rest\n\nof the article.\n",
            )

    def test_html_file_goes_through_article_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved.html"
            path.write_text("<html><body><article><p>Extracted body.</p></article></body></html>")
            with patch.object(handlers, "extract_article_text", return_value="Extracted body."):
                result = handlers.resolve_append_text(None, str(path), service.ReaderIngestError)
            self.assertEqual(result, "Extracted body.")

    def test_pdf_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.pdf"
            path.write_bytes(b"%PDF-1.4 stub")
            with self.assertRaises(service.ReaderIngestError) as ctx:
                handlers.resolve_append_text(None, str(path), service.ReaderIngestError)
            self.assertIn("PDF", ctx.exception.message)


class AppendServiceTests(unittest.TestCase):
    def test_append_reader_document_passes_text_and_replace_through(self):
        seen = {}

        async def fake_start(db_path, doc_id, text, err, replace=False):
            seen.update(db_path=db_path, doc_id=doc_id, text=text, replace=replace)
            return {"id": doc_id, "status": "processing", "title": "T", "replace": replace}

        with patch.object(service, "start_append_ingest", side_effect=fake_start):
            result = asyncio.run(
                service.append_reader_document("/tmp/x.db", 3, {"text": "more", "replace": True})
            )
        self.assertEqual(seen, {"db_path": Path("/tmp/x.db"), "doc_id": 3, "text": "more", "replace": True})
        self.assertTrue(result["replace"])

    def test_string_false_does_not_replace(self):
        """bool("false") is True; a tool call that serialises the flag as a
        string must not destructively rebuild the document."""
        seen = {}

        async def fake_start(db_path, doc_id, text, err, replace=False):
            seen["replace"] = replace
            return {"id": doc_id, "status": "processing", "title": "T", "replace": replace}

        with patch.object(service, "start_append_ingest", side_effect=fake_start):
            for value in ("false", "False", "0", "", 0, None, False):
                asyncio.run(service.append_reader_document("/tmp/x.db", 3, {"text": "m", "replace": value}))
                self.assertFalse(seen["replace"], value)
            for value in ("true", "True", "1", 1, True):
                asyncio.run(service.append_reader_document("/tmp/x.db", 3, {"text": "m", "replace": value}))
                self.assertTrue(seen["replace"], value)

    def test_unparseable_replace_is_rejected(self):
        with self.assertRaises(service.ReaderIngestError):
            asyncio.run(service.append_reader_document("/tmp/x.db", 3, {"text": "m", "replace": "maybe"}))

    def test_non_object_body_is_rejected(self):
        with self.assertRaises(service.ReaderIngestError):
            asyncio.run(service.append_reader_document("/tmp/x.db", 3, ["text"]))


class AppendLocalToolTests(unittest.TestCase):
    def test_requires_session(self):
        result = asyncio.run(local_tool_reader.append_to_reader_document({"document_id": 1, "text": "x"}))
        self.assertEqual(result, "Error: no database connection available.")

    def test_requires_db_path_not_just_conn(self):
        """The service opens its own connection from db_path; a session with a
        conn but no db_path would fail opaquely inside connect_db."""
        session = SimpleNamespace(conn=object(), db_path=None)
        result = asyncio.run(
            local_tool_reader.append_to_reader_document({"document_id": 1, "text": "x"}, session=session)
        )
        self.assertEqual(result, "Error: no database connection available.")

    def test_requires_integer_document_id(self):
        session = SimpleNamespace(conn=object(), db_path="/tmp/t.db")
        result = asyncio.run(local_tool_reader.append_to_reader_document({"text": "x"}, session=session))
        self.assertTrue(result.startswith("Error: document_id"))

    def test_requires_text_or_path(self):
        session = SimpleNamespace(conn=object(), db_path="/tmp/t.db")
        result = asyncio.run(local_tool_reader.append_to_reader_document({"document_id": 4}, session=session))
        self.assertTrue(result.startswith("Error: provide either text"))

    def test_delegates_to_service_and_reports(self):
        session = SimpleNamespace(conn=object(), db_path="/tmp/t.db")
        seen = {}

        async def fake_append(db_path, doc_id, body):
            seen.update(db_path=db_path, doc_id=doc_id, body=body)
            return {"id": doc_id, "status": "processing", "title": "Article", "replace": False}

        with patch.object(local_tool_reader, "append_reader_document", side_effect=fake_append):
            result = asyncio.run(
                local_tool_reader.append_to_reader_document(
                    {"document_id": "12", "text": "the rest"}, session=session
                )
            )
        self.assertEqual(seen["doc_id"], 12)
        self.assertEqual(seen["body"]["text"], "the rest")
        self.assertIn("document #12", result)
        self.assertIn("extended", result)
        self.assertIn("position is kept", result)

    def test_replace_wording(self):
        session = SimpleNamespace(conn=object(), db_path="/tmp/t.db")

        async def fake_append(db_path, doc_id, body):
            return {"id": doc_id, "status": "processing", "title": "Article", "replace": True}

        with patch.object(local_tool_reader, "append_reader_document", side_effect=fake_append):
            result = asyncio.run(
                local_tool_reader.append_to_reader_document(
                    {"document_id": 1, "text": "x", "replace": True}, session=session
                )
            )
        self.assertIn("rebuilt", result)

    def test_service_errors_are_returned_as_text(self):
        session = SimpleNamespace(conn=object(), db_path="/tmp/t.db")

        async def fake_append(db_path, doc_id, body):
            raise service.ReaderIngestError("Document is still processing", status_code=409)

        with patch.object(local_tool_reader, "append_reader_document", side_effect=fake_append):
            result = asyncio.run(
                local_tool_reader.append_to_reader_document({"document_id": 1, "text": "x"}, session=session)
            )
        self.assertEqual(result, "Error: Document is still processing")


@unittest.skipIf(TestClient is None, "fastapi dependency not installed")
class AppendRouteTests(unittest.TestCase):
    def _app(self):
        app = FastAPI()
        app.include_router(reader_api.router)
        app.state.db_path = Path("/tmp/route-test.db")
        return app

    def test_append_route_returns_service_result(self):
        seen = {}

        async def fake_append(db_path, doc_id, body):
            seen.update(doc_id=doc_id, body=body)
            return {"id": doc_id, "status": "processing", "title": "T", "replace": False}

        with patch.object(reader_api, "append_reader_document", side_effect=fake_append):
            with TestClient(self._app()) as client:
                resp = client.post("/api/reader/documents/8/append", json={"text": "more"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], 8)
        self.assertEqual(seen, {"doc_id": 8, "body": {"text": "more"}})

    def test_append_route_rejects_non_object_body(self):
        with TestClient(self._app()) as client:
            resp = client.post("/api/reader/documents/8/append", json=["more"])
        self.assertEqual(resp.status_code, 400)
        with TestClient(self._app()) as client:
            resp = client.post("/api/reader/documents/8/append", content="null",
                               headers={"content-type": "application/json"})
        self.assertEqual(resp.status_code, 400)
        with TestClient(self._app()) as client:
            resp = client.post("/api/reader/documents/8/append", content="not json",
                               headers={"content-type": "application/json"})
        self.assertEqual(resp.status_code, 400)

    def test_append_route_maps_errors_to_status(self):
        async def fake_append(db_path, doc_id, body):
            raise service.ReaderIngestError("Document not found", status_code=404)

        with patch.object(reader_api, "append_reader_document", side_effect=fake_append):
            with TestClient(self._app()) as client:
                resp = client.post("/api/reader/documents/8/append", json={"text": "more"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json(), {"error": "Document not found"})


if __name__ == "__main__":
    unittest.main()
