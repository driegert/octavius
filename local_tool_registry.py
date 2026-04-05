"""Local tool registry and dispatch."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Callable

from local_tool_downloads import download_file
from local_tool_inbox import read_item_content, save_to_inbox
from local_tool_reader import process_pdf_background, read_document

if TYPE_CHECKING:
    from history import ConversationSession


def get_local_tool_handlers() -> dict[str, Callable]:
    return {
        "download_file": download_file,
        "save_to_inbox": save_to_inbox,
        "read_item_content": read_item_content,
        "read_document": read_document,
        "process_pdf": process_pdf_background,
    }


async def call_local_tool(
    name: str,
    arguments: dict,
    history_session: "ConversationSession | None" = None,
) -> str:
    handler = get_local_tool_handlers().get(name)
    if not handler:
        return f"Error: unknown local tool '{name}'"
    if inspect.iscoroutinefunction(handler):
        return await handler(arguments, history_session)
    return handler(arguments, history_session)
