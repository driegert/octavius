from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from db import connect_db
from history import get_conversation_messages

router = APIRouter()


@router.get("/api/conversations")
async def conversations_list(request: Request, limit: int = 20, offset: int = 0):
    with connect_db(request.app.state.db_path) as conn:
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


@router.get("/api/conversations/{conv_id}/messages")
async def conversation_messages(conv_id: int, request: Request):
    with connect_db(request.app.state.db_path) as conn:
        msgs = get_conversation_messages(conn, conv_id)
    return JSONResponse({"messages": msgs})
