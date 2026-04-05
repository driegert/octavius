"""Public entrypoint for local tool specs and dispatch."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Callable

from local_tool_downloads import download_file
from local_tool_inbox import read_item_content, save_to_inbox
from local_tool_reader import process_pdf_background, read_document
from local_tool_specs import TOOLS

if TYPE_CHECKING:
    from history import ConversationSession
    from mcp_manager import MCPManager


def get_local_tool_handlers() -> dict[str, Callable]:
    return {
        "download_file": download_file,
        "save_to_inbox": save_to_inbox,
        "read_item_content": read_item_content,
        "read_document": read_document,
        "process_pdf": process_pdf_background,
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
