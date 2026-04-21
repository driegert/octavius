"""Public entrypoint for local tool specs and dispatch."""

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING, Callable

from local_tool_downloads import download_file
from local_tool_inbox import list_stash_items, read_item_content, save_to_stash
from local_tool_reader import list_reader_documents, process_pdf_background, read_document
from local_tool_specs import TOOLS

if TYPE_CHECKING:
    from history import ConversationSession
    from mcp_manager import MCPManager


async def _delegate_task(args: dict, history_session=None, mcp_manager=None, session=None) -> str:
    domain = args.get("domain", "")
    task = args.get("task", "")
    if not domain or not task:
        return "Error: domain and task are required."
    if session is None:
        return "Error: delegation session unavailable."
    summary = await session.spawn_delegation(domain, task)
    summary["note"] = (
        "Reply briefly to acknowledge (e.g. 'on it'). Results will be spoken "
        "when ready. Do not wait."
    )
    return json.dumps(summary)


async def _cancel_delegation(args: dict, history_session=None, mcp_manager=None, session=None) -> str:
    handle = args.get("handle", "")
    if not handle:
        return "Error: handle is required."
    if session is None:
        return "Error: delegation session unavailable."
    result = await session.cancel_delegation(handle)
    return json.dumps(result)


def get_local_tool_handlers() -> dict[str, Callable]:
    return {
        "download_file": download_file,
        "save_to_stash": save_to_stash,
        "list_stash_items": list_stash_items,
        "read_item_content": read_item_content,
        "read_document": read_document,
        "list_reader_documents": list_reader_documents,
        "process_pdf": process_pdf_background,
        "delegate_task": _delegate_task,
        "cancel_delegation": _cancel_delegation,
    }


def validate_local_tool_registry() -> list[str]:
    """Return a list of inconsistencies between local tool specs and handlers.
    Empty list means everything matches. Each entry is a human-readable
    description of one drift — suitable for log.warning output.
    """
    spec_names = {t["function"]["name"] for t in TOOLS}
    handler_names = set(get_local_tool_handlers().keys())
    issues: list[str] = []
    for name in sorted(spec_names - handler_names):
        issues.append(f"Local tool spec '{name}' has no handler in tools.py")
    for name in sorted(handler_names - spec_names):
        issues.append(f"Local tool handler '{name}' has no spec in local_tool_specs.py")
    return issues


async def call_tool(
    name: str,
    arguments: dict,
    history_session: "ConversationSession | None" = None,
    mcp_manager: "MCPManager | None" = None,
    session=None,
) -> str:
    handler = get_local_tool_handlers().get(name)
    if not handler:
        return f"Error: unknown local tool '{name}'"
    params = inspect.signature(handler).parameters
    arity = len(params)
    if inspect.iscoroutinefunction(handler):
        if arity >= 4:
            return await handler(arguments, history_session, mcp_manager, session)
        if arity >= 3:
            return await handler(arguments, history_session, mcp_manager)
        return await handler(arguments, history_session)
    if arity >= 4:
        return handler(arguments, history_session, mcp_manager, session)
    if arity >= 3:
        return handler(arguments, history_session, mcp_manager)
    return handler(arguments, history_session)
