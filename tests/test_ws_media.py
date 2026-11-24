"""Image and file media turns (ws_media), split out of test_websocket_session."""
import unittest
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import media_uploads
from settings import settings
from tests.test_websocket_session import _FakeHistorySession, _FakeWS, _settings_with_spool_dirs
from websocket_session import WebSocketSessionHandler, WebSocketDisconnect


class ImageInputTests(unittest.TestCase):
    def _make_handler(self):
        ws = _FakeWS()
        handler = WebSocketSessionHandler(ws)
        handler.state.history_session = _FakeHistorySession(conv_id=1)
        return handler, ws

    def test_missing_path_sends_status_and_does_not_spawn_turn(self):
        async def run():
            handler, ws = self._make_handler()
            handler._spawn_turn = unittest.mock.Mock()
            await handler.handle_image_input({"text": "", "path": "", "mime": "image/png", "filename": "a.png"})
            handler._spawn_turn.assert_not_called()
            statuses = [json.loads(t)["text"] for t in ws.sent if json.loads(t).get("type") == "status"]
            self.assertTrue(any("couldn't find" in s for s in statuses))
            self.assertEqual(statuses[-1], "audio_done")

        asyncio.run(run())

    def test_path_outside_allowlisted_roots_is_rejected(self):
        """A real, readable file whose path just isn't under any configured
        spool root must be rejected exactly like a missing one — the
        allowlist, not existence, is the gate."""
        async def run():
            handler, ws = self._make_handler()
            handler._spawn_turn = unittest.mock.Mock()
            with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as allowed:
                f = Path(outside) / "cat.png"
                f.write_bytes(b"\x89PNG\r\n\x1a\nfakepngbytes")
                with patch("ws_media.settings", _settings_with_spool_dirs(allowed)):
                    await handler.handle_image_input(
                        {"text": "", "path": str(f), "mime": "image/png", "filename": "cat.png"}
                    )
            handler._spawn_turn.assert_not_called()
            statuses = [json.loads(t)["text"] for t in ws.sent if json.loads(t).get("type") == "status"]
            self.assertTrue(any("couldn't find" in s for s in statuses))
            self.assertEqual(statuses[-1], "audio_done")

        asyncio.run(run())

    def test_symlink_escaping_allowlisted_root_is_rejected(self):
        """A symlink that lives inside the allowed root but points outside it
        must not be followed into an unauthorized read."""
        async def run():
            handler, ws = self._make_handler()
            handler._spawn_turn = unittest.mock.Mock()
            with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as allowed:
                secret = Path(outside) / "secret.png"
                secret.write_bytes(b"\x89PNG\r\n\x1a\nfakepngbytes")
                link = Path(allowed) / "link.png"
                link.symlink_to(secret)
                with patch("ws_media.settings", _settings_with_spool_dirs(allowed)):
                    await handler.handle_image_input(
                        {"text": "", "path": str(link), "mime": "image/png", "filename": "link.png"}
                    )
            handler._spawn_turn.assert_not_called()
            statuses = [json.loads(t)["text"] for t in ws.sent if json.loads(t).get("type") == "status"]
            self.assertTrue(any("couldn't find" in s for s in statuses))
            self.assertEqual(statuses[-1], "audio_done")

        asyncio.run(run())

    def test_oversize_image_rejected_on_actual_disk_size(self):
        """The frame's size_bytes is never trusted — the file's real size on
        disk is what gets checked against IMAGE_MAX_BYTES."""
        async def run():
            handler, ws = self._make_handler()
            handler._spawn_turn = unittest.mock.Mock()
            with tempfile.TemporaryDirectory() as allowed, patch.object(
                media_uploads, "IMAGE_MAX_BYTES", 10
            ), patch("ws_media.settings", _settings_with_spool_dirs(allowed)):
                f = Path(allowed) / "big.png"
                f.write_bytes(b"x" * 100)
                await handler.handle_image_input(
                    # Frame lies and says it's tiny; the handler must check the
                    # real file, not this.
                    {"text": "", "path": str(f), "mime": "image/png", "filename": "big.png", "size_bytes": 1}
                )
            handler._spawn_turn.assert_not_called()
            statuses = [json.loads(t)["text"] for t in ws.sent if json.loads(t).get("type") == "status"]
            self.assertTrue(any("too large" in s for s in statuses))
            self.assertEqual(statuses[-1], "audio_done")

        asyncio.run(run())

    def test_non_image_mime_sends_status_and_does_not_spawn_turn(self):
        async def run():
            handler, ws = self._make_handler()
            handler._spawn_turn = unittest.mock.Mock()
            with tempfile.NamedTemporaryFile(suffix=".bin") as f, patch(
                "ws_media.settings", _settings_with_spool_dirs(tempfile.gettempdir())
            ):
                f.write(b"not an image")
                f.flush()
                await handler.handle_image_input(
                    {"text": "", "path": f.name, "mime": "application/octet-stream", "filename": "a.bin"}
                )
            handler._spawn_turn.assert_not_called()
            statuses = [json.loads(t)["text"] for t in ws.sent if json.loads(t).get("type") == "status"]
            self.assertTrue(any("isn't an image" in s for s in statuses))
            # Every early-return branch must end the turn so clients gated on
            # audio_done (Android's Send button, the Matrix sidecar) don't hang.
            self.assertEqual(statuses[-1], "audio_done")

        asyncio.run(run())

    def test_valid_image_spawns_turn_with_vision_content_array(self):
        async def run():
            import base64
            handler, ws = self._make_handler()
            captured = {}

            def fake_spawn(user_text, source, user_content=None, attachment=None):
                captured["user_text"] = user_text
                captured["source"] = source
                captured["user_content"] = user_content
                captured["attachment"] = attachment

            handler._spawn_turn = fake_spawn
            with tempfile.NamedTemporaryFile(suffix=".png") as f, patch(
                "ws_media.settings", _settings_with_spool_dirs(tempfile.gettempdir())
            ):
                f.write(b"\x89PNG\r\n\x1a\nfakepngbytes")
                f.flush()
                await handler.handle_image_input(
                    {"text": "what is this", "path": f.name, "mime": "image/png", "filename": "cat.png", "size_bytes": 20}
                )
                expected_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfakepngbytes").decode("ascii")

            self.assertEqual(captured["source"], "image")
            self.assertEqual(captured["user_text"], "[image: cat.png] what is this")
            self.assertEqual(captured["user_content"][0], {"type": "text", "text": "what is this"})
            self.assertEqual(
                captured["user_content"][1],
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{expected_b64}"}},
            )
            self.assertEqual(captured["attachment"]["type"], "image")
            self.assertEqual(captured["attachment"]["title"], "cat.png")

            transcripts = [json.loads(t)["text"] for t in ws.sent if json.loads(t).get("type") == "transcript"]
            self.assertEqual(transcripts, ["[image: cat.png] what is this"])

        asyncio.run(run())

    def test_no_caption_uses_default_vision_text(self):
        async def run():
            handler, ws = self._make_handler()
            captured = {}
            handler._spawn_turn = lambda user_text, source, user_content=None, attachment=None: captured.update(
                user_text=user_text, user_content=user_content
            )
            with tempfile.NamedTemporaryFile(suffix=".jpg") as f, patch(
                "ws_media.settings", _settings_with_spool_dirs(tempfile.gettempdir())
            ):
                f.write(b"fakejpgbytes")
                f.flush()
                await handler.handle_image_input(
                    {"text": "", "path": f.name, "mime": "image/jpeg", "filename": "photo.jpg"}
                )
            self.assertEqual(captured["user_text"], "[image: photo.jpg]")
            self.assertEqual(
                captured["user_content"][0],
                {"type": "text", "text": "The user sent an image: photo.jpg"},
            )

        asyncio.run(run())


class FileInputTests(unittest.TestCase):
    def _make_handler(self):
        ws = _FakeWS()
        handler = WebSocketSessionHandler(ws)
        handler.state.history_session = _FakeHistorySession(conv_id=1)
        return handler, ws

    def test_missing_path_sends_status_only(self):
        async def run():
            handler, ws = self._make_handler()
            handler._spawn_turn = unittest.mock.Mock()
            await handler.handle_file_input({"text": "", "path": "", "mime": "application/pdf", "filename": "a.pdf"})
            handler._spawn_turn.assert_not_called()
            statuses = [json.loads(t)["text"] for t in ws.sent if json.loads(t).get("type") == "status"]
            self.assertTrue(any("couldn't find" in s for s in statuses))
            self.assertEqual(statuses[-1], "audio_done")

        asyncio.run(run())

    def test_path_outside_allowlisted_roots_is_rejected(self):
        async def run():
            handler, ws = self._make_handler()
            handler._spawn_turn = unittest.mock.Mock()
            with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as allowed:
                f = Path(outside) / "notes.pdf"
                f.write_bytes(b"%PDF-1.4 fake")
                with patch("ws_media.settings", _settings_with_spool_dirs(allowed)):
                    await handler.handle_file_input(
                        {"text": "", "path": str(f), "mime": "application/pdf", "filename": "notes.pdf"}
                    )
            handler._spawn_turn.assert_not_called()
            statuses = [json.loads(t)["text"] for t in ws.sent if json.loads(t).get("type") == "status"]
            self.assertTrue(any("couldn't find" in s for s in statuses))
            self.assertEqual(statuses[-1], "audio_done")

        asyncio.run(run())

    def test_non_pdf_acknowledges_without_docproc_call(self):
        async def run():
            handler, ws = self._make_handler()
            captured = {}
            handler._spawn_turn = lambda instruction, source, user_content=None, attachment=None: captured.update(
                instruction=instruction, source=source, attachment=attachment
            )
            with tempfile.NamedTemporaryFile(suffix=".docx") as f, patch(
                "ws_media.settings", _settings_with_spool_dirs(tempfile.gettempdir())
            ):
                f.write(b"not a pdf")
                f.flush()
                with patch("ws_media.docproc_client.submit_job") as submit:
                    await handler.handle_file_input(
                        {"text": "", "path": f.name, "mime": "application/vnd.openxmlformats", "filename": "notes.docx"}
                    )
                    submit.assert_not_called()

            self.assertIn("only process PDFs", captured["instruction"])
            self.assertEqual(captured["source"], "file")
            self.assertEqual(captured["attachment"]["type"], "file")

        asyncio.run(run())

    def test_pdf_without_caption_submits_and_acks_immediately(self):
        async def run():
            from unittest.mock import AsyncMock
            handler, ws = self._make_handler()
            captured = {}
            handler._spawn_turn = lambda instruction, source, user_content=None, attachment=None: captured.update(
                instruction=instruction, source=source, attachment=attachment
            )
            with tempfile.NamedTemporaryFile(suffix=".pdf") as f, patch(
                "ws_media.settings", _settings_with_spool_dirs(tempfile.gettempdir())
            ):
                f.write(b"%PDF-1.4 fake")
                f.flush()
                with patch(
                    "ws_media.docproc_client.submit_job",
                    new=AsyncMock(return_value="job-123"),
                ) as submit:
                    await handler.handle_file_input(
                        {"text": "", "path": f.name, "mime": "application/pdf", "filename": "paper.pdf"}
                    )
                    submit.assert_called_once_with(handler.state.mcp_manager, f.name)

            self.assertIn("job-123", captured["instruction"])
            self.assertIn("paper.pdf", captured["instruction"])
            self.assertEqual(captured["source"], "file")

        asyncio.run(run())

    def test_pdf_with_caption_schedules_background_poll_not_spawn_turn(self):
        async def run():
            from unittest.mock import AsyncMock
            handler, ws = self._make_handler()
            handler._spawn_turn = unittest.mock.Mock()
            created_tasks = []

            def fake_create_task(coro):
                created_tasks.append(coro)
                coro.close()
                return None

            with tempfile.NamedTemporaryFile(suffix=".pdf") as f, patch(
                "ws_media.settings", _settings_with_spool_dirs(tempfile.gettempdir())
            ):
                f.write(b"%PDF-1.4 fake")
                f.flush()
                with (
                    patch(
                        "ws_media.docproc_client.submit_job",
                        new=AsyncMock(return_value="job-456"),
                    ),
                    patch("ws_media.asyncio.create_task", side_effect=fake_create_task),
                ):
                    await handler.handle_file_input(
                        {"text": "summarize this", "path": f.name, "mime": "application/pdf", "filename": "paper.pdf"}
                    )

            handler._spawn_turn.assert_not_called()
            self.assertEqual(len(created_tasks), 1)

        asyncio.run(run())

    def test_docproc_submit_failure_acknowledges_error(self):
        async def run():
            handler, ws = self._make_handler()
            captured = {}
            handler._spawn_turn = lambda instruction, source, user_content=None, attachment=None: captured.update(
                instruction=instruction
            )
            with tempfile.NamedTemporaryFile(suffix=".pdf") as f, patch(
                "ws_media.settings", _settings_with_spool_dirs(tempfile.gettempdir())
            ):
                f.write(b"%PDF-1.4 fake")
                f.flush()
                with patch(
                    "ws_media.docproc_client.submit_job",
                    side_effect=RuntimeError("queue unreachable"),
                ):
                    await handler.handle_file_input(
                        {"text": "", "path": f.name, "mime": "application/pdf", "filename": "paper.pdf"}
                    )
            self.assertIn("failed", captured["instruction"])
            self.assertIn("queue unreachable", captured["instruction"])

        asyncio.run(run())


class AwaitPdfAndRunTests(unittest.TestCase):
    def _make_handler(self):
        ws = _FakeWS()
        handler = WebSocketSessionHandler(ws)
        handler.state.history_session = _FakeHistorySession(conv_id=1)
        return handler, ws

    def test_success_inlines_short_document_content(self):
        async def run():
            import tempfile
            from unittest.mock import AsyncMock
            handler, ws = self._make_handler()
            with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
                f.write("# Paper\n\nShort body.")
                md_path = f.name
            try:
                with (
                    patch(
                        "ws_media.docproc_client.poll_job",
                        new=AsyncMock(return_value={"status": "done", "result_md_path": md_path}),
                    ),
                    patch.object(handler, "run_turn", new=AsyncMock()) as run_turn,
                ):
                    await handler._await_pdf_and_run("job-1", "paper.pdf", "summarize this")
                run_turn.assert_called_once()
                instruction, kwargs = run_turn.call_args.args[0], run_turn.call_args.kwargs
                self.assertIn("summarize this", instruction)
                self.assertIn("Short body.", instruction)
                self.assertIn("job-1", instruction)
                self.assertEqual(kwargs.get("source"), "file")
            finally:
                import os
                os.unlink(md_path)

        asyncio.run(run())

    def test_long_document_uses_excerpt_instead_of_full_inline(self):
        async def run():
            import tempfile
            from dataclasses import replace
            from unittest.mock import AsyncMock
            handler, ws = self._make_handler()
            long_text = "word " * 5000  # well over a tiny inline budget
            with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
                f.write(long_text)
                md_path = f.name
            try:
                with (
                    patch(
                        "ws_media.docproc_client.poll_job",
                        new=AsyncMock(return_value={"status": "done", "result_md_path": md_path}),
                    ),
                    patch.object(handler, "run_turn", new=AsyncMock()) as run_turn,
                    patch(
                        "ws_media.settings",
                        replace(settings, docproc_inline_char_budget=100, docproc_excerpt_chars=20),
                    ),
                ):
                    await handler._await_pdf_and_run("job-2", "big.pdf", "read it")
                instruction = run_turn.call_args.args[0]
                self.assertIn("is long", instruction)
                self.assertIn(md_path, instruction)
            finally:
                import os
                os.unlink(md_path)

        asyncio.run(run())

    def test_poll_failure_still_runs_a_turn_reporting_the_error(self):
        async def run():
            from unittest.mock import AsyncMock
            import docproc_client
            handler, ws = self._make_handler()
            with (
                patch(
                    "ws_media.docproc_client.poll_job",
                    new=AsyncMock(side_effect=docproc_client.DocprocError("timed out after 300s")),
                ),
                patch.object(handler, "run_turn", new=AsyncMock()) as run_turn,
            ):
                await handler._await_pdf_and_run("job-3", "paper.pdf", "summarize")
            run_turn.assert_called_once()
            instruction = run_turn.call_args.args[0]
            self.assertIn("failed to convert or timed out", instruction)
            self.assertIn("timed out after 300s", instruction)

        asyncio.run(run())

    def test_disconnect_mid_poll_is_swallowed_not_raised(self):
        """A client that disconnects during the (potentially minutes-long)
        poll must not blow up the background task — mirrors
        _run_turn_guarded's handling of the same exceptions."""
        async def run():
            from unittest.mock import AsyncMock
            handler, ws = self._make_handler()
            with (
                patch(
                    "ws_media.docproc_client.poll_job",
                    new=AsyncMock(return_value={"status": "done", "result_md_path": None}),
                ),
                patch.object(handler, "run_turn", new=AsyncMock(side_effect=WebSocketDisconnect())),
            ):
                # Must not raise.
                await handler._await_pdf_and_run("job-4", "paper.pdf", "summarize")

        asyncio.run(run())
