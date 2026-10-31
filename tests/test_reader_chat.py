"""The Reader chat panel's server side: document look-up tools, the per-turn place
context, and `doc_chat_open` / `doc_chat_position` (one keyed chat per document)."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import history
import local_tool_reader_lookup as lookup
from pi_brain import build_prompt
from reader_store import create_document, update_document
from websocket_session import WebSocketSessionHandler

CHUNKS = [
    {"index": 0, "heading": "Introduction", "level": 1, "speech_text": "Tapers reduce leakage.",
     "sentences": ["Tapers reduce leakage."], "source_text": "Tapers reduce leakage (Thomson, 1982)."},
    {"index": 1, "heading": None, "speech_text": "The estimate averages K eigenspectra. It is consistent.",
     "sentences": ["The estimate averages K eigenspectra.", "It is consistent."]},
    {"index": 2, "heading": "Methods", "level": 2, "speech_text": "We use Slepian sequences.",
     "sentences": ["We use Slepian sequences."], "source_text": "We use Slepian sequences $v_t^{(k)}$."},
]


class _Fixture:
    def __init__(self, test: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = self.dir / "h.db"
        history.init_db(self.db).close()
        self.recorder = history.HistoryRecorder(self.db)
        self.conn = self.recorder.connect()
        test.addCleanup(self.conn.close)

    def doc(self, title="Multitaper notes", status="ready", chunks=CHUNKS) -> int:
        doc_id = create_document(self.conn, title, "markdown")
        speech = self.dir / f"{doc_id}.json"
        speech.write_text(json.dumps({"title": title, "chunks": chunks}))
        update_document(self.conn, doc_id, status=status, speech_file=str(speech))
        return doc_id

    def session(self):
        return SimpleNamespace(conn=self.conn)


class LookupToolTests(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture(self)
        self.doc_id = self.fx.doc()

    def test_outline_lists_sections_with_their_chunk(self):
        out = lookup.reader_document_outline({"document_id": self.doc_id}, self.fx.session())
        self.assertIn("3 chunks, 4 sentences", out)
        self.assertIn("[0] Introduction", out)
        self.assertIn("  [2] Methods", out)  # level 2 is indented

    def test_read_prefers_source_text_and_carries_the_section(self):
        out = lookup.read_reader_document({"document_id": self.doc_id, "start_chunk": 0, "end_chunk": 1}, self.fx.session())
        self.assertIn("(Thomson, 1982)", out)  # citation kept in source_text
        self.assertIn("[chunk 1] (section: Introduction)", out)  # heading carries forward
        self.assertIn("averages K eigenspectra", out)  # no source_text: speech text
        self.assertIn("continues at chunk 2", out)

    def test_read_rejects_out_of_range(self):
        out = lookup.read_reader_document({"document_id": self.doc_id, "start_chunk": 9}, self.fx.session())
        self.assertTrue(out.startswith("Error"))

    def test_search_ranks_by_distinct_terms(self):
        out = lookup.search_reader_document({"document_id": self.doc_id, "query": "Slepian sequences"}, self.fx.session())
        self.assertIn("[chunk 2]", out)
        self.assertIn("2/2 terms", out)
        self.assertIn("No chunk", lookup.search_reader_document({"document_id": self.doc_id, "query": "wavelets"}, self.fx.session()))

    def test_unready_or_missing_document_is_an_error(self):
        processing = self.fx.doc(status="processing")
        for args in ({"document_id": processing}, {"document_id": 999}, {"document_id": "x"}):
            self.assertTrue(lookup.reader_document_outline(args, self.fx.session()).startswith("Error"), args)

    def test_place_context_names_the_document_and_the_sentence(self):
        ctx = lookup.place_context(self.fx.conn, self.doc_id, {"chunk_index": 1, "sentence_index": 1})
        self.assertIn(f"document #{self.doc_id}", ctx)
        self.assertIn("Multitaper notes", ctx)
        self.assertIn('section "Introduction"', ctx)
        self.assertIn("The estimate averages K eigenspectra. It is consistent.", ctx)
        # Without a place it still names the document; a bad place adds nothing.
        self.assertNotIn("He is at", lookup.place_context(self.fx.conn, self.doc_id, None))
        self.assertNotIn("He is at", lookup.place_context(self.fx.conn, self.doc_id, {"chunk_index": 7, "sentence_index": 0}))

    def test_place_context_falls_back_to_the_saved_place(self):
        # Paused: the browser sends no place, but pausing saved it.
        update_document(self.fx.conn, self.doc_id, last_chunk=2, last_sentence=0)
        self.assertIn("We use Slepian sequences.", lookup.place_context(self.fx.conn, self.doc_id, None))
        self.assertIn("chunk 1, sentence 0", lookup.place_context(self.fx.conn, self.doc_id, {"chunk_index": 1, "sentence_index": 0}))

    def test_tools_are_registered_and_served_to_pi(self):
        import pi_tools_server
        import tools
        for name in ("reader_document_outline", "read_reader_document", "search_reader_document"):
            self.assertIn(name, tools.get_local_tool_handlers())
            self.assertIn(name, pi_tools_server.EXPOSED_TOOLS)


class BuildPromptTests(unittest.TestCase):
    def test_context_rides_in_the_user_message(self):
        out = build_prompt("what does this mean?", "STYLE", context="DOC CONTEXT")
        self.assertEqual(out, "STYLE\n\nDOC CONTEXT\n\nDave's message:\nwhat does this mean?")
        self.assertEqual(build_prompt("hi", "STYLE"), "STYLE\n\nhi")


class _WS:
    def __init__(self, recorder):
        self.sent = []
        self.app = SimpleNamespace(state=SimpleNamespace(history=recorder, mcp_manager=object(), subagent_dispatcher=object()))

    async def send_text(self, text):
        self.sent.append(json.loads(text))


async def _no_summary(_messages):
    return SimpleNamespace(summary=None, index=False)


async def _no_tags(_messages):
    return []


@patch("history.generate_summary_async", _no_summary)
@patch("history.generate_tags_async", _no_tags)
class DocChatOpenTests(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture(self)
        self.doc_id = self.fx.doc()

    def _handler(self):
        handler = WebSocketSessionHandler(_WS(self.fx.recorder))
        handler.state.history_session = self.fx.recorder.start_conversation()
        return handler

    def _last(self, handler, type_):
        return [m for m in handler.ws.sent if m.get("type") == type_][-1]

    def test_one_chat_per_document_resumes_and_new_starts_fresh(self):
        async def run():
            h = self._handler()
            await h.handle_doc_chat_open({"doc_id": self.doc_id})
            first = self._last(h, "doc_chat_opened")
            self.assertEqual(first["title"], "Multitaper notes")
            self.assertEqual(first["messages"], [])
            self.assertEqual(h.state.doc_chat_id, self.doc_id)
            h.state.history_session.add_message("user", "What is leakage?")
            h.state.history_session.add_message("assistant", "Energy spilling between frequencies.")

            # Another connection (a page reload) resumes the same conversation.
            h2 = self._handler()
            await h2.handle_doc_chat_open({"doc_id": self.doc_id})
            again = self._last(h2, "doc_chat_opened")
            self.assertEqual(again["conversation_id"], first["conversation_id"])
            self.assertEqual([m["role"] for m in again["messages"]], ["user", "assistant"])
            self.assertEqual(len(h2.state.conversation.get_messages()), 3)  # system + 2

            # New chat: a different conversation; the old one is kept under a renamed key.
            await h2.handle_doc_chat_open({"doc_id": self.doc_id, "new": True})
            fresh = self._last(h2, "doc_chat_opened")
            self.assertNotEqual(fresh["conversation_id"], first["conversation_id"])
            self.assertEqual(fresh["messages"], [])
            key = self.fx.conn.execute("SELECT session_id FROM conversations WHERE id = ?", (first["conversation_id"],)).fetchone()[0]
            self.assertEqual(key, f"reader-doc:{self.doc_id}:{first['conversation_id']}")
            source = self.fx.conn.execute("SELECT source FROM conversations WHERE id = ?", (fresh["conversation_id"],)).fetchone()[0]
            self.assertEqual(source, "reader")

        asyncio.run(run())

    def test_missing_document_is_an_error_and_keeps_the_conversation(self):
        async def run():
            h = self._handler()
            before = h.state.history_session.conv_id
            for doc_id in (999, "abc"):
                await h.handle_doc_chat_open({"doc_id": doc_id})
                self.assertEqual(self._last(h, "doc_chat_error")["doc_id"], doc_id)
            self.assertEqual(h.state.history_session.conv_id, before)
            self.assertIsNone(h.state.doc_chat_id)

        asyncio.run(run())

    def test_position_is_kept_only_for_the_open_document(self):
        async def run():
            h = self._handler()
            await h.handle_doc_chat_position({"doc_id": self.doc_id, "chunk_index": 1, "sentence_index": 0})
            self.assertIsNone(h.state.doc_chat_position)  # no chat open yet
            await h.handle_doc_chat_open({"doc_id": self.doc_id})
            await h.handle_doc_chat_position({"doc_id": self.doc_id + 1, "chunk_index": 1, "sentence_index": 0})
            self.assertIsNone(h.state.doc_chat_position)
            await h.handle_doc_chat_position({"doc_id": self.doc_id, "chunk_index": 1, "sentence_index": 0})
            self.assertEqual(h.state.doc_chat_position, {"chunk_index": 1, "sentence_index": 0})
            await h.handle_reset({})
            self.assertIsNone(h.state.doc_chat_id)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
