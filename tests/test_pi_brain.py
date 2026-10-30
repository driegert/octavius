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
