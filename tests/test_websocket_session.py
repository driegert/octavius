import unittest
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from websocket_session import build_item_chat_context, create_item_conversation
from websocket_session import WebSocketSessionHandler, WebSocketDisconnect
from settings import settings


class _FakeHistorySession:
    def __init__(self, conv_id):
        self.conv_id = conv_id
        self.ended = False
        self.messages = []
        self.attachments = []
        self._next_msg_id = 1000

    async def end_async(self):
        self.ended = True

    async def add_message_async(self, role, content, model=None, **kwargs):
        entry = {"role": role, "content": content, "model": model}
        entry.update(kwargs)
        self.messages.append(entry)
        msg_id = self._next_msg_id
        self._next_msg_id += 1
        return msg_id

    def add_attachment(self, message_id, type, reference, title=None):
        self.attachments.append(
            {"message_id": message_id, "type": type, "reference": reference, "title": title}
        )
        return len(self.attachments)


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
    def __init__(self):
        self.last_ticket = None

    def snapshot(self):
        return {}

    def fallback_url(self):
        return None

    async def reserve(self):
        self.last_ticket = _StubTicket()
        return self.last_ticket


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

            with patch("websocket_session.run_subagent", side_effect=fake_subagent):
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

            with patch("websocket_session.run_subagent", side_effect=boom):
                with self.assertRaises(RuntimeError):
                    await handler.run_inline_subagent("email", "x")

            self.assertTrue(handler.state.subagent_dispatcher.last_ticket.released)

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


class SpawnTurnTests(unittest.TestCase):
    """_spawn_turn runs the turn off the receive loop so heartbeat pings
    keep being answered while the agent streams. Overlapping turns are
    dropped rather than queued.
    """

    def _make_handler(self):
        ws = _FakeWS()
        handler = WebSocketSessionHandler(ws)
        handler.state.history_session = _FakeHistorySession(conv_id=1)
        handler.state.tts_enabled = False
        return handler, ws

    def test_spawn_turn_runs_as_background_task(self):
        async def run():
            handler, _ws = self._make_handler()

            async def fake_stream(*args, **kwargs):
                yield "Done."

            with patch("agent.stream_agent_turn", side_effect=fake_stream):
                handler._spawn_turn("hello", source="text")
                self.assertIsNotNone(handler.state.turn_task)
                self.assertFalse(handler.state.turn_task.done())
                await handler.state.turn_task

            self.assertTrue(handler.state.turn_task.done())

        asyncio.run(run())

    def test_overlapping_turn_is_dropped(self):
        async def run():
            handler, _ws = self._make_handler()
            release = asyncio.Event()

            async def slow_stream(*args, **kwargs):
                await release.wait()
                yield "Done."

            with patch("agent.stream_agent_turn", side_effect=slow_stream):
                handler._spawn_turn("first", source="text")
                first_task = handler.state.turn_task
                # Second request while the first is still in flight.
                handler._spawn_turn("second", source="text")
                self.assertIs(handler.state.turn_task, first_task)
                release.set()
                await first_task

        asyncio.run(run())

    def test_turn_after_previous_finishes_is_accepted(self):
        async def run():
            handler, _ws = self._make_handler()

            async def fake_stream(*args, **kwargs):
                yield "Done."

            with patch("agent.stream_agent_turn", side_effect=fake_stream):
                handler._spawn_turn("first", source="text")
                first_task = handler.state.turn_task
                await first_task
                handler._spawn_turn("second", source="text")
                self.assertIsNot(handler.state.turn_task, first_task)
                await handler.state.turn_task

        asyncio.run(run())

    def test_stt_start_cancels_inflight_turn(self):
        """Barge-in: opening a new capture cancels a reply still in flight so
        the client's interruption actually stops the server too."""
        async def run():
            handler, _ws = self._make_handler()
            release = asyncio.Event()

            async def slow_stream(*args, **kwargs):
                await release.wait()
                yield "Done."

            with patch("agent.stream_agent_turn", side_effect=slow_stream), \
                    patch("vad.SileroVAD", return_value=SimpleNamespace(reset=lambda: None)):
                handler._spawn_turn("first", source="text")
                turn = handler.state.turn_task
                self.assertFalse(turn.done())
                await handler.handle_stt_start({})
                self.assertTrue(turn.done())
                self.assertTrue(turn.cancelled())
            release.set()

        asyncio.run(run())

    def test_stt_start_without_inflight_turn_is_fine(self):
        """The cancel path is a no-op when no turn is running (normal flow)."""
        async def run():
            handler, _ws = self._make_handler()
            with patch("vad.SileroVAD", return_value=SimpleNamespace(reset=lambda: None)):
                await handler.handle_stt_start({})
                self.assertTrue(handler.state.stt_stream.active)

        asyncio.run(run())

    def test_guarded_turn_swallows_disconnect(self):
        async def run():
            handler, _ws = self._make_handler()

            async def disconnecting_stream(*args, **kwargs):
                raise WebSocketDisconnect()
                yield  # unreachable; marks this an async generator

            with patch("agent.stream_agent_turn", side_effect=disconnecting_stream):
                # Must not raise out of the task.
                await handler._run_turn_guarded("hello", source="text")

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


class UnknownFrameTests(unittest.TestCase):
    """The dispatch dict is keyed on 'type'; anything not in it is a silent
    no-op. This is the safety net for 'ignore unknown frame fields/types
    gracefully' from the frozen media contract."""

    def test_unknown_frame_type_is_ignored(self):
        async def run():
            handler = WebSocketSessionHandler(_FakeWS())
            await handler.handle_text_message(json.dumps({"type": "some_future_frame", "text": "x"}))
            self.assertEqual(handler.ws.sent, [])

        asyncio.run(run())

    def test_known_frame_with_unexpected_extra_fields_does_not_crash(self):
        async def run():
            handler = WebSocketSessionHandler(_FakeWS())
            handler._spawn_turn = lambda *a, **kw: None
            await handler.handle_text_message(
                json.dumps({"type": "image_input", "text": "", "path": "/nonexistent",
                            "mime": "image/jpeg", "filename": "x.jpg", "size_bytes": 5,
                            "future_field": {"nested": True}})
            )
            # Path doesn't exist -> handled via the "couldn't find the file" branch,
            # not a crash.
            statuses = [json.loads(t)["text"] for t in handler.ws.sent if json.loads(t).get("type") == "status"]
            self.assertTrue(any("couldn't find" in s for s in statuses))

        asyncio.run(run())


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

        asyncio.run(run())

    def test_non_image_mime_sends_status_and_does_not_spawn_turn(self):
        async def run():
            import tempfile
            handler, ws = self._make_handler()
            handler._spawn_turn = unittest.mock.Mock()
            with tempfile.NamedTemporaryFile(suffix=".bin") as f:
                f.write(b"not an image")
                f.flush()
                await handler.handle_image_input(
                    {"text": "", "path": f.name, "mime": "application/octet-stream", "filename": "a.bin"}
                )
            handler._spawn_turn.assert_not_called()
            statuses = [json.loads(t)["text"] for t in ws.sent if json.loads(t).get("type") == "status"]
            self.assertTrue(any("isn't an image" in s for s in statuses))

        asyncio.run(run())

    def test_valid_image_spawns_turn_with_vision_content_array(self):
        async def run():
            import base64
            import tempfile
            handler, ws = self._make_handler()
            captured = {}

            def fake_spawn(user_text, source, user_content=None, attachment=None):
                captured["user_text"] = user_text
                captured["source"] = source
                captured["user_content"] = user_content
                captured["attachment"] = attachment

            handler._spawn_turn = fake_spawn
            with tempfile.NamedTemporaryFile(suffix=".png") as f:
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
            import tempfile
            handler, ws = self._make_handler()
            captured = {}
            handler._spawn_turn = lambda user_text, source, user_content=None, attachment=None: captured.update(
                user_text=user_text, user_content=user_content
            )
            with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
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

        asyncio.run(run())

    def test_non_pdf_acknowledges_without_docproc_call(self):
        async def run():
            import tempfile
            handler, ws = self._make_handler()
            captured = {}
            handler._spawn_turn = lambda instruction, source, user_content=None, attachment=None: captured.update(
                instruction=instruction, source=source, attachment=attachment
            )
            with tempfile.NamedTemporaryFile(suffix=".docx") as f:
                f.write(b"not a pdf")
                f.flush()
                with patch("websocket_session.docproc_client.submit_job") as submit:
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
            import tempfile
            from unittest.mock import AsyncMock
            handler, ws = self._make_handler()
            captured = {}
            handler._spawn_turn = lambda instruction, source, user_content=None, attachment=None: captured.update(
                instruction=instruction, source=source, attachment=attachment
            )
            with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
                f.write(b"%PDF-1.4 fake")
                f.flush()
                with patch(
                    "websocket_session.docproc_client.submit_job",
                    new=AsyncMock(return_value={"id": "job-123", "status": "queued"}),
                ) as submit:
                    await handler.handle_file_input(
                        {"text": "", "path": f.name, "mime": "application/pdf", "filename": "paper.pdf"}
                    )
                    submit.assert_called_once_with(f.name)

            self.assertIn("job-123", captured["instruction"])
            self.assertIn("paper.pdf", captured["instruction"])
            self.assertEqual(captured["source"], "file")

        asyncio.run(run())

    def test_pdf_with_caption_schedules_background_poll_not_spawn_turn(self):
        async def run():
            import tempfile
            from unittest.mock import AsyncMock
            handler, ws = self._make_handler()
            handler._spawn_turn = unittest.mock.Mock()
            created_tasks = []

            def fake_create_task(coro):
                created_tasks.append(coro)
                coro.close()
                return None

            with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
                f.write(b"%PDF-1.4 fake")
                f.flush()
                with (
                    patch(
                        "websocket_session.docproc_client.submit_job",
                        new=AsyncMock(return_value={"id": "job-456", "status": "queued"}),
                    ),
                    patch("websocket_session.asyncio.create_task", side_effect=fake_create_task),
                ):
                    await handler.handle_file_input(
                        {"text": "summarize this", "path": f.name, "mime": "application/pdf", "filename": "paper.pdf"}
                    )

            handler._spawn_turn.assert_not_called()
            self.assertEqual(len(created_tasks), 1)

        asyncio.run(run())

    def test_docproc_submit_failure_acknowledges_error(self):
        async def run():
            import tempfile
            handler, ws = self._make_handler()
            captured = {}
            handler._spawn_turn = lambda instruction, source, user_content=None, attachment=None: captured.update(
                instruction=instruction
            )
            with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
                f.write(b"%PDF-1.4 fake")
                f.flush()
                with patch(
                    "websocket_session.docproc_client.submit_job",
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
                        "websocket_session.docproc_client.poll_job",
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
                        "websocket_session.docproc_client.poll_job",
                        new=AsyncMock(return_value={"status": "done", "result_md_path": md_path}),
                    ),
                    patch.object(handler, "run_turn", new=AsyncMock()) as run_turn,
                    patch(
                        "websocket_session.settings",
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
                    "websocket_session.docproc_client.poll_job",
                    new=AsyncMock(side_effect=docproc_client.DocprocError("timed out after 300s")),
                ),
                patch.object(handler, "run_turn", new=AsyncMock()) as run_turn,
            ):
                await handler._await_pdf_and_run("job-3", "paper.pdf", "summarize")
            run_turn.assert_called_once()
            instruction = run_turn.call_args.args[0]
            self.assertIn("failed to convert or timed out", instruction)
            self.assertIn("timed out after 300s", instruction)

    def test_disconnect_mid_poll_is_swallowed_not_raised(self):
        """A client that disconnects during the (potentially minutes-long)
        poll must not blow up the background task — mirrors
        _run_turn_guarded's handling of the same exceptions."""
        async def run():
            from unittest.mock import AsyncMock
            handler, ws = self._make_handler()
            with (
                patch(
                    "websocket_session.docproc_client.poll_job",
                    new=AsyncMock(return_value={"status": "done", "result_md_path": None}),
                ),
                patch.object(handler, "run_turn", new=AsyncMock(side_effect=WebSocketDisconnect())),
            ):
                # Must not raise.
                await handler._await_pdf_and_run("job-4", "paper.pdf", "summarize")

        asyncio.run(run())

        asyncio.run(run())


class RunTurnMediaTests(unittest.TestCase):
    def _make_handler(self):
        ws = _FakeWS()
        handler = WebSocketSessionHandler(ws)
        handler.state.history_session = _FakeHistorySession(conv_id=1)
        handler.state.tts_enabled = False
        return handler, ws

    def test_image_turn_records_attachment_and_uses_vision_model(self):
        async def run():
            handler, ws = self._make_handler()
            captured = {}

            async def fake_stream(conversation, mcp, user_text, status_callback=None,
                                   history_session=None, session=None, user_content=None):
                captured["user_content"] = user_content
                yield "It's a cat."

            with patch("agent.stream_agent_turn", side_effect=fake_stream):
                await handler.run_turn(
                    "[image: cat.png]", source="image",
                    user_content=[{"type": "text", "text": "x"}],
                    attachment={"type": "image", "reference": "/tmp/cat.png", "title": "cat.png"},
                )

            self.assertIsNotNone(captured["user_content"])
            hs = handler.state.history_session
            self.assertEqual(len(hs.attachments), 1)
            self.assertEqual(hs.attachments[0], {
                "message_id": 1000, "type": "image", "reference": "/tmp/cat.png", "title": "cat.png",
            })
            assistant_entries = [m for m in hs.messages if m["role"] == "assistant"]
            self.assertEqual(assistant_entries[-1]["model"], settings.vision_llm_chain[0]["model"])

        asyncio.run(run())

    def test_text_turn_unaffected_no_attachment_default_model(self):
        async def run():
            handler, ws = self._make_handler()

            async def fake_stream(*args, **kwargs):
                yield "Hi."

            with patch("agent.stream_agent_turn", side_effect=fake_stream):
                await handler.run_turn("hello", source="text")

            hs = handler.state.history_session
            self.assertEqual(hs.attachments, [])
            assistant_entries = [m for m in hs.messages if m["role"] == "assistant"]
            self.assertEqual(assistant_entries[-1]["model"], settings.llm_chain[0]["model"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
