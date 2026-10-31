import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from db import DEFAULT_DB_PATH
from history import HistoryRecorder, init_db
from history_enrichment import drain_inflight
from history_store import assert_history_serveable
from mcp_manager import MCPManager
from pi_brain import PiProcessManager
from settings import settings
from routes.conversations import router as conversations_router
from routes.inbox import router as inbox_router
from routes.media import router as media_router
from routes.reader_api import router as reader_router
from routes.vault import router as vault_router
from reader_store import fail_stale_processing_documents
from service_clients import embedding_client, llm_client
from subagent_dispatcher import SubagentDispatcher
import tools as local_tools
from websocket_session import handle_websocket_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    mcp_manager = app.state.mcp_manager_factory(settings.mcp_servers)
    history_conn = app.state.db_init()
    db_path = app.state.db_path
    history = HistoryRecorder(db_path)
    stale_count = fail_stale_processing_documents(history_conn)
    app.state.mcp_manager = mcp_manager
    app.state.history = history
    app.state.db_path = db_path
    app.state.subagent_dispatcher = SubagentDispatcher(settings.subagent_llm_chain)
    log.info(
        "Subagent dispatcher ready: %s",
        app.state.subagent_dispatcher.snapshot(),
    )
    if stale_count:
        log.warning("Marked %d stale reader document(s) as failed on startup", stale_count)
    # Loud, at startup: conversation/inbox search reads sidecar tables that
    # history-index.timer maintains in the file sites.toml names. If this app
    # opens a different file, or the sidecars are missing or L2-metric, search
    # would keep answering with stale or magnitude-ranked results instead of
    # failing. Injected so tests can run against a throwaway database.
    app.state.verify_search(history_conn, db_path)
    history_conn.close()
    log.info("Connecting MCP servers...")
    await mcp_manager.connect_all()
    log.info("MCP ready — %d tools available", len(mcp_manager.tools))
    for issue in local_tools.validate_local_tool_registry():
        log.warning("%s", issue)
    local_names = {t["function"]["name"] for t in local_tools.TOOLS}
    overlap = local_names & mcp_manager.get_registered_tool_names()
    if overlap:
        log.warning(
            "Local tool name(s) collide with MCP tools (local handler wins): %s",
            sorted(overlap),
        )
    # The embedding sweeper started here until 2026-09-15. It backfilled the
    # legacy vec0 tables; history-index.timer (`hybrid-corpus run history`,
    # every 15 min) owns freshness and embed-debt healing now, for all three
    # history collections rather than just two.
    # OCTAVIUS_BRAIN=pi: voice/text turns run on pi processes (pi_brain.py).
    app.state.pi_brain = None
    if settings.pi_brain.enabled:
        app.state.pi_brain = PiProcessManager(settings.pi_brain)
        app.state.pi_brain.start_reaper()
        # Warm system prompt + tools in the background (pi /warm, never awaited).
        app.state.pi_brain.warm_static_prefix("startup")
        log.info("Brain: pi (%s via %s)", settings.pi_brain.model, settings.pi_brain.launcher)
    yield
    if app.state.pi_brain is not None:
        await app.state.pi_brain.shutdown()
    log.info("Shutting down MCP...")
    await mcp_manager.disconnect_all()
    # Nothing spawns detached embeds any more (see history_enrichment), so this
    # drains an empty set. Kept because it is the shutdown hook's only await on
    # in-flight background work and costs nothing when there is none.
    await drain_inflight()


UI_PAGES = {"/", "/inbox", "/reader"}

# Defence in depth for pages that render untrusted text (replies quoting web
# pages, saved items, reader documents): even if an escaping bug lets markup
# through, it cannot run script, load remote images, or post anywhere.
# No 'unsafe-inline' anywhere: the pages have no inline <script>, no on*=
# handlers and no style="" attributes (JS sets styles through el.style, which
# CSP allows). Adding an external resource means adding its origin here; see
# AGENTS.md "Frontend". connect-src 'self' covers same-origin ws:/wss: (CSP3).
UI_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",  # marked is vendored in static/vendor/, so no CDN is trusted
    "style-src 'self' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
])
UI_SECURITY_HEADERS = {
    "Content-Security-Policy": UI_CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def create_app(*, mcp_manager_factory=MCPManager, db_init=init_db, db_path=DEFAULT_DB_PATH,
               verify_search=assert_history_serveable) -> FastAPI:
    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def revalidate_ui(request: Request, call_next):
        # Pages and their assets change together on disk with no build step, so a
        # browser must not run a cached old script against new HTML: always
        # revalidate (StaticFiles/FileResponse answer with ETag + 304).
        response = await call_next(request)
        if request.url.path.startswith("/static/") or request.url.path in UI_PAGES:
            response.headers.setdefault("Cache-Control", "no-cache")
        if request.url.path in UI_PAGES:
            for name, value in UI_SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
        return response

    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.include_router(inbox_router)
    app.include_router(conversations_router)
    app.include_router(media_router)
    app.include_router(reader_router)
    app.include_router(vault_router)
    app.state.mcp_manager_factory = mcp_manager_factory
    app.state.db_init = db_init
    app.state.db_path = db_path
    app.state.verify_search = verify_search
    return app


app = create_app()


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/health")
async def health(request: Request):
    mcp_manager = getattr(request.app.state, "mcp_manager", None)
    history = getattr(request.app.state, "history", None)
    pi_brain = getattr(request.app.state, "pi_brain", None)
    llm_health = llm_client.get_health()
    mcp_health = (
        mcp_manager.get_health()
        if mcp_manager and hasattr(mcp_manager, "get_health")
        else {
            "configured_servers": 0,
            "connected_servers": 0,
            "ready": False,
            "degraded": False,
            "servers": {},
        }
    )
    database_ready = history is not None
    ready = database_ready and mcp_health["ready"]
    degraded = mcp_health["degraded"] or llm_health.get("terminal_failures", 0) > 0
    status = "ok" if ready and not degraded else "degraded" if ready else "starting"
    return JSONResponse(
        {
            "status": status,
            "alive": True,
            "ready": ready,
            "degraded": degraded,
            "database_ready": database_ready,
            "mcp_connected": mcp_manager is not None,
            "mcp_tool_count": len(mcp_manager.tools) if mcp_manager else 0,
            "mcp": mcp_health,
            "llm_chain": llm_health,
            "embedding_chain": embedding_client.get_health(),
            "pi_brain": pi_brain.health() if pi_brain else {"brain": settings.pi_brain.brain},
        }
    )


@app.get("/api/voices")
async def voices():
    return JSONResponse({
        "voices": settings.tts.voices,
        "default": settings.tts.voice,
        "voices_by_engine": {
            "voxtral": settings.tts.voxtral_voices,
            "kokoro": settings.tts.kokoro_voices,
        },
    })


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await handle_websocket_session(ws)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=settings.agent_port)
