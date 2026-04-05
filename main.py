import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from db import DEFAULT_DB_PATH
from history import HistoryRecorder, init_db
from mcp_manager import MCPManager
from settings import settings
from routes.conversations import router as conversations_router
from routes.inbox import router as inbox_router
from routes.reader_api import router as reader_router
from reader_store import fail_stale_processing_documents
from runtime import set_mcp_manager
from service_clients import llm_client
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
    history_conn.close()
    app.state.mcp_manager = mcp_manager
    app.state.history = history
    app.state.db_path = db_path
    set_mcp_manager(mcp_manager)
    if stale_count:
        log.warning("Marked %d stale reader document(s) as failed on startup", stale_count)
    log.info("Connecting MCP servers...")
    await mcp_manager.connect_all()
    log.info("MCP ready — %d tools available", len(mcp_manager.tools))
    yield
    log.info("Shutting down MCP...")
    await mcp_manager.disconnect_all()


def create_app(*, mcp_manager_factory=MCPManager, db_init=init_db, db_path=DEFAULT_DB_PATH) -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.include_router(inbox_router)
    app.include_router(conversations_router)
    app.include_router(reader_router)
    app.state.mcp_manager_factory = mcp_manager_factory
    app.state.db_init = db_init
    app.state.db_path = db_path
    return app


app = create_app()


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/health")
async def health(request: Request):
    mcp_manager = getattr(request.app.state, "mcp_manager", None)
    history = getattr(request.app.state, "history", None)
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
        }
    )


@app.get("/api/voices")
async def voices():
    return JSONResponse({"voices": settings.tts.voices, "default": settings.tts.voice})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await handle_websocket_session(ws)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=settings.agent_port)
