"""Internal subagent for delegated tool-heavy tasks.

The main Octavius agent keeps a lean set of core tools. When the user asks
about email, research, or tasks, the main agent calls delegate_task which
runs a scoped subagent here — a separate non-streaming LLM loop with only
the tools for that domain.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Callable

from service_clients import llm_client
from settings import format_vikunja_default, format_vikunja_projects, settings

if TYPE_CHECKING:
    from mcp_manager import MCPManager

log = logging.getLogger(__name__)

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

SUBAGENT_DOMAINS: dict[str, dict] = {
    "email": {
        "servers": ["evangeline-email"],
        "system_prompt": (
            "You are an email assistant for Dave. You have access to email tools "
            "to search, read, and analyze Dave's email. Be thorough but concise — "
            "your output will be spoken aloud by a voice assistant.\n"
            "Do NOT use markdown formatting, bullet points, or numbered lists. "
            "Summarize findings conversationally. Don't read out full email addresses or URLs."
        ),
        "max_rounds": 5,
    },
    "research": {
        "servers": ["openalex"],
        "system_prompt": (
            "You are a research assistant for Dave with access to OpenAlex. "
            "Find papers, authors, citations, and research trends as requested. "
            "Be thorough but concise — your output will be spoken aloud by a voice assistant.\n"
            "Do NOT use markdown formatting, bullet points, or numbered lists. "
            "Summarize findings conversationally. Don't read out URLs or DOIs."
        ),
        "max_rounds": 5,
    },
    "tasks": {
        "servers": ["vikunja-tasks"],
        "system_prompt": (
            "You are a task management assistant for Dave with access to Vikunja. "
            "Search, create, and update tasks as requested. "
            "Be concise — your output will be spoken aloud by a voice assistant.\n"
            "Do NOT use markdown formatting, bullet points, or numbered lists.\n"
            "Vikunja guidelines:\n"
            "- Always set done=false when searching tasks unless Dave asks about completed ones.\n"
            "- Sort by due_date or created when listing tasks so the most relevant appear first.\n"
            "- When creating tasks, ask which project if not obvious from context.\n"
            "- Avoid calling search_tasks more than twice for the same intent. "
            "If two searches haven't surfaced the specific task, stop searching — "
            "summarize what you did find and ask Dave to narrow it down (e.g. by "
            "project, keyword, or due date) instead of retrying the same query.\n"
            f"- Key projects: {format_vikunja_projects()}.\n"
            f"- Default to {format_vikunja_default()} if Dave doesn't specify a project."
        ),
        "max_rounds": 6,
    },
}

MAX_RESULT_CHARS = 4000
TOOL_DATA_HEADER = (
    "===TOOL DATA (authoritative source for IDs and field values; "
    "do not read aloud)==="
)


def _compose_result(final_text: str, observations: list[tuple[str, dict, str]]) -> str:
    """Combine the subagent's natural-language summary with a verbatim block
    of the raw tool observations, so the caller can extract exact IDs and
    field values instead of relying on LLM-paraphrased numbers.
    """
    if not observations:
        return final_text[:MAX_RESULT_CHARS]

    header = f"\n\n{TOOL_DATA_HEADER}\n"
    # Reserve budget for the natural-language summary + header + newlines.
    remaining = MAX_RESULT_CHARS - len(final_text) - len(header)
    if remaining <= 0:
        return final_text[:MAX_RESULT_CHARS]

    # Include observations newest-first so the most recent (usually the
    # mutation the caller will ask about next) always survives truncation.
    kept: list[str] = []
    for name, args, result in reversed(observations):
        try:
            args_repr = json.dumps(args, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            args_repr = str(args)
        line = f"[{name}] args={args_repr}\n{result}"
        if len(line) + 2 > remaining:
            if remaining > 40:
                line = line[: remaining - len("\n...(truncated)")] + "\n...(truncated)"
                kept.append(line)
            break
        kept.append(line)
        remaining -= len(line) + 2  # 2 for the "\n\n" separator

    # Reverse back to chronological order for readability.
    block = "\n\n".join(reversed(kept))
    combined = f"{final_text}{header}{block}"
    return combined[:MAX_RESULT_CHARS]


async def run_subagent(
    task: str,
    domain: str,
    mcp: "MCPManager",
    status_callback: Callable[[str], object] | None = None,
) -> str:
    """Run a scoped subagent loop and return the final text plus raw tool data."""
    config = SUBAGENT_DOMAINS.get(domain)
    if not config:
        return f"Error: unknown delegation domain '{domain}'"

    tools = mcp.get_tools_for_servers(config["servers"])
    if not tools:
        return f"Error: no tools available for domain '{domain}'"

    messages: list[dict] = [
        {"role": "system", "content": config["system_prompt"]},
        {"role": "user", "content": task},
    ]

    max_rounds = config["max_rounds"]
    last_text = ""
    observations: list[tuple[str, dict, str]] = []

    for round_num in range(max_rounds):
        payload = {
            "model": settings.llm_chain[0]["model"],
            "messages": messages,
            "tools": tools,
        }

        log.info(
            "Subagent [%s] round %d/%d, %d messages, %d tools",
            domain, round_num + 1, max_rounds, len(messages), len(tools),
        )

        message = await llm_client.complete_with_tools(payload)
        if message is None:
            fallback = last_text or "Error: all LLM endpoints failed during delegation."
            return _compose_result(fallback, observations)

        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []

        # Strip Qwen think tags
        clean_content = THINK_RE.sub("", content).strip()
        if clean_content:
            last_text = clean_content

        if not tool_calls:
            # Final text response
            final_text = last_text or "The assistant completed but produced no output."
            return _compose_result(final_text, observations)

        # Append the assistant message (with tool_calls) to context
        messages.append(message)

        # Execute each tool call
        for tc in tool_calls:
            func = tc.get("function", {})
            name = func.get("name", "")
            call_id = tc.get("id", "")

            if status_callback:
                label = settings.tool_labels.get(name, name)
                await status_callback(f"{label}...")

            try:
                args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}

            result = await mcp.call_tool(name, args)
            try:
                args_preview = json.dumps(args, separators=(",", ":"), default=str)
            except (TypeError, ValueError):
                args_preview = str(args)
            if len(args_preview) > 200:
                args_preview = args_preview[:200] + "..."
            log.info(
                "Subagent [%s] tool %s args=%s → %d chars",
                domain, name, args_preview, len(result),
            )
            observations.append((name, args, result))

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result,
            })

    # Max rounds exhausted
    final_text = last_text or "I ran out of steps before finishing. Here's what I found so far."
    return _compose_result(final_text, observations)
