"""Media turns (image_input, file_input, PDF conversion) for a WebSocket session.

A mixin composed into ``websocket_session.WebSocketSessionHandler``; the
session state lives on ``self.state`` (see ``websocket_session``).
"""
from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

import docproc_client
import media_uploads
from settings import settings
from ws_common import WebSocketDisconnect

log = logging.getLogger(__name__)


class MediaTurnsMixin:
    async def handle_image_input(self, data: dict):
        """Handle the frozen WS media contract's `image_input` frame.

        See docs/ws-media-contract.md. The sidecar has already spooled the
        file to disk, but the frame's `path` and `size_bytes` are still
        frame-supplied input: we resolve `path` against
        `settings.media_spool_dirs` (`media_uploads.resolve_spooled_media`)
        rather than trusting a bare existence check, check the mime looks
        like an image, and revalidate the actual file size on disk against
        `IMAGE_MAX_BYTES` rather than trusting `size_bytes`. Once validated,
        we build an OpenAI-style multimodal content array and route the turn
        through the vision chain (agent.py's `use_vision` handling in
        `stream_agent_turn`).
        Vision routing is sticky: once a thread has carried an image
        (`Conversation.has_images`), its follow-up turns stay on the vision
        chain and the content array stays in the in-memory conversation.
        Unknown/extra frame fields are ignored by construction (we only read
        the keys we know about).
        """
        path = data.get("path", "")
        mime = data.get("mime", "") or ""
        filename = data.get("filename") or (Path(path).name if path else "image")
        caption = (data.get("text") or "").strip()

        resolved = media_uploads.resolve_spooled_media(path, settings.media_spool_dirs)
        if resolved is None:
            log.warning("image_input with missing/unauthorized path: %r", path)
            await self.send_json("status", "I got notified about an image but couldn't find the file.")
            await self.send_json("status", "audio_done")
            return
        path = str(resolved)
        if not mime.startswith("image/"):
            log.warning("image_input with non-image mime %r for %s", mime, path)
            await self.send_json(
                "status", f"Received '{filename}' but its type ({mime or 'unknown'}) isn't an image I can view."
            )
            await self.send_json("status", "audio_done")
            return

        # Revalidate actual size on disk rather than trusting the frame's
        # size_bytes, which describes what the sidecar/uploader claims to
        # have spooled, not what's actually sitting at `path`.
        try:
            actual_size = resolved.stat().st_size
        except OSError:
            log.exception("Failed to stat spooled image at %s", resolved)
            await self.send_json("status", "Received an image but couldn't read the file.")
            await self.send_json("status", "audio_done")
            return
        if actual_size > media_uploads.IMAGE_MAX_BYTES:
            log.warning("image_input at %s is %d bytes, over the %d cap", resolved, actual_size, media_uploads.IMAGE_MAX_BYTES)
            await self.send_json("status", "That image is too large for me to view (max 20 MB).")
            await self.send_json("status", "audio_done")
            return

        try:
            image_bytes = resolved.read_bytes()
        except OSError:
            log.exception("Failed to read spooled image at %s", resolved)
            await self.send_json("status", "Received an image but couldn't read the file.")
            await self.send_json("status", "audio_done")
            return

        b64 = base64.b64encode(image_bytes).decode("ascii")
        # Text part sent to the vision model for THIS turn.
        vision_text = caption or f"The user sent an image: {filename}"
        # Text representation used everywhere else: transcript display,
        # persisted history, and memory queries. Never base64 — the content
        # array lives only in the in-memory conversation (see agent.py's
        # stream_agent_turn / memory trust boundary).
        placeholder_text = f"[image: {filename}]" + (f" {caption}" if caption else "")
        content = [
            {"type": "text", "text": vision_text},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]

        await self.send_json("transcript", placeholder_text)
        self._spawn_turn(
            placeholder_text,
            source="image",
            user_content=content,
            attachment={"type": "image", "reference": path, "title": filename},
        )


    async def handle_file_input(self, data: dict):
        """Handle the frozen WS media contract's `file_input` frame.

        See docs/ws-media-contract.md. The frame's `path` is resolved against
        `settings.media_spool_dirs` (`media_uploads.resolve_spooled_media`)
        rather than trusted outright. PDFs are submitted to the docproc queue
        DETERMINISTICALLY (plain code — not an LLM tool-call decision);
        non-PDF files just get a brief acknowledgement since Octavius can't
        process them yet. Unknown/extra frame fields are ignored by
        construction.
        """
        path = data.get("path", "")
        mime = data.get("mime", "") or ""
        filename = data.get("filename") or (Path(path).name if path else "file")
        caption = (data.get("text") or "").strip()

        resolved = media_uploads.resolve_spooled_media(path, settings.media_spool_dirs)
        if resolved is None:
            log.warning("file_input with missing/unauthorized path: %r", path)
            await self.send_json("status", "I got notified about a file but couldn't find it on disk.")
            await self.send_json("status", "audio_done")
            return
        path = str(resolved)

        if mime != "application/pdf":
            await self.send_json("transcript", f"[file: {filename}]")
            instruction = (
                f"[Dave just sent a file named '{filename}' (stored at {path}). "
                "Octavius can only process PDFs into readable text so far. Briefly "
                "acknowledge receipt, mention PDFs are the only file type handled "
                "right now, and note where it's stored.]"
            )
            self._spawn_turn(
                instruction, source="file",
                attachment={"type": "file", "reference": path, "title": filename},
            )
            return

        await self.send_json("transcript", f"[file: {filename}]" + (f" {caption}" if caption else ""))
        await self._handle_pdf_file_input(path, filename, caption)


    async def _handle_pdf_file_input(self, path: str, filename: str, caption: str) -> None:
        """Deterministically submit a PDF to the docproc queue, then either
        acknowledge immediately (no caption) or poll in the background and
        run the caption as instructions once conversion completes.
        """
        try:
            job_id = await docproc_client.submit_job(self.state.mcp_manager, path)
        except Exception as exc:
            log.exception("docproc submission failed for %s", path)
            instruction = (
                f"[Dave sent a PDF named '{filename}' but submitting it for "
                f"conversion failed: {exc}. Let him know something went wrong "
                "and he may need to resend it.]"
            )
            self._spawn_turn(
                instruction, source="file",
                attachment={"type": "file", "reference": path, "title": filename},
            )
            return

        log.info("Submitted PDF %s to docproc: job %s (caption=%r)", path, job_id, bool(caption))

        if caption:
            # Caption = instructions. Poll in the background (bounded,
            # doesn't block the WS loop or other sessions) and run the turn
            # once conversion lands.
            asyncio.create_task(self._await_pdf_and_run(job_id, filename, caption))
            return

        # No caption: acknowledge right away rather than making Dave wait on
        # the conversion.
        instruction = (
            f"[Dave just sent a PDF named '{filename}' for conversion (docproc "
            f"job {job_id}). Briefly acknowledge receipt, mention it's "
            "converting to markdown in the background, and ask what he'd like "
            "done with it once it's ready.]"
        )
        self._spawn_turn(
            instruction, source="file",
            attachment={"type": "file", "reference": path, "title": filename},
        )


    async def _await_pdf_and_run(self, job_id: str, filename: str, caption: str) -> None:
        """Background task: poll a docproc job to completion, then run the
        agent turn with the caption as instructions plus the converted
        document (inlined if it fits the char budget, else path + excerpt).

        Runs off the WS receive loop (spawned via asyncio.create_task) and
        calls run_turn directly rather than _spawn_turn: unlike a fresh WS
        frame, dropping this turn on overlap would silently lose Dave's
        instructions, so it queues behind turn_lock instead. Wrapped like
        _run_turn_guarded so a client disconnect mid-poll (this can run for
        several minutes) doesn't surface as an unretrieved task exception.
        """
        try:
            await self._await_pdf_and_run_unguarded(job_id, filename, caption)
        except (WebSocketDisconnect, RuntimeError):
            log.info("PDF-turn aborted: client disconnected mid-conversion (job %s)", job_id)
        except Exception:
            log.exception("PDF-turn task crashed (job %s)", job_id)


    async def _await_pdf_and_run_unguarded(self, job_id: str, filename: str, caption: str) -> None:
        try:
            row = await docproc_client.poll_job(self.state.mcp_manager, job_id)
        except docproc_client.DocprocError as exc:
            log.warning("docproc job %s did not complete: %s", job_id, exc)
            instruction = (
                f"[The PDF '{filename}' Dave sent (docproc job {job_id}) failed "
                f"to convert or timed out: {exc}. Let him know and ask if he'd "
                "like to try again.]"
            )
            await self.run_turn(instruction, source="file")
            return
        except Exception as exc:
            log.exception("Unexpected error polling docproc job %s", job_id)
            instruction = (
                f"[Something went wrong checking on the PDF '{filename}' Dave "
                f"sent (docproc job {job_id}): {exc}. Let him know.]"
            )
            await self.run_turn(instruction, source="file")
            return

        md_path = row.get("result_md_path")
        doc_text = ""
        if md_path:
            try:
                doc_text = Path(md_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                log.warning("Could not read converted markdown at %s", md_path)

        if doc_text and len(doc_text) <= settings.docproc_inline_char_budget:
            body = f"Converted document content:\n\n{doc_text}"
        elif doc_text:
            head = doc_text[: settings.docproc_excerpt_chars]
            body = (
                f"The converted document is long ({len(doc_text)} chars); here is "
                f"the markdown path and a head excerpt (use check_document_status "
                f"with job_id {job_id} to fetch more).\nMarkdown file: {md_path}\n\n"
                f"Excerpt:\n{head}"
            )
        else:
            body = f"Markdown file: {md_path or '(unknown — use check_document_status)'}"

        instruction = (
            f"[Dave sent a PDF named '{filename}' with instructions: \"{caption}\". "
            f"It has finished converting (docproc job {job_id}).\n{body}\n\n"
            "Follow Dave's instructions using this document content.]"
        )
        await self.run_turn(instruction, source="file")
