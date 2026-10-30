"""pi as Octavius's brain (docs/pi-backend-design.md, rollout step 2).

With ``OCTAVIUS_BRAIN=pi``, voice and text turns run on a long-lived
``pi --mode rpc`` process per conversation (pi_harness ``scripts/pi-octavius``,
the ``octavius`` profile) instead of agent.py's loop. Octavius keeps everything
else: STT, TTS, the WebSocket protocol and the history DB, which stays the
authoritative record. A pi session is a working cache of the model's context:
when a process is missing or died, the next turn starts a fresh one and seeds it
with a compact transcript of the in-memory ``Conversation`` (itself loaded from
SQLite on restore/attach/load).

``stream_pi_turn`` has ``agent.stream_agent_turn``'s contract (an async
generator of sentences), so ``run_turn`` treats both brains alike. Speaking rule,
phase 1: an assistant message is spoken only once its ``message_end`` says
``stopReason: "stop"``; text that came with tool calls is discarded, exactly as
agent.py does. Image turns, vision threads, item chat and file turns stay native.

Guardrails pi lacks (checked in step 1): pi has no tool-round cap, so this module
counts ``turn_end``, steers at ``STEER_AT_ROUND`` and aborts at
``settings.max_tool_rounds``. Confirmations that octavius-guard asks for over RPC
(``extension_ui_request``, vikunja writes) are spoken as a question; Dave's next
transcript or text input answers it (``WebSocketSessionHandler._spawn_turn``).
"""

from __future__ import annotations

import asyncio
import collections
import itertools
import json
import logging
import os
import re
import signal
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from settings import settings

log = logging.getLogger(__name__)

STEER_AT_ROUND = 5
STEER_MESSAGE = (
    "[System note: You have used many tool calls. Summarize what you've found so far "
    "and respond to Dave now. Do not make more tool calls.]"
)
ROUND_CAP_REPLY = (
    "I ran into a limit on how many tool calls I can make. Could you try rephrasing your request?"
)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
SEED_MESSAGES = 20
SEED_CHARS_PER_MESSAGE = 800
SEED_TOTAL_CHARS = 6000
# A confirmation is a short, clear yes and nothing else: the whole utterance must
# match, and any negation or hedge anywhere cancels ("yes, but wait" is a no).
YES_RE = re.compile(
    r"^\W*(yes|yeah|yep|yup|sure|ok|okay|confirm(ed)?|do it|go ahead|please do|affirmative)"
    r"(\W+(please|do it|go ahead|thanks|thank you))?\W*$",
    re.IGNORECASE,
)
NO_WORDS = re.compile(
    r"\b(no|not|don'?t|do not|wait|cancel|stop|never|hold on|but)\b", re.IGNORECASE,
)
CONFIRM_VALUE_CHARS = 80
STDOUT_LIMIT = 64 * 1024 * 1024  # one JSONL record can carry a whole tool result or image
ABORT_SETTLE_S = 10.0
COMMAND_TIMEOUT_S = 30.0


class PiTurnError(RuntimeError):
    """A pi turn failed: process died, prompt rejected, provider error or timeout."""


def is_yes(text: str) -> bool:
    text = text or ""
    return bool(YES_RE.match(text)) and not NO_WORDS.search(text)


def tool_label(pi_tool_name: str) -> str:
    """Status label for a pi tool name, reusing Octavius's labels.

    pi names MCP tools ``mcp__<server>__<tool>``; Octavius's labels key the
    prefixed form ``<server>_<tool>`` (``email_hybrid_search``).
    """
    if pi_tool_name == "subagent":
        return "Researching"
    if not pi_tool_name.startswith("mcp__"):
        return settings.tool_labels.get(pi_tool_name, pi_tool_name)
    _, server, tool = pi_tool_name.split("__", 2)
    # Prefixed servers (email) are labelled by `<server>_<tool>`, others by bare tool name.
    return settings.tool_labels.get(f"{server}_{tool}") or settings.tool_labels.get(tool, tool)


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in SENTENCE_END.split(text.strip())]
    return [p + " " for p in parts[:-1] if p] + ([parts[-1]] if parts and parts[-1] else [])


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(
        b.get("text", "") for b in content or []
        if isinstance(b, dict) and b.get("type") == "text"
    )


def build_seed(messages: list[dict]) -> str:
    """Compact transcript of earlier user/assistant turns for a fresh pi session."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        text = content.strip()
        if len(text) > SEED_CHARS_PER_MESSAGE:
            text = text[:SEED_CHARS_PER_MESSAGE] + "…"
        lines.append(f"{'Dave' if role == 'user' else 'Octavius'}: {text}")
    lines = lines[-SEED_MESSAGES:]
    while lines and sum(len(line) + 1 for line in lines) > SEED_TOTAL_CHARS:
        lines.pop(0)
    if not lines:
        return ""
    return (
        "[Earlier in this conversation, restored from history]\n"
        + "\n".join(lines)
        + "\n[End of earlier conversation]"
    )


def build_prompt(text: str, style: str, memory: str = "", seed: str = "") -> str:
    """The RPC prompt for one turn: per-turn context rides in the user message,
    never in the system prompt, so the cached prefix stays identical."""
    blocks = [style]
    if memory:
        blocks.append(memory)
    if seed:
        blocks.append(seed)
    if len(blocks) > 1:
        blocks.append("Dave's message:\n" + text)
    else:
        blocks.append(text)
    return "\n\n".join(blocks)


def child_env(cfg) -> dict[str, str]:
    """pi's environment: Octavius's own config and keys (OCTAVIUS_*) stay out."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OCTAVIUS_")}
    env["PI_BIN"] = cfg.pi_bin
    env["PI_OCTAVIUS_MODEL"] = cfg.model
    return env


class PiRpcProcess:
    """One ``pi --mode rpc`` child: JSONL commands in, responses and events out."""

    _ids = itertools.count(1)

    def __init__(self, argv: list[str], env: dict[str, str] | None = None):
        self.argv = argv
        self.env = env
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()  # one driver at a time
        self.last_used = time.monotonic()
        self.turns = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._events: asyncio.Queue | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self._tasks: list[asyncio.Task] = []

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            # Its own process group, so pi and its MCP/subagent children die together.
            start_new_session=True,
            limit=STDOUT_LIMIT,
        )
        self._tasks = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
        ]

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                # StreamReader.readline splits on LF only, as the RPC framing requires
                # (U+2028/U+2029 inside JSON strings are not record boundaries).
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    record = json.loads(line.decode("utf-8").rstrip("\r\n"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if record.get("type") == "response" and record.get("id") in self._pending:
                    fut = self._pending.pop(record["id"])
                    if not fut.done():
                        fut.set_result(record)
                    continue
                if self._events is not None:
                    self._events.put_nowait(record)
                elif record.get("type") == "extension_ui_request" and record.get("method") in (
                    "select", "confirm", "input", "editor"
                ):
                    # Nobody is driving a turn: dismiss, never leave pi blocked.
                    await self.respond_ui(record.get("id"), cancelled=True)
        except Exception:
            log.exception("pi stdout reader failed")
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(PiTurnError("pi process exited"))
            self._pending.clear()
            if self._events is not None:
                self._events.put_nowait({"type": "_exit"})

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            self._stderr_tail.append(line.decode("utf-8", "replace").rstrip()[:300])

    def stderr_tail(self) -> str:
        return " | ".join(self._stderr_tail)

    async def _write(self, record: dict) -> None:
        if not self.alive or self.proc.stdin is None:
            raise PiTurnError("pi process is not running")
        self.proc.stdin.write((json.dumps(record) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def command(self, type_: str, timeout: float = COMMAND_TIMEOUT_S, **fields) -> dict:
        rid = f"oc-{next(self._ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._write({"id": rid, "type": type_, **fields})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def respond_ui(self, rid: str | None, **fields) -> None:
        if rid is None:
            return
        try:
            await self._write({"type": "extension_ui_response", "id": rid, **fields})
        except PiTurnError:
            pass

    def subscribe(self) -> asyncio.Queue:
        self._events = asyncio.Queue()
        return self._events

    def unsubscribe(self) -> None:
        self._events = None

    async def kill(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self.proc is None or self.proc.returncode is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.proc.wait(), 3)
        except asyncio.TimeoutError:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.proc.wait()


@dataclass
class PiProcessManager:
    """conversation_id → pi process. Lazy start, idle reaping, killed on shutdown."""

    cfg: object
    processes: dict[int, PiRpcProcess] = field(default_factory=dict)
    starts: int = 0
    failures: int = 0
    last_error: str | None = None
    _reaper: asyncio.Task | None = None

    def argv(self) -> list[str]:
        return [self.cfg.launcher, "--mode", "rpc"]

    async def get(self, conv_id: int) -> tuple[PiRpcProcess, bool]:
        """The live process for a conversation, and whether it was just started."""
        proc = self.processes.get(conv_id)
        if proc is not None and proc.alive:
            return proc, False
        if proc is not None:
            await proc.kill()
        proc = PiRpcProcess(self.argv(), child_env(self.cfg))
        await proc.start()
        self.processes[conv_id] = proc
        self.starts += 1
        log.info("pi: started process %s for conversation %s", proc.proc.pid, conv_id)
        return proc, True

    def rekey(self, old: int, new: int) -> None:
        if old == new or old not in self.processes:
            return
        proc = self.processes.pop(old)
        stale = self.processes.pop(new, None)
        self.processes[new] = proc
        if stale is not None and stale is not proc:
            asyncio.create_task(stale.kill())

    async def discard(self, conv_id: int | None) -> None:
        proc = self.processes.pop(conv_id, None) if conv_id is not None else None
        if proc is not None:
            await proc.kill()

    def record_failure(self, err: str) -> None:
        self.failures += 1
        self.last_error = err[:300]

    def start_reaper(self, interval: float = 300.0) -> None:
        async def loop():
            while True:
                await asyncio.sleep(interval)
                await self.reap_idle()
        self._reaper = asyncio.create_task(loop())

    async def reap_idle(self) -> None:
        now = time.monotonic()
        for conv_id, proc in list(self.processes.items()):
            if not proc.alive or (not proc.lock.locked() and now - proc.last_used > self.cfg.idle_reap_s):
                log.info("pi: reaping process for conversation %s", conv_id)
                await self.discard(conv_id)

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        for conv_id in list(self.processes):
            await self.discard(conv_id)

    def health(self) -> dict:
        return {
            "brain": self.cfg.brain,
            "model": self.cfg.model,
            "processes": sum(1 for p in self.processes.values() if p.alive),
            "starts": self.starts,
            "failures": self.failures,
            "last_error": self.last_error,
        }


def wants_pi(source: str, user_content, conversation) -> bool:
    """Whether a turn runs on pi: voice/text only, never images or vision threads."""
    return (
        settings.pi_brain.enabled
        and source in ("voice", "text")
        and user_content is None
        and not getattr(conversation, "has_images", False)
    )


async def stream_pi_turn(
    session,
    user_text: str,
    *,
    source: str = "voice",
    status_callback=None,
) -> AsyncGenerator[str, None]:
    """Run one turn on pi, yielding sentences of the spoken answer."""
    from agent import _build_memory_block, _style_directive

    state = session.state
    manager: PiProcessManager = session.ws.app.state.pi_brain
    history_session = state.history_session
    conversation = state.conversation
    conv_id = history_session.conv_id

    prior = [m for m in conversation.get_messages() if m.get("role") in ("user", "assistant")]
    conversation.add_user(user_text)
    conversation.trim()

    memory = ""
    try:
        memory = await _build_memory_block(
            getattr(history_session, "db_path", None), user_text,
            first_turn=not prior, current_conv_id=conv_id,
        )
    except Exception:
        log.warning("Memory injection failed; pi turn proceeds without memory", exc_info=True)

    proc, fresh = await manager.get(conv_id)
    seed = build_seed(prior) if fresh else ""
    message = build_prompt(user_text, _style_directive(source), memory, seed)

    async with proc.lock:
        proc.last_used = time.monotonic()
        proc.turns += 1
        events = proc.subscribe()
        settled = False
        answer_parts: list[str] = []
        rounds = 0
        tool_args: dict[str, dict] = {}
        started = time.monotonic()
        try:
            resp = await proc.command("prompt", message=message)
            if not resp.get("success"):
                raise PiTurnError(f"pi rejected the prompt: {resp.get('error')}")
            if (resp.get("data") or {}).get("disposition") == "handled":
                settled = True
                return
            deadline = started + settings.pi_brain.turn_timeout_s
            provider_error: str | None = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PiTurnError("pi turn timed out")
                ev = await asyncio.wait_for(events.get(), remaining)
                etype = ev.get("type")
                if etype == "_exit":
                    raise PiTurnError(f"pi process exited mid-turn ({proc.stderr_tail()[:200]})")
                if etype == "message_end":
                    msg = ev.get("message") or {}
                    if msg.get("role") != "assistant":
                        continue
                    stop = msg.get("stopReason")
                    if stop == "error":
                        provider_error = msg.get("errorMessage") or "provider error"
                    elif stop in ("stop", "length"):
                        provider_error = None
                        text = message_text(msg).strip()
                        if text:
                            answer_parts.append(text)
                            for sentence in split_sentences(text):
                                yield sentence
                elif etype == "tool_execution_start":
                    tool_args[ev.get("toolCallId", "")] = ev.get("args") or {}
                    if status_callback:
                        await status_callback(f"{tool_label(ev.get('toolName', ''))}...")
                elif etype == "tool_execution_end":
                    await _record_tool(history_session, ev, tool_args.pop(ev.get("toolCallId", ""), {}))
                elif etype == "turn_end":
                    rounds += 1
                    if rounds == STEER_AT_ROUND:
                        await proc.command("steer", message=STEER_MESSAGE)
                    elif rounds >= settings.max_tool_rounds:
                        await proc.command("abort", timeout=ABORT_SETTLE_S)
                        settled = True
                        answer_parts.append(ROUND_CAP_REPLY)
                        yield ROUND_CAP_REPLY
                        break
                elif etype == "extension_ui_request":
                    async for sentence in _handle_ui_request(session, proc, ev):
                        yield sentence
                elif etype == "auto_retry_end" and ev.get("success") is False:
                    provider_error = ev.get("finalError") or provider_error
                elif etype == "agent_settled":
                    settled = True
                    break
            if provider_error and not answer_parts:
                raise PiTurnError(provider_error)
        except PiTurnError as exc:
            manager.record_failure(str(exc))
            if not proc.alive:
                await manager.discard(conv_id)
            raise
        except asyncio.TimeoutError as exc:
            manager.record_failure("pi turn timed out")
            raise PiTurnError("pi turn timed out") from exc
        finally:
            if not settled and proc.alive:
                # Barge-in, reset or error mid-turn: stop pi before the next prompt.
                try:
                    await proc.command("abort", timeout=ABORT_SETTLE_S)
                except Exception:
                    log.warning("pi abort failed; discarding the process", exc_info=True)
                    await manager.discard(conv_id)
            proc.unsubscribe()
            proc.last_used = time.monotonic()
            log.info(
                "pi turn: %.2fs, %d round(s), %s, process %s",
                time.monotonic() - started, rounds,
                "settled" if settled else "aborted", "fresh" if fresh else "warm",
            )

    clean = " ".join(answer_parts).strip()
    if not clean:
        clean = "I'm not sure how to respond to that."
        yield clean
    conversation.add_assistant(clean)


async def _handle_ui_request(session, proc: PiRpcProcess, ev: dict) -> AsyncGenerator[str, None]:
    """Answer an extension dialog. Only ``confirm`` is put to Dave; others are dismissed."""
    rid = ev.get("id")
    if ev.get("method") != "confirm":
        if ev.get("method") in ("select", "input", "editor"):
            await proc.respond_ui(rid, cancelled=True)
        return
    title = str(ev.get("title") or "Confirm?")
    detail = str(ev.get("message") or "")
    question = f"Before I go ahead: {_describe_confirm(title, detail)} Shall I? "
    await session.send_payload({"type": "confirm_request", "title": title, "message": detail})
    yield question
    fut = asyncio.get_running_loop().create_future()
    session.state.pending_confirm = fut
    # The question has been spoken; re-arm the client's mic for the answer.
    await session.send_json("status", "audio_done")
    try:
        answer = await asyncio.wait_for(fut, settings.pi_brain.confirm_timeout_s)
    except asyncio.TimeoutError:
        answer = False
    finally:
        session.state.pending_confirm = None
    await proc.respond_ui(rid, confirmed=bool(answer))
    await session.send_json("status", "Working..." if answer else "Cancelled.")


def _describe_confirm(title: str, detail: str) -> str:
    """A speakable version of octavius-guard's confirm: the action and EVERY argument.

    Ids and target fields are read out, not just model-written text, so Dave hears
    which task changes and how. Long values are shortened but every key is named;
    the full JSON goes to the client in ``confirm_request``.
    """
    action = title.removeprefix("Allow ").rstrip("?").replace("_", " ")
    try:
        args = json.loads(detail)
    except (json.JSONDecodeError, TypeError):
        args = None
    if not isinstance(args, dict):
        return f"{action}, with arguments I can't read aloud; check the screen."
    parts = []
    for key, value in args.items():
        text = value if isinstance(value, str) else json.dumps(value)
        if len(text) > CONFIRM_VALUE_CHARS:
            text = text[:CONFIRM_VALUE_CHARS] + f"… ({len(text)} characters)"
        parts.append(f"{key.replace('_', ' ')} {text!r}" if isinstance(value, str) else f"{key.replace('_', ' ')} {text}")
    return f"{action}: {'; '.join(parts)}." if parts else f"{action}."


async def _record_tool(history_session, ev: dict, args: dict) -> None:
    """Record a pi tool call the way agent.py records its own."""
    if history_session is None:
        return
    result = ev.get("result") or {}
    text = message_text(result) if isinstance(result, dict) else str(result)
    name = ev.get("toolName", "")
    server = name.split("__")[1] if name.startswith("mcp__") else "pi"
    try:
        msg_id = await history_session.add_message_async(
            role="tool", content=text[:500], model=None, latency_ms=ev.get("durationMs"),
        )
        history_session.add_tool_call(
            message_id=msg_id,
            tool_name=name,
            server_name=server,
            arguments=args,
            status="error" if ev.get("isError") else "success",
            result_summary=text[:500],
            result_size=len(text),
            duration_ms=ev.get("durationMs"),
        )
    except Exception:
        log.warning("Recording pi tool call failed", exc_info=True)
