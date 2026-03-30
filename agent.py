import json
import re
import uuid
import logging

import httpx

from config import LLM_URL, LLM_MODEL, MAX_TOOL_ROUNDS
from conversation import Conversation
from mcp_manager import MCPManager

log = logging.getLogger(__name__)

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


async def run_agent_turn(
    conversation: Conversation,
    mcp: MCPManager,
    user_text: str,
    status_callback=None,
) -> str:
    """Run one user turn through the agentic loop. Returns assistant text."""
    conversation.add_user(user_text)
    conversation.trim()

    for round_num in range(MAX_TOOL_ROUNDS):
        messages = conversation.get_messages()
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
        }
        if mcp.tools:
            payload["tools"] = mcp.tools

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(LLM_URL, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            log.exception("LLM request failed")
            return f"I'm having trouble reaching my brain right now. Error: {e}"

        choice = data["choices"][0]
        message = choice["message"]

        # Check for tool calls
        tool_calls = message.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                func = tc["function"]
                call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                name = func["name"]
                args_str = func.get("arguments", "{}")

                if status_callback:
                    await status_callback(f"Using tool: {name}...")

                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except json.JSONDecodeError:
                    args = {}

                conversation.add_tool_call(call_id, name, args_str if isinstance(args_str, str) else json.dumps(args_str))
                result = await mcp.call_tool(name, args)
                conversation.add_tool_result(call_id, result)

            conversation.trim()
            continue  # Loop back for the LLM to process tool results

        # No tool calls — we have a final text response
        content = message.get("content", "")
        content = THINK_RE.sub("", content).strip()

        if not content:
            content = "I'm not sure how to respond to that."

        conversation.add_assistant(content)
        return content

    # Safety limit reached
    fallback = "I've been going back and forth with my tools for a while. Let me just give you what I have so far."
    conversation.add_assistant(fallback)
    return fallback
