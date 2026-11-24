from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from db import connect_db
from reader_store import (
    delete_document, get_document, list_documents, load_speech_data, purge_temporary_documents,
    sentences_before, set_finished,
)
from reader_ingest_service import (
    ReaderIngestError,
    append_reader_document,
    rename_reader_document,
    retry_reader_document,
    start_reader_ingest,
)

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
        purge_temporary_documents(conn)
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
                if doc.get("progress_sentences") is None:
                    doc["progress_sentences"] = sentences_before(
                        speech["chunks"], doc["last_chunk"], doc["last_sentence"])
                doc["sections"] = [
                    {
                        "index": c["index"],
                        "heading": c["heading"],
                        # 1-3 when the heading's depth is known; older documents omit it.
                        **({"level": c["level"]} if c.get("level") and c.get("heading") else {}),
                        "sentence_count": len(c["sentences"]),
                        # Full sentence text so clients can render a tappable read-along and
                        # seek to any sentence. Additive; the PWA ignores the extra field.
                        "sentences": c["sentences"],
                        # The chunk's markdown (LaTeX and tables intact), which /reader can
                        # show in place of the spoken text. Chunks converted before
                        # 2026-10-31 have none and are shown as spoken.
                        **({"source_text": c["source_text"]} if c.get("source_text") else {}),
                    }
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


@router.post("/api/reader/documents/{doc_id}/append")
async def reader_append(doc_id: int, request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Expected a JSON object"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected a JSON object"}, status_code=400)
    try:
        result = await append_reader_document(request.app.state.db_path, doc_id, body)
        return JSONResponse(result)
    except ReaderIngestError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status_code)


@router.patch("/api/reader/documents/{doc_id}")
async def reader_rename(doc_id: int, request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Expected a JSON object"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected a JSON object"}, status_code=400)
    try:
        result = await rename_reader_document(request.app.state.db_path, doc_id, body)
        return JSONResponse(result)
    except ReaderIngestError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status_code)


@router.post("/api/reader/documents/{doc_id}/finished")
async def reader_set_finished(doc_id: int, request: Request):
    """{"finished": true} marks the document read without playing it to the end;
    false marks it unread. Either way the next Play starts from the beginning."""
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Expected a JSON object"}, status_code=400)
    # Strict: bool("false") is True, and this overwrites the saved place.
    if not isinstance(body, dict) or not isinstance(body.get("finished"), bool):
        return JSONResponse({"error": "Expected {\"finished\": true|false}"}, status_code=400)
    with connect_db(request.app.state.db_path) as conn:
        doc = get_document(conn, doc_id)
        if not doc:
            return JSONResponse({"error": "not found"}, status_code=404)
        result = set_finished(conn, doc_id, body["finished"])
    if result is None:
        return JSONResponse({"error": "Only a ready document can be marked"}, status_code=409)
    return JSONResponse(result)


@router.delete("/api/reader/documents/{doc_id}")
async def reader_delete(doc_id: int, request: Request):
    with connect_db(request.app.state.db_path) as conn:
        ok = delete_document(conn, doc_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})
