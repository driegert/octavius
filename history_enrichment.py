import json
import logging
import re
import sqlite3

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
    "Summarize the following conversation in 2-3 sentences. "
    "Focus on the key topics discussed, decisions made, and any actions taken. "
    "Be concise and factual. Do not use markdown formatting. "
    "Do not include any preamble like 'Here is a summary' — just the summary itself."
)

TAG_SYSTEM_PROMPT = (
    "Extract 1-5 short topic tags from this conversation. "
    "Return ONLY a JSON array of lowercase strings, e.g. [\"statistics\", \"email\"]. "
    "No explanation, no markdown, just the JSON array."
)


def embed_text(text: str) -> bytes | None:
    return embedding_client.embed_text(text, timeout=EMBEDDING_TIMEOUT)


def store_embedding(conn: sqlite3.Connection, table: str, id_col: str, row_id: int, text: str):
    emb = embed_text(text)
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
    if not text:
        return None
    text = THINK_RE.sub("", text).strip()
    return text if text else None


def generate_summary(messages: list[dict]) -> str | None:
    transcript = build_transcript(messages, max_content_chars=1000)
    if not transcript:
        return None

    payload = {
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
        "max_tokens": 1024,
        "temperature": 0.3,
    }
    return _request_completion(payload)


def generate_tags(messages: list[dict]) -> list[str]:
    transcript = build_transcript(messages, max_content_chars=500)
    if not transcript:
        return []
    if transcript.count("\n") + 1 < TAG_GENERATION_MIN_MESSAGES:
        return []

    payload = {
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": TAG_SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
        "max_tokens": 768,
        "temperature": 0.2,
    }
    text = _request_completion(payload)
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
