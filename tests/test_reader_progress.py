"""Reading progress per document and heading levels for the reader's table of contents."""

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import history  # noqa: E402
import reader_playback  # noqa: E402
import reader_store  # noqa: E402
import reader_text  # noqa: E402
from tests.test_reader_append import READER_DOCUMENTS_DDL, _fake_settings, _identity_convert, _insert_doc  # noqa: E402

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import reader_api
except ImportError:  # pragma: no cover
    FastAPI = TestClient = reader_api = None

SPEECH = {
    "title": "Doc",
    "total_sentences": 5,
    "chunks": [
        {"index": 0, "heading": "Intro", "level": 1, "speech_text": "", "sentences": ["One.", "Two."]},
        {"index": 1, "heading": "Detail", "level": 2, "speech_text": "", "sentences": ["Three.", ""]},
        {"index": 2, "heading": None, "speech_text": "", "sentences": ["Five."]},
    ],
}


def _db_with_doc(tmp: str, **kw) -> tuple[Path, Path]:
    db_path = Path(tmp) / "r.db"
    speech_path = Path(tmp) / "1.json"
    speech_path.write_text(json.dumps(SPEECH))
    conn = sqlite3.connect(db_path)
    conn.execute(READER_DOCUMENTS_DDL)
    _insert_doc(conn, speech_file=str(speech_path), **kw)
    conn.close()
    return db_path, speech_path


def _row(db_path: Path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT last_chunk, last_sentence, total_sentences, progress_sentences FROM reader_documents WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()


class _WS:
    def __init__(self, stop_after_audio: int | None = None):
        self.frames, self.audio, self.stop_after_audio = [], 0, stop_after_audio

    async def send_text(self, text):
        self.frames.append(json.loads(text))

    async def send_bytes(self, _data):
        self.audio += 1
        if self.audio == self.stop_after_audio:
            raise asyncio.CancelledError


class HeadingLevelTests(unittest.TestCase):
    def test_split_into_chunks_records_heading_depth(self):
        md = "# Top\n\nA.\n\n## Sub\n\nB.\n\n### Deeper\n\nC."
        chunks = reader_text.split_into_chunks(md)
        self.assertEqual([(c["heading"], c["level"]) for c in chunks], [("Top", 1), ("Sub", 2), ("Deeper", 3)])

    def test_level_only_on_the_first_piece_of_a_split_section(self):
        md = "## Long\n\nA.\n\nB.\n\nC.\n\nD."
        raw = reader_text.split_into_chunks(md)
        speech = reader_text._build_speech_chunks(raw, [c["text"] for c in raw])
        self.assertEqual(speech[0]["level"], 2)
        self.assertTrue(all("level" not in c for c in speech[1:]))

    def test_text_before_any_heading_has_no_level(self):
        raw = reader_text.split_into_chunks("Preamble.\n\n# H\n\nBody.")
        speech = reader_text._build_speech_chunks(raw, [c["text"] for c in raw])
        self.assertNotIn("level", speech[0])
        self.assertEqual(speech[1]["level"], 1)


class ProgressStoreTests(unittest.TestCase):
    def test_sentences_before_counts_blanks_and_clamps(self):
        chunks = SPEECH["chunks"]
        self.assertEqual(reader_store.sentences_before(chunks, 0, 0), 0)
        self.assertEqual(reader_store.sentences_before(chunks, 1, 1), 3)
        self.assertEqual(reader_store.sentences_before(chunks, 2, 0), 4)
        self.assertEqual(reader_store.sentences_before(chunks, 9, 9), 5)

    def test_list_backfills_rows_from_before_the_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp, last_chunk=1, last_sentence=0)
            conn = sqlite3.connect(db_path)
            docs = reader_store.list_documents(conn)
            conn.close()
            self.assertEqual((docs[0]["total_sentences"], docs[0]["progress_sentences"]), (5, 2))
            self.assertEqual(_row(db_path)[2:], (5, 2))

    def test_list_leaves_unready_rows_null(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(READER_DOCUMENTS_DDL)
        _insert_doc(conn, status="processing")
        doc = reader_store.list_documents(conn)[0]
        self.assertIsNone(doc["total_sentences"])
        self.assertIsNone(doc["progress_sentences"])

    def test_migration_adds_columns_to_an_old_table(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, last_extracted_message_id INTEGER)")
        conn.execute(READER_DOCUMENTS_DDL.replace("    total_sentences INTEGER,\n    progress_sentences INTEGER,\n", ""))
        history._run_migrations(conn)
        history._run_migrations(conn)  # idempotent
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reader_documents)")}
        self.assertTrue({"total_sentences", "progress_sentences"} <= cols)


class ProgressOnAppendTests(unittest.TestCase):
    def _run(self, text, replace=False):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp, last_chunk=1, last_sentence=0)
            conn = sqlite3.connect(db_path)
            with patch.object(reader_text, "READER_PATH", Path(tmp)), \
                    patch.object(reader_store, "settings", _fake_settings(tmp)), \
                    patch.object(reader_text, "_convert_all_chunks", _identity_convert):
                asyncio.run(reader_text.append_to_document(conn, 1, text, replace=replace))
            conn.close()
            return _row(db_path)

    def test_append_keeps_progress_and_grows_total(self):
        self.assertEqual(self._run("## More\n\nSix. Seven."), (1, 0, 7, 2))

    def test_replace_resets_progress(self):
        self.assertEqual(self._run("Fresh. Start.", replace=True), (0, 0, 2, 0))


class ProgressDuringPlaybackTests(unittest.TestCase):
    async def _no_tts(self, *_a, **_k):
        return b"wav"

    def _play(self, ws, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp)
            with patch.object(reader_playback, "synthesize", self._no_tts):
                try:
                    asyncio.run(reader_playback.stream_reader_audio(ws, 1, db_path, **kw))
                except asyncio.CancelledError:
                    pass
            return _row(db_path)

    def test_reading_to_the_end_marks_finished_and_rewinds_place(self):
        ws = _WS()
        self.assertEqual(self._play(ws), (0, 0, None, 5))
        self.assertEqual(ws.frames[-1]["type"], "reader_audio_done")

    def test_stopping_keeps_the_sentence_being_read(self):
        # Cancelled while the third sentence's audio goes out: chunk 1, sentence 0.
        self.assertEqual(self._play(_WS(stop_after_audio=3)), (1, 0, None, 2))

    def test_starting_mid_document_counts_from_there(self):
        self.assertEqual(self._play(_WS(stop_after_audio=1), chunk_index=2), (2, 0, None, 4))


class DownloadStreamTests(unittest.TestCase):
    """save_place=False (reader_play download:true): the stream must not touch the place."""

    async def _no_tts(self, *_a, **_k):
        return b"wav"

    @staticmethod
    def _seed_progress(db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE reader_documents SET total_sentences = 5, progress_sentences = 2 WHERE id = 1")
        conn.commit()
        conn.close()

    def _play(self, ws):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp, last_chunk=1, last_sentence=0)
            self._seed_progress(db_path)
            before = _row(db_path)
            with patch.object(reader_playback, "synthesize", self._no_tts):
                try:
                    asyncio.run(reader_playback.stream_reader_audio(ws, 1, db_path, save_place=False))
                except asyncio.CancelledError:
                    pass
            return before, _row(db_path)

    def test_completed_download_leaves_place_and_progress_alone(self):
        ws = _WS()
        before, after = self._play(ws)
        self.assertEqual(before, (1, 0, 5, 2))
        self.assertEqual(after, before)
        self.assertEqual(ws.frames[-1]["type"], "reader_audio_done")
        self.assertEqual(ws.audio, 4)
        self.assertEqual(sum(f["type"] == "reader_position" for f in ws.frames), 4)

    def test_cancelled_download_leaves_place_and_progress_alone(self):
        before, after = self._play(_WS(stop_after_audio=3))
        self.assertEqual(after, before)

    def test_bare_pause_after_a_cancelled_download_saves_nothing(self):
        from types import SimpleNamespace
        from tests.test_websocket_session import _FakeWS
        from websocket_session import WebSocketSessionHandler

        class _BytesWS(_FakeWS):
            async def send_bytes(self, _data):
                await asyncio.sleep(10)  # stream stays mid-document until the pause cancels it

        async def run(db_path):
            handler = WebSocketSessionHandler(_BytesWS())
            handler.state.history = SimpleNamespace(db_path=db_path)
            with patch.object(reader_playback, "synthesize", self._no_tts):
                await handler.handle_reader_play({"doc_id": 1, "seq": 1, "download": True, "paced": True})
                self.assertIsNone(handler.state.reader_pacer)
                await asyncio.sleep(0.05)
                await handler.handle_reader_pause({})

        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp, last_chunk=1, last_sentence=0)
            self._seed_progress(db_path)
            before = _row(db_path)
            asyncio.run(run(db_path))
            self.assertEqual(_row(db_path), before)


class HeardPositionTests(unittest.TestCase):
    def test_client_place_replaces_the_streamed_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp, last_chunk=2, last_sentence=0)
            self.assertTrue(reader_playback.save_heard_position(db_path, 1, 0, 1))
            self.assertEqual(_row(db_path), (0, 1, None, 1))

    def test_clamped_to_the_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp)
            self.assertTrue(reader_playback.save_heard_position(db_path, 1, 9, 9))
            self.assertEqual(_row(db_path), (2, 0, None, 4))

    def test_malformed_or_unknown_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp, last_chunk=1, last_sentence=1)
            for args in ((1, "0", 1), (1, None, 0), (1, True, 0), (99, 0, 0), (None, 0, 0)):
                self.assertFalse(reader_playback.save_heard_position(db_path, *args), args)
            self.assertEqual(_row(db_path), (1, 1, None, None))


@unittest.skipIf(TestClient is None, "fastapi not installed")
class DocumentRouteLevelTests(unittest.TestCase):
    def test_sections_carry_level_only_when_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp, last_chunk=2)
            app = FastAPI()
            app.include_router(reader_api.router)
            app.state.db_path = db_path
            with TestClient(app) as client:
                doc = client.get("/api/reader/documents/1").json()["document"]
        self.assertEqual([s.get("level") for s in doc["sections"]], [1, 2, None])
        self.assertEqual(doc["progress_sentences"], 4)  # derived from the saved place when the row predates it
        self.assertEqual(doc["total_sentences"], 5)



@unittest.skipIf(TestClient is None, "fastapi not installed")
class MarkFinishedTests(unittest.TestCase):
    def _post(self, db_path, body, doc_id=1):
        app = FastAPI()
        app.include_router(reader_api.router)
        app.state.db_path = db_path
        with TestClient(app) as client:
            return client.post(f"/api/reader/documents/{doc_id}/finished", json=body)

    def test_finished_sets_full_progress_and_rewinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp, last_chunk=1, last_sentence=0)
            resp = self._post(db_path, {"finished": True})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"id": 1, "total_sentences": 5, "progress_sentences": 5})
            self.assertEqual(_row(db_path), (0, 0, 5, 5))
            docs = reader_store.list_documents(sqlite3.connect(db_path))
            self.assertEqual(docs[0]["progress_sentences"], docs[0]["total_sentences"])

    def test_unread_clears_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp, last_chunk=2)
            self._post(db_path, {"finished": True})
            resp = self._post(db_path, {"finished": False})
            self.assertEqual(resp.json()["progress_sentences"], 0)
            self.assertEqual(_row(db_path), (0, 0, 5, 0))

    def test_rejects_non_bool_and_non_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp, last_chunk=1)
            for body in ({"finished": "false"}, {"finished": 1}, {}, [True]):
                self.assertEqual(self._post(db_path, body).status_code, 400, body)
            self.assertEqual(_row(db_path), (1, 0, None, None))

    def test_missing_is_404_and_unready_is_409(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path, _ = _db_with_doc(tmp, status="processing")
            self.assertEqual(self._post(db_path, {"finished": True}, doc_id=9).status_code, 404)
            self.assertEqual(self._post(db_path, {"finished": True}).status_code, 409)

if __name__ == "__main__":
    unittest.main()
