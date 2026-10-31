"""pi_brain: the RPC process, the manager, and stream_pi_turn against a fake pi.

tests/fake_pi_rpc.py runs as a real subprocess, so pipes, process groups,
abort and the confirm round trip are exercised end to end.
"""

import asyncio
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pi_brain  # noqa: E402
from conversation import Conversation  # noqa: E402
from settings import PiBrainSettings  # noqa: E402

FAKE = Path(__file__).with_name("fake_pi_rpc.py")


def _cfg(launcher: str, **over) -> PiBrainSettings:
    base = dict(brain="pi", launcher=launcher, pi_bin="pi", model="fake/model",
                idle_reap_s=3600, turn_timeout_s=20, confirm_timeout_s=5)
    base.update(over)
    return PiBrainSettings(**base)


class _History:
    def __init__(self, conv_id=7):
        self.conv_id = conv_id
        self.db_path = None
        self.messages = []
        self.tool_calls = []

    async def add_message_async(self, role, content, **kw):
        self.messages.append((role, content))
        return len(self.messages)

    def add_tool_call(self, **kw):
        self.tool_calls.append(kw)


class _Session:
    def __init__(self, manager, conv_id=7):
        self.ws = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pi_brain=manager)))
        self.state = SimpleNamespace(
            history_session=_History(conv_id), conversation=Conversation(), pending_confirm=None,
        )
        self.sent = []

    async def send_payload(self, payload):
        self.sent.append(payload)

    async def send_json(self, t, text):
        self.sent.append({"type": t, "text": text})


class PiBrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "commands.jsonl")
        launcher = os.path.join(self.tmp.name, "pi-octavius")
        with open(launcher, "w") as f:
            f.write(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
        os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IEXEC)
        os.environ["FAKE_PI_LOG"] = self.log
        self.cfg = _cfg(launcher)
        self.manager = pi_brain.PiProcessManager(self.cfg)
        self.settings = SimpleNamespace(
            pi_brain=self.cfg, max_tool_rounds=7, tool_labels={"email_hybrid_search": "Email Search"},
        )
        self.patches = [
            mock.patch.object(pi_brain, "settings", self.settings),
            mock.patch("agent._build_memory_block", mock.AsyncMock(return_value="")),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.manager.shutdown()
        self.tmp.cleanup()

    def commands(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    async def turn(self, session, text, **kw):
        statuses = []

        async def status(t):
            statuses.append(t)
        out = []
        async for s in pi_brain.stream_pi_turn(session, text, source="voice", status_callback=status, **kw):
            out.append(s)
        return out, statuses

    async def test_plain_turn_yields_sentences_and_records_conversation(self):
        session = _Session(self.manager)
        out, _ = await self.turn(session, "hi")
        self.assertEqual("".join(out), "Hello there. How are you?")
        self.assertEqual(session.state.conversation.get_messages()[-1],
                         {"role": "assistant", "content": "Hello there. How are you?"})
        prompt = [c for c in self.commands() if c["type"] == "prompt"][0]["message"]
        self.assertTrue(prompt.startswith("[Channel: VOICE]"))
        self.assertTrue(prompt.endswith("hi"))

    async def test_tool_turn_discards_tool_message_text_and_records_tool(self):
        session = _Session(self.manager)
        out, statuses = await self.turn(session, "TOOL")
        self.assertEqual("".join(out), "Found three. The first is from Alice.")
        self.assertNotIn("Let me check", "".join(out))
        self.assertEqual(statuses, ["Email Search..."])
        call = session.state.history_session.tool_calls[0]
        self.assertEqual(call["tool_name"], "mcp__email__hybrid_search")
        self.assertEqual(call["server_name"], "email")
        self.assertEqual(call["arguments"], {"query": "x", "folder": "Inbox"})

    async def test_process_is_reused_and_seeded_only_when_fresh(self):
        session = _Session(self.manager)
        session.state.conversation.add_user("earlier question")
        session.state.conversation.add_assistant("earlier answer")
        await self.turn(session, "first")
        await self.turn(session, "second")
        prompts = [c["message"] for c in self.commands() if c["type"] == "prompt"]
        self.assertIn("Dave: earlier question", prompts[0])
        self.assertIn("Octavius: earlier answer", prompts[0])
        self.assertNotIn("[Earlier in this conversation", prompts[1])
        self.assertEqual(self.manager.starts, 1)

    async def test_round_cap_steers_then_aborts(self):
        session = _Session(self.manager)
        out, _ = await self.turn(session, "LOOP")
        self.assertEqual("".join(out), pi_brain.ROUND_CAP_REPLY)
        types = [c["type"] for c in self.commands()]
        self.assertIn("steer", types)
        self.assertIn("abort", types)
        # The process survives and takes the next turn.
        out, _ = await self.turn(session, "hi")
        self.assertEqual("".join(out), "Hello there. How are you?")

    async def test_closing_mid_turn_aborts_pi(self):
        session = _Session(self.manager)
        stream = pi_brain.stream_pi_turn(session, "SLOW", source="voice")
        task = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await stream.aclose()
        self.assertIn("abort", [c["type"] for c in self.commands()])
        out, _ = await self.turn(session, "hi")
        self.assertEqual("".join(out), "Hello there. How are you?")

    async def test_process_death_raises_and_next_turn_restarts(self):
        session = _Session(self.manager)
        with self.assertRaises(pi_brain.PiTurnError):
            await self.turn(session, "DIE")
        self.assertEqual(self.manager.failures, 1)
        out, _ = await self.turn(session, "hi")
        self.assertEqual("".join(out), "Hello there. How are you?")
        self.assertEqual(self.manager.starts, 2)

    async def test_provider_error_raises(self):
        session = _Session(self.manager)
        with self.assertRaisesRegex(pi_brain.PiTurnError, "502"):
            await self.turn(session, "ERROR")

    async def _confirm(self, answer):
        session = _Session(self.manager)
        out = []

        async def drive():
            async for s in pi_brain.stream_pi_turn(session, "CONFIRM", source="voice"):
                out.append(s)
        task = asyncio.create_task(drive())
        for _ in range(100):
            if session.state.pending_confirm is not None:
                break
            await asyncio.sleep(0.05)
        self.assertIsNotNone(session.state.pending_confirm, "no confirmation was asked")
        self.assertIn("create task: title 'Buy milk'; project id 1", out[0])
        self.assertIn({"type": "status", "text": "audio_done"}, session.sent)
        self.assertEqual(session.sent[0]["type"], "confirm_request")
        session.state.pending_confirm.set_result(pi_brain.is_yes(answer))
        await asyncio.wait_for(task, 10)
        return out[-1]

    async def test_confirm_yes(self):
        self.assertEqual(await self._confirm("yes please"), "Confirmed is true.")

    async def test_confirm_no(self):
        self.assertEqual(await self._confirm("actually no"), "Confirmed is false.")

    async def test_rekey_and_discard(self):
        session = _Session(self.manager, conv_id=1)
        await self.turn(session, "hi")
        proc = self.manager.processes[1]
        self.manager.rekey(1, 2)
        self.assertIs(self.manager.processes[2], proc)
        await self.manager.discard(2)
        self.assertFalse(proc.alive)
        self.assertEqual(self.manager.processes, {})

    async def test_reap_idle(self):
        session = _Session(self.manager)
        await self.turn(session, "hi")
        self.manager.processes[7].last_used -= 7200
        await self.manager.reap_idle()
        self.assertEqual(self.manager.processes, {})


class PiWarmTests(unittest.IsolatedAsyncioTestCase):
    """/warm triggers: fire-and-forget, reports into /health, never on a turn's path."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "commands.jsonl")
        self.argv_log = os.path.join(self.tmp.name, "argv.log")
        launcher = os.path.join(self.tmp.name, "pi-octavius")
        with open(launcher, "w") as f:
            f.write(f"#!/bin/sh\necho \"$@\" >> {self.argv_log}\nexec {sys.executable} {FAKE} \"$@\"\n")
        os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(os.environ, {"FAKE_PI_LOG": self.log})
        self.env.start()
        self.cfg = _cfg(launcher, warm_debounce_s=5.0, warm_retry_s=0.05)
        self.manager = pi_brain.PiProcessManager(self.cfg)
        self.patches = [
            mock.patch.object(pi_brain, "settings", SimpleNamespace(
                pi_brain=self.cfg, max_tool_rounds=7, tool_labels={})),
            mock.patch("agent._build_memory_block", mock.AsyncMock(return_value="")),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.manager.shutdown()
        self.env.stop()
        self.tmp.cleanup()

    def prompts(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line).get("message") for line in f if '"prompt"' in line]

    async def settle(self, pred=lambda: True, timeout=10.0):
        """Wait for background warm tasks, then for ``pred``."""
        end = asyncio.get_running_loop().time() + timeout
        while self.manager._warm_tasks or not pred():
            if asyncio.get_running_loop().time() > end:
                self.fail("background warm did not settle")
            await asyncio.sleep(0.02)

    async def test_warm_with_start_prestarts_process_and_records_result(self):
        self.manager.request_warm(7, start=True, reason="stt_start")
        await self.settle(lambda: self.manager.warm_stats.results.get("ok") == 1)
        self.assertEqual(self.prompts(), ["/warm"])
        h = self.manager.health()["warm"]
        self.assertEqual((h["requested"], h["sent"]), (1, 1))
        self.assertEqual(h["last"]["prompt_n"], 24)
        self.assertEqual(h["last"]["cache_n"], 10151)
        # The pre-started process takes the turn, and the turn still seeds history.
        session = _Session(self.manager)
        session.state.conversation.add_user("earlier question")
        session.state.conversation.add_assistant("earlier answer")
        out = [s async for s in pi_brain.stream_pi_turn(session, "hi", source="voice")]
        self.assertEqual("".join(out), "Hello there. How are you?")
        self.assertEqual(self.manager.starts, 1)
        self.assertIn("Dave: earlier question", self.prompts()[-1])

    async def test_warm_without_start_leaves_missing_process_alone(self):
        self.manager.request_warm(7, start=False, reason="restore_session")
        await self.settle()
        self.assertEqual(self.manager.processes, {})
        self.assertEqual(self.manager.warm_stats.skipped, 1)
        self.assertEqual(self.prompts(), [])

    async def test_debounced_per_conversation(self):
        self.manager.request_warm(7, start=True)
        self.manager.request_warm(7, start=True)
        self.manager.request_warm(8, start=False)
        await self.settle(lambda: self.manager.warm_stats.sent == 1)
        self.assertEqual(self.manager.warm_stats.debounced, 1)
        self.assertEqual(self.prompts().count("/warm"), 1)

    async def test_disabled_does_nothing(self):
        import dataclasses
        self.manager.cfg = dataclasses.replace(self.cfg, warm=False)
        self.manager.request_warm(7, start=True)
        self.manager.warm_static_prefix()
        await self.settle()
        self.assertEqual(self.manager.processes, {})
        self.assertEqual(self.manager.health()["warm"], {"enabled": False})

    async def test_concurrent_warm_and_turn_start_one_process(self):
        self.manager.request_warm(7, start=True)
        session = _Session(self.manager)
        out = [s async for s in pi_brain.stream_pi_turn(session, "hi", source="voice")]
        await self.settle()
        self.assertEqual("".join(out), "Hello there. How are you?")
        self.assertEqual(self.manager.starts, 1)

    async def test_warm_skipped_while_turn_holds_the_process(self):
        proc, _ = await self.manager.get(7)
        async with proc.lock:
            self.manager.request_warm(7, start=True)
            await self.settle()
        self.assertEqual(self.manager.warm_stats.skipped, 1)
        self.assertEqual(self.prompts(), [])

    async def test_unhandled_warm_is_aborted_and_counted(self):
        os.environ["FAKE_PI_WARM"] = "unsupported"
        self.manager.request_warm(7, start=True)
        await self.settle(lambda: self.manager.warm_stats.unsupported == 1)
        with open(self.log) as f:
            types = [json.loads(line)["type"] for line in f]
        self.assertIn("abort", types)
        self.assertIn("not handled", self.manager.health()["warm"]["last_error"])
        # The process is still good for a real turn.
        out = [s async for s in pi_brain.stream_pi_turn(_Session(self.manager), "hi", source="voice")]
        self.assertTrue(out)

    async def test_static_prefix_uses_a_short_lived_no_session_process(self):
        self.manager.warm_static_prefix("startup")
        await self.settle(lambda: self.manager.warm_stats.results.get("ok") == 1)
        self.assertEqual(self.manager.processes, {})
        with open(self.argv_log) as f:
            self.assertIn("--no-session", f.read())
        self.assertEqual(self.manager.warm_stats.sent, 1)

    async def test_static_prefix_prefers_a_live_process(self):
        await self.manager.get(7)
        self.manager.warm_static_prefix("recovery")
        await self.settle(lambda: self.manager.warm_stats.results.get("ok") == 1)
        self.assertEqual(self.manager.starts, 1)
        with open(self.argv_log) as f:
            self.assertNotIn("--no-session", f.read())

    async def test_failed_warm_schedules_recovery_until_success(self):
        os.environ["FAKE_PI_WARM_RESULT"] = "error"
        self.manager.request_warm(7, start=True)
        await self.settle(lambda: self.manager.warm_stats.recovery_attempts >= 1)
        # The fake reads its env at spawn: drop the erroring process, so recovery's
        # short-lived process is the one that answers "ok".
        os.environ["FAKE_PI_WARM_RESULT"] = "ok"
        await self.manager.discard(7)
        await self.settle(lambda: self.manager.warm_stats.results.get("ok", 0) >= 1)
        await asyncio.wait_for(self.manager._recovery, 5)
        self.assertGreaterEqual(self.manager.warm_stats.results["error"], 1)

    async def test_turn_failure_schedules_recovery(self):
        with self.assertRaises(pi_brain.PiTurnError):
            async for _ in pi_brain.stream_pi_turn(_Session(self.manager), "ERROR", source="voice"):
                pass
        self.assertIsNotNone(self.manager._recovery)
        await self.settle(lambda: self.manager.warm_stats.recovery_attempts >= 1)

    async def test_warm_report_during_turn_is_not_an_event(self):
        proc, _ = await self.manager.get(7)
        events = proc.subscribe()
        proc._warm_report(json.dumps({"event": "wait", "waited_ms": 1200, "capped": False}))
        self.assertTrue(events.empty())
        self.assertEqual(self.manager.health()["warm"]["last_wait_ms"], 1200)


PRIMARY = "fake/model"
HOP2 = "lrv-p01/qwen3.8-27b"
HOP3 = "wh-vllm/gemma4-12b"


class PiFailoverTests(unittest.IsolatedAsyncioTestCase):
    """Failover over RPC: classification, the silence watchdog, resume, the shared breaker."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "commands.jsonl")
        self.fail_file = os.path.join(self.tmp.name, "fail.json")
        launcher = os.path.join(self.tmp.name, "pi-octavius")
        with open(launcher, "w") as f:
            f.write(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
        os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(os.environ, {"FAKE_PI_LOG": self.log, "FAKE_PI_FAIL_FILE": self.fail_file})
        self.env.start()
        self.cfg = _cfg(launcher, fallback_models=(HOP2, HOP3), failover_cooldown_s=300.0,
                        silence_timeout_s=0.5, first_output_timeout_s=0.5, warm_debounce_s=0.0)
        self.manager = pi_brain.PiProcessManager(self.cfg)
        self.patches = [
            mock.patch.object(pi_brain, "settings", SimpleNamespace(
                pi_brain=self.cfg, max_tool_rounds=7, tool_labels={})),
            mock.patch("agent._build_memory_block", mock.AsyncMock(return_value="")),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.manager.shutdown()
        self.env.stop()
        self.tmp.cleanup()

    def fail(self, **models):
        with open(self.fail_file, "w") as f:
            json.dump(models, f)

    def records(self, type_=None):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            rows = [json.loads(line) for line in f]
        return [r for r in rows if type_ is None or r["type"] == type_]

    def runs(self):
        return [(r["model"], r["script"]) for r in self.records("_run")]

    async def turn(self, text, session=None, **kw):
        session = session or _Session(self.manager)
        statuses = []

        async def status(t):
            statuses.append(t)
        out = []
        async for s in pi_brain.stream_pi_turn(session, text, source="voice", status_callback=status, **kw):
            out.append(s)
        return "".join(out), statuses, session

    # --- classification -----------------------------------------------------------

    def test_classify_provider_error(self):
        c = pi_brain.classify_provider_error
        self.assertEqual(c('500: {"message":"Internal Server Error"}'), "server_error")
        self.assertEqual(c("503: busy"), "server_error")
        self.assertEqual(c("429: slow down"), "server_error")
        self.assertEqual(c('401: {"message":"Invalid API key"}'), "auth")
        self.assertEqual(c("403: no"), "auth")
        self.assertEqual(c("404: model not found"), "client_error")
        self.assertEqual(c("400: model does not exist"), "client_error")
        self.assertEqual(c("Connection error."), "connect")
        self.assertEqual(c("fetch failed"), "connect")
        self.assertEqual(c("Request timed out."), "timeout")
        self.assertEqual(c("something odd"), "bad_response")

    # --- each failure class moves the turn to the next hop --------------------------

    async def _fails_over(self, primary_fault, kind, hang=False):
        self.fail(**{PRIMARY: primary_fault})
        t0 = time.monotonic()
        text, statuses, _ = await self.turn("hi")
        self.assertEqual(text, f"Resumed on {HOP2}. All good.")
        self.assertIn("Trying another model...", statuses)
        self.assertEqual(self.runs(), [(PRIMARY, "hi"), (HOP2, "RESUME")])
        # Dave's message is sent once; the second prompt is the resume note.
        prompts = [r["message"] for r in self.records("prompt")]
        self.assertEqual(len(prompts), 2)
        self.assertTrue(prompts[0].endswith("hi"))
        self.assertTrue(prompts[1].startswith(pi_brain.RESUME_MESSAGE[:30]))
        self.assertIn({"type": "set_model", "provider": "lrv-p01", "modelId": "qwen3.8-27b"},
                      [{k: v for k, v in r.items() if k != "id"} for r in self.records("set_model")])
        health = self.manager.failover.health()
        self.assertEqual(health["current"], HOP2)
        self.assertEqual(health["failures_by_kind"], {kind: 1})
        self.assertEqual(health["last_failure"]["hop"], PRIMARY)
        self.assertEqual(self.manager.failures, 0)  # the turn succeeded
        if hang:
            self.assertGreaterEqual(time.monotonic() - t0, 0.5)
            self.assertTrue(self.records("abort"))  # the hung request was cancelled

    async def test_5xx_fails_over(self):
        await self._fails_over("error:500: Internal Server Error", "server_error")

    async def test_auth_fails_over(self):
        await self._fails_over("error:401: Invalid API key", "auth")

    async def test_missing_alias_fails_over(self):
        await self._fails_over("error:404: model not found", "client_error")

    async def test_connect_failure_fails_over(self):
        await self._fails_over("error:Connection error.", "connect")

    async def test_zombie_that_never_generates_fails_over_after_the_silence_timeout(self):
        await self._fails_over("hang", "timeout", hang=True)

    async def test_stall_after_a_thinking_delta_fails_over(self):
        await self._fails_over("stall", "timeout", hang=True)

    async def test_set_model_refusal_skips_the_hop(self):
        self.fail(**{PRIMARY: "error:500: x", "_unknown": [HOP2]})
        text, _, _ = await self.turn("hi")
        self.assertEqual(text, f"Resumed on {HOP3}. All good.")
        self.assertEqual(self.manager.failover.health()["failures_by_kind"], {"server_error": 1, "client_error": 1})
        # Nothing was sent to the hop that does not exist, and the prompt went out once per sent hop.
        self.assertEqual(self.runs(), [(PRIMARY, "hi"), (HOP3, "RESUME")])

    async def test_every_hop_failing_raises_and_counts_one_failure(self):
        self.fail(**{PRIMARY: "error:500: a", HOP2: "error:401: b", HOP3: "error:Connection error."})
        with self.assertRaisesRegex(pi_brain.PiTurnError, "every model failed"):
            await self.turn("hi")
        self.assertEqual(self.manager.failures, 1)
        health = self.manager.failover.health()
        # Exhausted: the next turn probes from the top, not on the smallest hop.
        self.assertEqual(health["current"], PRIMARY)
        self.assertEqual(health["failures_by_kind"], {"server_error": 1, "auth": 1, "connect": 1})

    # --- tools -----------------------------------------------------------------------

    async def test_failure_after_a_tool_resumes_without_rerunning_it(self):
        self.fail(**{PRIMARY: "tool_then_error:500: gone"})
        session = _Session(self.manager)
        text, statuses, _ = await self.turn("TOOL", session)
        self.assertEqual(text, f"Resumed on {HOP2}. All good. Tools were kept.")
        self.assertEqual(self.runs(), [(PRIMARY, "TOOL"), (HOP2, "RESUME_TOOLS")])
        self.assertEqual(statuses.count("Email Search...") + statuses.count("hybrid_search..."), 1)
        self.assertEqual(len(session.state.history_session.tool_calls), 1)
        prompts = [r["message"] for r in self.records("prompt")]
        self.assertEqual(prompts[1], pi_brain.RESUME_AFTER_TOOLS_MESSAGE)

    # --- the watchdog ------------------------------------------------------------------

    async def test_thinking_deltas_are_progress(self):
        text, _, _ = await self.turn("THINK")  # 1.2 s of reasoning, 0.5 s silence limit
        self.assertEqual(text, "Done thinking.")
        self.assertEqual(self.manager.failover.health()["failovers"], 0)

    async def test_a_running_tool_is_not_silence(self):
        text, _, _ = await self.turn("SLOWTOOL")  # the tool takes 1.0 s
        self.assertEqual(text, "The slow tool finished.")
        self.assertEqual(self.manager.failover.health()["failovers"], 0)
        self.assertEqual(self.records("abort"), [])

    # --- the shared breaker --------------------------------------------------------------

    async def test_new_processes_start_on_the_current_hop(self):
        self.fail(**{PRIMARY: "error:500: x"})
        await self.turn("hi", _Session(self.manager, conv_id=7))
        self.fail()  # the primary is "back", but the breaker has not cooled down
        text, _, _ = await self.turn("hi", _Session(self.manager, conv_id=8))
        self.assertEqual(text, "Hello there. How are you?")
        self.assertEqual(self.runs()[-1], (HOP2, "hi"))
        # Started on the hop through the environment, so there was no set_model for it.
        self.assertEqual(self.manager._new_process().env["PI_OCTAVIUS_MODEL"], HOP2)
        self.assertEqual(len([r for r in self.records("set_model") if r["modelId"] == "qwen3.8-27b"]), 1)

    async def test_live_processes_switch_before_their_next_turn(self):
        await self.turn("hi", _Session(self.manager, conv_id=9))  # a process on the primary
        self.fail(**{PRIMARY: "error:500: x"})
        await self.turn("hi", _Session(self.manager, conv_id=7))  # moves the shared hop
        self.fail()
        before = len(self.records("set_model"))
        await self.turn("again", _Session(self.manager, conv_id=9))
        sets = self.records("set_model")
        self.assertEqual(len(sets), before + 1)
        self.assertEqual(sets[-1]["modelId"], "qwen3.8-27b")
        self.assertEqual(self.runs()[-1], (HOP2, "again"))

    async def test_primary_is_tried_again_after_the_cooldown(self):
        self.manager.failover.cooldown_s = 0.3
        self.fail(**{PRIMARY: "error:500: x"})
        await self.turn("hi")
        self.assertEqual(self.manager.failover.health()["current"], HOP2)
        self.assertGreater(self.manager.failover.health()["cooldown_remaining"], 0)
        self.fail()
        await asyncio.sleep(0.4)
        text, _, _ = await self.turn("hi")
        self.assertEqual(self.runs()[-1], (PRIMARY, "hi"))
        self.assertEqual(text, "Hello there. How are you?")
        health = self.manager.failover.health()
        self.assertEqual((health["current"], health["returns_to_primary"], health["cooldown_remaining"]), (PRIMARY, 1, 0.0))

    async def test_primary_still_down_after_the_cooldown_fails_over_again(self):
        self.manager.failover.cooldown_s = 0.3
        self.fail(**{PRIMARY: "error:Connection error."})
        await self.turn("hi")
        await asyncio.sleep(0.4)
        text, _, _ = await self.turn("hi")
        self.assertEqual(text, f"Resumed on {HOP2}. All good.")
        health = self.manager.failover.health()
        self.assertEqual((health["failovers"], health["returns_to_primary"], health["current"]), (2, 1, HOP2))

    async def test_concurrent_conversations_do_not_regress_the_hop(self):
        state = self.manager.failover
        self.assertEqual(state.fail(0, "connect", "x"), 1)
        self.assertEqual(state.fail(0, "connect", "x"), 1)  # a second conversation hit hop 0 too
        self.assertEqual((state.failovers, state.index), (1, 1))

    # --- cancellation, overflow, budgets, repeated speech, generations -------------------------

    def reconfigure(self, **over):
        import dataclasses
        self.cfg = dataclasses.replace(self.cfg, **over)
        self.manager.cfg = self.cfg
        pi_brain.settings.pi_brain = self.cfg

    async def _until(self, pred, timeout=5.0):
        end = time.monotonic() + timeout
        while not pred() and time.monotonic() < end:
            await asyncio.sleep(0.02)
        self.assertTrue(pred())

    async def test_cancel_during_the_prompt_ack_still_aborts_pi(self):
        self.fail(_ack_delay={PRIMARY: 1.0})
        task = asyncio.create_task(self.turn("hi"))
        await self._until(lambda: len(self.records("prompt")) == 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.records("abort"))

    async def test_cancel_during_the_ack_of_a_failover_attempt_aborts_pi(self):
        self.fail(**{PRIMARY: "error:500: x", "_ack_delay": {HOP2: 1.0}})
        task = asyncio.create_task(self.turn("hi"))
        await self._until(lambda: len(self.records("prompt")) == 2)
        self.assertEqual(self.records("abort"), [])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.records("abort"))

    def test_context_overflow_is_classified_not_blamed_on_the_hop(self):
        c = pi_brain.classify_provider_error
        for msg in (
            '400: {"message":"This model\'s maximum context length is 65536 tokens. However, you requested 70000"}',
            '400: {"error":{"code":400,"message":"the request exceeds the available context size, try increasing it",'
            '"type":"exceed_context_size_error"}}',
            "400: prompt is too long", "400: too many tokens",
        ):
            self.assertEqual(c(msg), "context_overflow", msg)
        self.assertEqual(c("404: model not found"), "client_error")
        self.assertEqual(c("400: Invalid schema for function"), "client_error")

    async def test_context_overflow_moves_this_turn_to_a_bigger_window_only(self):
        self.reconfigure(model_contexts={PRIMARY: 1000, HOP2: 5000, HOP3: 500})
        self.fail(**{PRIMARY: "error:400: maximum context length is 1000 tokens"})
        text, _, _ = await self.turn("hi")
        self.assertEqual(text, f"Resumed on {HOP2}. All good.")  # HOP3 (500) is smaller: never chosen
        health = self.manager.failover.health()
        self.assertEqual((health["current"], health["failovers"], health["failures_by_kind"]), (PRIMARY, 0, {}))
        self.assertEqual(health["context_overflows"], 1)
        self.fail()
        text, _, _ = await self.turn("hi")  # the next turn starts on the primary again
        self.assertEqual(self.runs()[-1], (PRIMARY, "hi"))

    async def test_context_overflow_with_no_bigger_hop_says_so_and_ends_the_turn(self):
        self.reconfigure(model_contexts={PRIMARY: 1000, HOP2: 500, HOP3: 500})
        self.fail(**{PRIMARY: "error:400: exceeds the available context size"})
        text, _, session = await self.turn("hi")
        self.assertEqual(text, pi_brain.CONTEXT_OVERFLOW_REPLY)
        self.assertEqual(self.manager.failures, 0)
        self.assertEqual(self.manager.failover.health()["failovers"], 0)
        self.assertEqual(self.runs(), [(PRIMARY, "hi")])

    async def test_a_slow_first_output_gets_the_larger_budget(self):
        self.reconfigure(first_output_timeout_s=3.0)  # silence limit stays 0.5 s
        text, _, _ = await self.turn("LATE")  # first output after 1.0 s
        self.assertEqual(text, "Late but fine.")
        self.assertEqual(self.manager.failover.health()["failovers"], 0)

    async def test_silence_limit_applies_once_output_has_started(self):
        self.reconfigure(first_output_timeout_s=3.0)
        self.fail(**{PRIMARY: "stall"})  # one thinking delta, then nothing
        t0 = time.monotonic()
        await self.turn("hi")
        self.assertLess(time.monotonic() - t0, 2.0)  # 0.5 s of silence, not the 3 s budget
        self.assertEqual(self.manager.failover.health()["failures_by_kind"], {"timeout": 1})

    async def test_hang_with_no_output_waits_the_first_output_budget(self):
        self.reconfigure(first_output_timeout_s=1.2)
        self.fail(**{PRIMARY: "hang"})
        t0 = time.monotonic()
        await self.turn("hi")
        self.assertGreaterEqual(time.monotonic() - t0, 1.2)

    async def test_failover_after_speech_does_not_repeat_it(self):
        text, _, _ = await self.turn("SPEAKSTALL")
        self.assertEqual(text.count("First part."), 1)
        self.assertIn(f"Resumed on {HOP2}", text)
        note = [r["message"] for r in self.records("prompt")][1]
        self.assertIn("First part.", note)
        self.assertIn("without repeating", note)

    def test_stale_failure_does_not_move_the_shared_hop(self):
        st = pi_brain.FailoverState(hops=(PRIMARY, HOP2, HOP3), cooldown_s=300.0)
        gen = st.generation
        self.assertEqual(st.fail(0, "connect", "x", gen), 1)
        slow_gen = st.generation  # a slow attempt starts on HOP2 here
        st.since -= 1000  # the cooldown passes: back on the primary
        self.assertEqual(st.current(), 0)
        self.assertGreater(st.generation, slow_gen)
        self.assertEqual(st.fail(1, "timeout", "slow", slow_gen), 2)  # its own chain moves on
        self.assertEqual((st.index, st.failovers, st.stale_failures), (0, 1, 1))
        self.assertEqual(st.by_kind, {"connect": 1, "timeout": 1})  # stats still recorded

    # --- second review round -------------------------------------------------------------------

    async def test_empty_length_answer_tries_the_next_hop_without_moving_the_breaker(self):
        self.fail(**{PRIMARY: "empty_length"})
        text, _, _ = await self.turn("hi")
        self.assertEqual(text, f"Resumed on {HOP2}. All good.")
        health = self.manager.failover.health()
        self.assertEqual((health["current"], health["failovers"], health["failures_by_kind"]), (PRIMARY, 0, {}))
        self.assertEqual(health["turn_local"], {"empty_output": 1})
        self.assertNotIn("I'm not sure", text)

    async def test_empty_length_on_every_hop_ends_with_the_fallback_sentence(self):
        self.fail(**{PRIMARY: "empty_length", HOP2: "empty_length", HOP3: "empty_length"})
        text, _, _ = await self.turn("hi")
        self.assertEqual(text, "I'm not sure how to respond to that.")
        self.assertEqual(self.manager.failover.health()["failovers"], 0)

    async def test_exhausted_chain_resets_to_the_primary(self):
        st = pi_brain.FailoverState(hops=(PRIMARY, HOP2, HOP3), cooldown_s=300.0)
        st.fail(0, "connect", "x")
        st.fail(1, "connect", "x")
        gen = st.generation
        self.assertIsNone(st.fail(2, "connect", "x"))
        self.assertEqual(st.index, 0)
        self.assertGreater(st.generation, gen)

    async def test_provider_error_after_speech_ends_the_turn_and_records_the_hop(self):
        text, _, _ = await self.turn("SPEAKERR")
        self.assertEqual(text, "Said it.")
        health = self.manager.failover.health()
        self.assertEqual((health["current"], health["failures_by_kind"]), (HOP2, {"server_error": 1}))
        self.assertEqual(self.runs(), [(PRIMARY, "SPEAKERR")])  # no resume: it had spoken

    async def test_a_started_tool_counts_as_run_for_the_resume_note(self):
        self.fail(**{PRIMARY: "toolstart_then_error:500: x"})
        await self.turn("TOOL")
        prompts = [r["message"] for r in self.records("prompt")]
        self.assertEqual(prompts[1], pi_brain.RESUME_AFTER_TOOLS_MESSAGE)

    async def test_hung_tool_is_cut_off_with_an_apology_not_a_failover(self):
        self.reconfigure(tool_timeout_s=0.5)
        self.fail(**{PRIMARY: "toolhang"})
        session = _Session(self.manager)
        text, _, _ = await self.turn("TOOL", session)
        self.assertEqual(text, pi_brain.TOOL_TIMEOUT_REPLY)
        health = self.manager.failover.health()
        self.assertEqual((health["failovers"], health["failures_by_kind"], health["current"]), (0, {}, PRIMARY))
        self.assertEqual(self.manager.failures, 1)
        self.assertTrue(self.records("abort"))
        # Events drained while stopping were handled: the tool is recorded, and the dialog
        # that appeared during the abort was cancelled so it cannot block settling.
        self.assertEqual(len(session.state.history_session.tool_calls), 1)
        replies = [r for r in self.records("extension_ui_response") if r["id"] == "zz"]
        self.assertTrue(replies and replies[0]["cancelled"] is True)
        # and the process is still usable
        text, _, _ = await self.turn("hi", session)
        self.assertEqual(text, "Hello there. How are you?")

    async def test_slow_tool_inside_its_budget_is_not_cut_off(self):
        self.reconfigure(tool_timeout_s=3.0)
        text, _, _ = await self.turn("SLOWTOOL")
        self.assertEqual(text, "The slow tool finished.")

    async def test_cancel_while_a_confirm_is_pending_cancels_the_dialog(self):
        session = _Session(self.manager)
        task = asyncio.create_task(self.turn("CONFIRM", session))
        await self._until(lambda: any(p.get("type") == "confirm_request" for p in session.sent))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        replies = [r for r in self.records("extension_ui_response") if r["id"] == "u1"]
        self.assertTrue(replies and replies[0]["cancelled"] is True)
        self.assertIsNone(session.state.pending_confirm)

    async def test_health_does_not_trigger_the_return_to_the_primary(self):
        self.manager.failover.cooldown_s = 0.1
        self.manager.failover.fail(0, "connect", "x")
        await asyncio.sleep(0.2)
        for _ in range(3):
            health = self.manager.health()["failover"]
        self.assertEqual((health["current"], health["returns_to_primary"], health["cooldown_remaining"]), (HOP2, 0, 0.0))
        self.assertEqual(self.manager.failover.current(), 0)  # the next turn does it
        self.assertEqual(self.manager.failover.returns, 1)

    # --- housekeeping ----------------------------------------------------------------------

    async def test_auto_retry_is_off_before_the_first_prompt(self):
        await self.turn("hi")
        types = [r["type"] for r in self.records() if r["type"] in ("set_auto_retry", "prompt")]
        self.assertEqual(types, ["set_auto_retry", "prompt"])
        self.assertIs(self.records("set_auto_retry")[0]["enabled"], False)

    async def test_warm_targets_the_current_hop(self):
        await self.turn("hi")  # idle process on the primary
        self.manager.failover.fail(0, "connect", "x")
        self.manager.request_warm(7, reason="test")
        end = time.monotonic() + 5
        while not self.manager.warm_stats.sent and time.monotonic() < end:
            await asyncio.sleep(0.02)
        order = [r["type"] + ":" + str(r.get("modelId") or r.get("message")) for r in self.records()
                 if r["type"] in ("set_model", "prompt")][-2:]
        self.assertEqual(order, ["set_model:qwen3.8-27b", "prompt:/warm"])
        # and a short-lived warm process starts on the hop too
        self.assertEqual(self.manager._new_process(["--no-session"]).model, HOP2)

    async def test_health_has_a_failover_block(self):
        health = self.manager.health()["failover"]
        self.assertEqual(health["hops"], [PRIMARY, HOP2, HOP3])
        self.assertFalse(health["failed_over"])
        self.assertIsNone(health["last_failure"])


class WarmTriggerTests(unittest.IsolatedAsyncioTestCase):
    """websocket_session's triggers: which events warm, with which ``start``."""

    def handler(self, has_images=False):
        from websocket_session import WebSocketSessionHandler
        calls = []
        recorder = SimpleNamespace(
            request_warm=lambda conv_id, start=False, reason="": calls.append((conv_id, start, reason)),
            rekey=lambda old, new: None,
            discard=mock.AsyncMock(),
        )
        ws = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pi_brain=recorder, history=None, mcp_manager=None, subagent_dispatcher=None)))
        h = WebSocketSessionHandler(ws)
        h.state.history_session = SimpleNamespace(conv_id=5)
        h.state.conversation.has_images = has_images
        return h, calls

    async def test_stt_start_and_typing_start_warm_with_start(self):
        h, calls = self.handler()
        with mock.patch("vad.SileroVAD", return_value=SimpleNamespace(reset=lambda: None)):
            await h.handle_stt_start({})
        await h.handle_text_message(json.dumps({"type": "typing_start"}))
        self.assertEqual(calls, [(5, True, "stt_start"), (5, True, "typing_start")])

    async def test_vision_thread_is_not_warmed(self):
        h, calls = self.handler(has_images=True)
        await h.handle_typing_start({})
        self.assertEqual(calls, [])

    async def test_native_brain_and_errors_are_silent(self):
        from websocket_session import WebSocketSessionHandler
        h = WebSocketSessionHandler(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(history=None, mcp_manager=None, subagent_dispatcher=None))))
        h.state.history_session = SimpleNamespace(conv_id=5)
        await h.handle_typing_start({})  # no pi_brain: no-op
        h2, _ = self.handler()
        h2.ws.app.state.pi_brain.request_warm = mock.Mock(side_effect=RuntimeError("boom"))
        await h2.handle_typing_start({})  # swallowed


class PiBrainHelperTests(unittest.TestCase):
    def test_is_yes(self):
        for t in ("yes", "Yeah, do it.", "OK", "go ahead"):
            self.assertTrue(pi_brain.is_yes(t), t)
        for t in ("no", "nope", "yesterday was fine", "", "wait", "yes, but wait",
                  "ok don't", "sure, not that one", "yes and also delete the rest",
                  "okay cancel", "yes no"):
            self.assertFalse(pi_brain.is_yes(t), t)

    def test_describe_confirm_reads_every_argument(self):
        d = pi_brain._describe_confirm(
            "Allow update_task?", json.dumps({"task_id": 42, "done": True, "title": "x" * 200}))
        self.assertTrue(d.startswith("update task:"))
        self.assertIn("task id 42", d)
        self.assertIn("done true", d)
        self.assertIn("(200 characters)", d)
        self.assertIn("check the screen", pi_brain._describe_confirm("Allow create_task?", "not json"))

    def test_tool_label(self):
        labels = {"email_get": "Reading Email", "search_tasks": "Task Search"}
        with mock.patch.object(pi_brain, "settings", SimpleNamespace(tool_labels=labels)):
            self.assertEqual(pi_brain.tool_label("mcp__email__get"), "Reading Email")
            self.assertEqual(pi_brain.tool_label("mcp__vikunja__search_tasks"), "Task Search")
            self.assertEqual(pi_brain.tool_label("mcp__vikunja__create_task"), "create_task")
            self.assertEqual(pi_brain.tool_label("web_search"), "web_search")
            self.assertEqual(pi_brain.tool_label("subagent"), "Researching")

    def test_build_seed_caps(self):
        msgs = [{"role": "user", "content": "x" * 2000}] * 30
        seed = pi_brain.build_seed(msgs)
        self.assertLessEqual(len(seed), pi_brain.SEED_TOTAL_CHARS + 200)
        self.assertEqual(pi_brain.build_seed([{"role": "system", "content": "s"}]), "")

    def test_build_prompt_keeps_text_last(self):
        p = pi_brain.build_prompt("hello", "[Channel: TEXT] x", memory="facts")
        self.assertTrue(p.startswith("[Channel: TEXT]"))
        self.assertTrue(p.endswith("Dave's message:\nhello"))
        self.assertEqual(pi_brain.build_prompt("hello", "S"), "S\n\nhello")

    def test_child_env_drops_octavius_vars(self):
        with mock.patch.dict(os.environ, {"OCTAVIUS_LR_API_KEY": "k", "HOME": "/h"}):
            env = pi_brain.child_env(_cfg("/x"))
        self.assertNotIn("OCTAVIUS_LR_API_KEY", env)
        self.assertEqual(env["PI_OCTAVIUS_MODEL"], "fake/model")

    def test_wants_pi(self):
        conv = Conversation()
        on = SimpleNamespace(pi_brain=_cfg("/x"))
        off = SimpleNamespace(pi_brain=_cfg("/x", brain="native"))
        with mock.patch.object(pi_brain, "settings", on):
            self.assertTrue(pi_brain.wants_pi("voice", None, conv))
            self.assertTrue(pi_brain.wants_pi("text", None, conv))
            self.assertFalse(pi_brain.wants_pi("image", [{"type": "text"}], conv))
            self.assertFalse(pi_brain.wants_pi("file", None, conv))
            conv.has_images = True
            self.assertFalse(pi_brain.wants_pi("voice", None, conv))
        with mock.patch.object(pi_brain, "settings", off):
            self.assertFalse(pi_brain.wants_pi("voice", None, Conversation()))


if __name__ == "__main__":
    unittest.main()
