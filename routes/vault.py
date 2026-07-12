import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import vault_files

router = APIRouter()


@router.get("/api/vault/recent")
async def vault_recent(limit: int = 20):
    try:
        items = vault_files.list_recent_fleeting(limit)
    except vault_files.VaultError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    return JSONResponse(items)


@router.get("/api/vault/note")
async def vault_note(path: str):
    try:
        note = vault_files.read_note(path)
    except vault_files.VaultError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    return JSONResponse(note)


@router.get("/api/vault/search")
async def vault_search(request: Request, q: str, limit: int = 10):
    mcp = getattr(request.app.state, "mcp_manager", None)
    return JSONResponse(await _proxy_search(mcp, q, limit))


@router.post("/api/vault/note")
async def vault_create(request: Request):
    body = await request.json()
    title = body.get("title", "")
    content = body.get("content", "")
    tags = body.get("tags") or []
    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    try:
        res = vault_files.create_note(title, content, tags)
    except vault_files.VaultError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    return JSONResponse(res, status_code=201)


@router.put("/api/vault/note")
async def vault_update(request: Request):
    body = await request.json()
    path = body.get("path", "")
    content = body.get("content")
    base_hash = body.get("base_hash", "")
    if not path or content is None or not base_hash:
        return JSONResponse(
            {"error": "path, content, and base_hash are required"}, status_code=400
        )
    try:
        res = vault_files.commit_edit(path, content, base_hash)
    except vault_files.ConflictError as e:
        return JSONResponse(
            {"error": str(e), "current_base_hash": e.current_base_hash},
            status_code=409,
        )
    except vault_files.VaultError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    return JSONResponse(res, status_code=200)


async def _proxy_search(mcp, q: str, limit: int) -> list[dict]:
    """Proxy the search_vault MCP; map to the API shape and drop journaling."""
    if mcp is None:
        return []
    limit = max(1, min(int(limit), 50))
    raw = await mcp.call_tool("search_vault", {"query": q, "limit": limit}, max_chars=None)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    for r in data.get("results", []):
        path = r.get("path") or ""
        if vault_files.is_denylisted(path):
            continue
        out.append({
            "path": path,
            "title": r.get("title"),
            "folder": r.get("folder"),
            "heading": r.get("heading"),
            "snippet": r.get("snippet", ""),
            "score": r.get("score", 0),
        })
    return out[:limit]
