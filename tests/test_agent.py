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

    async def test_image_turn_downgrades_history_after_completion(self):
        """History-representation choice: after the turn completes, the
        in-memory conversation's image content array is swapped back for the
        plain-text placeholder — see agent.py docstring / commit message."""
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
        self.assertEqual(user_msg["content"], "[image: cat.png]")
        self.assertIsInstance(user_msg["content"], str)
        # No base64 leaks into what's left in conversation state.
        self.assertNotIn("AAAA", str(messages))

    async def test_image_turn_downgrades_history_even_on_llm_failure(self):
        """A failed vision call must not leave a base64 blob stuck in the
        in-memory conversation for later turns."""
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
        self.assertEqual(user_msg["content"], "[image: cat.png]")


if __name__ == "__main__":
    unittest.main()
