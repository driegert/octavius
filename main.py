import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from config import AGENT_PORT, MCP_SERVERS
from conversation import Conversation
from mcp_manager import MCPManager
from stt import transcribe
from tts import synthesize
from agent import run_agent_turn

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


async def send_json(ws: WebSocket, msg_type: str, text: str):
    await ws.send_text(json.dumps({"type": msg_type, "text": text}))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global conversation
    await ws.accept()
    log.info("WebSocket connected")

    try:
        while True:
            message = await ws.receive()

            # Text message — control commands (e.g. reset)
            if "text" in message:
                try:
                    data = json.loads(message["text"])
                    if data.get("type") == "reset":
                        conversation.reset()
                        await send_json(ws, "status", "Conversation reset.")
                        log.info("Conversation reset by client")
                        continue
                except json.JSONDecodeError:
                    continue

            # Binary message — audio blob
            if "bytes" in message:
                audio_bytes = message["bytes"]

                # 1. Transcribe
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

                # 2. Agent loop
                await send_json(ws, "status", "Thinking...")

                async def status_cb(text: str):
                    await send_json(ws, "status", text)

                try:
                    reply = await run_agent_turn(
                        conversation, mcp_manager, user_text, status_callback=status_cb
                    )
                except Exception as e:
                    log.exception("Agent failed")
                    await send_json(ws, "status", f"Agent error: {e}")
                    continue

                await send_json(ws, "response", reply)

                # 3. TTS
                await send_json(ws, "status", "Speaking...")
                try:
                    wav_bytes = await synthesize(reply)
                    await ws.send_bytes(wav_bytes)
                except Exception as e:
                    log.exception("TTS failed")
                    await send_json(ws, "status", f"TTS failed: {e}")

    except (WebSocketDisconnect, RuntimeError):
        log.info("WebSocket disconnected")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=AGENT_PORT)
