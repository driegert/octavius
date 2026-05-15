import json
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from service_clients import embedding_client, summary_client
from settings import settings

log = logging.getLogger(__name__)

EMBEDDING_TIMEOUT = settings.embedding_timeout

SUMMARY_MODEL = settings.summary_model
SUMMARY_TIMEOUT = settings.summary_timeout

RESULT_SUMMARY_MAX_CHARS = settings.result_summary_max_chars
TAG_GENERATION_MIN_MESSAGES = settings.tag_generation_min_messages

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

SUMMARY_SYSTEM_PROMPT = (
    "/no_think\n"
    "Produce a one-sentence summary of this conversation for later search, "
    "and decide whether it is worth indexing.\n\n"
    "Rules for the summary:\n"
    "- One sentence, action-oriented, past tense.\n"
    "- Include the specific subject — project name, person, document, concept — "
    "not a generic noun like 'tasks' or 'emails'.\n"
    "- No preamble. No markdown.\n\n"
    "Rules for the index flag:\n"
    "- index=true if the conversation contains decisions, novel content, "
    "drafted text, conclusions, or anything the user might later want to find.\n"
    "- index=false if the conversation is purely read-only retrieval "
    "(listing emails, listing tasks, asking the date, weather lookups, etc.) "
    "and nothing was added on top.\n\n"
    "Output ONLY a single-line JSON object, no markdown fence:\n"
    '{"summary": "...", "index": true}'
)


@dataclass
class SummaryResult:
    summary: str | None
    index: bool

TAG_SYSTEM_PROMPT = (
    "/no_think\n"
    "Extract 1-5 short topic tags from this conversation. "
    "Return ONLY a JSON array of lowercase strings, e.g. [\"statistics\", \"email\"]. "
    "No explanation, no markdown, just the JSON array."
)


def embed_text(text: str) -> bytes | None:
    return embedding_client.embed_text(text, timeout=EMBEDDING_TIMEOUT)


async def embed_text_async(text: str) -> bytes | None:
    return await embedding_client.aembed_text(text, timeout=EMBEDDING_TIMEOUT)


def store_embedding(conn: sqlite3.Connection, table: str, id_col: str, row_id: int, text: str):
    emb = embed_text(text)
    _store_embedding_bytes(conn, table, id_col, row_id, emb)


async def store_embedding_async(conn: sqlite3.Connection, table: str, id_col: str, row_id: int, text: str):
    emb = await embed_text_async(text)
    _store_embedding_bytes(conn, table, id_col, row_id, emb)


def _store_embedding_bytes(
    conn: sqlite3.Connection,
    table: str,
    id_col: str,
    row_id: int,
    emb: bytes | None,
):
    if emb is None:
        return
    try:
        conn.execute(f"DELETE FROM {table} WHERE {id_col} = ?", (row_id,))
        conn.execute(
            f"INSERT INTO {table}({id_col}, embedding) VALUES (?, ?)",
            (row_id, emb),
        )
        conn.commit()
    except Exception:
        log.debug("Failed to store embedding in %s", table, exc_info=True)


def build_transcript(messages: list[dict], *, max_content_chars: int) -> str:
    transcript_parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "system" or not content:
            continue
        if len(content) > max_content_chars:
            content = content[:max_content_chars] + "..."
        transcript_parts.append(f"{role}: {content}")
    return "\n".join(transcript_parts)


def _request_completion(payload: dict) -> str | None:
    text = summary_client.complete(payload, timeout=SUMMARY_TIMEOUT)
    return _clean_completion_text(text)


async def _request_completion_async(payload: dict) -> str | None:
    text = await summary_client.acomplete(payload, timeout=SUMMARY_TIMEOUT)
    return _clean_completion_text(text)


def _clean_completion_text(text: str | None) -> str | None:
    if text is None:
        return None
    if not text:
        log.warning("Summary/tag pipeline received empty content from upstream LLM")
        return None
    cleaned = THINK_RE.sub("", text).strip()
    if not cleaned:
        log.warning(
            "Summary/tag pipeline got %d chars but all of it was <think> content — "
            "visible output is empty (try /no_think in prompt or bump max_tokens)",
            len(text),
        )
        return None
    return cleaned


def _summary_payload(transcript: str) -> dict:
    return {
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
        "max_tokens": 1024,
        "temperature": 0.3,
        # Qwen 3.x: disable think mode so max_tokens isn't consumed by
        # hidden reasoning before any visible output is produced.
        "chat_template_kwargs": {"enable_thinking": False},
    }


def generate_summary(messages: list[dict]) -> SummaryResult:
    transcript = build_transcript(messages, max_content_chars=1000)
    if not transcript:
        return SummaryResult(summary=None, index=False)
    return _parse_summary_result(_request_completion(_summary_payload(transcript)))


async def generate_summary_async(messages: list[dict]) -> SummaryResult:
    transcript = build_transcript(messages, max_content_chars=1000)
    if not transcript:
        return SummaryResult(summary=None, index=False)
    return _parse_summary_result(await _request_completion_async(_summary_payload(transcript)))


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_summary_result(text: str | None) -> SummaryResult:
    if not text:
        return SummaryResult(summary=None, index=False)
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        log.warning("Summary output had no JSON object; treating as plain summary: %r", text[:120])
        return SummaryResult(summary=text.strip() or None, index=True)
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("Summary JSON failed to parse; treating raw text as summary: %r", text[:120])
        return SummaryResult(summary=text.strip() or None, index=True)
    if not isinstance(obj, dict):
        return SummaryResult(summary=text.strip() or None, index=True)
    summary = obj.get("summary")
    if isinstance(summary, str):
        summary = summary.strip() or None
    else:
        summary = None
    index_raw = obj.get("index", True)
    if isinstance(index_raw, bool):
        index = index_raw
    elif isinstance(index_raw, str):
        index = index_raw.strip().lower() not in {"false", "0", "no"}
    else:
        index = bool(index_raw)
    return SummaryResult(summary=summary, index=index)


def generate_tags(messages: list[dict]) -> list[str]:
    return _parse_tags(_build_tags_text(messages, _request_completion))


async def generate_tags_async(messages: list[dict]) -> list[str]:
    return _parse_tags(await _build_tags_text_async(messages, _request_completion_async))


def _build_tags_payload(messages: list[dict]) -> dict | None:
    transcript = build_transcript(messages, max_content_chars=500)
    if not transcript:
        return None
    if transcript.count("\n") + 1 < TAG_GENERATION_MIN_MESSAGES:
        return None

    return {
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": TAG_SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
        "max_tokens": 768,
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def _build_tags_text(
    messages: list[dict],
    request_completion: Callable[[dict], str | None],
) -> str | None:
    payload = _build_tags_payload(messages)
    if payload is None:
        return None
    return request_completion(payload)


async def _build_tags_text_async(
    messages: list[dict],
    request_completion: Callable[[dict], Awaitable[str | None]],
) -> str | None:
    payload = _build_tags_payload(messages)
    if payload is None:
        return None
    return await request_completion(payload)


def _parse_tags(text: str | None) -> list[str]:
    if not text:
        return []
    try:
        tags = json.loads(text)
    except json.JSONDecodeError:
        log.debug("Tag generation returned invalid JSON", exc_info=True)
        return []
    if not isinstance(tags, list):
        return []
    return [str(tag).lower().strip() for tag in tags if tag][:5]
