import logging
import re
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger(__name__)

MAX_RECONNECT_ATTEMPTS = 2


def _sanitize_prefix(text: str) -> str:
    """Make a server key / prefix safe inside an OpenAI function name."""
    return re.sub(r"[^A-Za-z0-9_]", "_", text)


def display_tool_name(upstream_name: str, prefix: str | None) -> str:
    """Model-facing name for an upstream MCP tool.

    With no `tool_prefix` configured the upstream name is used unchanged
    (the historical behaviour). With a prefix, the name becomes
    `<prefix>_<upstream>` unless the upstream name already starts with
    `<prefix>_` — the same rule pi-mcp-adapter's formatToolName applies
    (2.35.0+), so e.g. `email` + `stats` -> `email_stats` in both clients.
    """
    if not prefix:
        return upstream_name
    p = _sanitize_prefix(prefix)
    if upstream_name.startswith(f"{p}_") and len(upstream_name) > len(p) + 1:
        return upstream_name
    return f"{p}_{upstream_name}"


class MCPManager:
    def __init__(self, server_configs: dict):
        self._configs = server_configs
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        # Keyed by the MODEL-FACING (display) name. `_tool_route` maps it to
        # the owning server; `_tool_upstream` maps it to the tool's real name
        # on that server when the two differ (a `tool_prefix` or a collision
        # rename). A display name absent from `_tool_upstream` is its own
        # upstream name.
        self._tool_route: dict[str, str] = {}  # display name -> server_name
        self._tool_upstream: dict[str, str] = {}  # display name -> upstream tool name
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
        description_suffix = cfg.get("tool_description_suffix") or ""
        upstream_names = {t.name for t in result.tools}
        prefix = cfg.get("tool_prefix")
        # A reconnect re-lists the server's tools: drop this server's previous
        # registrations first so a tool removed/renamed upstream doesn't
        # linger, and so re-registration is never mistaken for a collision.
        stale = {n for n, owner in self._tool_route.items() if owner == server_name}
        if stale:
            self.tools = [t for t in self.tools if t["function"]["name"] not in stale]
            for n in stale:
                self._tool_route.pop(n, None)
                self._tool_upstream.pop(n, None)
        for t in result.tools:
            if allowlist and t.name not in allowlist:
                log.debug("Skipping tool %s from %s (not in allowlist)", t.name, server_name)
                continue
            name = display_tool_name(t.name, prefix)
            # Collision: a *different* server already owns this display name.
            # The first owner keeps it — a later server must never silently
            # take over a name (2026-09-23: before this, the later server
            # won, so two servers both exposing `search` would make one of
            # them unreachable). The newcomer is exposed as
            # `<server key>_<name>` instead; if even that is taken, skipped.
            existing_owner = self._tool_route.get(name)
            if existing_owner and existing_owner != server_name:
                fallback = f"{_sanitize_prefix(server_name)}_{name}"
                if fallback in self._tool_route:
                    log.error(
                        "Tool name collision: '%s' from '%s' is owned by '%s' and "
                        "fallback '%s' is taken too; skipping",
                        name, server_name, existing_owner, fallback,
                    )
                    continue
                log.warning(
                    "Tool name collision: '%s' already registered by '%s'; "
                    "registering '%s' from '%s' as '%s'",
                    name, existing_owner, t.name, server_name, fallback,
                )
                name = fallback
            tool_count += 1
            openai_tool = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": (t.description or "") + description_suffix,
                    "parameters": t.inputSchema
                    or {"type": "object", "properties": {}},
                },
            }
            self.tools.append(openai_tool)
            self._tool_route[name] = server_name
            if name != t.name:
                self._tool_upstream[name] = t.name
            log.info("Registered tool %s from %s (upstream %s)", name, server_name, t.name)
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
        """Model-facing names of all MCP tools currently registered (post-allowlist)."""
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

    async def call_tool(self, name: str, arguments: dict, max_chars: int | None = 4000) -> str:
        """Call a tool by its MODEL-FACING name (what `self.tools` advertises)."""
        server_name = self._tool_route.get(name)
        if not server_name:
            return f"Error: unknown tool '{name}'"
        return await self._call(server_name, self._tool_upstream.get(name, name), arguments, max_chars)

    async def call_server_tool(
        self, server_name: str, tool_name: str, arguments: dict, max_chars: int | None = 4000,
    ) -> str:
        """Call a tool by (server key, upstream tool name), bypassing display
        names entirely. For code paths (REST routes, pipelines) that target
        one specific server and must not depend on `tool_prefix` or on
        collision renames."""
        if server_name not in self._server_status:
            return f"Error: unknown server '{server_name}'"
        return await self._call(server_name, tool_name, arguments, max_chars)

    async def _call(
        self, server_name: str, name: str, arguments: dict, max_chars: int | None,
    ) -> str:
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
                if max_chars is not None and len(text) > max_chars:
                    text = text[:max_chars] + "\n... (truncated)"
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
