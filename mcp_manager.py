import logging
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger(__name__)

MAX_RECONNECT_ATTEMPTS = 2


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

        self._register_tools(name, await session.list_tools())

    def _register_tools(self, server_name: str, result):
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
            # Replace existing entry if re-registering after reconnect
            self.tools = [
                tool for tool in self.tools
                if tool["function"]["name"] != t.name
            ]
            self.tools.append(openai_tool)
            self._tool_route[t.name] = server_name
            log.info("Registered tool %s from %s", t.name, server_name)

    async def _reconnect(self, server_name: str) -> bool:
        """Tear down and re-establish a single server connection."""
        cfg = self._configs.get(server_name)
        if not cfg or cfg["transport"] != "http":
            return False
        log.info("Reconnecting MCP server %s", server_name)
        # Old session/streams are orphaned in the exit stack — acceptable
        # since the server already dropped them.
        try:
            read_stream, write_stream, _ = await self._stack.enter_async_context(
                streamablehttp_client(cfg["url"])
            )
            session = await self._stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()
            self._sessions[server_name] = session
            self._register_tools(server_name, await session.list_tools())
            log.info("Reconnected MCP server %s", server_name)
            return True
        except Exception:
            log.exception("Reconnect failed for MCP server %s", server_name)
            return False

    def get_server_for_tool(self, name: str) -> str | None:
        """Return the server name that hosts a given tool."""
        return self._tool_route.get(name)

    async def call_tool(self, name: str, arguments: dict) -> str:
        server_name = self._tool_route.get(name)
        if not server_name:
            return f"Error: unknown tool '{name}'"

        for attempt in range(1 + MAX_RECONNECT_ATTEMPTS):
            session = self._sessions.get(server_name)
            if not session:
                return f"Error: server '{server_name}' not connected"
            try:
                result = await session.call_tool(name, arguments=arguments)
                parts = []
                for block in result.content:
                    if hasattr(block, "text"):
                        parts.append(block.text)
                text = "\n".join(parts)
                if len(text) > 4000:
                    text = text[:4000] + "\n... (truncated)"
                return text
            except Exception as e:
                err_str = str(e).lower()
                is_session_lost = (
                    "session terminated" in err_str
                    or "404" in err_str
                    or "connection" in err_str
                )
                if is_session_lost and attempt < MAX_RECONNECT_ATTEMPTS:
                    log.warning(
                        "Tool call %s failed (%s), attempting reconnect %d/%d",
                        name, e, attempt + 1, MAX_RECONNECT_ATTEMPTS,
                    )
                    if await self._reconnect(server_name):
                        continue
                log.exception("Tool call %s failed", name)
                return f"Error calling {name}: {e}"

        return f"Error calling {name}: max reconnect attempts exceeded"

    async def disconnect_all(self):
        await self._stack.aclose()
