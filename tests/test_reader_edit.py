"""Reader "rename" feature — PATCH /api/reader/documents/{id}.

Renaming a document updates the DB row's title and, if a speech JSON exists,
rewrites its stored title too. It is allowed in any status; while `processing`
the JSON is left to the running job (which re-reads the row's title before it
writes), and a missing/unreadable speech file is not an error.
"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
                source_type="url", source_path="https://x/a"):
    cur = conn.execute(
        """INSERT INTO reader_documents
           (title, source_type, source_path, speech_file, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'now')""",
        (title, source_type, source_path, speech_file, status),
    )
    conn.commit()
    return cur.lastrowid


def _fake_settings(directory: str):
    return SimpleNamespace(reader=SimpleNamespace(directory=directory))


class RenameDocumentTests(unittest.TestCase):
    """reader_text.rename_document — the DB + speech-JSON title update."""

    def test_rename_updates_db_and_speech_json_title(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)):
            conn = sqlite3.connect(":memory:")
            conn.execute(READER_DOCUMENTS_DDL)
            speech_path = Path(tmp) / "1.json"
            speech_path.write_text(json.dumps({
                "title": "Old Title", "total_sentences": 1,
                "chunks": [{"index": 0, "heading": None, "speech_text": "One.", "sentences": ["One."]}],
            }))
            doc_id = _insert_doc(conn, title="Old Title", speech_file=str(speech_path))

            reader_text.rename_document(conn, doc_id, "New Title")

            row = conn.execute("SELECT title FROM reader_documents WHERE id = ?", (doc_id,)).fetchone()
            self.assertEqual(row[0], "New Title")
            speech = json.loads(speech_path.read_text())
            self.assertEqual(speech["title"], "New Title")
            # Nothing else in the speech JSON was disturbed.
            self.assertEqual(speech["chunks"][0]["sentences"], ["One."])

    def test_rename_with_no_speech_file_succeeds(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(READER_DOCUMENTS_DDL)
        doc_id = _insert_doc(conn, title="Old", speech_file=None, status="processing")

        reader_text.rename_document(conn, doc_id, "New")

        row = conn.execute("SELECT title FROM reader_documents WHERE id = ?", (doc_id,)).fetchone()
        self.assertEqual(row[0], "New")

    def test_rename_with_missing_speech_file_leaves_db_title_updated(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)):
            conn = sqlite3.connect(":memory:")
            conn.execute(READER_DOCUMENTS_DDL)
            missing_path = Path(tmp) / "does-not-exist.json"
            doc_id = _insert_doc(conn, title="Old", speech_file=str(missing_path))

            # Should not raise even though the speech file is gone.
            reader_text.rename_document(conn, doc_id, "New")

            row = conn.execute("SELECT title FROM reader_documents WHERE id = ?", (doc_id,)).fetchone()
            self.assertEqual(row[0], "New")
            self.assertFalse(missing_path.exists())

    def test_rename_writes_speech_json_atomically(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)):
            conn = sqlite3.connect(":memory:")
            conn.execute(READER_DOCUMENTS_DDL)
            speech_path = Path(tmp) / "1.json"
            existing = {"title": "Old", "total_sentences": 0, "chunks": []}
            speech_path.write_text(json.dumps(existing))
            doc_id = _insert_doc(conn, title="Old", speech_file=str(speech_path))

            with patch.object(reader_text.os, "replace", side_effect=OSError("rename failed")):
                with self.assertRaises(OSError):
                    reader_text.rename_document(conn, doc_id, "New")

            # The speech file on disk is untouched by the failed write...
            self.assertEqual(json.loads(speech_path.read_text()), existing)
            self.assertEqual([p.name for p in Path(tmp).glob("*.tmp")], [])
            # ...but the DB write happens first and is not rolled back by this helper.
            row = conn.execute("SELECT title FROM reader_documents WHERE id = ?", (doc_id,)).fetchone()
            self.assertEqual(row[0], "New")


    def test_rename_while_processing_leaves_speech_json_to_the_running_job(self):
        """An ingest/append job is about to replace the speech file; rewriting it
        here could race that write and clobber freshly converted chunks."""
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)):
            conn = sqlite3.connect(":memory:")
            conn.execute(READER_DOCUMENTS_DDL)
            speech_path = Path(tmp) / "1.json"
            existing = {"title": "Old", "total_sentences": 0, "chunks": []}
            speech_path.write_text(json.dumps(existing))
            doc_id = _insert_doc(conn, title="Old", speech_file=str(speech_path), status="processing")

            reader_text.rename_document(conn, doc_id, "New")

            row = conn.execute("SELECT title FROM reader_documents WHERE id = ?", (doc_id,)).fetchone()
            self.assertEqual(row[0], "New")
            self.assertEqual(json.loads(speech_path.read_text()), existing)

    def test_write_speech_uses_the_row_title_not_the_one_captured_at_job_start(self):
        """The other half of the race: a rename that lands while a job is converting
        must survive the job's final write."""
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_text, "READER_PATH", Path(tmp)):
            conn = sqlite3.connect(":memory:")
            conn.execute(READER_DOCUMENTS_DDL)
            doc_id = _insert_doc(conn, title="Captured At Start", status="processing")
            reader_store.update_document(conn, doc_id, title="Renamed Mid-Job")

            chunks = [{"index": 0, "heading": None, "speech_text": "One.", "sentences": ["One."]}]
            reader_text._write_speech(conn, doc_id, "Captured At Start", chunks)

            speech = json.loads((Path(tmp) / f"{doc_id}.json").read_text())
            self.assertEqual(speech["title"], "Renamed Mid-Job")
            row = conn.execute("SELECT title, status FROM reader_documents WHERE id = ?", (doc_id,)).fetchone()
            self.assertEqual(tuple(row), ("Renamed Mid-Job", "ready"))

    def test_speech_json_round_trips_non_ascii_titles(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(reader_store, "settings", _fake_settings(tmp)):
            conn = sqlite3.connect(":memory:")
            conn.execute(READER_DOCUMENTS_DDL)
            speech_path = Path(tmp) / "1.json"
            speech_path.write_text(json.dumps({"title": "Old", "total_sentences": 0, "chunks": []}), encoding="utf-8")
            doc_id = _insert_doc(conn, title="Old", speech_file=str(speech_path))

            reader_text.rename_document(conn, doc_id, "Thomson \u2014 multitaper \U0001f4ca")

            speech = reader_store.load_speech_data({"speech_file": str(speech_path)})
            self.assertEqual(speech["title"], "Thomson \u2014 multitaper \U0001f4ca")


class RenameReaderDocumentServiceTests(unittest.TestCase):
    """reader_ingest_service.rename_reader_document — validation + wiring."""

    def test_renames_and_returns_id_and_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn, title="Old")

            result = asyncio.run(service.rename_reader_document(db_path, doc_id, {"title": "  New Title  "}))

            self.assertEqual(result, {"id": doc_id, "title": "New Title"})
            with sqlite3.connect(db_path) as conn:
                row = conn.execute("SELECT title FROM reader_documents WHERE id = ?", (doc_id,)).fetchone()
            self.assertEqual(row[0], "New Title")

    def test_processing_document_can_be_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn, title="Old", status="processing")

            result = asyncio.run(service.rename_reader_document(db_path, doc_id, {"title": "New"}))
            self.assertEqual(result["title"], "New")

    def test_missing_document_is_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with self.assertRaises(service.ReaderIngestError) as ctx:
                asyncio.run(service.rename_reader_document(db_path, 42, {"title": "New"}))
            self.assertEqual(ctx.exception.status_code, 404)

    def test_blank_title_is_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn)
            with self.assertRaises(service.ReaderIngestError) as ctx:
                asyncio.run(service.rename_reader_document(db_path, doc_id, {"title": "   "}))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("empty", ctx.exception.message)

    def test_too_long_title_is_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn)
            with self.assertRaises(service.ReaderIngestError) as ctx:
                asyncio.run(service.rename_reader_document(db_path, doc_id, {"title": "x" * 201}))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("too long", ctx.exception.message)

    def test_non_string_title_is_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn)
            with self.assertRaises(service.ReaderIngestError) as ctx:
                asyncio.run(service.rename_reader_document(db_path, doc_id, {"title": 123}))
            self.assertEqual(ctx.exception.status_code, 400)

    def test_missing_title_is_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn)
            with self.assertRaises(service.ReaderIngestError) as ctx:
                asyncio.run(service.rename_reader_document(db_path, doc_id, {}))
            self.assertEqual(ctx.exception.status_code, 400)

    def test_non_object_body_is_400(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn)
            with self.assertRaises(service.ReaderIngestError):
                asyncio.run(service.rename_reader_document(db_path, doc_id, ["New"]))

    def test_title_exactly_200_chars_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "h.db"
            _make_db(db_path)
            with sqlite3.connect(db_path) as conn:
                doc_id = _insert_doc(conn)
            title = "x" * 200
            result = asyncio.run(service.rename_reader_document(db_path, doc_id, {"title": title}))
            self.assertEqual(result["title"], title)


@unittest.skipIf(TestClient is None, "fastapi dependency not installed")
class RenameRouteTests(unittest.TestCase):
    def _app(self):
        app = FastAPI()
        app.include_router(reader_api.router)
        app.state.db_path = Path("/tmp/route-test.db")
        return app

    def test_rename_route_returns_service_result(self):
        seen = {}

        async def fake_rename(db_path, doc_id, body):
            seen.update(doc_id=doc_id, body=body)
            return {"id": doc_id, "title": "New Title"}

        with patch.object(reader_api, "rename_reader_document", side_effect=fake_rename):
            with TestClient(self._app()) as client:
                resp = client.patch("/api/reader/documents/8", json={"title": "New Title"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"id": 8, "title": "New Title"})
        self.assertEqual(seen, {"doc_id": 8, "body": {"title": "New Title"}})

    def test_rename_route_rejects_non_object_body(self):
        with TestClient(self._app()) as client:
            resp = client.patch("/api/reader/documents/8", json=["New Title"])
        self.assertEqual(resp.status_code, 400)
        with TestClient(self._app()) as client:
            resp = client.patch("/api/reader/documents/8", content="null",
                                 headers={"content-type": "application/json"})
        self.assertEqual(resp.status_code, 400)
        with TestClient(self._app()) as client:
            resp = client.patch("/api/reader/documents/8", content="not json",
                                 headers={"content-type": "application/json"})
        self.assertEqual(resp.status_code, 400)

    def test_rename_route_maps_errors_to_status(self):
        async def fake_rename(db_path, doc_id, body):
            raise service.ReaderIngestError("Document not found", status_code=404)

        with patch.object(reader_api, "rename_reader_document", side_effect=fake_rename):
            with TestClient(self._app()) as client:
                resp = client.patch("/api/reader/documents/8", json={"title": "New"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json(), {"error": "Document not found"})

    def test_rename_route_maps_400_from_service(self):
        async def fake_rename(db_path, doc_id, body):
            raise service.ReaderIngestError("Title is empty")

        with patch.object(reader_api, "rename_reader_document", side_effect=fake_rename):
            with TestClient(self._app()) as client:
                resp = client.patch("/api/reader/documents/8", json={"title": "   "})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "Title is empty"})


if __name__ == "__main__":
    unittest.main()
