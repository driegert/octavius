import copy
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import agent
from conversation import Conversation
from settings import settings


class _FakeMCP:
    def get_tools_for_servers(self, server_names):
        return []

    def get_server_for_tool(self, name):
        return None

    async def call_tool(self, name, arguments):
        return f"Result for {name}"


class _FakeResp:
    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeChainClient:
    """Stand-in for LLMChainClient recording every payload it's asked to stream."""

    def __init__(self, lines):
        self._lines = lines
        self.payloads = []

    @asynccontextmanager
    async def stream_chat(self, payload):
        # Snapshot now, like httpx would serialize the JSON body at request
        # time — real requests don't see later in-process mutation of the
        # message dicts (e.g. the post-turn image->placeholder downgrade).
        self.payloads.append(copy.deepcopy(payload))
        yield _FakeResp(self._lines)


class _FakeMultiReplyChainClient:
    """Like _FakeChainClient, but returns a different canned reply (a list of
    SSE lines) on each successive call — for tests that drive multiple turns
    through the same client and need distinguishable responses."""

    def __init__(self, reply_lines_sequence: list[list[str]]):
        self._replies = list(reply_lines_sequence)
        self.payloads = []

    @asynccontextmanager
    async def stream_chat(self, payload):
        self.payloads.append(copy.deepcopy(payload))
        lines = self._replies.pop(0)
        yield _FakeResp(lines)


def _text_reply_lines(text: str) -> list[str]:
    import json
    return [
        f'data: {json.dumps({"choices": [{"delta": {"content": text}}]})}',
        "data: [DONE]",
    ]


class TextTurnUsesDefaultChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_only_turn_uses_default_chain_and_plain_content(self):
        text_client = _FakeChainClient(_text_reply_lines("Hi there."))
        vision_client = _FakeChainClient([])

        conversation = Conversation()
        with patch.object(agent, "llm_client", text_client), \
             patch.object(agent, "vision_llm_client", vision_client):
            chunks = []
            async for chunk in agent.stream_agent_turn(conversation, _FakeMCP(), "hello"):
                chunks.append(chunk)

        self.assertEqual("".join(chunks).strip(), "Hi there.")
        self.assertEqual(len(text_client.payloads), 1)
        self.assertEqual(vision_client.payloads, [])
        self.assertEqual(text_client.payloads[0]["model"], settings.llm_chain[0]["model"])

        messages = conversation.get_messages()
        user_msg = next(m for m in messages if m["role"] == "user")
        self.assertEqual(user_msg["content"], "hello")
        self.assertIsInstance(user_msg["content"], str)


class ImageTurnUsesVisionChainTests(unittest.IsolatedAsyncioTestCase):
    def _content(self):
        return [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]

    async def test_image_turn_routes_to_vision_chain_with_content_array(self):
        text_client = _FakeChainClient([])
        vision_client = _FakeChainClient(_text_reply_lines("It's a cat."))

        conversation = Conversation()
        content = self._content()
        with patch.object(agent, "llm_client", text_client), \
             patch.object(agent, "vision_llm_client", vision_client):
            chunks = []
            async for chunk in agent.stream_agent_turn(
                conversation, _FakeMCP(), "[image: cat.png]", user_content=content,
            ):
                chunks.append(chunk)

        self.assertEqual("".join(chunks).strip(), "It's a cat.")
        self.assertEqual(text_client.payloads, [])
        self.assertEqual(len(vision_client.payloads), 1)
        self.assertEqual(vision_client.payloads[0]["model"], settings.vision_llm_chain[0]["model"])

        # The request actually sent to the vision model carried the content array.
        sent_messages = vision_client.payloads[0]["messages"]
        sent_user_msg = next(m for m in sent_messages if m["role"] == "user")
        self.assertEqual(sent_user_msg["content"], content)

    async def test_image_turn_content_array_persists_after_completion(self):
        """Stickiness: after the turn completes, the in-memory conversation
        keeps the image content array (no more downgrade-to-text) so a
        follow-up turn can still see the image — see agent.py docstring."""
        vision_client = _FakeChainClient(_text_reply_lines("It's a cat."))

        conversation = Conversation()
        content = self._content()
        with patch.object(agent, "llm_client", _FakeChainClient([])), \
             patch.object(agent, "vision_llm_client", vision_client):
            async for _ in agent.stream_agent_turn(
                conversation, _FakeMCP(), "[image: cat.png]", user_content=content,
            ):
                pass

        messages = conversation.get_messages()
        user_msg = next(m for m in messages if m["role"] == "user")
        self.assertEqual(user_msg["content"], content)
        self.assertIsInstance(user_msg["content"], list)
        self.assertTrue(conversation.has_images)

    async def test_image_turn_content_array_persists_even_on_llm_failure(self):
        """A failed vision call must not lose the image content array — the
        turn still failed, but the thread should stay vision-sticky."""
        class _BoomClient:
            @asynccontextmanager
            async def stream_chat(self, payload):
                raise RuntimeError("connection refused")
                yield  # pragma: no cover - unreachable, marks async generator

        conversation = Conversation()
        content = self._content()
        with patch.object(agent, "llm_client", _FakeChainClient([])), \
             patch.object(agent, "vision_llm_client", _BoomClient()):
            async for _ in agent.stream_agent_turn(
                conversation, _FakeMCP(), "[image: cat.png]", user_content=content,
            ):
                pass

        messages = conversation.get_messages()
        user_msg = next(m for m in messages if m["role"] == "user")
        self.assertEqual(user_msg["content"], content)
        self.assertTrue(conversation.has_images)

    async def test_followup_text_turn_after_image_stays_on_vision_chain(self):
        """Once a thread has seen an image, a SUBSEQUENT text-only turn (no
        ``user_content``) still routes through the vision chain and still
        carries the earlier image content array in the outgoing payload."""
        vision_client = _FakeMultiReplyChainClient([
            _text_reply_lines("It's a cat."),
            _text_reply_lines("The cat is orange."),
        ])
        text_client = _FakeChainClient([])

        conversation = Conversation()
        content = self._content()
        with patch.object(agent, "llm_client", text_client), \
             patch.object(agent, "vision_llm_client", vision_client):
            async for _ in agent.stream_agent_turn(
                conversation, _FakeMCP(), "[image: cat.png]", user_content=content,
            ):
                pass

            chunks = []
            async for chunk in agent.stream_agent_turn(
                conversation, _FakeMCP(), "what color is it?",
            ):
                chunks.append(chunk)

        self.assertEqual("".join(chunks).strip(), "The cat is orange.")
        # Both turns went to the vision client; none to the text client.
        self.assertEqual(len(vision_client.payloads), 2)
        self.assertEqual(text_client.payloads, [])
        self.assertEqual(
            vision_client.payloads[1]["model"], settings.vision_llm_chain[0]["model"]
        )

        # The second request still carried the original image content array.
        sent_messages = vision_client.payloads[1]["messages"]
        user_msgs = [m for m in sent_messages if m["role"] == "user"]
        self.assertEqual(user_msgs[0]["content"], content)
        self.assertEqual(user_msgs[-1]["content"], "what color is it?")


class _RecordingMCP(_FakeMCP):
    """Captures the server-name sets the agent asks for tools from."""

    def __init__(self):
        self.requested_servers: list[list[str]] = []

    def get_tools_for_servers(self, server_names):
        self.requested_servers.append(list(server_names))
        return []


class MainAgentToolScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_agent_offers_web_reader_alongside_search(self):
        text_client = _FakeChainClient(_text_reply_lines("Done."))
        vision_client = _FakeChainClient([])
        mcp = _RecordingMCP()

        conversation = Conversation()
        with patch.object(agent, "llm_client", text_client), \
             patch.object(agent, "vision_llm_client", vision_client):
            async for _ in agent.stream_agent_turn(conversation, mcp, "hello"):
                pass

        # The main agent scopes MCP tools to the web search/read + docproc
        # servers; the specialist domains (email/research/tasks) stay behind
        # consult_specialist and must not be offered directly.
        self.assertTrue(mcp.requested_servers)
        requested = mcp.requested_servers[0]
        self.assertIn("web-reader", requested)
        self.assertIn("web-search", requested)
        self.assertIn("document-processing", requested)
        for hidden in ("evangeline-email", "openalex", "vikunja-tasks"):
            self.assertNotIn(hidden, requested)


class StyleDirectiveTests(unittest.IsolatedAsyncioTestCase):
    def test_voice_source_selects_voice_directive(self):
        self.assertEqual(agent._style_directive("voice"), agent.VOICE_STYLE)

    def test_non_voice_sources_select_text_directive(self):
        for src in ("text", "matrix", "image", "file", "inbox_chat"):
            self.assertEqual(agent._style_directive(src), agent.TEXT_STYLE)

    async def _system_message_for(self, source: str) -> str:
        client = _FakeChainClient(_text_reply_lines("ok"))
        conversation = Conversation()
        with patch.object(agent, "llm_client", client), \
             patch.object(agent, "vision_llm_client", _FakeChainClient([])):
            async for _ in agent.stream_agent_turn(
                conversation, _FakeMCP(), "hello", source=source,
            ):
                pass
        return client.payloads[0]["messages"][0]["content"]

    async def test_voice_turn_injects_voice_directive_into_system_message(self):
        system = await self._system_message_for("voice")
        self.assertIn("[Channel: VOICE]", system)
        self.assertNotIn("[Channel: TEXT]", system)

    async def test_matrix_turn_injects_text_directive_into_system_message(self):
        system = await self._system_message_for("matrix")
        self.assertIn("[Channel: TEXT]", system)
        self.assertNotIn("[Channel: VOICE]", system)

    async def test_default_source_is_voice(self):
        # stream_agent_turn defaults source to "voice" when omitted.
        client = _FakeChainClient(_text_reply_lines("ok"))
        conversation = Conversation()
        with patch.object(agent, "llm_client", client), \
             patch.object(agent, "vision_llm_client", _FakeChainClient([])):
            async for _ in agent.stream_agent_turn(conversation, _FakeMCP(), "hello"):
                pass
        self.assertIn("[Channel: VOICE]", client.payloads[0]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
