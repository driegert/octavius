import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reader_ingest_handlers as handlers


class _ReaderIngestError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class _FakeMCP:
    async def call_tool(self, name, arguments):
        return ""


class _FakeTrafilatura:
    def __init__(self, extract_result=None, metadata=None):
        self._extract_result = extract_result
        self._metadata = metadata

    def extract(self, *args, **kwargs):
        return self._extract_result

    def extract_metadata(self, *args, **kwargs):
        return self._metadata


class ReaderIngestHandlerTests(unittest.TestCase):
    def test_extract_article_text_returns_none_when_both_attempts_fail(self):
        fake = _FakeTrafilatura()
        with patch.object(handlers, "get_trafilatura", return_value=fake):
            self.assertIsNone(handlers.extract_article_text("<html></html>", _ReaderIngestError))

    def test_extract_article_text_drops_comment_and_newsletter_widgets(self):
        para = "The builder runs eight used data center cards in one office, on two ordinary circuits. " * 4
        html = f"""<html><body class="news-page has-comments"><article><h1>Lawyer builds a server</h1>
        <div id="article-body"><p>{para}</p>
        <div class="newsletter-form__wrapper"><p class="newsletter-form__strapline">Get Example's best news and in-depth reviews, straight to your inbox.</p></div>
        <p>{para.replace("builder", "owner")}</p>
        <p>Follow Example on Google News, or add us as a preferred source, to get our latest news in your feeds.</p>
        <p>One commenter traced the same problem to a bad cable, which the article itself reports.</p></div></article>
        <div class="xenforo-comment-widget"><ul class="xenforo-comments-list">
        <li class="xenforo-comment"><div class="xenforo-comment-content">marmarmar34 Reply Seems like a huge waste.</div></li>
        </ul></div></body></html>"""
        text = handlers.extract_article_text(html, _ReaderIngestError)
        self.assertIn("eight used data center cards", text)
        self.assertIn("The owner runs", text)
        self.assertIn("One commenter traced", text)  # article prose that mentions comments stays
        for gone in ("marmarmar34", "huge waste", "in-depth reviews", "Google News"):
            self.assertNotIn(gone, text)
        self.assertGreater(text.count("\n\n"), 1)  # paragraphs, not one block

    def test_prune_keeps_container_holding_the_article(self):
        tree = handlers._prune_page_furniture(
            '<html><body><div class="comments-enabled"><article><h1>T</h1><p>Body text.</p></article></div></body></html>'
        )
        self.assertEqual(len(tree.xpath("//article")), 1)

    def test_extract_article_text_prefers_the_dominant_article_over_teaser_cards(self):
        # The Register, 2026-11-10: a dozen teaser <article> cards beside the story,
        # and a "READ MORE" link list inside it; trafilatura read only "Top stories".
        def card(i):
            return (f'<article class="column"><a href="/s{i}"><h4>Teaser headline number {i} about chips</h4></a>'
                    f'<p>Standfirst for teaser {i}, a line or two of bait.</p></article>')
        para = "Researchers had one model rewrite another model's weights directly, without training. " * 12
        html = f"""<html><head><title>Story</title></head><body><main>
        <section class="top"><h3>TOP STORIES</h3>{''.join(card(i) for i in range(8))}</section>
        <article><h1>Forget retraining</h1><div class="bodytext">
        <div class="toplist"><div class="content"><h3>READ MORE</h3><ul>
        <li><a href="/x"><h4>Stanford study admits it is out of date</h4></a></li>
        <li><a href="/y"><h4>Agents reveal secrets to each other</h4></a></li></ul></div></div>
        <p>{para}</p><p>{para.replace("Researchers", "The authors")}</p>
        <article class="inline-promo"><a href="/z">Nested promo card</a></article></div></article>
        <section class="related">{''.join(card(i) for i in range(8, 12))}</section></main></body></html>"""
        text = handlers.extract_article_text(html, _ReaderIngestError)
        self.assertIn("rewrite another model's weights", text)
        self.assertIn("The authors had one model", text)
        for gone in ("TOP STORIES", "Teaser headline", "READ MORE", "Stanford study", "Nested promo"):
            self.assertNotIn(gone, text)

    def test_scope_leaves_pages_without_a_dominant_article_alone(self):
        import lxml.html
        body = "Words of a perfectly ordinary paragraph. " * 60
        two = lxml.html.fromstring(f"<html><body><article><p>{body}</p></article><article><p>{body}</p></article></body></html>")
        self.assertIs(handlers._scope_to_main_article(two), two)
        short = lxml.html.fromstring("<html><body><article><p>Short.</p></article><article><p>x</p></article></body></html>")
        self.assertIs(handlers._scope_to_main_article(short), short)

    def test_link_list_heading_over_prose_is_kept(self):
        tree = handlers._prune_page_furniture(
            '<html><body><article><h1>T</h1><div><h3>Related reading</h3>'
            '<p>This section explains, at length and in plain prose, which earlier work matters and why.</p>'
            '<a href="/a">one</a></div></article></body></html>'
        )
        self.assertIn("earlier work matters", tree.text_content())

    def test_repair_markdown_tables_rejoins_one_cell_per_line(self):
        broken = "See the table.\n\nPrompt length |\nWait |\n|---|---|\n1K tokens |\n1.2 seconds |\n32K tokens |\n17.2 seconds |\n\nAfter."
        fixed = handlers._repair_markdown_tables(broken)
        self.assertIn("| Prompt length | Wait |\n|---|---|\n| 1K tokens | 1.2 seconds |\n| 32K tokens | 17.2 seconds |", fixed)
        self.assertTrue(fixed.startswith("See the table.") and fixed.endswith("After."))
        good = "| a | b |\n|---|---|\n| 1 | 2 |"
        self.assertEqual(handlers._repair_markdown_tables(good), good)
        ragged = "a |\nb |\n|---|---|\n1 |"  # cells do not fill the last row: left alone
        self.assertEqual(handlers._repair_markdown_tables(ragged), ragged)

    def test_drop_promo_paragraphs_leaves_prose(self):
        text = "Get our newsletter in your inbox.\n\nGet the cards seated properly, the builder says, or they drop to x4.\n\nSubscribe to Example's newsletter."
        self.assertEqual(
            handlers._drop_promo_paragraphs(text),
            "Get the cards seated properly, the builder says, or they drop to x4.",
        )

    def test_refine_title_from_web_page_uses_title_suffix_when_generic(self):
        fake = _FakeTrafilatura(metadata=None)
        with patch.object(handlers, "get_trafilatura", return_value=fake):
            title = handlers.refine_title_from_web_page(
                "<title>Paper Title - arXiv</title>",
                "Untitled",
                "https://arxiv.org/abs/1234.5678",
                _ReaderIngestError,
            )
        self.assertEqual(title, "Paper Title (arXiv)")

    def test_resolve_markdown_output_falls_back_to_only_md_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            actual = out_dir / "2604.md"
            actual.write_text("content")
            resolved = handlers.resolve_markdown_output(out_dir / "2604.02238_1.md")
            self.assertEqual(resolved, actual)

    def test_start_retry_task_rejects_missing_markdown_retry_source(self):
        doc = {
            "id": 9,
            "title": "Missing",
            "source_type": "markdown",
            "source_path": None,
            "original_md_file": None,
        }
        with self.assertRaises(_ReaderIngestError) as ctx:
            handlers.start_retry_task(Path("/tmp/fake.db"), _FakeMCP(), doc, _ReaderIngestError)
        self.assertEqual(ctx.exception.status_code, 400)

    def _run_text_ingest(self, text, title=None, *, save_side_effect=None):
        """Run start_text_ingest with persistence stubbed; returns (result, tasks, saved)."""
        created_tasks = []
        saved = {}

        def fake_create_task(coro):
            created_tasks.append(coro)
            coro.close()
            return None

        def fake_save(doc_id, body, doc_title):
            if save_side_effect:
                raise save_side_effect
            saved.update(doc_id=doc_id, text=body, title=doc_title)
            return Path(f"/reader/pasted/{doc_id}-slug.md")

        with (
            patch.object(handlers, "create_document", return_value=42),
            patch.object(handlers, "save_pasted_text", side_effect=fake_save),
            patch.object(handlers, "update_document") as update_doc,
            patch.object(handlers, "connect_db"),
            patch.object(handlers.asyncio, "create_task", side_effect=fake_create_task),
        ):
            result = asyncio.run(handlers.start_text_ingest(Path("/tmp/test.db"), text, title))
        return result, created_tasks, saved, update_doc

    def test_start_text_ingest_creates_background_task(self):
        result, tasks, _, _ = self._run_text_ingest("hello", "Hello")
        self.assertEqual(result, {"id": 42, "status": "processing", "title": "Hello"})
        self.assertEqual(len(tasks), 1)

    def test_start_text_ingest_derives_title_when_omitted(self):
        result, _, saved, _ = self._run_text_ingest("# Multitaper Notes\n\nbody text", None)
        self.assertEqual(result["title"], "Multitaper Notes")
        self.assertEqual(saved["title"], "Multitaper Notes")

    def test_start_text_ingest_ignores_blank_title(self):
        result, _, _, _ = self._run_text_ingest("first line here", "   ")
        self.assertEqual(result["title"], "first line here")

    def test_start_text_ingest_persists_source_path_for_retry(self):
        """Pasted text has no file behind it, so it is written out and recorded
        as source_path — otherwise start_retry_task has nothing to re-read."""
        _, _, saved, update_doc = self._run_text_ingest("body", "Title")
        self.assertEqual(saved["text"], "body")
        self.assertEqual(saved["doc_id"], 42)
        update_doc.assert_called_once()
        self.assertEqual(
            update_doc.call_args.kwargs["source_path"], "/reader/pasted/42-slug.md"
        )

    def test_start_text_ingest_survives_persistence_failure(self):
        """A failed write costs retryability, never the document itself."""
        result, tasks, _, update_doc = self._run_text_ingest(
            "body", "Title", save_side_effect=OSError("read-only fs")
        )
        self.assertEqual(result["id"], 42)
        self.assertEqual(len(tasks), 1)
        update_doc.assert_not_called()

    def test_pasted_text_document_is_retryable_through_markdown_branch(self):
        """The persisted file makes retry work with no new retry code."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "42-notes.md"
            source.write_text("# Notes\n\nbody", encoding="utf-8")
            doc = {
                "id": 42,
                "title": "Notes",
                "source_type": "markdown",
                "source_path": str(source),
                "original_md_file": None,
            }
            created = []
            with patch.object(
                handlers.asyncio, "create_task",
                side_effect=lambda coro: (created.append(coro), coro.close(), None)[2],
            ):
                handlers.start_retry_task(Path("/tmp/fake.db"), _FakeMCP(), doc, _ReaderIngestError)
            self.assertEqual(len(created), 1)

    def test_ingest_pdf_document_marks_failed_when_no_job_id_returned(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """CREATE TABLE reader_documents (
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
                    total_sentences INTEGER,
                    progress_sentences INTEGER,
                    temporary INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT
                )"""
            )
            conn.execute(
                """INSERT INTO reader_documents
                   (id, title, source_type, status, created_at)
                   VALUES (1, 'Paper', 'pdf', 'processing', 'now')"""
            )
            conn.commit()
            conn.close()

            class _MCP:
                async def call_tool(self, name, arguments):
                    return "conversion backend unavailable"

            asyncio.run(handlers.ingest_pdf_document(db_path, _MCP(), 1, "/tmp/paper.pdf", "Paper"))

            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT status, error FROM reader_documents WHERE id = 1").fetchone()
            conn.close()
            self.assertEqual(row[0], "failed")
            self.assertIn("PDF conversion failed", row[1])

    def test_ingest_pdf_document_ingests_markdown_when_poll_succeeds(self):
        class _MCP:
            def __init__(self):
                self.calls = 0

            async def call_tool(self, name, arguments):
                self.calls += 1
                if name == "convert_pdf_to_md":
                    return "Job ID: abc123"
                return "Done: /tmp/output.md"

        ingested = []

        async def fake_ingest_document_task(db_path, doc_id, markdown, title, original_md_path=None):
            ingested.append(
                {
                    "db_path": Path(db_path),
                    "doc_id": doc_id,
                    "markdown": markdown,
                    "title": title,
                    "original_md_path": original_md_path,
                }
            )

        with (
            patch.object(handlers.asyncio, "sleep", return_value=None),
            patch.object(handlers, "resolve_markdown_output", return_value=Path("/tmp/output.md")),
            patch.object(handlers, "read_text_file", return_value="# Title\n\ncontent"),
            patch.object(handlers, "ingest_document_task", side_effect=fake_ingest_document_task),
        ):
            asyncio.run(handlers.ingest_pdf_document(Path("/tmp/test.db"), _MCP(), 7, "/tmp/paper.pdf", "Paper"))

        self.assertEqual(len(ingested), 1)
        self.assertEqual(ingested[0]["doc_id"], 7)
        self.assertEqual(ingested[0]["markdown"], "# Title\n\ncontent")
        self.assertEqual(ingested[0]["original_md_path"], "/tmp/output.md")
