"""Look-up tools over a Reader document, for chatting about it (/reader's chat panel).

They read the speech JSON, so chunk and sentence indices are the same ones playback
and `reader_position` use. Documents converted since 2026-10-31 keep each chunk's
source markdown (`source_text`: LaTeX and citations intact); older ones fall back to
the speech text, which has citations stripped and math spelled out.
"""

from __future__ import annotations

import re

from reader_store import get_document, load_speech_data

OUTLINE_CHARS = 3500
READ_CHARS = 6000
READ_DEFAULT_CHUNKS = 3
SEARCH_SNIPPET = 500
_WORD = re.compile(r"[a-z0-9]+")


def _ready_speech(session, args: dict) -> tuple[dict | None, dict | None, str | None]:
    """(doc, speech, error) for the document_id in args."""
    conn = session.conn if session else None
    if conn is None:
        return None, None, "Error: no database connection available."
    try:
        doc_id = int(args.get("document_id"))
    except (TypeError, ValueError):
        return None, None, "Error: document_id (an integer) is required."
    doc = get_document(conn, doc_id)
    if not doc:
        return None, None, f"Error: no reader document #{doc_id}."
    if doc["status"] != "ready":
        return None, None, f"Error: document #{doc_id} is {doc['status']}, not ready."
    speech = load_speech_data(doc)
    if not speech or not speech.get("chunks"):
        return None, None, f"Error: document #{doc_id} has no converted text."
    return doc, speech, None


def _chunk_text(chunk: dict) -> str:
    return (chunk.get("source_text") or chunk.get("speech_text") or "").strip()


def _sections(chunks: list[dict]) -> list[str | None]:
    """The section heading each chunk falls under (a heading carries forward)."""
    current = None
    out = []
    for chunk in chunks:
        if chunk.get("heading"):
            current = chunk["heading"]
        out.append(current)
    return out


def reader_document_outline(args: dict, session=None, _mcp_manager=None) -> str:
    doc, speech, error = _ready_speech(session, args)
    if error:
        return error
    chunks = speech["chunks"]
    sentences = sum(len(c.get("sentences") or []) for c in chunks)
    lines = [f"#{doc['id']} {doc['title']}: {len(chunks)} chunks, {sentences} sentences.",
             "Sections (first chunk index, heading):"]
    for chunk in chunks:
        if chunk.get("heading"):
            indent = "  " * (max(chunk.get("level") or 1, 1) - 1)
            lines.append(f"{indent}[{chunk['index']}] {chunk['heading']}")
    if len(lines) == 2:
        lines.append("(no headings; read it by chunk index, 0 to %d)" % (len(chunks) - 1))
    out = "\n".join(lines)
    if len(out) > OUTLINE_CHARS:
        out = out[:OUTLINE_CHARS].rsplit("\n", 1)[0] + "\n… (outline cut; use search_reader_document)"
    return out


def read_reader_document(args: dict, session=None, _mcp_manager=None) -> str:
    doc, speech, error = _ready_speech(session, args)
    if error:
        return error
    chunks = speech["chunks"]
    try:
        start = int(args.get("start_chunk", 0))
        end = int(args.get("end_chunk", start + READ_DEFAULT_CHUNKS - 1))
    except (TypeError, ValueError):
        return "Error: start_chunk and end_chunk must be integers."
    if not 0 <= start < len(chunks):
        return f"Error: start_chunk must be between 0 and {len(chunks) - 1}."
    end = max(start, min(end, len(chunks) - 1))
    sections = _sections(chunks)
    parts: list[str] = []
    used = 0
    last = start - 1
    for i in range(start, end + 1):
        block = f"[chunk {i}] (section: {sections[i] or 'none'})\n{_chunk_text(chunks[i])}"
        if parts and used + len(block) > READ_CHARS:
            break
        if not parts and len(block) > READ_CHARS:
            block = block[:READ_CHARS] + " …(chunk cut)"
        parts.append(block)
        used += len(block)
        last = i
    out = "\n\n".join(parts)
    if last < len(chunks) - 1:
        out += f"\n\n(continues at chunk {last + 1} of {len(chunks) - 1})"
    return out


def search_reader_document(args: dict, session=None, _mcp_manager=None) -> str:
    doc, speech, error = _ready_speech(session, args)
    if error:
        return error
    terms = set(_WORD.findall(str(args.get("query", "")).lower()))
    if not terms:
        return "Error: query is required."
    try:
        limit = max(1, min(int(args.get("limit", 5)), 10))
    except (TypeError, ValueError):
        limit = 5
    chunks = speech["chunks"]
    sections = _sections(chunks)
    scored = []
    for i, chunk in enumerate(chunks):
        text = _chunk_text(chunk)
        words = _WORD.findall(text.lower())
        hits = sum(1 for w in words if w in terms)
        distinct = len(terms & set(words))
        if distinct:
            scored.append((distinct, hits, -i, i, text))
    if not scored:
        return f"No chunk of #{doc['id']} mentions any of: {', '.join(sorted(terms))}."
    scored.sort(reverse=True)
    lines = [f"Best matches in #{doc['id']} (read more with read_reader_document):"]
    for distinct, _hits, _neg, i, text in scored[:limit]:
        snippet = text if len(text) <= SEARCH_SNIPPET else text[:SEARCH_SNIPPET] + " …"
        lines.append(f"[chunk {i}] (section: {sections[i] or 'none'}; {distinct}/{len(terms)} terms)\n{snippet}")
    return "\n\n".join(lines)


def place_context(conn, doc_id: int, position: dict | None) -> str:
    """The per-turn note that tells the model which document is open and where Dave is."""
    doc = get_document(conn, doc_id)
    if not doc:
        return ""
    lines = [
        f"Dave is chatting from the Reader about document #{doc_id}, \"{doc['title']}\". "
        "Answer from the document first: look it up with reader_document_outline, "
        "read_reader_document and search_reader_document (document_id "
        f"{doc_id}) rather than guessing. Use web search or scholar when the question "
        "goes beyond the document. \"This\" or \"here\" means where he is listening."
    ]
    speech = load_speech_data(doc) if doc["status"] == "ready" else None
    if position is None and (doc.get("last_chunk") or doc.get("last_sentence")):
        # Paused (or not playing): the place the reader saved, which pausing writes.
        position = {"chunk_index": doc["last_chunk"], "sentence_index": doc["last_sentence"]}
    if speech and position:
        chunks = speech.get("chunks") or []
        ci, si = position.get("chunk_index"), position.get("sentence_index")
        if isinstance(ci, int) and 0 <= ci < len(chunks):
            sentences = chunks[ci].get("sentences") or []
            section = _sections(chunks)[ci]
            if isinstance(si, int) and 0 <= si < len(sentences):
                around = " ".join(sentences[max(si - 1, 0):si + 1])
                lines.append(
                    f"He is at chunk {ci}, sentence {si}"
                    + (f" (section \"{section}\")" if section else "")
                    + f". The sentence he was hearing (and the one before): \"{around}\""
                )
    return "\n".join(lines)
