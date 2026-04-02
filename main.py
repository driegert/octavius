import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import AGENT_PORT, LLM_CHAIN, MCP_SERVERS, TTS_MODEL, TTS_VOICES, TTS_VOICE
from conversation import Conversation
from history import HistoryRecorder, init_db
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
conversation: Conversation
history: HistoryRecorder


@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp_manager, conversation, history
    mcp_manager = MCPManager(MCP_SERVERS)
    conversation = Conversation()
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
    global conversation
    await ws.accept()
    log.info("WebSocket connected")
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
