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
        self.assertIn("create task, 'Buy milk'", out[0])
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


class PiBrainHelperTests(unittest.TestCase):
    def test_is_yes(self):
        for t in ("yes", "Yeah, do it.", "OK", "go ahead"):
            self.assertTrue(pi_brain.is_yes(t), t)
        for t in ("no", "nope", "yesterday was fine", "", "wait"):
            self.assertFalse(pi_brain.is_yes(t), t)

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
