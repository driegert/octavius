import asyncio
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
from subagent import _unwrap_double_encoded_args, parse_xml_tool_calls
import tools as local_tools

log = logging.getLogger(__name__)

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# Sentence-ending punctuation followed by space or end-of-string
SENTENCE_END = re.compile(r'(?<=[.!?])\s+')

# Thread-start episodic recall (Q1): non-authoritative "related past discussions".
RECALL_DISTANCE_CUTOFF = 0.6
RECALL_LIMIT = 2


async def _build_memory_block(db_path, user_text, *, first_turn=False,
                              current_conv_id=None) -> str:
    """Assemble the per-turn long-term-memory text folded into messages[0].

    Always-on profile + per-turn facts come from the memory SERVICE over loopback
    (deduped server-side against the profile). First-turn episodic recall stays
    LOCAL (Octavius's own summary corpus). Best-effort: any failure returns "" and
    the turn proceeds memory-less.
    """
    from memory_client import memory_client

    parts: list[str] = []
    try:
        profile, fact_lines = await memory_client.fetch_injection(user_text)
    except Exception:
        log.warning("Memory fetch failed; turn proceeds without memory", exc_info=True)
        profile, fact_lines = "", []
    if profile:
        parts.append(profile)
    if fact_lines:
        lines = "\n".join(f"- {line}" for line in fact_lines)
        parts.append("Possibly relevant facts for this message:\n" + lines)

    # First message of a thread: surface related past conversations (episodic,
    # non-authoritative). Local summary-embedding search; embeds, so run off-loop.
    if first_turn and db_path is not None:
        try:
            recall = await asyncio.to_thread(
                _episodic_recall, db_path, user_text, current_conv_id)
        except Exception:
            recall = ""
        if recall:
            parts.append(recall)

    return "\n\n".join(parts)


def _episodic_recall(db_path, user_text, current_conv_id) -> str:
    """First-turn episodic recall over Octavius's LOCAL summary corpus. Opens its
    own short-lived connection (runs in a worker thread; sync embed stays off-loop)."""
    try:
        from db import connect as _connect
        from history_store import search_conversations
    except Exception:
        return ""
    conn = None
    try:
        conn = _connect(db_path)
        convs = search_conversations(conn, user_text, service=None,
                                     limit=RECALL_LIMIT + 3)
        related = [
            c for c in convs
            if c.get("conversation_id") != current_conv_id and c.get("summary")
            and (c.get("distance") is None or c["distance"] < RECALL_DISTANCE_CUTOFF)
        ][:RECALL_LIMIT]
        if related:
            lines = "\n".join(f"- {c['summary']}" for c in related)
            return ("You may have discussed related topics before "
                    "(context only, may be irrelevant):\n" + lines)
        return ""
    except Exception:
        log.warning("Episodic recall failed", exc_info=True)
        return ""
    finally:
        if conn is not None:
            conn.close()


async def run_agent_turn(
    conversation: Conversation,
    mcp: MCPManager,
    user_text: str,
    status_callback=None,
    history_session=None,
    session=None,
) -> str:
    """Run one user turn (non-streaming). Returns full assistant text."""
    result_parts = []
    async for chunk in stream_agent_turn(
        conversation, mcp, user_text, status_callback, history_session, session,
    ):
        result_parts.append(chunk)
    return "".join(result_parts)


async def stream_agent_turn(
    conversation: Conversation,
    mcp: MCPManager,
    user_text: str,
    status_callback=None,
    history_session=None,
    session=None,
) -> AsyncGenerator[str, None]:
    """Run one user turn, yielding sentence chunks as the LLM streams them.

    Tool call rounds are handled internally (non-streaming). Only the final
    text response is streamed sentence-by-sentence.
    """
    conversation.add_user(user_text)
    conversation.trim()

    # --- Long-term memory injection (Step 4) ---------------------------------
    # Built once per turn, folded into messages[0] ONLY below — never persisted,
    # so it refreshes every turn and is immune to conversation.trim().
    memory_block = ""
    db_path = getattr(history_session, "db_path", None)
    ua_count = sum(
        1 for m in conversation.get_messages() if m.get("role") in ("user", "assistant")
    )
    try:
        memory_block = await _build_memory_block(
            db_path, user_text,
            first_turn=(ua_count == 1),
            current_conv_id=getattr(history_session, "conv_id", None),
        )
    except Exception:
        log.warning("Memory injection failed; turn proceeds without memory", exc_info=True)
        memory_block = ""

    for round_num in range(settings.max_tool_rounds):
        messages = conversation.get_messages()

        # Fold memory into the system message (index 0). Qwen requires system only
        # at the start, so we rebuild messages[0] rather than insert mid-list.
        if memory_block and messages and messages[0].get("role") == "system":
            messages = [
                {"role": "system",
                 "content": messages[0]["content"] + "\n\n" + memory_block}
            ] + messages[1:]

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
        # Only offer core MCP tools + local tools. Specialist domains (email,
        # research, tasks) are handled by the scoped subagent, invoked inline
        # via the consult_specialist local tool.
        all_tools = mcp.get_tools_for_servers(["searxng", "document-processing"]) + local_tools.TOOLS
        if all_tools:
            payload["tools"] = all_tools

        payload_chars = sum(len(json.dumps(m)) for m in messages)
        tool_chars = sum(len(json.dumps(t)) for t in all_tools) if all_tools else 0
        log.debug(
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

                    # Only treat tool_calls as exclusive of content when the
                    # list is actually populated. vLLM emits tool_calls=[]
                    # alongside every content delta — using `in` alone would
                    # skip all content tokens.
                    if delta.get("tool_calls"):
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

        # Some OpenAI-compatible servers (vLLM with Qwen, certain llama.cpp
        # chat templates) emit tool calls as Hermes-XML inside content with
        # tool_calls=[]. Parse them into tool_calls_acc so the agent still
        # executes the tool instead of speaking the XML.
        if not tool_calls_acc and "<tool_call>" in full_content:
            parsed, stripped = parse_xml_tool_calls(full_content)
            if parsed:
                for idx, call in enumerate(parsed):
                    tool_calls_acc[idx] = {
                        "id": call["id"] or f"call_{uuid.uuid4().hex[:8]}",
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                    }
                full_content = stripped
                log.info("Agent parsed %d XML tool call(s) from content", len(parsed))

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
                args = _unwrap_double_encoded_args(args)

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
                        session=session,
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
