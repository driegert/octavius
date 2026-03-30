import logging
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger(__name__)


class MCPManager:
    def __init__(self, server_configs: dict):
        self._configs = server_configs
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tool_route: dict[str, str] = {}  # tool_name -> server_name
        self.tools: list[dict] = []  # OpenAI function-calling format

    async def connect_all(self):
        for name, cfg in self._configs.items():
            try:
                await self._connect_one(name, cfg)
            except Exception:
                log.exception("Failed to connect MCP server %s", name)

    async def _connect_one(self, name: str, cfg: dict):
        if cfg["transport"] == "stdio":
            params = StdioServerParameters(
                command=cfg["command"],
                args=cfg.get("args", []),
                env=cfg.get("env"),
            )
            read_stream, write_stream = await self._stack.enter_async_context(
                stdio_client(params)
            )
        elif cfg["transport"] == "http":
            read_stream, write_stream, _ = await self._stack.enter_async_context(
                streamablehttp_client(cfg["url"])
            )
        else:
            raise ValueError(f"Unknown transport: {cfg['transport']}")

        session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        self._sessions[name] = session

        result = await session.list_tools()
        for t in result.tools:
            openai_tool = {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.inputSchema
                    or {"type": "object", "properties": {}},
                },
            }
            self.tools.append(openai_tool)
            self._tool_route[t.name] = name
            log.info("Registered tool %s from %s", t.name, name)

    async def call_tool(self, name: str, arguments: dict) -> str:
        server_name = self._tool_route.get(name)
        if not server_name:
            return f"Error: unknown tool '{name}'"
        session = self._sessions[server_name]
        try:
            result = await session.call_tool(name, arguments=arguments)
            parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
            text = "\n".join(parts)
            # Truncate verbose tool results to avoid blowing context
            if len(text) > 2000:
                text = text[:2000] + "\n... (truncated)"
            return text
        except Exception as e:
            log.exception("Tool call %s failed", name)
            return f"Error calling {name}: {e}"

    async def disconnect_all(self):
        await self._stack.aclose()
