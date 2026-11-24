"""Delegations, proactive speech and inline subagent runs for a WebSocket session.

A mixin composed into ``websocket_session.WebSocketSessionHandler``; the
session state lives on ``self.state`` (see ``websocket_session``).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from settings import settings
from subagent import run_subagent
from subagent_dispatcher import SubagentTicket

log = logging.getLogger(__name__)


# Max chars in the per-delegation preview line shown in the UI badge.
_DELEGATION_PREVIEW_CHARS = 180


def _build_preview(text: str) -> str:
    """Build a single-line preview of a delegation result for the UI badge."""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if len(first_line) <= _DELEGATION_PREVIEW_CHARS:
        return first_line
    cut = first_line[:_DELEGATION_PREVIEW_CHARS].rsplit(" ", 1)[0]
    return cut + "..."


@dataclass
class DelegationRecord:
    handle: str
    domain: str
    submitted_task: str
    ticket: SubagentTicket
    created_at: datetime
    task: asyncio.Task | None = None
    status: str = "running"  # 'running' | 'ready' | 'failed'
    result: str | None = None  # full result text including TOOL DATA, parked for pull
    preview: str | None = None  # short one-line preview for the UI badge
    error: str | None = None


@dataclass
class ProactiveMessage:
    handle: str
    domain: str
    text: str


class DelegationMixin:
    async def _proactive_worker(self) -> None:
        try:
            while True:
                msg = await self.state.proactive_queue.get()
                try:
                    async with self.state.turn_lock:
                        await self._proactive_speak(msg)
                except Exception:
                    log.exception("Proactive speak failed for %s", msg.handle)
        except asyncio.CancelledError:
            pass


    async def _proactive_speak(self, msg: ProactiveMessage) -> None:
        from agent import SENTENCE_END
        from tts import speakable, synthesize

        spoken = msg.text.split("\n\n===TOOL DATA", 1)[0].strip()
        if not spoken:
            return

        prelude = f"(from {msg.domain}) "
        full = prelude + spoken
        self.state.conversation.add_assistant(full)
        if self.state.history_session:
            await self.state.history_session.add_message_async(
                role="assistant",
                content=full,
                model=settings.subagent_llm_chain[0]["model"],
            )

        await self.send_payload({
            "type": "subagent_done",
            "handle": msg.handle,
            "domain": msg.domain,
            "text": full,
        })

        if not self.state.tts_enabled:
            return

        await self.send_json("status", "Speaking...")
        parts = SENTENCE_END.split(spoken)
        sentences = [prelude + parts[0]] + parts[1:] if parts else [full]
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            try:
                wav_bytes = await synthesize(await speakable(sentence), voice=self.state.voice)
                await self.send_bytes(wav_bytes)
            except Exception:
                log.exception("TTS failed for proactive chunk")
        await self.send_json("status", "audio_done")


    async def run_inline_subagent(self, domain: str, task: str) -> str:
        """Run a scoped subagent synchronously and return its result text.

        Used by the consult_specialist local tool. The main agent's turn blocks
        on this and then weaves the returned text into its spoken reply in the
        SAME turn (no badge, no pull). A dispatcher ticket is reserved so inline
        consults still respect endpoint capacity, and the subagent's per-step
        progress is forwarded to the UI status line so Dave isn't left in
        silence while it works.
        """
        ticket = await self.state.subagent_dispatcher.reserve()
        try:
            assigned_url = await ticket.acquire()
            fallback_url = self.state.subagent_dispatcher.fallback_url()

            async def status_cb(text: str):
                # run_subagent already maps tool names through tool_labels and
                # formats the text (e.g. "Email Search..."); forward as-is.
                await self.send_json("status", text)

            return await run_subagent(
                task,
                domain,
                self.state.mcp_manager,
                assigned_url=assigned_url,
                fallback_url=fallback_url,
                status_callback=status_cb,
            )
        finally:
            await ticket.release()


    async def spawn_delegation(self, domain: str, task: str) -> dict:
        """Reserve an endpoint, create a background asyncio.Task running the
        subagent, and register it. Returns a summary dict for the tool response.
        """
        ticket = await self.state.subagent_dispatcher.reserve()
        handle = f"dlg_{uuid.uuid4().hex[:12]}"
        record = DelegationRecord(
            handle=handle,
            domain=domain,
            submitted_task=task,
            ticket=ticket,
            created_at=datetime.now(),
        )
        record.task = asyncio.create_task(self._run_and_announce(record))
        self.state.delegations[handle] = record
        log.info(
            "Spawned delegation %s (domain=%s, dispatcher=%s)",
            handle, domain, self.state.subagent_dispatcher.snapshot(),
        )
        return {
            "handle": handle,
            "domain": domain,
            "status": "started",
        }


    async def cancel_delegation(self, handle: str) -> dict:
        record = self.state.delegations.get(handle)
        if record is None:
            return {"cancelled": False, "reason": "unknown handle", "handle": handle}
        if record.ticket.assigned_url is None:
            await record.ticket.cancel_pending()
        if record.task and not record.task.done():
            record.task.cancel()
        self.state.delegations.pop(handle, None)
        return {"cancelled": True, "handle": handle}


    async def _emit_delegation_update(self, record: DelegationRecord) -> None:
        await self.send_payload({
            "type": "delegation_update",
            "handle": record.handle,
            "domain": record.domain,
            "submitted_task": record.submitted_task,
            "status": record.status,
            "preview": record.preview,
            "error": record.error,
        })


    async def _run_and_announce(self, record: DelegationRecord) -> None:
        assigned_url: str | None = None
        try:
            await self._emit_delegation_update(record)
            assigned_url = await record.ticket.acquire()
            fallback_url = self.state.subagent_dispatcher.fallback_url()

            async def status_cb(text: str):
                label = settings.tool_labels.get(text, text)
                await self.send_payload({
                    "type": "subagent_progress",
                    "handle": record.handle,
                    "domain": record.domain,
                    "text": label,
                })

            result = await run_subagent(
                record.submitted_task,
                record.domain,
                self.state.mcp_manager,
                assigned_url=assigned_url,
                fallback_url=fallback_url,
                status_callback=status_cb,
            )
            spoken = result.split("\n\n===TOOL DATA", 1)[0].strip()
            record.result = result
            record.preview = _build_preview(spoken)
            record.status = "ready"
            await self._emit_delegation_update(record)
            if self.state.proactive_speak_enabled:
                await self.state.proactive_queue.put(
                    ProactiveMessage(handle=record.handle, domain=record.domain, text=result)
                )
        except asyncio.CancelledError:
            log.info("Delegation %s cancelled", record.handle)
            # Cancelled records are removed by cancel_delegation already.
            raise
        except Exception as exc:
            log.exception("Delegation %s crashed", record.handle)
            record.status = "failed"
            record.error = str(exc)
            await self._emit_delegation_update(record)
        finally:
            await record.ticket.release()

            # Note: we intentionally do NOT remove the record here. Ready and
            # failed records stay parked until the user pulls or dismisses
            # them (or the WebSocket disconnects, at which point cleanup
            # cancels everything).

    async def handle_delegation_list(self, _data: dict):
        """Reply with the current snapshot. Used by the UI on connect/reconnect."""
        for record in list(self.state.delegations.values()):
            await self._emit_delegation_update(record)


    async def handle_delegation_dismiss(self, data: dict):
        handle = data.get("handle", "")
        record = self.state.delegations.get(handle)
        if record is None:
            return
        if record.status == "running":
            # Treat dismiss-while-running as cancel.
            await self.cancel_delegation(handle)
        else:
            self.state.delegations.pop(handle, None)
        await self.send_payload({"type": "delegation_removed", "handle": handle})


    async def handle_delegation_pull(self, data: dict):
        handle = data.get("handle", "")
        mode = data.get("mode", "merge")
        await self.pull_delegation(handle=handle, mode=mode, via="ui")


    async def pull_delegation(self, handle: str, mode: str, via: str = "ui") -> str:
        """Bring a parked delegation result into the conversation.

        mode='merge' injects the result into the running conversation. When
        called via UI, runs a fresh agent turn so Octavius summarizes the
        result naturally; when called via the agent's pull_delegation tool
        (via='voice'), returns the result text as the tool observation so
        the in-flight agent turn can incorporate it.

        mode='new' (UI only) creates a fresh history session seeded with the
        original task and result, and tells the UI to switch to it. The
        previous conversation is left intact.
        """
        record = self.state.delegations.get(handle)
        if record is None:
            return f"No pending delegation found with handle {handle}."
        if record.status == "running":
            return (
                f"Delegation {handle} is still running. "
                "I'll pull the result once it's ready."
            )

        if record.status == "failed":
            err = record.error or "unknown error"
            self.state.delegations.pop(handle, None)
            await self.send_payload({"type": "delegation_removed", "handle": handle})
            return f"That delegation failed: {err}"

        spoken = (record.result or "").split("\n\n===TOOL DATA", 1)[0].strip()
        domain = record.domain
        submitted = record.submitted_task
        self.state.delegations.pop(handle, None)
        await self.send_payload({"type": "delegation_removed", "handle": handle})

        if mode == "new":
            await self._spawn_seeded_conversation(domain, submitted, spoken)
            return f"Started a new conversation seeded with the {domain} results."

        # mode == "merge"
        if via == "ui":
            prompt = (
                f"[I earlier delegated this {domain} task and the specialist has "
                f"now returned. Original task: {submitted}\n\nSpecialist reply:\n"
                f"{spoken}\n\nSummarize this for me conversationally.]"
            )
            await self.send_json("transcript", f"(reviewing {domain} results)")
            self._spawn_turn(prompt, source="text")
            return ""
        # via == "voice" — caller is the agent mid-turn; hand it the text.
        return spoken


    async def _spawn_seeded_conversation(
        self, domain: str, submitted_task: str, result_text: str,
    ) -> None:
        """End the current history session, start a new one seeded with the
        delegation exchange, and tell the UI to switch to it.
        """
        if self.state.history_session:
            await self.state.history_session.end_async()
        new_session = self.state.history.start_conversation(
            service="octavius",
            source="voice",
            model=settings.llm_chain[0]["model"],
        )
        seed_user = (
            f"[Earlier I delegated this {domain} task and the specialist has "
            f"returned. Original task: {submitted_task}]"
        )
        seed_assistant = result_text or "(no content)"
        await new_session.add_message_async(
            role="user", content=seed_user, model=None,
        )
        await new_session.add_message_async(
            role="assistant",
            content=seed_assistant,
            model=settings.subagent_llm_chain[0]["model"],
        )
        self.state.history_session = new_session
        self.state.conversation.reset()
        self.state.conversation.add_user(seed_user)
        self.state.conversation.add_assistant(seed_assistant)
        await self.send_payload({
            "type": "conversation_loaded",
            "conversation_id": new_session.conv_id,
            "messages": [
                {"role": "user", "content": seed_user},
                {"role": "assistant", "content": seed_assistant},
            ],
        })
        await self.send_payload({
            "type": "session_id",
            "conversation_id": new_session.conv_id,
        })
