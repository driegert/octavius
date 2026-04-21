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
