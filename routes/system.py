from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from service_clients import embedding_client, llm_client
from settings import settings
from websocket_session import chat_models_payload

router = APIRouter()


@router.get("/")
async def index():
    return FileResponse("static/index.html")


@router.get("/health")
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


@router.get("/api/voices")
async def voices():
    return JSONResponse({
        "voices": settings.tts.voices,
        "default": settings.tts.voice,
        "agent_default": settings.tts.agent_voice,
        "agent_names": {v: settings.tts.agent_name(v) for v in settings.tts.voices},
    })


@router.get("/api/chat-models")
async def chat_models():
    """The agent's model menu; a client sends its pick as a `chat_model` WS frame."""
    return JSONResponse(chat_models_payload())
