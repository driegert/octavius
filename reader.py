"""Document reader — ingest pipeline, math-to-speech conversion, audio streaming."""

import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from config import READER_DIR
from service_clients import llm_client
from settings import settings
from tts import synthesize

log = logging.getLogger(__name__)

READER_PATH = Path(READER_DIR)
READER_PATH.mkdir(parents=True, exist_ok=True)

# Sentence boundary regex (same as agent.py)
SENTENCE_END = re.compile(r'(?<=[.!?])\s+')

# Heading regex for chunk splitting
HEADING_RE = re.compile(r'^(#{1,3})\s+(.+)$', re.MULTILINE)

MATH_TO_SPEECH_PROMPT = """Rewrite the following text, replacing all LaTeX math expressions with natural spoken English. Keep all surrounding non-math text exactly as-is. Only change the math parts.

Examples:
- $x^2$ becomes "x squared"
- $\\hat{\\beta}$ becomes "beta hat"
- $\\bar{x}$ becomes "x bar"
- $\\frac{a}{b}$ becomes "a over b"
- $\\sum_{i=1}^{n}$ becomes "the sum from i equals 1 to n"
- $\\int_0^1 f(x) dx$ becomes "the integral from 0 to 1 of f of x dx"
- $\\alpha$ becomes "alpha"
- $p < 0.05$ becomes "p less than 0.05"

Output ONLY the rewritten text. No preamble, no explanation."""

# Regex to detect math expressions in text
MATH_RE = re.compile(r'\$\$?.+?\$\$?', re.DOTALL)

# Strip <think> tags from Qwen3.5 responses
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- Document CRUD -----------------------------------------------------------

def create_document(conn: sqlite3.Connection, title: str, source_type: str,
                    source_path: str | None = None,
                    saved_item_id: int | None = None) -> int:
    """Create a reader_documents row. Returns the document ID."""
    now = _now()
    cursor = conn.execute(
        """INSERT INTO reader_documents
           (title, source_type, source_path, saved_item_id, status, created_at)
           VALUES (?, ?, ?, ?, 'processing', ?)""",
        (title, source_type, source_path, saved_item_id, now),
    )
    conn.commit()
    return cursor.lastrowid


def update_document(conn: sqlite3.Connection, doc_id: int, **kwargs):
    """Update fields on a reader_documents row."""
    kwargs["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [doc_id]
    conn.execute(f"UPDATE reader_documents SET {sets} WHERE id = ?", vals)
    conn.commit()


def get_document(conn: sqlite3.Connection, doc_id: int) -> dict | None:
    row = conn.execute(
        """SELECT id, title, source_type, source_path, saved_item_id,
                  speech_file, original_md_file, chunk_count, status, error,
                  last_chunk, last_sentence, created_at, updated_at
           FROM reader_documents WHERE id = ?""",
        (doc_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "title": row[1], "source_type": row[2],
        "source_path": row[3], "saved_item_id": row[4],
        "speech_file": row[5], "original_md_file": row[6],
        "chunk_count": row[7], "status": row[8], "error": row[9],
        "last_chunk": row[10], "last_sentence": row[11],
        "created_at": row[12], "updated_at": row[13],
    }


def list_documents(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT id, title, source_type, chunk_count, status, error, created_at
           FROM reader_documents
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {"id": r[0], "title": r[1], "source_type": r[2],
         "chunk_count": r[3], "status": r[4], "error": r[5], "created_at": r[6]}
        for r in rows
    ]


def delete_document(conn: sqlite3.Connection, doc_id: int) -> bool:
    doc = get_document(conn, doc_id)
    if not doc:
        return False
    # Remove speech file
    if doc["speech_file"]:
        p = Path(doc["speech_file"])
        if p.exists():
            p.unlink()
    conn.execute("DELETE FROM reader_documents WHERE id = ?", (doc_id,))
    conn.commit()
    return True


def fail_stale_processing_documents(
    conn: sqlite3.Connection,
    error_message: str = "Document processing was interrupted before completion.",
) -> int:
    """Mark orphaned processing rows as failed on startup.

    Reader ingest tasks are in-memory background tasks, so anything still marked
    processing after a restart cannot complete without being requeued.
    """
    cursor = conn.execute(
        """UPDATE reader_documents
           SET status = 'failed', error = ?, updated_at = ?
           WHERE status = 'processing'""",
        (error_message, _now()),
    )
    conn.commit()
    return cursor.rowcount


# -- Markdown chunking -------------------------------------------------------

def _split_into_chunks(markdown: str) -> list[dict]:
    """Split markdown into chunks by headings and paragraphs.

    Returns list of {"heading": str|None, "text": str}.
    """
    chunks = []
    current_heading = None
    current_text_parts = []

    for line in markdown.split("\n"):
        heading_match = HEADING_RE.match(line)
        if heading_match:
            # Flush current chunk
            text = "\n".join(current_text_parts).strip()
            if text:
                chunks.append({"heading": current_heading, "text": text})
            current_heading = heading_match.group(2).strip()
            current_text_parts = []
        else:
            current_text_parts.append(line)

    # Flush last chunk
    text = "\n".join(current_text_parts).strip()
    if text:
        chunks.append({"heading": current_heading, "text": text})

    # Split large chunks further on double-newlines
    final_chunks = []
    for chunk in chunks:
        paragraphs = re.split(r'\n\s*\n', chunk["text"])
        if len(paragraphs) <= 3:
            final_chunks.append(chunk)
        else:
            # First paragraph keeps the heading
            for i, para in enumerate(paragraphs):
                para = para.strip()
                if not para:
                    continue
                final_chunks.append({
                    "heading": chunk["heading"] if i == 0 else None,
                    "text": para,
                })

    return final_chunks


# -- LLM math-to-speech conversion ------------------------------------------

def _clean_for_speech(text: str) -> str:
    """Remove markdown formatting, HTML artifacts, citations, and other
    non-speech content, keeping clean readable text."""
    # HTML tags (<sup>, <span>, etc.)
    text = re.sub(r'<[^>]+>', '', text)
    # HTML entities
    text = re.sub(r'&[a-zA-Z]+;', '', text)
    text = re.sub(r'&#\d+;', '', text)

    # Citations with escaped brackets: \[Author et al., 2023\]
    text = re.sub(r'\\\[(?:[^\]]*?et\s+al[^\]]*?\d{4}[^\]]*?)\\\]', '', text)
    # Citations: [Author et al., 2023] or [Author et al., 2023, Other et al., 2024]
    text = re.sub(r'\[(?:[A-Z][a-zA-Z\s\']+(?:et\s+al\.?)?,?\s*\d{4}[,;\s]*)+\]', '', text)
    # Parenthetical citations: (Author et al., 2023)
    text = re.sub(r'\((?:[A-Z][a-zA-Z\s\']+(?:et\s+al\.?)?,?\s*\d{4}[,;\s]*)+\)', '', text)
    # Numeric citation brackets: [1], [1, 2], [1-3]
    text = re.sub(r'\[\d+(?:[,\s\-]+\d+)*\]', '', text)

    # Markdown links [text](url) -> text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Raw URLs
    text = re.sub(r'https?://\S+', '', text)
    # Page anchors
    text = re.sub(r'#page-\d+[-\d]*', '', text)

    # Markdown formatting
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # bold
    text = re.sub(r'\*(.+?)\*', r'\1', text)       # italic
    text = re.sub(r'`(.+?)`', r'\1', text)         # inline code
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)  # bullets
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)  # numbered
    text = re.sub(r'^#{1,4}\s+', '', text, flags=re.MULTILINE)  # headings

    # LaTeX remnants (backslashes, escaped brackets)
    text = re.sub(r'\\\[', '', text)
    text = re.sub(r'\\\]', '', text)
    text = text.replace('\\\\', ' ')
    # Stray backslashes before letters (e.g. \text, \mathbb leftover)
    text = re.sub(r'\\([a-zA-Z]+)', r'\1', text)

    # Footnote markers
    text = text.replace('†', '')
    text = text.replace('∗', '')
    text = text.replace('‡', '')

    # Collapse multiple spaces/newlines
    text = re.sub(r'  +', ' ', text)
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)

    return text.strip()


def _has_math(text: str) -> bool:
    """Check if text contains LaTeX math expressions."""
    return bool(MATH_RE.search(text))


async def _llm_convert_math(client: httpx.AsyncClient, text: str) -> str:
    """Send a short piece of text to the LLM to convert math to speech."""
    payload = {
        "model": settings.reader.llm_model,
        "messages": [
            {"role": "system", "content": MATH_TO_SPEECH_PROMPT},
            {"role": "user", "content": text},
        ],
        "max_tokens": 2048,
        "temperature": 0.1,
        "stream": False,
    }

    try:
        raw = await llm_client.complete(payload, urls=[settings.reader.llm_url])
        result = THINK_RE.sub("", raw or "").strip()
        if result:
            return result
    except Exception as e:
        log.warning("Reader LLM failed: %s", e)

    # Fallback: strip the dollar signs at least
    return re.sub(r'\$+', '', text)


async def _convert_chunk(client: httpx.AsyncClient, chunk: dict) -> str:
    """Convert a single chunk to speech-ready text.

    Only calls the LLM for paragraphs that contain math. Everything else
    just gets markdown stripped.
    """
    text = chunk["text"]
    if chunk["heading"]:
        text = f"{chunk['heading']}\n\n{text}"

    # Split into paragraphs, only send math-containing ones to LLM
    paragraphs = re.split(r'\n\s*\n', text)
    result_parts = []

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if _has_math(para):
            converted = await _llm_convert_math(client, para)
            result_parts.append(_clean_for_speech(converted))
        else:
            result_parts.append(_clean_for_speech(para))

    return "\n\n".join(result_parts)


async def _convert_all_chunks(chunks: list[dict]) -> list[str]:
    """Convert all chunks to speech-ready text.

    Returns list of speech-ready text strings, one per chunk.
    """
    math_count = sum(1 for c in chunks if _has_math(c["text"]))
    log.info("Reader: %d of %d chunks contain math — only those hit the LLM", math_count, len(chunks))

    results = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for chunk in chunks:
            converted = await _convert_chunk(client, chunk)
            results.append(converted)
    return results


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences."""
    parts = SENTENCE_END.split(text)
    sentences = [s.strip() for s in parts if s.strip()]
    return sentences


# -- Full ingest pipeline ----------------------------------------------------

async def ingest_document(
    conn: sqlite3.Connection,
    doc_id: int,
    markdown: str,
    title: str,
    original_md_path: str | None = None,
):
    """Process markdown into speech-ready JSON. Updates the DB row on completion."""
    try:
        log.info("Reader: ingesting document %d: %s", doc_id, title)

        # Try to extract a better title from content only if current title is clearly generic
        is_generic = title in ("Untitled", "") or title.startswith("reader_") or title.endswith((".pdf", ".md", ".txt"))
        if is_generic:
            heading_match = HEADING_RE.search(markdown)
            if heading_match:
                extracted = heading_match.group(2).strip()
                if len(extracted) > 5 and len(extracted) < 200:
                    title = extracted
                    update_document(conn, doc_id, title=title)
                    log.info("Reader: extracted title from content: %s", title)
            elif markdown.strip():
                # Use first non-empty line as title
                first_line = markdown.strip().split("\n")[0].strip()[:120]
                if first_line:
                    title = first_line
                    update_document(conn, doc_id, title=title)

        # 1. Split into chunks
        raw_chunks = _split_into_chunks(markdown)
        if not raw_chunks:
            update_document(conn, doc_id, status="failed", error="No content found")
            return

        log.info("Reader: document %d split into %d chunks", doc_id, len(raw_chunks))

        # 2. Convert math to speech (only paragraphs with $ hit the LLM)
        converted_texts = await _convert_all_chunks(raw_chunks)

        speech_chunks = []
        for i, (raw_chunk, speech_text) in enumerate(zip(raw_chunks, converted_texts)):
            sentences = _split_sentences(speech_text)
            if not sentences:
                sentences = [speech_text] if speech_text else [""]

            speech_chunks.append({
                "index": i,
                "heading": raw_chunk["heading"],
                "speech_text": speech_text,
                "sentences": sentences,
            })

        # 3. Compute totals
        total_sentences = sum(len(c["sentences"]) for c in speech_chunks)

        # 4. Save speech JSON
        speech_data = {
            "title": title,
            "total_sentences": total_sentences,
            "chunks": speech_chunks,
        }
        speech_path = READER_PATH / f"{doc_id}.json"
        speech_path.write_text(json.dumps(speech_data, indent=2))

        # 5. Update DB
        update_document(
            conn, doc_id,
            speech_file=str(speech_path),
            original_md_file=original_md_path,
            chunk_count=len(speech_chunks),
            status="ready",
        )
        log.info(
            "Reader: document %d ready — %d chunks, %d sentences",
            doc_id, len(speech_chunks), total_sentences,
        )

    except Exception as e:
        log.exception("Reader: ingest failed for document %d", doc_id)
        update_document(conn, doc_id, status="failed", error=str(e))


def load_speech_data(doc: dict) -> dict | None:
    """Load the speech JSON file for a document."""
    speech_file = doc.get("speech_file")
    if not speech_file:
        return None
    p = Path(speech_file)
    if not p.exists():
        return None
    return json.loads(p.read_text())


# -- Audio streaming ---------------------------------------------------------

async def stream_reader_audio(ws, doc_id: int, conn: sqlite3.Connection,
                              chunk_index: int = 0, sentence_index: int = 0,
                              voice: str | None = None):
    """Stream TTS audio for a document over WebSocket.

    Sends reader_position JSON before each sentence, then WAV bytes.
    Designed to be run as an asyncio.Task so it can be cancelled for pause/seek.
    """
    doc = get_document(conn, doc_id)
    if not doc or doc["status"] != "ready":
        await ws.send_text(json.dumps({"type": "status", "text": "Document not ready."}))
        return

    speech = load_speech_data(doc)
    if not speech:
        await ws.send_text(json.dumps({"type": "status", "text": "Speech file not found."}))
        return

    chunks = speech["chunks"]
    total_chunks = len(chunks)
    total_sentences = speech.get("total_sentences", 0)

    # Compute global sentence offset for the starting position
    sentence_global = 0
    for c in chunks[:chunk_index]:
        sentence_global += len(c["sentences"])
    sentence_global += sentence_index

    try:
        for ci in range(chunk_index, total_chunks):
            chunk = chunks[ci]
            sentences = chunk["sentences"]
            start_si = sentence_index if ci == chunk_index else 0

            for si in range(start_si, len(sentences)):
                sentence = sentences[si]
                if not sentence.strip():
                    sentence_global += 1
                    continue

                # Send position update
                await ws.send_text(json.dumps({
                    "type": "reader_position",
                    "chunk_index": ci,
                    "sentence_index": si,
                    "total_chunks": total_chunks,
                    "total_sentences": total_sentences,
                    "sentence_global": sentence_global,
                    "heading": chunk.get("heading"),
                    "sentence_text": sentence,
                }))

                # Save position to DB periodically (every sentence)
                update_document(conn, doc_id, last_chunk=ci, last_sentence=si)

                # Synthesize and send audio
                wav_bytes = await synthesize(sentence, voice=voice)
                await ws.send_bytes(wav_bytes)

                sentence_global += 1

        # Document finished
        update_document(conn, doc_id, last_chunk=0, last_sentence=0)
        await ws.send_text(json.dumps({"type": "reader_audio_done"}))

    except asyncio.CancelledError:
        # Pause/seek — save position so it can be resumed later.
        update_document(conn, doc_id, last_chunk=ci, last_sentence=si)
        log.info("Reader: playback paused at chunk %d, sentence %d", ci, si)
