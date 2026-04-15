"""Public entrypoint for local tool specs and dispatch."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Callable

from local_tool_downloads import download_file
from local_tool_inbox import list_stash_items, read_item_content, save_to_stash
from local_tool_reader import list_reader_documents, process_pdf_background, read_document
from local_tool_specs import TOOLS

if TYPE_CHECKING:
    from history import ConversationSession
    from mcp_manager import MCPManager

# Status callback is stashed here by websocket_session before the agent turn
# so that delegate_task can forward it to the subagent.
_status_callback = None


async def _delegate_task(args: dict, session=None, mcp_manager=None) -> str:
    from subagent import run_subagent

    domain = args.get("domain", "")
    task = args.get("task", "")
    if not domain or not task:
        return "Error: domain and task are required."
    if mcp_manager is None:
        return "Error: MCP manager unavailable."
    return await run_subagent(task, domain, mcp_manager, status_callback=_status_callback)


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
    }


async def call_tool(
    name: str,
    arguments: dict,
    history_session: "ConversationSession | None" = None,
    mcp_manager: "MCPManager | None" = None,
) -> str:
    handler = get_local_tool_handlers().get(name)
    if not handler:
        return f"Error: unknown local tool '{name}'"
    params = inspect.signature(handler).parameters
    if inspect.iscoroutinefunction(handler):
        if len(params) >= 3:
            return await handler(arguments, history_session, mcp_manager)
        return await handler(arguments, history_session)
    if len(params) >= 3:
        return handler(arguments, history_session, mcp_manager)
    return handler(arguments, history_session)
