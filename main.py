import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import AGENT_PORT, MCP_SERVERS, TTS_VOICES, TTS_VOICE
from history import (
    HistoryRecorder, init_db,
    list_saved_items, search_saved_items, get_saved_item, update_saved_item_status,
    get_conversation_messages,
)
from mcp_manager import MCPManager
from reader_ingest_service import ReaderIngestError, start_reader_ingest
from runtime import set_mcp_manager
from websocket_session import handle_websocket_session
import reader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    mcp_manager = app.state.mcp_manager_factory(MCP_SERVERS)
    history_conn = app.state.db_init()
    history = HistoryRecorder(history_conn)
    stale_count = reader.fail_stale_processing_documents(history_conn)
    app.state.mcp_manager = mcp_manager
    app.state.history = history
    app.state.history_conn = history_conn
    set_mcp_manager(mcp_manager)
    if stale_count:
        log.warning("Marked %d stale reader document(s) as failed on startup", stale_count)
    log.info("Connecting MCP servers...")
    await mcp_manager.connect_all()
    log.info("MCP ready — %d tools available", len(mcp_manager.tools))
    yield
    log.info("Shutting down MCP...")
    await mcp_manager.disconnect_all()
    history_conn.close()


def create_app(*, mcp_manager_factory=MCPManager, db_init=init_db) -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.state.mcp_manager_factory = mcp_manager_factory
    app.state.db_init = db_init
    return app


app = create_app()


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/health")
async def health(request: Request):
    mcp_manager = getattr(request.app.state, "mcp_manager", None)
    history = getattr(request.app.state, "history", None)
    return JSONResponse(
        {
            "status": "ok",
            "database_ready": history is not None,
            "mcp_connected": mcp_manager is not None,
            "mcp_tool_count": len(mcp_manager.tools) if mcp_manager else 0,
        }
    )


@app.get("/api/voices")
async def voices():
    return JSONResponse({"voices": TTS_VOICES, "default": TTS_VOICE})


# -- Knowledge Inbox API -------------------------------------------------------

@app.get("/inbox")
async def inbox_page():
    return FileResponse("static/inbox.html")


@app.get("/api/inbox")
async def inbox_list(
    request: Request,
    status: str | None = None,
    type: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    conn = request.app.state.history.conn
    if q:
        items = search_saved_items(conn, q, limit=limit)
    else:
        items = list_saved_items(conn, status=status, item_type=type, limit=limit, offset=offset)
    return JSONResponse({"items": items})


@app.get("/api/inbox/{item_id}")
async def inbox_get(item_id: int, request: Request):
    item = get_saved_item(request.app.state.history.conn, item_id)
    if not item:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"item": item})


@app.patch("/api/inbox/{item_id}")
async def inbox_update(item_id: int, request: Request):
    history = request.app.state.history
    body = await request.json()
    new_status = body.get("status")
    if new_status not in ("pending", "done", "dismissed"):
        return JSONResponse({"error": "invalid status"}, status_code=400)
    ok = update_saved_item_status(history.conn, item_id, new_status)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@app.delete("/api/inbox/{item_id}")
async def inbox_delete(item_id: int, request: Request):
    conn = request.app.state.history.conn
    # Delete embeddings first (FK-like cleanup)
    conn.execute("DELETE FROM saved_item_embeddings WHERE saved_item_id = ?", (item_id,))
    cursor = conn.execute("DELETE FROM saved_items WHERE id = ?", (item_id,))
    conn.commit()
    if cursor.rowcount == 0:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


# -- Conversation History API --------------------------------------------------

@app.get("/api/conversations")
async def conversations_list(request: Request, limit: int = 20, offset: int = 0):
    """List recent Octavius conversations with summaries."""
    conn = request.app.state.history.conn
    rows = conn.execute(
        """SELECT id, session_id, started_at, ended_at, summary, message_count
           FROM conversations
           WHERE service = 'octavius' AND message_count > 0
           ORDER BY started_at DESC
           LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()
    items = []
    for r in rows:
        # Fetch tags
        tags = conn.execute(
            """SELECT t.name FROM tags t
               JOIN conversation_tags ct ON t.id = ct.tag_id
               WHERE ct.conversation_id = ?""",
            (r[0],),
        ).fetchall()
        items.append({
            "id": r[0], "session_id": r[1][:8],
            "started_at": r[2], "ended_at": r[3],
            "summary": r[4], "message_count": r[5],
            "tags": [t[0] for t in tags],
        })
    return JSONResponse({"conversations": items})


@app.get("/api/conversations/{conv_id}/messages")
async def conversation_messages(conv_id: int, request: Request):
    """Get all messages for a conversation."""
    msgs = get_conversation_messages(request.app.state.history.conn, conv_id)
    return JSONResponse({"messages": msgs})


# -- Document Reader API -------------------------------------------------------

@app.get("/reader")
async def reader_page():
    return FileResponse("static/reader.html")


@app.post("/api/reader/documents")
async def reader_ingest(request: Request):
    """Ingest a document for reading. Kicks off background processing."""
    body = await request.json()
    conn = request.app.state.history.conn
    mcp_manager = request.app.state.mcp_manager
    try:
        result = await start_reader_ingest(conn, mcp_manager, body)
        return JSONResponse(result)
    except ReaderIngestError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status_code)


@app.get("/api/reader/documents")
async def reader_list(request: Request):
    docs = reader.list_documents(request.app.state.history.conn)
    return JSONResponse({"documents": docs})


@app.get("/api/reader/documents/{doc_id}")
async def reader_get(doc_id: int, request: Request):
    doc = reader.get_document(request.app.state.history.conn, doc_id)
    if not doc:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Include chunk headings if ready
    if doc["status"] == "ready":
        speech = reader.load_speech_data(doc)
        if speech:
            doc["total_sentences"] = speech.get("total_sentences", 0)
            doc["sections"] = [
                {"index": c["index"], "heading": c["heading"],
                 "sentence_count": len(c["sentences"])}
                for c in speech["chunks"]
            ]
    return JSONResponse({"document": doc})


@app.delete("/api/reader/documents/{doc_id}")
async def reader_delete(doc_id: int, request: Request):
    ok = reader.delete_document(request.app.state.history.conn, doc_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await handle_websocket_session(ws)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=AGENT_PORT)
