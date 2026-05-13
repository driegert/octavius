import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from websocket_session import build_item_chat_context, create_item_conversation
from websocket_session import WebSocketSessionHandler
from settings import settings


class _FakeHistorySession:
    def __init__(self, conv_id):
        self.conv_id = conv_id
        self.ended = False
        self.messages = []

    async def end_async(self):
        self.ended = True

    async def add_message_async(self, role, content, model=None, **kwargs):
        entry = {"role": role, "content": content, "model": model}
        entry.update(kwargs)
        self.messages.append(entry)


class _FakeHistory:
    def __init__(self):
        self.started = []
        self.sessions = []

    def start_conversation(self, **kwargs):
        self.started.append(kwargs)
        session = _FakeHistorySession(conv_id=100 + len(self.sessions))
        self.sessions.append(session)
        return session

    def connect(self):
        class _ConnCtx:
            def __enter__(self_inner):
                return object()

            def __exit__(self_inner, exc_type, exc, tb):
                return False

        return _ConnCtx()


class _FakeDispatcher:
    def snapshot(self):
        return {}

    def fallback_url(self):
        return None


class _FakeWS:
    def __init__(self):
        self.sent = []
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                history=_FakeHistory(),
                mcp_manager=object(),
                subagent_dispatcher=_FakeDispatcher(),
            )
        )

    async def send_text(self, text):
        self.sent.append(text)


class WebSocketSessionTests(unittest.TestCase):
    def test_build_item_chat_context_includes_preview_and_id(self):
        item = {
            "title": "Paper",
            "item_type": "article",
            "content": "A" * 600,
        }
        context = build_item_chat_context(item, 42)
        self.assertIn("Title: Paper", context)
        self.assertIn("Type: article", context)
        self.assertIn("The item ID is 42.", context)
        self.assertIn("...", context)

    def test_create_item_conversation_injects_context(self):
        item = {
            "title": "Note",
            "item_type": "note",
            "content": "hello",
        }
        conversation = create_item_conversation(item, 7)
        self.assertEqual(conversation.get_messages()[0]["role"], "system")
        self.assertIn("Title: Note", conversation.get_messages()[0]["content"])

    def test_handle_reset_uses_settings_llm_chain_model(self):
        async def run():
            handler = WebSocketSessionHandler(_FakeWS())
            old_session = _FakeHistorySession(conv_id=1)
            handler.state.history_session = old_session

            await handler.handle_reset({})

            self.assertTrue(old_session.ended)
            self.assertEqual(
                handler.state.history.started[-1],
                {"source": "voice", "model": settings.llm_chain[0]["model"]},
            )

        asyncio.run(run())

    def test_handle_load_conversation_uses_settings_llm_chain_model(self):
        async def run():
            handler = WebSocketSessionHandler(_FakeWS())
            old_session = _FakeHistorySession(conv_id=1)
            handler.state.history_session = old_session

            with patch(
                "history.get_conversation_messages",
                return_value=[
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
            ):
                await handler.handle_load_conversation({"conversation_id": 42})

            self.assertTrue(old_session.ended)
            self.assertEqual(
                handler.state.history.started[-1],
                {"source": "voice", "model": settings.llm_chain[0]["model"]},
            )

        asyncio.run(run())

    def test_handle_item_chat_load_updates_saved_pointer_for_existing_chat(self):
        async def run():
            handler = WebSocketSessionHandler(_FakeWS())

            with (
                patch("history.get_saved_item", return_value={"title": "Note", "item_type": "note", "content": "hello"}),
                patch("history.get_item_chat_conversation_id", return_value=42),
                patch(
                    "history.get_conversation_messages",
                    return_value=[
                        {"role": "user", "content": "hello"},
                        {"role": "assistant", "content": "hi"},
                    ],
                ),
                patch("history.set_item_chat_conversation") as set_chat,
            ):
                await handler.handle_item_chat_load({"item_id": 7})

            set_chat.assert_called_once_with(unittest.mock.ANY, 7, 100)

        asyncio.run(run())


class _StubTicket:
    """Minimal SubagentTicket stand-in for the delegation lifecycle tests."""

    def __init__(self, url="http://stub/v1/chat/completions"):
        self.assigned_url = None
        self._url = url
        self.released = False
        self.cancel_pending_called = False

    async def acquire(self):
        self.assigned_url = self._url
        return self._url

    async def release(self):
        self.released = True

    async def cancel_pending(self):
        self.cancel_pending_called = True
        return True


def _payloads(ws):
    """Decode the JSON text messages sent on a _FakeWS."""
    import json
    return [json.loads(text) for text in ws.sent]


def _make_record(handler, *, domain="email", task="Check inbox", url="http://stub"):
    """Register a DelegationRecord in the handler with a stub ticket."""
    from datetime import datetime
    from websocket_session import DelegationRecord

    ticket = _StubTicket(url=url)
    record = DelegationRecord(
        handle=f"dlg_{domain}_{len(handler.state.delegations)}",
        domain=domain,
        submitted_task=task,
        ticket=ticket,
        created_at=datetime.now(),
    )
    handler.state.delegations[record.handle] = record
    return record


class RunTurnAudioDoneTests(unittest.TestCase):
    """Continuous mode re-arms the mic when it sees audio_done. If audio_done
    is missed on any completion path (including agent exceptions and
    empty replies), the browser sits forever in 'Speaking...'.
    """

    def _make_handler(self):
        ws = _FakeWS()
        handler = WebSocketSessionHandler(ws)
        handler.state.history_session = _FakeHistorySession(conv_id=1)
        handler.state.tts_enabled = False  # skip the TTS path
        return handler, ws

    def _payload_types(self, ws):
        import json
        return [json.loads(text).get("type") for text in ws.sent]

    def _statuses(self, ws):
        import json
        return [
            json.loads(text).get("text")
            for text in ws.sent
            if json.loads(text).get("type") == "status"
        ]

    def test_audio_done_sent_on_normal_reply(self):
        async def run():
            handler, ws = self._make_handler()

            async def fake_stream(*args, **kwargs):
                for sentence in ["Hi there. ", "How can I help?"]:
                    yield sentence

            with patch("agent.stream_agent_turn", side_effect=fake_stream):
                await handler.run_turn("hello", source="text")

            self.assertIn("audio_done", self._statuses(ws))

        asyncio.run(run())

    def test_audio_done_sent_on_empty_reply(self):
        async def run():
            handler, ws = self._make_handler()

            async def fake_stream(*args, **kwargs):
                if False:
                    yield  # async-generator that yields nothing

            with patch("agent.stream_agent_turn", side_effect=fake_stream):
                await handler.run_turn("hello", source="text")

            self.assertIn("audio_done", self._statuses(ws))

        asyncio.run(run())

    def test_audio_done_sent_when_agent_raises(self):
        async def run():
            handler, ws = self._make_handler()

            async def fake_stream(*args, **kwargs):
                raise RuntimeError("model unreachable")
                yield  # unreachable; marks this an async generator

            with patch("agent.stream_agent_turn", side_effect=fake_stream):
                await handler.run_turn("hello", source="text")

            statuses = self._statuses(ws)
            # Error status appears AND audio_done still fires.
            self.assertTrue(any("Agent error" in s for s in statuses))
            self.assertIn("audio_done", statuses)

        asyncio.run(run())


class DelegationLifecycleTests(unittest.TestCase):
    def test_run_and_announce_parks_result_when_proactive_disabled(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            record = _make_record(handler, domain="email", task="t")

            async def fake_subagent(*args, **kwargs):
                return "First line of summary.\n\n===TOOL DATA===\nraw stuff"

            with patch("websocket_session.run_subagent", side_effect=fake_subagent):
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

            with patch("websocket_session.run_subagent", side_effect=fake_subagent):
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

            with patch("websocket_session.run_subagent", side_effect=fake_subagent):
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


if __name__ == "__main__":
    unittest.main()
