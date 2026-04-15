import json
import re
import time
import uuid
import logging
from collections.abc import AsyncGenerator

from conversation import Conversation
from mcp_manager import MCPManager
from service_clients import llm_client
from settings import settings
import tools as local_tools

log = logging.getLogger(__name__)

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# Sentence-ending punctuation followed by space or end-of-string
SENTENCE_END = re.compile(r'(?<=[.!?])\s+')


async def run_agent_turn(
    conversation: Conversation,
    mcp: MCPManager,
    user_text: str,
    status_callback=None,
    history_session=None,
) -> str:
    """Run one user turn (non-streaming). Returns full assistant text."""
    result_parts = []
    async for chunk in stream_agent_turn(conversation, mcp, user_text, status_callback, history_session):
        result_parts.append(chunk)
    return "".join(result_parts)


async def stream_agent_turn(
    conversation: Conversation,
    mcp: MCPManager,
    user_text: str,
    status_callback=None,
    history_session=None,
) -> AsyncGenerator[str, None]:
    """Run one user turn, yielding sentence chunks as the LLM streams them.

    Tool call rounds are handled internally (non-streaming). Only the final
    text response is streamed sentence-by-sentence.
    """
    # Make status_callback available to delegate_task → subagent
    local_tools._status_callback = status_callback

    conversation.add_user(user_text)
    conversation.trim()

    for round_num in range(settings.max_tool_rounds):
        messages = conversation.get_messages()

        # On later rounds, nudge the LLM to wrap up instead of spiraling.
        # Must NOT use role=system here — Qwen's chat template requires
        # system messages only at the beginning.
        if round_num >= settings.max_tool_rounds - 2:
            messages = messages + [{
                "role": "user",
                "content": "[System note: You have used many tool calls. Summarize what you've found so far and respond to the user now. Do not make more tool calls.]",
            }]

        payload = {
            "model": settings.llm_chain[0]["model"],
            "messages": messages,
            "stream": True,
        }
        # Only offer core MCP tools + local tools. Delegated domains (email,
        # research, tasks) are handled by the subagent via delegate_task.
        all_tools = mcp.get_tools_for_servers(["searxng", "document-processing"]) + local_tools.TOOLS
        if all_tools:
            payload["tools"] = all_tools

        payload_chars = sum(len(json.dumps(m)) for m in messages)
        tool_chars = sum(len(json.dumps(t)) for t in all_tools) if all_tools else 0
        log.info(
            "LLM request: round %d/%d, %d messages (%d chars), %d tools (%d chars)",
            round_num + 1, settings.max_tool_rounds,
            len(messages), payload_chars,
            len(all_tools) if all_tools else 0, tool_chars,
        )

        # --- Streaming request ---
        try:
            async with llm_client.stream_chat(payload) as resp:
                full_content = ""
                tool_calls_acc: dict[int, dict] = {}
                in_think = False
                sentence_buffer = ""
                buffered_sentences: list[str] = []

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    delta = data["choices"][0].get("delta", {})

                    if "tool_calls" in delta:
                        for tc_delta in delta["tool_calls"]:
                            idx = tc_delta["index"]
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": tc_delta.get("id", ""),
                                    "name": tc_delta.get("function", {}).get("name", ""),
                                    "arguments": "",
                                }
                            else:
                                if tc_delta.get("id"):
                                    tool_calls_acc[idx]["id"] = tc_delta["id"]
                                if tc_delta.get("function", {}).get("name"):
                                    tool_calls_acc[idx]["name"] = tc_delta["function"]["name"]
                            args_chunk = tc_delta.get("function", {}).get("arguments", "")
                            if args_chunk:
                                tool_calls_acc[idx]["arguments"] += args_chunk
                        continue

                    token = delta.get("content", "")
                    if not token:
                        continue

                    full_content += token

                    if "<think>" in token:
                        in_think = True
                    if in_think:
                        if "</think>" in token:
                            in_think = False
                        continue

                    sentence_buffer += token

                    parts = SENTENCE_END.split(sentence_buffer)
                    if len(parts) > 1:
                        for sentence in parts[:-1]:
                            sentence = sentence.strip()
                            if sentence:
                                buffered_sentences.append(sentence + " ")
                        sentence_buffer = parts[-1]

        except Exception as e:
            log.exception("LLM request failed")
            yield f"I'm having trouble reaching my brain right now. Error: {e}"
            return

        # --- Handle tool calls if any ---
        if tool_calls_acc:
            # Discard any text generated alongside tool calls — the LLM will
            # produce a proper response after seeing tool results.
            buffered_sentences.clear()
            sentence_buffer = ""

            for idx in sorted(tool_calls_acc.keys()):
                tc = tool_calls_acc[idx]
                call_id = tc["id"] or f"call_{uuid.uuid4().hex[:8]}"
                name = tc["name"]
                args_str = tc["arguments"]

                if status_callback:
                    label = settings.tool_labels.get(name, name)
                    await status_callback(f"{label}...")

                try:
                    args = json.loads(args_str) if args_str else {}
                except json.JSONDecodeError:
                    args = {}

                conversation.add_tool_call(call_id, name, args_str)
                # Route to local tools or MCP
                local_tool_names = {t["function"]["name"] for t in local_tools.TOOLS}
                tc_start = time.monotonic()
                if name in local_tool_names:
                    result = await local_tools.call_tool(
                        name,
                        args,
                        history_session=history_session,
                        mcp_manager=mcp,
                    )
                    server_name = "local"
                else:
                    result = await mcp.call_tool(name, args)
                    server_name = mcp.get_server_for_tool(name)
                tc_duration_ms = int((time.monotonic() - tc_start) * 1000)
                conversation.add_tool_result(call_id, result)

                # Record tool call in history
                if history_session:
                    tc_status = "error" if result.startswith("Error") else "success"
                    # Use the last recorded assistant message as parent,
                    # or record a synthetic tool-role message
                    history_msg_id = await history_session.add_message_async(
                        role="tool", content=result[:500],
                        model=None, latency_ms=tc_duration_ms,
                    )
                    history_session.add_tool_call(
                        message_id=history_msg_id,
                        tool_name=name,
                        server_name=server_name,
                        arguments=args,
                        status=tc_status,
                        result_summary=result[:500],
                        result_size=len(result),
                        duration_ms=tc_duration_ms,
                    )

            conversation.trim()
            continue  # Loop back for LLM to process tool results

        # --- Final text response (no tool calls) ---
        # Yield all buffered sentences
        for sent in buffered_sentences:
            yield sent
        # Flush remaining sentence buffer
        remaining = sentence_buffer.strip()
        if remaining:
            yield remaining

        # Clean full content for conversation history
        clean = THINK_RE.sub("", full_content).strip()
        if not clean:
            clean = "I'm not sure how to respond to that."
            yield clean

        conversation.add_assistant(clean)
        return

    # Safety limit reached (shouldn't happen since last round has no tools)
    fallback = "I ran into a limit on how many tool calls I can make. Could you try rephrasing your request?"
    conversation.add_assistant(fallback)
    yield fallback
