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
        self._server_status: dict[str, dict] = {
            name: {
                "connected": False,
                "transport": cfg.get("transport"),
                "tool_count": 0,
                "error": None,
            }
            for name, cfg in server_configs.items()
        }

    async def connect_all(self):
        for name, cfg in self._configs.items():
            try:
                await self._connect_one(name, cfg)
            except Exception:
                self._server_status[name]["connected"] = False
                self._server_status[name]["tool_count"] = 0
                self._server_status[name]["error"] = "connect_failed"
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
        self._server_status[name]["connected"] = True
        self._server_status[name]["error"] = None

        self._register_tools(name, await session.list_tools())

    def _register_tools(self, server_name: str, result):
        tool_count = 0
        cfg = self._configs.get(server_name, {})
        allowlist = cfg.get("tool_allowlist")
        upstream_names = {t.name for t in result.tools}
        for t in result.tools:
            if allowlist and t.name not in allowlist:
                log.debug("Skipping tool %s from %s (not in allowlist)", t.name, server_name)
                continue
            tool_count += 1
            # Collision detection: warn only if a *different* server owns this
            # name. Same-server re-registration after reconnect is expected
            # and silent.
            existing_owner = self._tool_route.get(t.name)
            if existing_owner and existing_owner != server_name:
                log.warning(
                    "Tool name collision: '%s' previously registered by '%s', "
                    "now being overwritten by '%s'",
                    t.name, existing_owner, server_name,
                )
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
        if allowlist:
            missing = set(allowlist) - upstream_names
            if missing:
                log.warning(
                    "Allowlist drift for server '%s': %d entry(ies) in "
                    "tool_allowlist not present upstream: %s",
                    server_name, len(missing), sorted(missing),
                )
        self._server_status[server_name]["tool_count"] = tool_count

    def get_registered_tool_names(self) -> set[str]:
        """Names of all MCP tools currently registered (post-allowlist)."""
        return set(self._tool_route.keys())

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
            self._server_status[server_name]["connected"] = True
            self._server_status[server_name]["error"] = None
            self._register_tools(server_name, await session.list_tools())
            log.info("Reconnected MCP server %s", server_name)
            return True
        except Exception:
            self._server_status[server_name]["connected"] = False
            self._server_status[server_name]["tool_count"] = 0
            self._server_status[server_name]["error"] = "reconnect_failed"
            log.exception("Reconnect failed for MCP server %s", server_name)
            return False

    def get_server_for_tool(self, name: str) -> str | None:
        """Return the server name that hosts a given tool."""
        return self._tool_route.get(name)

    def get_tools_for_servers(self, server_names: list[str]) -> list[dict]:
        """Return only tools belonging to the named servers."""
        names = set(server_names)
        return [
            t for t in self.tools
            if self._tool_route.get(t["function"]["name"]) in names
        ]

    async def call_tool(self, name: str, arguments: dict) -> str:
        server_name = self._tool_route.get(name)
        if not server_name:
            return f"Error: unknown tool '{name}'"

        for attempt in range(1 + MAX_RECONNECT_ATTEMPTS):
            session = self._sessions.get(server_name)
            if not session:
                self._server_status[server_name]["connected"] = False
                self._server_status[server_name]["error"] = "session_missing"
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
                self._server_status[server_name]["connected"] = True
                self._server_status[server_name]["error"] = None
                return text
            except Exception as e:
                err_str = str(e).lower()
                is_session_lost = (
                    "session terminated" in err_str
                    or "404" in err_str
                    or "connection" in err_str
                    or "closedresource" in err_str
                    or "broken pipe" in err_str
                    or "eof" in err_str
                )
                if is_session_lost and attempt < MAX_RECONNECT_ATTEMPTS:
                    self._server_status[server_name]["connected"] = False
                    self._server_status[server_name]["error"] = "session_lost"
                    log.warning(
                        "Tool call %s failed (%s), attempting reconnect %d/%d",
                        name, e, attempt + 1, MAX_RECONNECT_ATTEMPTS,
                    )
                    if await self._reconnect(server_name):
                        continue
                self._server_status[server_name]["error"] = str(e)
                log.exception("Tool call %s failed", name)
                return f"Error calling {name}: {e}"

        return f"Error calling {name}: max reconnect attempts exceeded"

    async def disconnect_all(self):
        await self._stack.aclose()

    def get_health(self) -> dict:
        total_servers = len(self._configs)
        connected_servers = sum(1 for status in self._server_status.values() if status["connected"])
        degraded = connected_servers < total_servers
        ready = connected_servers > 0 if total_servers > 0 else True
        return {
            "configured_servers": total_servers,
            "connected_servers": connected_servers,
            "ready": ready,
            "degraded": degraded,
            "servers": self._server_status,
        }
