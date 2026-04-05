"""Reader markdown chunking and speech-preparation pipeline."""

import json
import logging
import re
import sqlite3
from pathlib import Path

import httpx

from service_clients import llm_client
from settings import settings

from reader_store import update_document

log = logging.getLogger(__name__)

READER_PATH = Path(settings.reader.directory)
READER_PATH.mkdir(parents=True, exist_ok=True)

SENTENCE_END = re.compile(r'(?<=[.!?])\s+')
HEADING_RE = re.compile(r'^(#{1,3})\s+(.+)$', re.MULTILINE)
MATH_RE = re.compile(r'\$\$?.+?\$\$?', re.DOTALL)
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

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


def split_into_chunks(markdown: str) -> list[dict]:
    """Split markdown into chunks by headings and paragraphs."""
    chunks = []
    current_heading = None
    current_text_parts = []

    for line in markdown.split("\n"):
        heading_match = HEADING_RE.match(line)
        if heading_match:
            text = "\n".join(current_text_parts).strip()
            if text:
                chunks.append({"heading": current_heading, "text": text})
            current_heading = heading_match.group(2).strip()
            current_text_parts = []
        else:
            current_text_parts.append(line)

    text = "\n".join(current_text_parts).strip()
    if text:
        chunks.append({"heading": current_heading, "text": text})

    final_chunks = []
    for chunk in chunks:
        paragraphs = re.split(r'\n\s*\n', chunk["text"])
        if len(paragraphs) <= 3:
            final_chunks.append(chunk)
        else:
            for i, para in enumerate(paragraphs):
                para = para.strip()
                if not para:
                    continue
                final_chunks.append({
                    "heading": chunk["heading"] if i == 0 else None,
                    "text": para,
                })

    return final_chunks


def clean_for_speech(text: str) -> str:
    """Remove markdown formatting, HTML artifacts, citations, and other non-speech content."""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&[a-zA-Z]+;', '', text)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'\\\[(?:[^\]]*?et\s+al[^\]]*?\d{4}[^\]]*?)\\\]', '', text)
    text = re.sub(r'\[(?:[A-Z][a-zA-Z\s\']+(?:et\s+al\.?)?,?\s*\d{4}[,;\s]*)+\]', '', text)
    text = re.sub(r'\((?:[A-Z][a-zA-Z\s\']+(?:et\s+al\.?)?,?\s*\d{4}[,;\s]*)+\)', '', text)
    text = re.sub(r'\[\d+(?:[,\s\-]+\d+)*\]', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'#page-\d+[-\d]*', '', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^#{1,4}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\\\[', '', text)
    text = re.sub(r'\\\]', '', text)
    text = text.replace('\\\\', ' ')
    text = re.sub(r'\\([a-zA-Z]+)', r'\1', text)
    text = text.replace('†', '')
    text = text.replace('∗', '')
    text = text.replace('‡', '')
    text = re.sub(r'  +', ' ', text)
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
    return text.strip()


def has_math(text: str) -> bool:
    return bool(MATH_RE.search(text))


async def _llm_convert_math(_client: httpx.AsyncClient, text: str) -> str:
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
    except Exception as exc:
        log.warning("Reader LLM failed: %s", exc)

    return re.sub(r'\$+', '', text)


async def _convert_chunk(client: httpx.AsyncClient, chunk: dict) -> str:
    text = chunk["text"]
    if chunk["heading"]:
        text = f"{chunk['heading']}\n\n{text}"

    paragraphs = re.split(r'\n\s*\n', text)
    result_parts = []

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if has_math(para):
            converted = await _llm_convert_math(client, para)
            result_parts.append(clean_for_speech(converted))
        else:
            result_parts.append(clean_for_speech(para))

    return "\n\n".join(result_parts)


async def _convert_all_chunks(chunks: list[dict]) -> list[str]:
    math_count = sum(1 for chunk in chunks if has_math(chunk["text"]))
    log.info("Reader: %d of %d chunks contain math — only those hit the LLM", math_count, len(chunks))

    results = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for chunk in chunks:
            results.append(await _convert_chunk(client, chunk))
    return results


def split_sentences(text: str) -> list[str]:
    return [sentence.strip() for sentence in SENTENCE_END.split(text) if sentence.strip()]


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

        is_generic = title in ("Untitled", "") or title.startswith("reader_") or title.endswith((".pdf", ".md", ".txt"))
        if is_generic:
            heading_match = HEADING_RE.search(markdown)
            if heading_match:
                extracted = heading_match.group(2).strip()
                if 5 < len(extracted) < 200:
                    title = extracted
                    update_document(conn, doc_id, title=title)
                    log.info("Reader: extracted title from content: %s", title)
            elif markdown.strip():
                first_line = markdown.strip().split("\n")[0].strip()[:120]
                if first_line:
                    title = first_line
                    update_document(conn, doc_id, title=title)

        raw_chunks = split_into_chunks(markdown)
        if not raw_chunks:
            update_document(conn, doc_id, status="failed", error="No content found")
            return

        log.info("Reader: document %d split into %d chunks", doc_id, len(raw_chunks))

        converted_texts = await _convert_all_chunks(raw_chunks)

        speech_chunks = []
        for i, (raw_chunk, speech_text) in enumerate(zip(raw_chunks, converted_texts)):
            sentences = split_sentences(speech_text)
            if not sentences:
                sentences = [speech_text] if speech_text else [""]

            speech_chunks.append({
                "index": i,
                "heading": raw_chunk["heading"],
                "speech_text": speech_text,
                "sentences": sentences,
            })

        total_sentences = sum(len(chunk["sentences"]) for chunk in speech_chunks)
        speech_data = {
            "title": title,
            "total_sentences": total_sentences,
            "chunks": speech_chunks,
        }
        speech_path = READER_PATH / f"{doc_id}.json"
        speech_path.write_text(json.dumps(speech_data, indent=2))

        update_document(
            conn,
            doc_id,
            speech_file=str(speech_path),
            original_md_file=original_md_path,
            chunk_count=len(speech_chunks),
            status="ready",
        )
        log.info("Reader: document %d ready — %d chunks, %d sentences", doc_id, len(speech_chunks), total_sentences)

    except Exception as exc:
        log.exception("Reader: ingest failed for document %d", doc_id)
        update_document(conn, doc_id, status="failed", error=str(exc))
