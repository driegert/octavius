"""pi_tools_server: Octavius's own tools over a loopback MCP server for the pi brain."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

import pi_brain  # noqa: E402
import pi_tools_server as pts  # noqa: E402
from local_tool_specs import TOOLS  # noqa: E402

EXPECTED = [
    "save_note", "read_document", "read_note", "edit_note", "commit_edit",
    "list_reader_documents", "append_to_reader_document",
    "search_conversation_history", "read_conversation",
    "remember", "forget", "correct", "what_do_you_know",
]


async def _server():
    server = pts.PiToolsServer(port=0)
    await server.start()
    return server


def _url(server):
    return f"http://127.0.0.1:{server.port}/mcp"


class Session:
    def __init__(self, server, token):
        self.url = _url(server)
        self.headers = {"Authorization": f"Bearer {token}"}

    async def __aenter__(self):
        self._cm = streamablehttp_client(self.url, headers=self.headers)
        r, w, _ = await self._cm.__aenter__()
        self._s = ClientSession(r, w)
        await self._s.__aenter__()
        await self._s.initialize()
        return self._s

    async def __aexit__(self, *exc):
        await self._s.__aexit__(*exc)
        await self._cm.__aexit__(*exc)


class ToolListTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await _server()
        self.addAsyncCleanup(self.server.stop)

    def test_exposed_set_is_the_thirteen(self):
        self.assertEqual(sorted(pts.EXPOSED_TOOLS), sorted(EXPECTED))
        self.assertEqual(len(pts.exposed_specs()), 13)

    async def test_list_equals_specs_in_order(self):
        token = self.server.new_token()
        async with Session(self.server, token) as s:
            listed = (await s.list_tools()).tools
        want = [t["function"] for t in TOOLS if t["function"]["name"] in pts.EXPOSED_TOOLS]
        self.assertEqual([t.name for t in listed], [f["name"] for f in want])
        for t, f in zip(listed, want):
            self.assertEqual(t.description, f["description"])
            self.assertEqual(t.inputSchema, f["parameters"])

    async def test_list_identical_across_tokens(self):
        a, b = self.server.new_token(), self.server.new_token()
        async with Session(self.server, a) as s1:
            l1 = (await s1.list_tools()).model_dump_json()
        async with Session(self.server, b) as s2:
            l2 = (await s2.list_tools()).model_dump_json()
        self.assertEqual(l1, l2)


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await _server()
        self.addAsyncCleanup(self.server.stop)
        self.token = self.server.new_token()

    async def _post(self, headers):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        h = {"Accept": "application/json, text/event-stream", **headers}
        async with httpx.AsyncClient() as c:
            return await c.post(_url(self.server), json=body, headers=h)

    async def test_no_token(self):
        self.assertEqual((await self._post({})).status_code, 401)

    async def test_wrong_token(self):
        r = await self._post({"Authorization": "Bearer nope"})
        self.assertEqual(r.status_code, 401)

    async def test_non_bearer_scheme(self):
        r = await self._post({"Authorization": f"Basic {self.token}"})
        self.assertEqual(r.status_code, 401)

    async def test_right_token(self):
        r = await self._post({"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r.status_code, 200)

    async def test_revoked_token(self):
        self.server.revoke(self.token)
        r = await self._post({"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r.status_code, 401)

    async def test_other_path_404(self):
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{self.server.port}/health")
        self.assertEqual(r.status_code, 404)

    async def test_listens_on_loopback_only(self):
        self.assertEqual(self.server._sock.getsockname()[0], "127.0.0.1")
        with self.assertRaises(ValueError):
            await pts.PiToolsServer(host="0.0.0.0", port=0).start()


class CallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await _server()
        self.addAsyncCleanup(self.server.stop)
        self.token = self.server.new_token()

    async def test_call_without_active_turn_is_a_tool_error(self):
        with mock.patch("tools.call_tool", new=mock.AsyncMock()) as handler:
            async with Session(self.server, self.token) as s:
                r = await s.call_tool("remember", {"fact": "x"})
        self.assertTrue(r.isError)
        self.assertIn("No active conversation turn", r.content[0].text)
        handler.assert_not_awaited()

    async def test_call_during_turn_reaches_handler_with_bound_context(self):
        ctx = pts.ToolContext(history_session="H", mcp_manager="M", session="S")
        unbind = self.server.bind(self.token, ctx)
        with mock.patch("tools.call_tool", new=mock.AsyncMock(return_value="ok!")) as handler:
            async with Session(self.server, self.token) as s:
                r = await s.call_tool("read_note", {"path": "a.md"})
        self.assertFalse(r.isError, r.content)
        self.assertEqual(r.content[0].text, "ok!")
        handler.assert_awaited_once_with(
            "read_note", {"path": "a.md"}, history_session="H", mcp_manager="M", session="S",
        )
        unbind()

    async def test_binding_is_per_token(self):
        other = self.server.new_token()
        self.server.bind(self.token, pts.ToolContext("H", "M", "S"))
        with mock.patch("tools.call_tool", new=mock.AsyncMock(return_value="x")):
            async with Session(self.server, other) as s:
                r = await s.call_tool("forget", {})
        self.assertTrue(r.isError)

    async def test_unexposed_tool_is_refused(self):
        self.server.bind(self.token, pts.ToolContext("H", "M", "S"))
        with mock.patch("tools.call_tool", new=mock.AsyncMock()) as handler:
            async with Session(self.server, self.token) as s:
                r = await s.call_tool("consult_specialist", {"domain": "email", "task": "x"})
        self.assertTrue(r.isError)
        handler.assert_not_awaited()

    async def test_unbind_removes_binding(self):
        unbind = self.server.bind(self.token, pts.ToolContext("H", "M", "S"))
        unbind()
        with self.assertRaises(pts.NoActiveTurn):
            await self.server.dispatch(self.token, "remember", {})


class BindingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """stream_pi_turn binds for the turn and unbinds in finally."""

    def _manager(self, server):
        cfg = SimpleNamespace(launcher="x", pi_bin="pi", model="m", warm=False)
        mgr = pi_brain.PiProcessManager(cfg)
        mgr.tools_server = server
        return mgr

    def test_new_process_gets_token_in_env_and_registry(self):
        server = pts.PiToolsServer(port=0)
        proc = self._manager(server)._new_process()
        self.assertTrue(proc.tool_token)
        self.assertEqual(proc.env["PI_OCTAVIUS_TOOLS_TOKEN"], proc.tool_token)
        self.assertTrue(server.is_valid(proc.tool_token))
        self.assertNotIn("OCTAVIUS_PI_TOOLS_TOKEN", proc.env)

    async def test_process_env_carries_the_bound_url(self):
        server = pts.PiToolsServer(port=0)
        await server.start()
        self.addAsyncCleanup(server.stop)
        proc = self._manager(server)._new_process()
        self.assertEqual(proc.env["PI_OCTAVIUS_TOOLS_URL"], f"http://127.0.0.1:{server.port}/mcp")
        self.assertNotEqual(server.port, 0)

    async def test_unbind_cancels_inflight_call_cleanly(self):
        server = await _server()
        self.addAsyncCleanup(server.stop)
        token = server.new_token()
        started = asyncio.Event()
        finished = []

        async def slow(*a, **k):
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                finished.append("cancelled")

        unbind = server.bind(token, pts.ToolContext("H", "M", "S"))
        with mock.patch("tools.call_tool", new=slow):
            async with Session(server, token) as s:
                call = asyncio.create_task(s.call_tool("read_conversation", {}))
                await asyncio.wait_for(started.wait(), 5)
                unbind()
                r = await asyncio.wait_for(call, 5)
                self.assertTrue(r.isError)
                self.assertIn("cancelled", r.content[0].text)
                self.assertEqual(finished, ["cancelled"])
                # the server survives: a later call on the same token still answers
                self.assertEqual(len((await s.list_tools()).tools), 13)
        self.assertFalse(server._inflight.get(token))

    def test_tokens_are_unique_per_process(self):
        mgr = self._manager(pts.PiToolsServer(port=0))
        self.assertNotEqual(mgr._new_process().tool_token, mgr._new_process().tool_token)

    async def test_kill_revokes_token(self):
        server = pts.PiToolsServer(port=0)
        proc = self._manager(server)._new_process()
        await proc.kill()
        self.assertFalse(server.is_valid(proc.tool_token))

    def test_no_tools_server_no_token(self):
        mgr = self._manager(None)
        proc = mgr._new_process()
        self.assertIsNone(proc.tool_token)
        self.assertNotIn("PI_OCTAVIUS_TOOLS_TOKEN", proc.env)
        mgr.bind_tools(proc, SimpleNamespace())()  # no-op, no error

    def test_bind_tools_binds_turn_context(self):
        server = pts.PiToolsServer(port=0)
        mgr = self._manager(server)
        proc = mgr._new_process()
        session = SimpleNamespace(state=SimpleNamespace(history_session="H", mcp_manager="M"))
        unbind = mgr.bind_tools(proc, session)
        ctx = server._bindings[proc.tool_token]
        self.assertEqual((ctx.history_session, ctx.mcp_manager, ctx.session), ("H", "M", session))
        unbind()
        self.assertNotIn(proc.tool_token, server._bindings)

    async def test_stream_pi_turn_unbinds_when_turn_raises(self):
        server = pts.PiToolsServer(port=0)
        mgr = self._manager(server)
        proc = mgr._new_process()
        seen = {}

        class Proc:
            tool_token = proc.tool_token
            turns = 0
            alive = True
            lock = asyncio.Lock()
            last_used = 0

            def subscribe(self):
                return asyncio.Queue()

            def unsubscribe(self):
                pass

            async def command(self, type_, **kw):
                if type_ == "prompt":
                    seen["bound"] = proc.tool_token in server._bindings
                    raise pi_brain.PiTurnError("boom")
                return {"success": True}

        async def get(conv_id):
            return Proc(), True

        mgr.get = get
        mgr.discard = mock.AsyncMock()
        conversation = SimpleNamespace(
            get_messages=lambda: [], add_user=lambda t: None, trim=lambda: None,
        )
        hs = SimpleNamespace(conv_id=7, db_path=None)
        session = SimpleNamespace(
            state=SimpleNamespace(history_session=hs, conversation=conversation, mcp_manager="M"),
            ws=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pi_brain=mgr))),
        )
        with mock.patch("agent._build_memory_block", new=mock.AsyncMock(return_value="")):
            with self.assertRaises(pi_brain.PiTurnError):
                async for _ in pi_brain.stream_pi_turn(session, "hi"):
                    pass
        self.assertTrue(seen["bound"])
        self.assertNotIn(proc.tool_token, server._bindings)


class LabelTests(unittest.TestCase):
    def test_labels_reuse_native(self):
        self.assertEqual(pi_brain.tool_label("mcp__octavius__save_note"), "Saving Note")


if __name__ == "__main__":
    unittest.main()
