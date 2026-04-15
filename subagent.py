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
from settings import settings

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
            "- Key projects: Inbox (id=1), Teaching and Trent (id=9), math1052 (id=10), "
            "amod5240 (id=2), math3560 (id=3), Email Tasks (id=14), Personal and "
            "Professional (id=13), PhD (id=4), Projects (id=5), AI Projects (id=6), "
            "SSC 2026 Workshop (id=11), Exploration (id=8).\n"
            "- Default to Inbox (id=1) if Dave doesn't specify a project."
        ),
        "max_rounds": 4,
    },
}

MAX_RESULT_CHARS = 4000


async def run_subagent(
    task: str,
    domain: str,
    mcp: "MCPManager",
    status_callback: Callable[[str], object] | None = None,
) -> str:
    """Run a scoped subagent loop and return the final text."""
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
            return last_text or "Error: all LLM endpoints failed during delegation."

        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []

        # Strip Qwen think tags
        clean_content = THINK_RE.sub("", content).strip()
        if clean_content:
            last_text = clean_content

        if not tool_calls:
            # Final text response
            return (last_text or "The assistant completed but produced no output.")[:MAX_RESULT_CHARS]

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
            log.info("Subagent [%s] tool %s → %d chars", domain, name, len(result))

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": result,
            })

    # Max rounds exhausted
    return (last_text or "I ran out of steps before finishing. Here's what I found so far.")[:MAX_RESULT_CHARS]
