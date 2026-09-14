"""Reader markdown chunking and speech-preparation pipeline."""

import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path

import httpx

from service_clients import llm_client
from settings import settings

from reader_store import (
    get_document,
    is_replaced,
    load_appendices,
    load_speech_data,
    save_appendix,
    update_document,
)

log = logging.getLogger(__name__)

READER_PATH = Path(settings.reader.directory)
READER_PATH.mkdir(parents=True, exist_ok=True)

SENTENCE_END = re.compile(r'(?<=[.!?])\s+')
HEADING_RE = re.compile(r'^(#{1,3})\s+(.+)$', re.MULTILINE)
MATH_RE = re.compile(
    r'\$\$?.+?\$\$?'          # $inline$ / $$display$$
    r'|\\\(.+?\\\)'           # \(inline\)
    r'|\\\[.+?\\\]'           # \[display\]
    r'|\\begin\{[a-z]+\*?\}', # equation/align/... environments
    re.DOTALL,
)
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


def strip_latex(text: str) -> str:
    """Last-resort de-LaTeX for when the LLM pass fails: not real spoken math, but far
    better than the command soup ("frac{1}{N} sum_{n=0}...") the TTS reads otherwise.
    Substitutions run twice to unwrap one level of nesting."""
    text = re.sub(r'\\tag\{[^}]*\}', '', text)
    text = re.sub(r'\\(?:left|right|quad|qquad|limits|displaystyle)\b|\\[,;!]', ' ', text)
    for _ in range(2):
        text = re.sub(r'\\(?:widehat|hat)\{([^{}]*)\}', r'\1 hat', text)
        text = re.sub(r'\\(?:overline|bar)\{([^{}]*)\}', r'\1 bar', text)
        text = re.sub(r'\\tilde\{([^{}]*)\}', r'\1 tilde', text)
        text = re.sub(r'\\frac\{([^{}]*)\}\{([^{}]*)\}', r'\1 over \2', text)
        text = re.sub(r'\\sqrt\{([^{}]*)\}', r'square root of \1', text)
        text = re.sub(r'\\(?:mathbf|mathrm|mathcal|text|operatorname)\{([^{}]*)\}', r'\1', text)
    text = re.sub(r'\^2(?![\d\w])', ' squared', text)
    text = re.sub(r'\^\{([^{}]*)\}|\^(\S)', lambda m: f' to the {m.group(1) or m.group(2)} ', text)
    text = re.sub(r'_\{([^{}]*)\}|_(\S)', lambda m: f' sub {m.group(1) or m.group(2)} ', text)
    text = re.sub(r'[${}]', '', text)
    text = re.sub(r'\\\(|\\\)|\\\[|\\\]', ' ', text)
    text = re.sub(r'\\(?:begin|end)\{[a-z]+\*?\}', ' ', text)
    return re.sub(r'  +', ' ', text).strip()


async def _llm_convert_math(_client: httpx.AsyncClient, text: str) -> str:
    attempts = [(settings.reader.llm_url, settings.reader.llm_model)]
    if settings.reader.llm_fallback_url:
        # complete() merges per-url params from the MAIN chain's entry for this
        # url (the fallback is also main-chain hop 3), so anything set there
        # applies to this attempt too — nothing today.
        attempts.append((settings.reader.llm_fallback_url, settings.reader.llm_fallback_model))
    for url, model in attempts:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": MATH_TO_SPEECH_PROMPT},
                {"role": "user", "content": text},
            ],
            # Reasoning models burn tokens inside <think> before the rewrite; 2048 could
            # truncate mid-think, leaving an UNCLOSED think block that THINK_RE can't strip.
            "max_tokens": 8192,
            "temperature": 0.1,
            "stream": False,
        }
        try:
            raw = await llm_client.complete(payload, urls=[url])
            result = THINK_RE.sub("", raw or "").strip()
        except Exception as exc:
            log.warning("Reader LLM failed at %s: %s", url, exc)
            continue
        if result and "<think>" in result:
            log.warning("Reader LLM at %s returned a truncated think block — trying next", url)
            continue
        if result:
            return result

    return strip_latex(text)


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

    # The reader model (qwen3.6-35b-a3b) serves 3 parallel slots on lilripper.
    sem = asyncio.Semaphore(3)

    async def convert(chunk: dict) -> str:
        async with sem:
            return await _convert_chunk(client, chunk)

    async with httpx.AsyncClient(timeout=60.0) as client:
        return list(await asyncio.gather(*(convert(chunk) for chunk in chunks)))


def split_sentences(text: str) -> list[str]:
    return [sentence.strip() for sentence in SENTENCE_END.split(text) if sentence.strip()]


def _build_speech_chunks(raw_chunks: list[dict], converted_texts: list[str], start_index: int = 0) -> list[dict]:
    speech_chunks = []
    for i, (raw_chunk, speech_text) in enumerate(zip(raw_chunks, converted_texts), start=start_index):
        sentences = split_sentences(speech_text)
        if not sentences:
            sentences = [speech_text] if speech_text else [""]
        speech_chunks.append({
            "index": i,
            "heading": raw_chunk["heading"],
            "speech_text": speech_text,
            "sentences": sentences,
        })
    return speech_chunks


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON to `path` via temp file + os.replace so a reader (playback,
    which reads this file at play start) never sees a half-written file, and a
    crash mid-write keeps the old one."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _write_speech(conn: sqlite3.Connection, doc_id: int, title: str, chunks: list[dict], **fields) -> None:
    total_sentences = sum(len(chunk["sentences"]) for chunk in chunks)
    speech_path = READER_PATH / f"{doc_id}.json"
    # The row's title wins over the one captured when the job started: a rename
    # that landed while this job was converting must not be undone here.
    row = get_document(conn, doc_id)
    if row and row.get("title"):
        title = row["title"]
    _atomic_write_json(speech_path, {"title": title, "total_sentences": total_sentences, "chunks": chunks})
    update_document(
        conn,
        doc_id,
        speech_file=str(speech_path),
        chunk_count=len(chunks),
        status="ready",
        error=None,
        **fields,
    )
    log.info("Reader: document %d ready — %d chunks, %d sentences", doc_id, len(chunks), total_sentences)


def rename_document(conn: sqlite3.Connection, doc_id: int, title: str) -> None:
    """Update a document's title in the DB and, if a speech JSON exists, rewrite
    its stored title too (temp + os.replace, same as `_write_speech`). A missing
    or unreadable speech file is not an error — the DB title is the source of
    truth (playback never reads the JSON title) and the rename still succeeds.
    While the document is `processing` the JSON is left alone (see below)."""
    update_document(conn, doc_id, title=title)
    doc = get_document(conn, doc_id)
    if not doc or not doc.get("speech_file"):
        return
    if doc.get("status") == "processing":
        # An ingest/append job is about to replace the speech file; rewriting it
        # here could race that write and clobber freshly converted chunks. The
        # job re-reads the row's title just before it writes, so nothing is lost.
        return
    speech = load_speech_data(doc)
    if not speech:
        log.warning("Reader: document %d has speech_file %s but it is missing or unreadable; title not rewritten there", doc_id, doc["speech_file"])
        return
    speech["title"] = title
    _atomic_write_json(Path(doc["speech_file"]), speech)


def with_appendices(doc_id: int, markdown: str) -> str:
    """Fold any text appended to this document after its original ingest onto the
    end of `markdown`. A retry replays the ORIGINAL source (re-downloads the URL,
    re-reads the file); without this step it would silently drop everything Dave
    appended later. If the document was `replace`d, the original source is what
    Dave discarded, so it is dropped here rather than resurrected. Each block is
    separated by a blank line so it starts its own paragraph and never merges
    into the last one of the base text."""
    appendices = load_appendices(doc_id)
    if not appendices:
        return markdown
    base = "" if is_replaced(doc_id) else markdown.rstrip()
    parts = [base] + [block.strip() for block in appendices]
    return "\n\n".join(part for part in parts if part)


def _derive_title(conn: sqlite3.Connection, doc_id: int, markdown: str, title: str) -> str:
    is_generic = title in ("Untitled", "") or title.startswith("reader_") or title.endswith((".pdf", ".md", ".txt"))
    if not is_generic:
        return title
    heading_match = HEADING_RE.search(markdown)
    if heading_match:
        extracted = heading_match.group(2).strip()
        if 5 < len(extracted) < 200:
            update_document(conn, doc_id, title=extracted)
            log.info("Reader: extracted title from content: %s", extracted)
            return extracted
    elif markdown.strip():
        first_line = markdown.strip().split("\n")[0].strip()[:120]
        if first_line:
            update_document(conn, doc_id, title=first_line)
            return first_line
    return title


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

        markdown = with_appendices(doc_id, markdown)
        title = _derive_title(conn, doc_id, markdown, title)

        raw_chunks = split_into_chunks(markdown)
        if not raw_chunks:
            update_document(conn, doc_id, status="failed", error="No content found")
            return

        log.info("Reader: document %d split into %d chunks", doc_id, len(raw_chunks))

        converted_texts = await _convert_all_chunks(raw_chunks)
        speech_chunks = _build_speech_chunks(raw_chunks, converted_texts)
        _write_speech(conn, doc_id, title, speech_chunks, original_md_file=original_md_path)

    except Exception as exc:
        log.exception("Reader: ingest failed for document %d", doc_id)
        update_document(conn, doc_id, status="failed", error=str(exc))


async def append_to_document(
    conn: sqlite3.Connection,
    doc_id: int,
    text: str,
    replace: bool = False,
    prior_status: str | None = None,
):
    """Add `text` to the end of an existing document's speech JSON.

    Incremental on purpose: only the new text is chunked and sent through math
    conversion, and the existing chunks are kept byte-for-byte with their
    indices, so `last_chunk` / `last_sentence` still point where Dave left
    off. Re-chunking the whole document would not guarantee that — a section
    that grows past three paragraphs is re-split per paragraph.

    With `replace=True` the existing chunks are discarded and the document is
    rebuilt from `text` alone (for when the original pull got a sign-in page
    rather than the article). A document with no usable speech file (e.g. a
    failed URL pull) is rebuilt from `text` either way.

    Atomic from the document's point of view: the appendix file is persisted
    only after conversion succeeds and just before the speech file is written,
    so a failed append (LLM down, disk full) leaves the document exactly as it
    was — a `ready` document stays `ready`, with the failure recorded in
    `error` — and no orphaned appendix waits to be folded into a later retry.
    `prior_status` is what the row said before the caller flipped it to
    `processing`; it is what a failure restores.
    """
    try:
        doc = get_document(conn, doc_id)
        if not doc:
            raise ValueError(f"Document {doc_id} not found")
        title = doc["title"]

        existing: list[dict] = []
        if not replace:
            speech = load_speech_data(doc)
            if speech:
                existing = list(speech.get("chunks") or [])
            elif doc.get("speech_file"):
                log.warning(
                    "Reader: document %d has speech_file %s but it is missing; rebuilding from appended text",
                    doc_id, doc["speech_file"],
                )

        raw_chunks = split_into_chunks(text)
        if not raw_chunks:
            raise ValueError("No content found in appended text")

        log.info(
            "Reader: %s document %d with %d new chunks (after %d existing)",
            "replacing" if replace else "appending to", doc_id, len(raw_chunks), len(existing),
        )
        converted_texts = await _convert_all_chunks(raw_chunks)
        new_chunks = _build_speech_chunks(raw_chunks, converted_texts, start_index=len(existing))

        # Persistence is mandatory, not best-effort: without the appendix a
        # retry replays the original source and silently drops this text — the
        # exact failure this feature exists to prevent.
        save_appendix(doc_id, text, replace=replace)

        # Position can only be kept when the chunks it points into survive.
        fields = {} if existing else {"last_chunk": 0, "last_sentence": 0}
        _write_speech(conn, doc_id, title, existing + new_chunks, **fields)

    except Exception as exc:
        log.exception("Reader: append failed for document %d", doc_id)
        restored = prior_status if prior_status in ("ready", "failed") else "failed"
        update_document(conn, doc_id, status=restored, error=f"Append failed: {exc}")
