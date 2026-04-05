"""Public entrypoint for local tool specs and dispatch."""

from local_tool_registry import call_local_tool
from local_tool_specs import TOOLS


async def call_tool(name: str, arguments: dict, history_session=None) -> str:
    return await call_local_tool(name, arguments, history_session)
