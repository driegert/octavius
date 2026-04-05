import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from db import connect_db
from history import get_saved_item, list_saved_items, search_saved_items, update_saved_item_status

router = APIRouter()


@router.get("/inbox")
async def inbox_page():
    return FileResponse("static/inbox.html")


@router.get("/api/inbox")
async def inbox_list(
    request: Request,
    status: str | None = None,
    type: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    with connect_db(request.app.state.db_path) as conn:
        if q:
            items = search_saved_items(conn, q, limit=limit)
        else:
            items = list_saved_items(conn, status=status, item_type=type, limit=limit, offset=offset)
    return JSONResponse({"items": items})


@router.get("/api/inbox/{item_id}")
async def inbox_get(item_id: int, request: Request):
    with connect_db(request.app.state.db_path) as conn:
        item = get_saved_item(conn, item_id)
    if not item:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"item": item})


@router.patch("/api/inbox/{item_id}")
async def inbox_update(item_id: int, request: Request):
    body = await request.json()
    new_status = body.get("status")
    if new_status not in ("pending", "done", "dismissed"):
        return JSONResponse({"error": "invalid status"}, status_code=400)
    with connect_db(request.app.state.db_path) as conn:
        ok = update_saved_item_status(conn, item_id, new_status)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.delete("/api/inbox/{item_id}")
async def inbox_delete(item_id: int, request: Request):
    try:
        with connect_db(request.app.state.db_path) as conn:
            conn.execute("DELETE FROM saved_item_embeddings WHERE saved_item_id = ?", (item_id,))
            cursor = conn.execute("DELETE FROM saved_items WHERE id = ?", (item_id,))
            conn.commit()
    except sqlite3.IntegrityError:
        return JSONResponse(
            {"error": "item is referenced by a reader document; delete the reader document first"},
            status_code=409,
        )
    if cursor.rowcount == 0:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})
