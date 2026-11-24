"""Delegations and inline subagent runs (ws_delegation), split out of test_websocket_session."""
import unittest
import asyncio
import json
from unittest.mock import patch

from tests.test_websocket_session import _FakeHistorySession, _FakeWS, _payloads, _make_record
from websocket_session import WebSocketSessionHandler


class InlineSubagentTests(unittest.TestCase):
    def test_run_inline_subagent_runs_releases_ticket_and_forwards_status(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)

            captured = {}

            async def fake_subagent(task, domain, mcp, assigned_url=None,
                                    fallback_url=None, status_callback=None):
                captured["task"] = task
                captured["domain"] = domain
                captured["assigned_url"] = assigned_url
                await status_callback("Email Search...")
                return "found 3 emails\n\n===TOOL DATA===\n..."

            with patch("ws_delegation.run_subagent", side_effect=fake_subagent):
                result = await handler.run_inline_subagent("email", "find tax emails")

            self.assertEqual(result, "found 3 emails\n\n===TOOL DATA===\n...")
            self.assertEqual(captured["domain"], "email")
            self.assertEqual(captured["task"], "find tax emails")
            # The reserved ticket's URL is handed to run_subagent and released.
            ticket = handler.state.subagent_dispatcher.last_ticket
            self.assertEqual(captured["assigned_url"], ticket._url)
            self.assertTrue(ticket.released)
            # The subagent's progress is forwarded to the UI status line.
            statuses = [p["text"] for p in _payloads(ws) if p.get("type") == "status"]
            self.assertIn("Email Search...", statuses)

        asyncio.run(run())

    def test_run_inline_subagent_releases_ticket_on_error(self):
        async def run():
            handler = WebSocketSessionHandler(_FakeWS())

            async def boom(*args, **kwargs):
                raise RuntimeError("subagent failed")

            with patch("ws_delegation.run_subagent", side_effect=boom):
                with self.assertRaises(RuntimeError):
                    await handler.run_inline_subagent("email", "x")

            self.assertTrue(handler.state.subagent_dispatcher.last_ticket.released)

        asyncio.run(run())


class DelegationLifecycleTests(unittest.TestCase):
    def test_run_and_announce_parks_result_when_proactive_disabled(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            record = _make_record(handler, domain="email", task="t")

            async def fake_subagent(*args, **kwargs):
                return "First line of summary.\n\n===TOOL DATA===\nraw stuff"

            with patch("ws_delegation.run_subagent", side_effect=fake_subagent):
                await handler._run_and_announce(record)

            self.assertEqual(record.status, "ready")
            self.assertEqual(record.preview, "First line of summary.")
            self.assertIn("First line of summary.", record.result)
            self.assertTrue(handler.state.proactive_queue.empty())
            self.assertIn(record.handle, handler.state.delegations)

            updates = [p for p in _payloads(ws) if p.get("type") == "delegation_update"]
            self.assertGreaterEqual(len(updates), 2)
            self.assertEqual(updates[0]["status"], "running")
            self.assertEqual(updates[-1]["status"], "ready")

        asyncio.run(run())

    def test_run_and_announce_speaks_when_proactive_enabled(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            handler.state.proactive_speak_enabled = True
            record = _make_record(handler)

            async def fake_subagent(*args, **kwargs):
                return "Some result."

            with patch("ws_delegation.run_subagent", side_effect=fake_subagent):
                await handler._run_and_announce(record)

            self.assertFalse(handler.state.proactive_queue.empty())
            queued = await handler.state.proactive_queue.get()
            self.assertEqual(queued.handle, record.handle)

        asyncio.run(run())

    def test_run_and_announce_marks_failure_on_exception(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            record = _make_record(handler)

            async def fake_subagent(*args, **kwargs):
                raise RuntimeError("boom")

            with patch("ws_delegation.run_subagent", side_effect=fake_subagent):
                await handler._run_and_announce(record)

            self.assertEqual(record.status, "failed")
            self.assertEqual(record.error, "boom")
            updates = [p for p in _payloads(ws) if p.get("type") == "delegation_update"]
            self.assertEqual(updates[-1]["status"], "failed")
            self.assertEqual(updates[-1]["error"], "boom")

        asyncio.run(run())

    def test_handle_delegation_list_replays_records(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            rec1 = _make_record(handler, domain="email")
            rec2 = _make_record(handler, domain="research")
            rec1.status = "ready"
            rec1.preview = "p1"
            rec2.status = "running"

            await handler.handle_delegation_list({})

            updates = [p for p in _payloads(ws) if p.get("type") == "delegation_update"]
            handles = {u["handle"] for u in updates}
            self.assertEqual(handles, {rec1.handle, rec2.handle})

        asyncio.run(run())

    def test_handle_delegation_dismiss_removes_ready_record(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            record = _make_record(handler)
            record.status = "ready"
            record.result = "x"

            await handler.handle_delegation_dismiss({"handle": record.handle})

            self.assertNotIn(record.handle, handler.state.delegations)
            removed = [p for p in _payloads(ws) if p.get("type") == "delegation_removed"]
            self.assertEqual(removed[-1]["handle"], record.handle)

        asyncio.run(run())

    def test_pull_unknown_handle_returns_message(self):
        async def run():
            handler = WebSocketSessionHandler(_FakeWS())
            msg = await handler.pull_delegation(handle="missing", mode="merge", via="voice")
            self.assertIn("No pending delegation", msg)

        asyncio.run(run())

    def test_pull_running_handle_reports_in_progress(self):
        async def run():
            handler = WebSocketSessionHandler(_FakeWS())
            record = _make_record(handler)  # status defaults to running
            msg = await handler.pull_delegation(handle=record.handle, mode="merge", via="voice")
            self.assertIn("still running", msg)
            self.assertIn(record.handle, handler.state.delegations)

        asyncio.run(run())

    def test_pull_merge_via_voice_returns_result_text(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            record = _make_record(handler, domain="email")
            record.status = "ready"
            record.result = "Two new emails about meetings.\n\n===TOOL DATA===\nraw"

            msg = await handler.pull_delegation(handle=record.handle, mode="merge", via="voice")

            self.assertEqual(msg, "Two new emails about meetings.")
            self.assertNotIn(record.handle, handler.state.delegations)
            removed = [p for p in _payloads(ws) if p.get("type") == "delegation_removed"]
            self.assertEqual(removed[-1]["handle"], record.handle)

        asyncio.run(run())

    def test_pull_new_mode_swaps_conversation_and_seeds_history(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            old_session = _FakeHistorySession(conv_id=1)
            handler.state.history_session = old_session
            handler.state.conversation.add_user("prev")
            handler.state.conversation.add_assistant("prev reply")
            record = _make_record(handler, domain="email", task="Original task")
            record.status = "ready"
            record.result = "Specialist summary."

            msg = await handler.pull_delegation(handle=record.handle, mode="new", via="ui")

            self.assertIn("new conversation", msg)
            self.assertTrue(old_session.ended)
            self.assertIsNot(handler.state.history_session, old_session)
            self.assertNotEqual(handler.state.history_session.conv_id, 1)
            seeded_msgs = handler.state.history_session.messages
            self.assertEqual(len(seeded_msgs), 2)
            self.assertEqual(seeded_msgs[0]["role"], "user")
            self.assertIn("Original task", seeded_msgs[0]["content"])
            self.assertEqual(seeded_msgs[1]["role"], "assistant")
            self.assertEqual(seeded_msgs[1]["content"], "Specialist summary.")

            roles = [m["role"] for m in handler.state.conversation.get_messages()]
            self.assertEqual(roles, ["system", "user", "assistant"])

            payloads = _payloads(ws)
            self.assertTrue(any(p.get("type") == "conversation_loaded" for p in payloads))
            self.assertTrue(any(p.get("type") == "delegation_removed" for p in payloads))

        asyncio.run(run())

    def test_pull_failed_returns_error_and_removes(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            record = _make_record(handler)
            record.status = "failed"
            record.error = "subagent crashed"

            msg = await handler.pull_delegation(handle=record.handle, mode="merge", via="voice")

            self.assertIn("subagent crashed", msg)
            self.assertNotIn(record.handle, handler.state.delegations)

        asyncio.run(run())


class DelegationToolTests(unittest.TestCase):
    def test_list_pending_delegations_filters_by_status_and_domain(self):
        async def run():
            import json
            from local_tool_delegations import list_pending_delegations

            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            r1 = _make_record(handler, domain="email")
            r2 = _make_record(handler, domain="email")
            r3 = _make_record(handler, domain="research")
            r1.status = "ready"
            r2.status = "running"
            r3.status = "ready"

            result = await list_pending_delegations({"status": "ready"}, session=handler)
            data = json.loads(result)
            self.assertEqual(data["count"], 2)
            handles = {item["handle"] for item in data["delegations"]}
            self.assertEqual(handles, {r1.handle, r3.handle})

            result = await list_pending_delegations({"domain": "email"}, session=handler)
            data = json.loads(result)
            self.assertEqual(data["count"], 2)
            self.assertEqual(
                {item["handle"] for item in data["delegations"]},
                {r1.handle, r2.handle},
            )

        asyncio.run(run())

    def test_pull_delegation_tool_picks_most_recent_ready_by_domain(self):
        async def run():
            from datetime import datetime, timedelta
            from local_tool_delegations import pull_delegation

            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            older = _make_record(handler, domain="email")
            newer = _make_record(handler, domain="email")
            older.status = "ready"
            older.result = "older"
            older.created_at = datetime.now() - timedelta(minutes=5)
            newer.status = "ready"
            newer.result = "newer"
            newer.created_at = datetime.now()

            text = await pull_delegation({"domain": "email"}, session=handler)
            self.assertEqual(text, "newer")
            self.assertNotIn(newer.handle, handler.state.delegations)
            self.assertIn(older.handle, handler.state.delegations)

        asyncio.run(run())

    def test_pull_delegation_tool_requires_handle_or_domain(self):
        async def run():
            from local_tool_delegations import pull_delegation

            handler = WebSocketSessionHandler(_FakeWS())
            msg = await pull_delegation({}, session=handler)
            self.assertIn("handle or domain", msg)

        asyncio.run(run())

    def test_pull_delegation_tool_returns_error_when_no_ready_in_domain(self):
        async def run():
            from local_tool_delegations import pull_delegation

            handler = WebSocketSessionHandler(_FakeWS())
            r = _make_record(handler, domain="email")
            r.status = "running"

            msg = await pull_delegation({"domain": "email"}, session=handler)
            self.assertIn("No ready email delegation", msg)
            self.assertIn(r.handle, handler.state.delegations)

        asyncio.run(run())
