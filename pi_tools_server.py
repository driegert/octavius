"""Loopback MCP server that gives the pi brain Octavius's own local tools.

docs/pi-backend-design.md, rollout step 5. pi can reach the fleet's MCP servers
but not Octavius's in-process handlers (vault notes, memory, conversation
history, reader). This module serves a fixed subset of ``local_tool_specs.TOOLS``
as MCP tools and dispatches every call through ``tools.call_tool``, so behaviour
is the native path's behaviour.

Isolation: the server listens on its own socket bound to 127.0.0.1 (never on the
FastAPI app, which Caddy publishes), and every request must carry a bearer token
that Octavius minted for one pi process (``PI_OCTAVIUS_TOOLS_TOKEN`` in that
process's environment). A token alone does nothing: ``stream_pi_turn`` binds it
to the running turn's context (history session, MCP manager, WebSocket session)
and unbinds it afterwards, and a call with no binding is a tool error. So a tool
call always acts for exactly the conversation whose turn is in flight.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hmac
import json
import logging
import secrets
import socket
from dataclasses import dataclass
from typing import Any

import mcp.types as mt
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

import tools as local_tools
from local_tool_specs import TOOLS

log = logging.getLogger(__name__)

SERVER_NAME = "octavius"
MCP_PATH = "/mcp"
TOKEN_ENV = "PI_OCTAVIUS_TOOLS_TOKEN"  # not OCTAVIUS_*: child_env strips that prefix
HOST = "127.0.0.1"

# The tools pi gets. Order of ``local_tool_specs.TOOLS`` decides listing order.
# Deferred (rollout "later"): download_file, process_pdf, check_document_status.
# Never: consult_specialist (pi has direct tools and scholar).
EXPOSED_TOOLS = frozenset({
    "save_note", "read_note", "edit_note", "commit_edit",
    "remember", "forget", "correct", "what_do_you_know",
    "search_conversation_history", "read_conversation",
    "read_document", "list_reader_documents", "append_to_reader_document",
})

_current_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "pi_tools_token", default=None,
)


@dataclass(frozen=True)
class ToolContext:
    """What ``tools.call_tool`` needs, as the native path passes it."""
    history_session: Any
    mcp_manager: Any
    session: Any


def exposed_specs() -> list[dict]:
    return [t for t in TOOLS if t["function"]["name"] in EXPOSED_TOOLS]


def mcp_tools() -> list[mt.Tool]:
    return [
        mt.Tool(
            name=t["function"]["name"],
            description=t["function"].get("description", ""),
            inputSchema=t["function"].get("parameters") or {"type": "object", "properties": {}},
        )
        for t in exposed_specs()
    ]


class NoActiveTurn(RuntimeError):
    pass


class PiToolsServer:
    def __init__(self, host: str = HOST, port: int = 8032):
        self.host = host
        self.port = port
        self._tokens: set[str] = set()
        self._bindings: dict[str, ToolContext] = {}
        self._inflight: dict[str, set[asyncio.Task]] = {}
        self._server = self._build_server()
        self._manager = StreamableHTTPSessionManager(
            app=self._server,
            json_response=True,
            stateless=True,
        )
        self._uvicorn: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._sock: socket.socket | None = None

    # --- tokens and bindings ----------------------------------------------------

    def new_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens.add(token)
        return token

    def revoke(self, token: str | None) -> None:
        if token:
            self._tokens.discard(token)
            self._bindings.pop(token, None)
            self._cancel_inflight(token)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{MCP_PATH}"

    def _cancel_inflight(self, token: str) -> None:
        for task in self._inflight.pop(token, set()):
            task.cancel()

    def is_valid(self, presented: str | None) -> bool:
        if not presented:
            return False
        ok = False
        for token in list(self._tokens):  # no early exit: constant time per token
            ok |= hmac.compare_digest(token.encode(), presented.encode())
        return ok

    def bind(self, token: str, ctx: ToolContext):
        """Bind a process's token to a turn's context; returns the unbind callable."""
        self._bindings[token] = ctx

        def unbind() -> None:
            if self._bindings.get(token) is ctx:
                del self._bindings[token]
                # Barge-in/abort: a call still running for this turn is cancelled
                # with it, as the native path cancels the tool coroutine.
                self._cancel_inflight(token)

        return unbind

    # --- MCP --------------------------------------------------------------------

    def _build_server(self) -> Server:
        server = Server(SERVER_NAME)

        @server.list_tools()
        async def _list() -> list[mt.Tool]:
            return mcp_tools()

        @server.call_tool(validate_input=False)  # handlers validate, as on the native path
        async def _call(name: str, arguments: dict | None):
            text = await self.dispatch(_current_token.get(), name, arguments or {})
            return [mt.TextContent(type="text", text=text if isinstance(text, str) else json.dumps(text))]

        return server

    async def dispatch(self, token: str | None, name: str, arguments: dict) -> str:
        if name not in EXPOSED_TOOLS:
            raise ValueError(f"Unknown tool: {name}")
        ctx = self._bindings.get(token) if token else None
        if ctx is None:
            raise NoActiveTurn(
                "No active conversation turn: this tool only works while Octavius is "
                "handling a turn. Tell Dave it is unavailable right now."
            )
        from subagent import _unwrap_double_encoded_args
        args = _unwrap_double_encoded_args(arguments)
        task = asyncio.ensure_future(local_tools.call_tool(
            name, args,
            history_session=ctx.history_session,
            mcp_manager=ctx.mcp_manager,
            session=ctx.session,
        ))
        self._inflight.setdefault(token, set()).add(task)
        try:
            # wait(), not await: a cancelled inner task must not look like our own cancellation.
            await asyncio.wait([task])
        except asyncio.CancelledError:
            task.cancel()  # the request itself was cancelled (client went away)
            raise
        finally:
            self._inflight.get(token, set()).discard(task)
        if task.cancelled():
            raise RuntimeError("Tool call cancelled: the turn ended before it finished.")
        return task.result()

    # --- ASGI + lifecycle ---------------------------------------------------------

    @staticmethod
    def _bearer(scope) -> str | None:
        for k, v in scope.get("headers") or []:
            if k == b"authorization":
                value = v.decode("latin-1")
                if value[:7].lower() == "bearer ":
                    return value[7:].strip()
        return None

    async def asgi(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        if scope.get("path", "").rstrip("/") != MCP_PATH:
            await self._reply(send, 404, {"error": "not found"})
            return
        token = self._bearer(scope)
        if not self.is_valid(token):
            await self._reply(send, 401, {"error": "unauthorized"}, www_authenticate=True)
            return
        reset = _current_token.set(token)
        try:
            await self._manager.handle_request(scope, receive, send)
        finally:
            _current_token.reset(reset)

    @staticmethod
    async def _reply(send, status: int, body: dict, www_authenticate: bool = False) -> None:
        payload = json.dumps(body).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
        if www_authenticate:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})

    async def start(self) -> None:
        """Bind the loopback socket and serve. Raises OSError if the port is taken."""
        if self.host != HOST:
            raise ValueError("pi tools server binds 127.0.0.1 only")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self.port = sock.getsockname()[1]
        # DNS-rebinding guard: only a Host header naming this loopback socket.
        self._manager.security_settings = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"127.0.0.1:{self.port}", f"localhost:{self.port}"],
            allowed_origins=[],
        )

        async def app(scope, receive, send):
            await self.asgi(scope, receive, send)

        config = uvicorn.Config(
            app, interface="asgi3", log_level="warning", lifespan="off", access_log=False,
        )
        self._uvicorn = uvicorn.Server(config)
        ready = asyncio.Event()

        async def run() -> None:
            async with self._manager.run():
                ready.set()
                await self._uvicorn.serve(sockets=[sock])

        self._task = asyncio.create_task(run())
        self._task.add_done_callback(lambda t: ready.set())
        await ready.wait()
        # uvicorn flips ``started`` once the socket is listening.
        for _ in range(100):
            if self._uvicorn.started or self._task.done():
                break
            await asyncio.sleep(0.02)
        if self._task.done():
            exc = self._task.exception()
            raise RuntimeError("pi tools server failed to start") from exc
        log.info("pi tools server listening on %s:%d (%d tools)", self.host, self.port, len(EXPOSED_TOOLS))

    async def stop(self) -> None:
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                with contextlib.suppress(BaseException):
                    await self._task
            except Exception:
                log.warning("pi tools server stopped with an error", exc_info=True)
        if self._sock is not None:
            self._sock.close()
        for token in list(self._inflight):
            self._cancel_inflight(token)
        self._tokens.clear()
        self._bindings.clear()
        self._task = self._uvicorn = self._sock = None
