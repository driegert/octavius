import logging

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from app_lifespan import lifespan
from db import DEFAULT_DB_PATH
from history import init_db
from history_store import assert_history_serveable
from mcp_manager import MCPManager
from settings import settings
from routes.conversations import router as conversations_router
from routes.inbox import router as inbox_router
from routes.media import router as media_router
from routes.reader_api import router as reader_router
from routes.system import router as system_router
from routes.vault import router as vault_router
from ui_security import UI_CSP, UI_PAGES, UI_SECURITY_HEADERS, install_ui_headers  # noqa: F401  (re-exported)
from websocket_session import handle_websocket_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


def create_app(*, mcp_manager_factory=MCPManager, db_init=init_db, db_path=DEFAULT_DB_PATH,
               verify_search=assert_history_serveable) -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    install_ui_headers(app)
    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.include_router(inbox_router)
    app.include_router(conversations_router)
    app.include_router(media_router)
    app.include_router(reader_router)
    app.include_router(vault_router)
    app.include_router(system_router)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await handle_websocket_session(ws)

    app.state.mcp_manager_factory = mcp_manager_factory
    app.state.db_init = db_init
    app.state.db_path = db_path
    app.state.verify_search = verify_search
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=settings.agent_port)
