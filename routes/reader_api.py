from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from db import connect_db
from reader_store import delete_document, get_document, list_documents, load_speech_data
from reader_ingest_service import ReaderIngestError, retry_reader_document, start_reader_ingest

router = APIRouter()


@router.get("/reader")
async def reader_page():
    return FileResponse("static/reader.html")


@router.post("/api/reader/documents")
async def reader_ingest(request: Request):
    body = await request.json()
    mcp_manager = request.app.state.mcp_manager
    try:
        result = await start_reader_ingest(request.app.state.db_path, mcp_manager, body)
        return JSONResponse(result)
    except ReaderIngestError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status_code)


@router.get("/api/reader/documents")
async def reader_list(request: Request):
    with connect_db(request.app.state.db_path) as conn:
        docs = list_documents(conn)
    return JSONResponse({"documents": docs})


@router.get("/api/reader/documents/{doc_id}")
async def reader_get(doc_id: int, request: Request):
    with connect_db(request.app.state.db_path) as conn:
        doc = get_document(conn, doc_id)
        if not doc:
            return JSONResponse({"error": "not found"}, status_code=404)
        if doc["status"] == "ready":
            speech = load_speech_data(doc)
            if speech:
                doc["total_sentences"] = speech.get("total_sentences", 0)
                doc["sections"] = [
                    {"index": c["index"], "heading": c["heading"], "sentence_count": len(c["sentences"])}
                    for c in speech["chunks"]
                ]
    return JSONResponse({"document": doc})


@router.post("/api/reader/documents/{doc_id}/retry")
async def reader_retry(doc_id: int, request: Request):
    mcp_manager = request.app.state.mcp_manager
    try:
        result = await retry_reader_document(request.app.state.db_path, mcp_manager, doc_id)
        return JSONResponse(result)
    except ReaderIngestError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status_code)


@router.delete("/api/reader/documents/{doc_id}")
async def reader_delete(doc_id: int, request: Request):
    with connect_db(request.app.state.db_path) as conn:
        ok = delete_document(conn, doc_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})
