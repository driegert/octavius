import asyncio
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
import reader

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


# -- Document Reader API -------------------------------------------------------

@app.get("/reader")
async def reader_page():
    return FileResponse("static/reader.html")


@app.post("/api/reader/documents")
async def reader_ingest(request: Request):
    """Ingest a document for reading. Kicks off background processing."""
    body = await request.json()
    source = body.get("source", "file")
    title = body.get("title", "Untitled")
    path = body.get("path")
    saved_item_id = body.get("saved_item_id")
    text = body.get("text")

    conn = history.conn

    # Determine source type and get markdown content
    if source == "inbox" and saved_item_id:
        item = get_saved_item(conn, saved_item_id)
        if not item:
            return JSONResponse({"error": "Inbox item not found"}, status_code=404)
        markdown = item["content"]
        title = title or item["title"]
        doc_id = reader.create_document(conn, title, "inbox_item", saved_item_id=saved_item_id)

    elif source == "text" and text:
        markdown = text
        doc_id = reader.create_document(conn, title, "markdown")

    elif source == "url" and (body.get("url") or path):
        url = body.get("url") or path
        doc_id = reader.create_document(conn, title, "url", source_path=url)
        asyncio.create_task(_ingest_url(conn, doc_id, url, title))
        return JSONResponse({"id": doc_id, "status": "processing"})

    elif source == "file" and path:
        from pathlib import Path as P
        p = P(path)
        if not p.exists():
            return JSONResponse({"error": f"File not found: {path}"}, status_code=404)
        if p.suffix.lower() == ".pdf":
            doc_id = reader.create_document(conn, title, "pdf", source_path=path)
            asyncio.create_task(_ingest_pdf(conn, doc_id, path, title))
            return JSONResponse({"id": doc_id, "status": "processing"})
        else:
            markdown = p.read_text()
            doc_id = reader.create_document(conn, title, "markdown", source_path=path)

    else:
        return JSONResponse({"error": "Provide source + path/url, text, or saved_item_id"}, status_code=400)

    # Start background ingest for non-PDF sources
    asyncio.create_task(reader.ingest_document(conn, doc_id, markdown, title))
    return JSONResponse({"id": doc_id, "status": "processing"})


async def _ingest_pdf(conn, doc_id: int, pdf_path: str, title: str):
    """Convert PDF to markdown via MCP, then run the reader ingest pipeline."""
    try:
        # Start conversion
        result = await mcp_manager.call_tool("convert_pdf_to_md", {"file_path": pdf_path})
        import re
        job_match = re.search(r'Job ID:\s*(\S+)', result)
        if not job_match:
            reader.update_document(conn, doc_id, status="failed", error=f"PDF conversion failed: {result}")
            return

        job_id = job_match.group(1)
        log.info("Reader: PDF conversion job %s started for document %d", job_id, doc_id)

        # Poll for completion
        for _ in range(120):  # up to 10 minutes
            await asyncio.sleep(5)
            poll_result = await mcp_manager.call_tool("get_conversion_result", {"job_id": job_id})
            if "still processing" in poll_result.lower() or "not yet" in poll_result.lower():
                continue
            # Check for markdown file path in result
            md_match = re.search(r'(/\S+\.md)', poll_result)
            if md_match:
                md_path = md_match.group(1)
                from pathlib import Path as P
                markdown = P(md_path).read_text()
                await reader.ingest_document(conn, doc_id, markdown, title, original_md_path=md_path)
                return
            if "error" in poll_result.lower() or "failed" in poll_result.lower():
                reader.update_document(conn, doc_id, status="failed", error=poll_result[:500])
                return

        reader.update_document(conn, doc_id, status="failed", error="PDF conversion timed out")

    except Exception as e:
        log.exception("Reader: PDF ingest failed for document %d", doc_id)
        reader.update_document(conn, doc_id, status="failed", error=str(e))


async def _ingest_url(conn, doc_id: int, url: str, title: str):
    """Download a URL, then route to PDF or markdown ingest."""
    try:
        import httpx as _httpx
        from pathlib import Path as P

        log.info("Reader: downloading URL for document %d: %s", doc_id, url)
        async with _httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        # Determine file type from content-type or URL
        content_type = resp.headers.get("content-type", "")
        is_pdf = "pdf" in content_type or url.lower().endswith(".pdf")

        # Save to downloads dir
        from config import DOWNLOADS_DIR
        dl_dir = P(DOWNLOADS_DIR)
        dl_dir.mkdir(parents=True, exist_ok=True)
        ext = ".pdf" if is_pdf else ".md"
        dest = dl_dir / f"reader_{doc_id}{ext}"
        dest.write_bytes(resp.content)
        log.info("Reader: downloaded %s (%d KB)", dest, len(resp.content) // 1024)

        if is_pdf:
            await _ingest_pdf(conn, doc_id, str(dest), title)
        else:
            # Treat as markdown/text
            markdown = resp.text
            await reader.ingest_document(conn, doc_id, markdown, title, original_md_path=str(dest))

    except Exception as e:
        log.exception("Reader: URL ingest failed for document %d", doc_id)
        reader.update_document(conn, doc_id, status="failed", error=str(e))


@app.get("/api/reader/documents")
async def reader_list():
    docs = reader.list_documents(history.conn)
    return JSONResponse({"documents": docs})


@app.get("/api/reader/documents/{doc_id}")
async def reader_get(doc_id: int):
    doc = reader.get_document(history.conn, doc_id)
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
async def reader_delete(doc_id: int):
    ok = reader.delete_document(history.conn, doc_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


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
    reader_task: asyncio.Task | None = None
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

                # -- Reader controls --
                if data.get("type") == "reader_play":
                    if reader_task and not reader_task.done():
                        reader_task.cancel()
                        try:
                            await reader_task
                        except (asyncio.CancelledError, Exception):
                            pass
                    reader_task = asyncio.create_task(
                        reader.stream_reader_audio(
                            ws, data["doc_id"], history.conn,
                            chunk_index=data.get("chunk_index", 0),
                            sentence_index=data.get("sentence_index", 0),
                            voice=data.get("voice", voice),
                        )
                    )
                    continue

                if data.get("type") == "reader_pause":
                    if reader_task and not reader_task.done():
                        reader_task.cancel()
                        try:
                            await reader_task
                        except (asyncio.CancelledError, Exception):
                            pass
                    continue

                if data.get("type") == "reader_stop":
                    if reader_task and not reader_task.done():
                        reader_task.cancel()
                        try:
                            await reader_task
                        except (asyncio.CancelledError, Exception):
                            pass
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
        if reader_task and not reader_task.done():
            reader_task.cancel()
        history_session.end()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=AGENT_PORT)
