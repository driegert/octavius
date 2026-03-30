import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from config import AGENT_PORT, MCP_SERVERS, TTS_VOICES, TTS_VOICE
from conversation import Conversation
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp_manager, conversation
    mcp_manager = MCPManager(MCP_SERVERS)
    conversation = Conversation()
    log.info("Connecting MCP servers...")
    await mcp_manager.connect_all()
    log.info("MCP ready — %d tools available", len(mcp_manager.tools))
    yield
    log.info("Shutting down MCP...")
    await mcp_manager.disconnect_all()


app = FastAPI(lifespan=lifespan)


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


async def _run_turn(ws, conversation, mcp_manager, user_text, voice, tts_enabled):
    """Run agent loop with streaming TTS: synthesize each sentence as it arrives."""
    await send_json(ws, "status", "Thinking...")

    async def status_cb(text: str):
        await send_json(ws, "status", text)

    full_reply_parts = []
    first_sentence = True

    try:
        async for sentence in stream_agent_turn(
            conversation, mcp_manager, user_text, status_callback=status_cb
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
        return

    full_reply = "".join(full_reply_parts).strip()
    if full_reply:
        await send_json(ws, "response", full_reply)

    # Signal end of audio stream
    await send_json(ws, "status", "audio_done")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global conversation
    await ws.accept()
    log.info("WebSocket connected")
    voice = TTS_VOICE
    tts_enabled = True

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
                    conversation.reset()
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
                    await _run_turn(ws, conversation, mcp_manager, user_text, voice, tts_enabled)
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
                await _run_turn(ws, conversation, mcp_manager, user_text, voice, tts_enabled)

    except (WebSocketDisconnect, RuntimeError):
        log.info("WebSocket disconnected")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=AGENT_PORT)
