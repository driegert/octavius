"""Compatibility wrapper around tools.py local-tool dispatch."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from tools import get_local_tool_handlers

if TYPE_CHECKING:
    from history import ConversationSession
    from mcp_manager import MCPManager


async def call_local_tool(
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
