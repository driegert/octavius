import unittest
import asyncio
import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import patch

from websocket_session import WebSocketSessionHandler, WebSocketDisconnect
from settings import settings


def _settings_with_spool_dirs(*dirs):
    """A `settings` replacement carrying only `media_spool_dirs` overridden.

    Media frame handlers read `settings.media_spool_dirs` at call time, and
    `Settings` is a frozen dataclass, so tests that spool into a tempdir
    outside the real allowlist patch `websocket_session.settings` with this
    rather than mutating the real settings object (which frozen forbids).
    """
    return dataclasses.replace(settings, media_spool_dirs=list(dirs))


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
        self.resumable = set()  # conversation ids resume_conversation will reopen
        self.resumed = []

    def resume_conversation(self, conv_id):
        if conv_id not in self.resumable:
            return None
        self.resumed.append(conv_id)
        return _FakeHistorySession(conv_id=conv_id)

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

    def test_load_conversation_continues_a_voice_record_and_reports_its_id(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            old_session = _FakeHistorySession(conv_id=1)
            handler.state.history_session = old_session
            handler.state.history.resumable.add(42)

            with patch("history.get_conversation_messages",
                       return_value=[{"role": "user", "content": "hello"}]):
                await handler.handle_load_conversation({"conversation_id": 42})

            self.assertTrue(old_session.ended)
            self.assertEqual(handler.state.history_session.conv_id, 42)
            self.assertEqual(handler.state.history.started, [])
            frames = [json.loads(t) for t in ws.sent]
            kinds = [f["type"] for f in frames]
            self.assertLess(kinds.index("conversation_loaded"), kinds.index("session_id"))
            self.assertEqual(frames[kinds.index("session_id")]["conversation_id"], 42)

        asyncio.run(run())

    def test_load_conversation_of_a_non_voice_record_reports_the_new_record(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            handler.state.history_session = _FakeHistorySession(conv_id=1)

            with patch("history.get_conversation_messages",
                       return_value=[{"role": "user", "content": "hello"}]):
                await handler.handle_load_conversation({"conversation_id": 42})

            new_id = handler.state.history_session.conv_id
            self.assertNotIn(new_id, (1, 42))
            frames = {json.loads(t)["type"]: json.loads(t) for t in ws.sent}
            self.assertEqual(frames["conversation_loaded"]["conversation_id"], 42)
            self.assertEqual(frames["session_id"]["conversation_id"], new_id)

        asyncio.run(run())

    def test_load_conversation_stops_an_in_flight_turn_first(self):
        async def run():
            handler = WebSocketSessionHandler(_FakeWS())
            handler.state.history_session = _FakeHistorySession(conv_id=1)
            handler.state.turn_task = asyncio.create_task(asyncio.sleep(10))

            with patch("history.get_conversation_messages",
                       return_value=[{"role": "user", "content": "hello"}]):
                await handler.handle_load_conversation({"conversation_id": 42})

            self.assertTrue(handler.state.turn_task.cancelled())

        asyncio.run(run())

    def test_restore_session_continues_the_old_record_under_the_same_id(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            connect_time = _FakeHistorySession(conv_id=9)
            handler.state.history_session = connect_time
            handler.state.history.resumable.add(3)

            with patch("history.get_conversation_messages",
                       return_value=[{"role": "user", "content": "hi"},
                                     {"role": "assistant", "content": "hello"}]):
                await handler.handle_restore_session({"conversation_id": 3})

            self.assertTrue(connect_time.ended)
            self.assertEqual(handler.state.history_session.conv_id, 3)
            self.assertEqual(json.loads(ws.sent[-1]), {"type": "session_id", "conversation_id": 3})
            self.assertEqual(len(handler.state.conversation.get_messages()), 3)

        asyncio.run(run())

    def test_restore_session_of_an_unknown_id_keeps_the_connect_record_and_is_silent(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            connect_time = _FakeHistorySession(conv_id=9)
            handler.state.history_session = connect_time
            handler.state.history.resumable.add(3)

            with patch("history.get_conversation_messages", return_value=[]):
                await handler.handle_restore_session({"conversation_id": 3})

            self.assertFalse(connect_time.ended)
            self.assertIs(handler.state.history_session, connect_time)
            self.assertEqual(ws.sent, [])

        asyncio.run(run())

    def test_handle_load_conversation_captions_pi_replies_only(self):
        async def run():
            ws = _FakeWS()
            handler = WebSocketSessionHandler(ws)
            handler.state.history_session = _FakeHistorySession(conv_id=1)

            with patch(
                "history.get_conversation_messages",
                return_value=[
                    {"role": "user", "content": "q1", "model": None},
                    {"role": "assistant", "content": "a1", "model": "pi:lrv-p01/xray"},
                    {"role": "assistant", "content": "a2", "model": "gemma4-26b-a4b"},
                ],
            ), patch("websocket_session.chat_model_label", side_effect=lambda m: f"L({m})"):
                await handler.handle_load_conversation({"conversation_id": 42})

            loaded = [json.loads(t) for t in ws.sent if '"conversation_loaded"' in t][-1]
            self.assertEqual(loaded["messages"], [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1", "answered_by": "L(lrv-p01/xray)"},
                {"role": "assistant", "content": "a2"},
            ])

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

    def test_reply_keeps_latex_while_tts_gets_the_spoken_math(self):
        import json

        async def run():
            handler, ws = self._make_handler()
            handler.state.tts_enabled = True
            spoken = []

            async def fake_stream(*args, **kwargs):
                yield "So $x^2$ works."

            async def fake_synthesize(text, voice=None):
                spoken.append(text)
                return b"wav"

            async def fake_speakable(text):
                return "So x squared works."

            with patch("agent.stream_agent_turn", fake_stream), \
                    patch("tts.synthesize", fake_synthesize), patch("tts.speakable", fake_speakable):
                await handler.run_turn("hi", "voice")
            return spoken, ws

        spoken, ws = asyncio.run(run())
        self.assertEqual(spoken, ["So x squared works."])
        replies = [json.loads(t)["text"] for t in ws.sent if json.loads(t).get("type") == "response"]
        self.assertEqual(replies, ["So $x^2$ works."])

    def test_a_large_formula_is_announced_before_its_clip(self):
        import json

        async def run():
            handler, ws = self._make_handler()
            handler.state.tts_enabled = True
            events = []

            async def record_bytes(data):
                events.append(data)

            real_payload = handler.send_payload

            async def record_payload(payload):
                if payload.get("type") == "speech_clip":
                    events.append(payload)
                await real_payload(payload)

            handler.send_bytes = record_bytes
            handler.send_payload = record_payload

            async def fake_stream(*args, **kwargs):
                yield r"Then $\sum_i x_i$ works."

            async def fake_synthesize(text, voice=None):
                return text.encode()

            async def fake_speakable(text):
                return text

            with patch("agent.stream_agent_turn", fake_stream), \
                    patch("tts.synthesize", fake_synthesize), patch("tts.speakable", fake_speakable):
                await handler.run_turn("hi", "voice")
            return events

        events = asyncio.run(run())
        self.assertEqual(events[0], b"Then")
        self.assertEqual(events[1], {"type": "speech_clip", "math": True})
        self.assertIn(b"sum", events[2])
        self.assertEqual(events[3], b"works.")

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

    def test_confirm_question_is_its_own_response(self):
        """pi's confirm question is committed before Dave answers, so the final
        response holds only what came after it instead of repeating it."""
        async def run():
            import json
            handler, ws = self._make_handler()

            async def fake_stream(*args, **kwargs):
                yield "Before I go ahead: create task. Shall I? "
                await handler.commit_reply_so_far()
                yield "Done."

            with patch("agent.stream_agent_turn", side_effect=fake_stream):
                await handler.run_turn("make a task", source="voice")

            responses = [json.loads(t)["text"] for t in ws.sent if json.loads(t).get("type") == "response"]
            self.assertEqual(responses, ["Before I go ahead: create task. Shall I?", "Done."])

        asyncio.run(run())

    def test_response_delta_streams_each_sentence_before_final_response(self):
        """Text clients (the Matrix sidecar) render progressively from
        response_delta; `response` still carries the authoritative full text."""
        async def run():
            import json
            handler, ws = self._make_handler()

            async def fake_stream(*args, **kwargs):
                for sentence in ["Hi there. ", "How can I help?"]:
                    yield sentence

            with patch("agent.stream_agent_turn", side_effect=fake_stream):
                await handler.run_turn("hello", source="matrix")

            frames = [json.loads(text) for text in ws.sent]
            deltas = [f["text"] for f in frames if f.get("type") == "response_delta"]
            self.assertEqual(deltas, ["Hi there. ", "How can I help?"])

            types = self._payload_types(ws)
            # Every delta lands before the final response.
            self.assertLess(
                max(i for i, t in enumerate(types) if t == "response_delta"),
                types.index("response"),
            )
            responses = [f["text"] for f in frames if f.get("type") == "response"]
            self.assertEqual(responses, ["Hi there. How can I help?"])

        asyncio.run(run())

    def test_no_response_delta_when_reply_is_empty(self):
        async def run():
            handler, ws = self._make_handler()

            async def fake_stream(*args, **kwargs):
                if False:
                    yield  # async-generator that yields nothing

            with patch("agent.stream_agent_turn", side_effect=fake_stream):
                await handler.run_turn("hello", source="matrix")

            self.assertNotIn("response_delta", self._payload_types(ws))

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


class RunLoopDisconnectTests(unittest.TestCase):
    """Starlette delivers a disconnect as a MESSAGE; calling receive() again
    afterwards raises RuntimeError. The run loop must break on the disconnect
    message itself — reaching cleanup through that RuntimeError chained the
    traceback into every warning logged during cleanup."""

    def test_disconnect_message_breaks_loop_without_re_receiving(self):
        class _DisconnectingWS(_FakeWS):
            def __init__(self):
                super().__init__()
                self.receive_calls = 0

            async def accept(self):
                pass

            async def receive(self):
                self.receive_calls += 1
                if self.receive_calls > 1:
                    raise RuntimeError(
                        'Cannot call "receive" once a disconnect message has been received.'
                    )
                return {"type": "websocket.disconnect", "code": 1000}

        async def run():
            ws = _DisconnectingWS()
            handler = WebSocketSessionHandler(ws)
            await handler.run()
            self.assertEqual(ws.receive_calls, 1)
            self.assertTrue(handler.state.history_session.ended)

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
                                   history_session=None, session=None, user_content=None,
                                   source="voice"):
                captured["user_content"] = user_content
                captured["source"] = source
                yield "It's a cat."

            with patch("agent.stream_agent_turn", side_effect=fake_stream):
                await handler.run_turn(
                    "[image: cat.png]", source="image",
                    user_content=[{"type": "text", "text": "x"}],
                    attachment={"type": "image", "reference": "/tmp/cat.png", "title": "cat.png"},
                )

            self.assertIsNotNone(captured["user_content"])
            self.assertEqual(captured["source"], "image")
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


class _FakePiBrain:
    def __init__(self):
        self.discarded = []
        self.rekeyed = []

    async def discard(self, conv_id):
        self.discarded.append(conv_id)

    def rekey(self, old, new):
        self.rekeyed.append((old, new))


class PiBrainWiringTests(unittest.TestCase):
    """websocket_session's side of pi_brain: confirmations, barge-in, process lifecycle."""

    def _handler(self):
        ws = _FakeWS()
        ws.app.state.pi_brain = _FakePiBrain()
        handler = WebSocketSessionHandler(ws)
        handler.state.history_session = _FakeHistorySession(conv_id=5)
        return handler, ws.app.state.pi_brain

    def test_pending_confirm_takes_the_next_input_instead_of_a_turn(self):
        async def run():
            handler, _ = self._handler()
            fut = asyncio.get_running_loop().create_future()
            handler.state.pending_confirm = fut
            with patch.object(handler, "_run_turn_guarded") as run_turn:
                handler._spawn_turn("yes, go ahead", source="voice")
                run_turn.assert_not_called()
            self.assertIs(fut.result(), True)
            handler.state.pending_confirm = asyncio.get_running_loop().create_future()
            handler._spawn_turn("no thanks", source="text")
            self.assertIs(handler.state.pending_confirm.result(), False)

        asyncio.run(run())

    def test_mic_open_does_not_cancel_a_turn_waiting_on_confirm(self):
        async def run():
            handler, _ = self._handler()
            handler.state.turn_task = asyncio.create_task(asyncio.sleep(10))
            handler.state.pending_confirm = asyncio.get_running_loop().create_future()
            with patch("vad.SileroVAD"):
                await handler.handle_stt_start({})
            self.assertFalse(handler.state.turn_task.done())
            # Without a pending confirm, opening the mic is a barge-in.
            handler.state.pending_confirm = None
            with patch("vad.SileroVAD"):
                await handler.handle_stt_start({})
            self.assertTrue(handler.state.turn_task.cancelled())

        asyncio.run(run())

    def test_reset_discards_the_conversations_pi_process(self):
        async def run():
            handler, brain = self._handler()
            await handler.handle_reset({})
            self.assertEqual(brain.discarded, [5])

        asyncio.run(run())

    def test_restore_rekeys_the_live_pi_process(self):
        async def run():
            handler, brain = self._handler()
            with patch("history.get_conversation_messages",
                       return_value=[{"role": "user", "content": "hi"}]):
                await handler.handle_restore_session({"conversation_id": 3})
            self.assertEqual(brain.rekeyed, [(3, 5)])

        asyncio.run(run())

    def test_restore_of_a_resumed_record_keeps_its_pi_process_key(self):
        async def run():
            handler, brain = self._handler()
            handler.state.history.resumable.add(3)
            with patch("history.get_conversation_messages",
                       return_value=[{"role": "user", "content": "hi"}]):
                await handler.handle_restore_session({"conversation_id": 3})
            self.assertEqual(brain.rekeyed, [(3, 3)])
            self.assertEqual(brain.discarded, [])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
