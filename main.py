import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import AGENT_PORT, LLM_CHAIN, MCP_SERVERS, TTS_MODEL, TTS_VOICES, TTS_VOICE
from conversation import Conversation
from history import (
    HistoryRecorder, init_db,
    list_saved_items, search_saved_items, get_saved_item, update_saved_item_status,
    get_conversation_messages,
)
from mcp_manager import MCPManager
from stt import transcribe
from tts import synthesize
from agent import run_agent_turn, stream_agent_turn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

mcp_manager: MCPManager
history: HistoryRecorder


@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp_manager, history
    mcp_manager = MCPManager(MCP_SERVERS)
    history_conn = init_db()
    history = HistoryRecorder(history_conn)
    log.info("Connecting MCP servers...")
    await mcp_manager.connect_all()
    log.info("MCP ready — %d tools available", len(mcp_manager.tools))
    yield
    log.info("Shutting down MCP...")
    await mcp_manager.disconnect_all()
    history_conn.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


@app.get("/api/voices")
async def voices():
    return JSONResponse({"voices": TTS_VOICES, "default": TTS_VOICE})


# -- Knowledge Inbox API -------------------------------------------------------

@app.get("/inbox")
async def inbox_page():
    return FileResponse("static/inbox.html")


@app.get("/api/inbox")
async def inbox_list(
    status: str | None = None,
    type: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    conn = history.conn
    if q:
        items = search_saved_items(conn, q, limit=limit)
    else:
        items = list_saved_items(conn, status=status, item_type=type, limit=limit, offset=offset)
    return JSONResponse({"items": items})


@app.get("/api/inbox/{item_id}")
async def inbox_get(item_id: int):
    item = get_saved_item(history.conn, item_id)
    if not item:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"item": item})


@app.patch("/api/inbox/{item_id}")
async def inbox_update(item_id: int, request: Request):
    body = await request.json()
    new_status = body.get("status")
    if new_status not in ("pending", "done", "dismissed"):
        return JSONResponse({"error": "invalid status"}, status_code=400)
    ok = update_saved_item_status(history.conn, item_id, new_status)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


# -- Conversation History API --------------------------------------------------

@app.get("/api/conversations")
async def conversations_list(limit: int = 20, offset: int = 0):
    """List recent Octavius conversations with summaries."""
    conn = history.conn
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
async def conversation_messages(conv_id: int):
    """Get all messages for a conversation."""
    msgs = get_conversation_messages(history.conn, conv_id)
    return JSONResponse({"messages": msgs})


async def send_json(ws: WebSocket, msg_type: str, text: str):
    await ws.send_text(json.dumps({"type": msg_type, "text": text}))


async def _run_turn(ws, conversation, mcp_manager, user_text, voice, tts_enabled,
                    history_session=None, source="voice"):
    """Run agent loop with streaming TTS: synthesize each sentence as it arrives."""
    import time
    turn_start = time.monotonic()

    await send_json(ws, "status", "Thinking...")

    # Record user message
    if history_session:
        user_kwargs = {}
        if source == "voice":
            user_kwargs["stt_model"] = "whisper"
        history_session.add_message(role="user", content=user_text, **user_kwargs)

    async def status_cb(text: str):
        await send_json(ws, "status", text)

    full_reply_parts = []
    first_sentence = True

    try:
        async for sentence in stream_agent_turn(
            conversation, mcp_manager, user_text,
            status_callback=status_cb, history_session=history_session,
        ):
            full_reply_parts.append(sentence)

            if tts_enabled:
                if first_sentence:
                    await send_json(ws, "status", "Speaking...")
                    first_sentence = False
                try:
                    wav_bytes = await synthesize(sentence, voice=voice)
                    await ws.send_bytes(wav_bytes)
                except Exception as e:
                    log.exception("TTS failed for chunk")
                    # Continue with remaining sentences

    except Exception as e:
        log.exception("Agent failed")
        await send_json(ws, "status", f"Agent error: {e}")
        if history_session:
            history_session.add_message(
                role="assistant", content=f"Error: {e}", error=str(e),
            )
        return

    full_reply = "".join(full_reply_parts).strip()
    if full_reply:
        await send_json(ws, "response", full_reply)

    # Record assistant message
    if history_session and full_reply:
        latency_ms = int((time.monotonic() - turn_start) * 1000)
        history_session.add_message(
            role="assistant", content=full_reply,
            model=LLM_CHAIN[0]["model"], latency_ms=latency_ms,
            tts_model=TTS_MODEL if tts_enabled else None,
        )

    # Signal end of audio stream
    await send_json(ws, "status", "audio_done")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    log.info("WebSocket connected")
    conversation = Conversation()
    voice = TTS_VOICE
    tts_enabled = True
    history_session = history.start_conversation(service="octavius", source="voice", model=LLM_CHAIN[0]["model"])

    try:
        while True:
            message = await ws.receive()

            # Text message — control commands or typed input
            if "text" in message:
                try:
                    data = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue

                if data.get("type") == "reset":
                    history_session.end()
                    conversation.reset()
                    history_session = history.start_conversation(
                        source="voice", model=LLM_CHAIN[0]["model"],
                    )
                    await send_json(ws, "status", "Conversation reset.")
                    log.info("Conversation reset by client")
                    continue

                if data.get("type") == "load_conversation":
                    conv_id = data.get("conversation_id")
                    if conv_id:
                        msgs = get_conversation_messages(history.conn, conv_id)
                        if msgs:
                            history_session.end()
                            conversation.load_from_history(msgs)
                            history_session = history.start_conversation(
                                source="voice", model=LLM_CHAIN[0]["model"],
                            )
                            # Send conversation history to client for display
                            history_pairs = []
                            for m in msgs:
                                if m["role"] in ("user", "assistant") and m.get("content"):
                                    history_pairs.append({"role": m["role"], "content": m["content"]})
                            await ws.send_text(json.dumps({
                                "type": "conversation_loaded",
                                "conversation_id": conv_id,
                                "messages": history_pairs,
                            }))
                            log.info("Loaded conversation %d (%d messages)", conv_id, len(msgs))
                        else:
                            await send_json(ws, "status", "Conversation not found.")
                    continue

                if data.get("type") == "settings":
                    if "voice" in data:
                        voice = data["voice"]
                        log.info("Voice set to %s", voice)
                    if "tts" in data:
                        tts_enabled = data["tts"]
                        log.info("TTS %s", "enabled" if tts_enabled else "disabled")
                    continue

                if data.get("type") == "text_input":
                    user_text = data.get("text", "").strip()
                    if not user_text:
                        continue
                    await send_json(ws, "transcript", user_text)
                    await _run_turn(
                        ws, conversation, mcp_manager, user_text, voice,
                        tts_enabled, history_session=history_session, source="text",
                    )
                    continue

            # Binary message — audio blob
            if "bytes" in message:
                audio_bytes = message["bytes"]

                await send_json(ws, "status", "Transcribing...")
                try:
                    user_text = await transcribe(audio_bytes)
                except Exception as e:
                    log.exception("STT failed")
                    await send_json(ws, "status", f"Transcription failed: {e}")
                    continue

                if not user_text:
                    await send_json(ws, "status", "Couldn't hear anything. Try again.")
                    continue

                await send_json(ws, "transcript", user_text)
                await _run_turn(
                    ws, conversation, mcp_manager, user_text, voice,
                    tts_enabled, history_session=history_session, source="voice",
                )

    except (WebSocketDisconnect, RuntimeError):
        log.info("WebSocket disconnected")
        history_session.end()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=AGENT_PORT)
