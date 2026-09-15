"""Reads over the octavius history database.

Search moved off this app's own legacy vec0 tables (`message_embeddings`,
`summary_embeddings`, `saved_item_embeddings`) and onto the shared
`hybrid-corpus` library on 2026-09-15. The library owns sidecar tables in this
same file (`history_summaries_*`, `history_saved_items_*`) and
`history-index.timer` keeps them fresh every 15 minutes; this module only
queries them. The legacy tables were L2-metric over mixed-norm vectors, with no
lexical index at all — in-app recall was ranking by vector magnitude rather than
by meaning.

Everything else here still reads the app-owned tables directly, unchanged.
`server_history.py` in mcp-tools is the sibling implementation of the same
search over the same collections; the two are deliberately kept in step.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Matches list_saved_items' snippet length, so a searched row and a listed row
# render identically in the inbox UI. (The library's own shape clips at 300.)
SAVED_ITEM_SNIPPET = 200

CORPUS = "history"
SUMMARIES = "history_summaries"
SAVED_ITEMS = "history_saved_items"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# hybrid-corpus wiring
# --------------------------------------------------------------------------- #

_backend_cache: tuple[dict, dict] | None = None


def _backend() -> tuple[dict, dict]:
    """`(adapters, embedders)` for this app's two history collections.

    Resolved lazily and cached: importing this module must not require a
    readable `sites.toml` or a reachable embedding endpoint, because the test
    suite imports it with neither. Mirrors `server_history.py`'s module-level
    `Config.load()` / `get_corpus("history").build(...)` pair.
    """
    global _backend_cache
    if _backend_cache is None:
        from hybrid_corpus.config import Config
        from hybrid_corpus.registry import get as get_corpus

        config = Config.load()
        adapters = {
            a.collection: a
            for a in get_corpus(CORPUS).build(config)
            if a.collection in (SUMMARIES, SAVED_ITEMS)
        }
        embedders = {name: config.profile(name).client() for name in adapters}
        _backend_cache = (adapters, embedders)
    return _backend_cache


def set_search_backend(adapters: dict | None, embedders: dict | None = None) -> None:
    """Inject adapters/embedders; `set_search_backend(None)` restores the real
    ones. Tests use this with `hybrid_corpus.embed.FakeEmbedder` so the suite
    never reaches the network.
    """
    global _backend_cache
    _backend_cache = None if adapters is None else (adapters, embedders or {})


@contextmanager
def _library_conn(conn: sqlite3.Connection):
    """Present an octavius connection the way `hybrid_corpus.db.connect` would.

    The library's `hydrate()` does `dict(row)` and `row["unit_id"]`, and its
    `atomic()` drives transactions with an explicit `BEGIN IMMEDIATE`; both need
    the settings that `hybrid_corpus.db.connect` applies and `db.connect_db`
    does not. Set them for the duration of the call and put them back, rather
    than changing `db.connect` globally — every other query in this app indexes
    its rows positionally.
    """
    prev_factory = conn.row_factory
    prev_isolation = conn.isolation_level
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    try:
        yield conn
    finally:
        conn.row_factory = prev_factory
        conn.isolation_level = prev_isolation


def assert_history_serveable(conn: sqlite3.Connection, db_path) -> None:
    """Refuse to start if conversation search cannot actually work. Raises.

    Two failure modes this closes, both of which are otherwise silent — search
    keeps answering, just with stale or meaningless rankings:

    1. **Path drift.** `sites.toml [corpora.history] database` is the file
       `history-index.timer` keeps indexed; `OCTAVIUS_DB_PATH` is the file this
       app opens. Two sources of truth for one path is exactly the config-drift
       shape this migration exists to remove, so they are compared rather than
       assumed, and a mismatch is fatal here instead of being discovered as
       "search results stopped including anything recent".

    2. **Bad sidecars.** `assert_serveable` is the library's own pre-serve gate:
       the collection exists, its vec0 table's stored DDL declares
       `distance_metric=cosine` (the restored-backup tripwire — reading the DDL
       rather than a meta row is deliberate, since a hand-edited meta row can
       lie), and `hc_index_meta`'s model/dim agree with the running config.

    Called from `main.py`'s lifespan; `create_app(verify_search=...)` injects a
    no-op for tests that run against a throwaway database.
    """
    from pathlib import Path

    from hybrid_corpus.config import Config
    from hybrid_corpus.db import assert_serveable

    configured = Path(Config.load().site(CORPUS).database).expanduser().resolve()
    opened = Path(db_path).expanduser().resolve()
    if configured != opened:
        raise RuntimeError(
            f"history database path drift: octavius opens {opened}, but "
            f"sites.toml [corpora.history] database is {configured}. "
            f"history-index.timer indexes the latter, so in-app search would "
            f"serve a file nothing is indexing. Fix OCTAVIUS_DB_PATH or sites.toml."
        )

    adapters, embedders = _backend()
    with _library_conn(conn):
        for collection in (SUMMARIES, SAVED_ITEMS):
            embedder = embedders.get(collection)
            assert_serveable(
                conn, collection,
                model=getattr(embedder, "model", None),
                dim=getattr(embedder, "dim", None),
                hard_max=getattr(adapters[collection], "hard_max", None),
            )
    log.info("History search ready: %s, %s over %s",
             SUMMARIES, SAVED_ITEMS, opened)


def _hybrid_search(conn: sqlite3.Connection, collection: str, query: str,
                   limit: int, filter_spec) -> list[dict]:
    """One library search, shaped by the collection's adapter.

    `search()` owns the degradation contract: an embedder outage runs the
    lexical arm alone and comes back with `degraded=True` — it neither raises
    nor silently returns `[]` (both were live octavius behaviours before this
    change). If the embedder cannot even be constructed, we ask for lexical-only
    explicitly rather than letting a config/network error escape into a voice
    turn. A `NotServeable` refusal is deliberately NOT swallowed: that means the
    sidecars are missing or mis-built, which `assert_history_serveable` is
    supposed to have caught at startup.
    """
    from hybrid_corpus.search import hydrate, search

    adapters, embedders = _backend()
    adapter = adapters[collection]
    try:
        embedder = embedders[collection]
    except Exception:  # noqa: BLE001 - lexical-only beats failing the turn
        log.warning("%s: no embedder available; searching lexical-only", collection,
                    exc_info=True)
        embedder = None

    with _library_conn(conn):
        result = search(conn, collection, query, embedder,
                        limit=limit, filter_spec=filter_spec, collapse=True)
        if result.degraded:
            log.warning(
                "%s: embedding chain unavailable, returning lexical-only results "
                "for %r (%d hit(s))", collection, query[:80], len(result.hits),
            )
        return hydrate(conn, adapter, result.hits)


def _conversation_tags(conn: sqlite3.Connection, conversation_id: int) -> list[str]:
    rows = conn.execute(
        """SELECT t.name FROM tags t
           JOIN conversation_tags ct ON t.id = ct.tag_id
           WHERE ct.conversation_id = ?""",
        (conversation_id,),
    ).fetchall()
    return [row[0] for row in rows]


def _summaries_filter(service: str | None, source: str | None, since: str | None):
    """`service` via the adapter; `source`/`since` AND-ed on as extra clauses.

    The shared adapter models only `service` (the MCP shim has no source/since
    parameters), and its `filters()` raises `TypeError` on an unknown kwarg
    precisely so a mistyped filter cannot widen a search — so these two
    octavius-only filters are composed here instead of being passed in. They use
    the same `u` alias `FilterSpec` requires, and every caller-supplied value
    goes through `params`, never into the SQL fragment.
    """
    from hybrid_corpus.adapter import FilterSpec

    adapters, _ = _backend()
    base = adapters[SUMMARIES].filters(service=service)
    clauses: list[str] = []
    params: list = []
    if base is not None:
        clauses.append(base.sql)
        params.extend(base.params)
    if source:
        clauses.append(
            "u.source_id IN (SELECT CAST(id AS TEXT) FROM conversations WHERE source = ?)"
        )
        params.append(source)
    if since:
        clauses.append(
            "u.source_id IN (SELECT CAST(id AS TEXT) FROM conversations"
            " WHERE started_at >= ?)"
        )
        params.append(since)
    if not clauses:
        return None
    return FilterSpec(
        sql=" AND ".join(clauses), params=tuple(params),
        description=f"service={service} source={source} since={since}",
    )


def search_conversations(
    conn: sqlite3.Connection,
    query: str,
    service: str | None = None,
    limit: int = 10,
    source: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Hybrid search over conversation summaries (`history_summaries`).

    Same signature and same result keys as the legacy summary-embedding
    version — `conversation_id`, `session_id` (8 chars), `started_at`,
    `ended_at`, `service`, `source`, `summary`, `model`, `message_count`,
    `tags`, and `distance` — plus the library's additive `score`/`unit_id` and
    the conversation's token/duration totals.

    One behaviour note for `agent.py`'s recall cutoff: results now also arrive
    from a BM25 arm, and a hit found by that arm alone carries no `distance`.
    Both callers already treat a missing `distance` as "keep", which is the
    right answer — there was no lexical arm at all before.
    """
    return _hybrid_search(conn, SUMMARIES, query, limit,
                          _summaries_filter(service, source, since))


def list_conversations(
    conn: sqlite3.Connection,
    service: str | None = None,
    source: str | None = None,
    since: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Recency-ordered conversation listing — no semantic query needed.

    Unlike search_conversations, this also surfaces conversations whose
    summaries were never embedded (index=False retrieval-only chats).
    Empty conversations are skipped — every WS connect leaves a
    message_count=0 row behind when the client attaches or never speaks.
    """
    sql = """SELECT id, session_id, started_at, ended_at, service, source,
                    summary, model, message_count
             FROM conversations WHERE message_count > 0"""
    params: list = []
    if service:
        sql += " AND service = ?"
        params.append(service)
    if source:
        sql += " AND source = ?"
        params.append(source)
    if since:
        sql += " AND started_at >= ?"
        params.append(since)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "conversation_id": row[0],
            "session_id": row[1][:8],
            "started_at": row[2],
            "ended_at": row[3],
            "service": row[4],
            "source": row[5],
            "summary": row[6],
            "model": row[7],
            "message_count": row[8],
            "tags": _conversation_tags(conn, row[0]),
        }
        for row in rows
    ]


def get_conversation(conn: sqlite3.Connection, conversation_id: int) -> dict | None:
    row = conn.execute(
        """SELECT id, session_id, started_at, ended_at, service, source,
                  summary, model, message_count
           FROM conversations WHERE id = ?""",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "conversation_id": row[0],
        "session_id": row[1][:8],
        "started_at": row[2],
        "ended_at": row[3],
        "service": row[4],
        "source": row[5],
        "summary": row[6],
        "model": row[7],
        "message_count": row[8],
    }


def get_conversation_messages(conn: sqlite3.Connection, conversation_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, role, content, created_at, model, input_tokens,
                  output_tokens, latency_ms, stt_model, stt_confidence,
                  audio_duration_ms, tts_model, error
           FROM messages
           WHERE conversation_id = ?
           ORDER BY created_at""",
        (conversation_id,),
    ).fetchall()
    messages = []
    for row in rows:
        message = {
            "message_id": row[0],
            "role": row[1],
            "content": row[2],
            "created_at": row[3],
            "model": row[4],
            "input_tokens": row[5],
            "output_tokens": row[6],
            "latency_ms": row[7],
        }
        if row[8]:
            message["stt_model"] = row[8]
        if row[9] is not None:
            message["stt_confidence"] = row[9]
        if row[10]:
            message["audio_duration_ms"] = row[10]
        if row[11]:
            message["tts_model"] = row[11]
        if row[12]:
            message["error"] = row[12]

        tool_rows = conn.execute(
            """SELECT tool_name, server_name, arguments, status,
                      result_summary, result_size, duration_ms
               FROM tool_calls WHERE message_id = ?""",
            (row[0],),
        ).fetchall()
        if tool_rows:
            message["tool_calls"] = [
                {
                    "tool_name": tool_row[0],
                    "server_name": tool_row[1],
                    "arguments": tool_row[2],
                    "status": tool_row[3],
                    "result_summary": tool_row[4],
                    "result_size": tool_row[5],
                    "duration_ms": tool_row[6],
                }
                for tool_row in tool_rows
            ]

        attachment_rows = conn.execute(
            """SELECT type, reference, title
               FROM attachments WHERE message_id = ?""",
            (row[0],),
        ).fetchall()
        if attachment_rows:
            message["attachments"] = [
                {
                    "type": att_row[0],
                    "reference": att_row[1],
                    "title": att_row[2],
                }
                for att_row in attachment_rows
            ]

        messages.append(message)
    return messages


def save_item(
    conn: sqlite3.Connection,
    item_type: str,
    title: str,
    content: str,
    conversation_id: int | None = None,
    source_url: str | None = None,
    metadata: dict | None = None,
) -> int:
    now = now_iso()
    metadata_json = json.dumps(metadata) if metadata else None
    cursor = conn.execute(
        """INSERT INTO saved_items
           (conversation_id, item_type, title, content, source_url, metadata, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (conversation_id, item_type, title, content, source_url, metadata_json, now),
    )
    conn.commit()
    item_id = cursor.lastrowid
    index_saved_item(conn, item_id)
    return item_id


def index_saved_item(conn: sqlite3.Connection, item_id: int) -> bool:
    """Index a just-written `saved_items` row through the library's `Indexer`.

    Same shape as `server_history.py`'s `save_to_inbox`: the app owns the row
    and has already committed it, so an indexing failure must never turn into a
    failed save. The row is keyword-searchable immediately — the FTS entry is
    written in the same transaction as the unit — and only the vector is owed,
    which the next `hybrid-corpus run history` (history-index.timer, 15 min)
    collects. Returns whether the vector landed.

    This replaces the old `saved_item_embeddings` write, which embedded only
    `title + content[:500]`; the adapter chunks the whole item.
    """
    try:
        from hybrid_corpus.index import Indexer

        adapters, embedders = _backend()
        with _library_conn(conn):
            indexer = Indexer(conn, adapters[SAVED_ITEMS], embedders[SAVED_ITEMS])
            indexer.ensure()  # idempotent; creates the sidecars on first use
            written = indexer.index_source(str(item_id))
        if written.pending:
            log.warning(
                "Saved item %s stored; its embedding is pending and will be "
                "collected by history-index.timer", item_id,
            )
        return not written.pending
    except Exception:  # noqa: BLE001 - the item saved; indexing is the recoverable part
        log.warning("Saved item %s stored but could not be indexed", item_id,
                    exc_info=True)
        return False


def list_saved_items(
    conn: sqlite3.Connection,
    status: str | None = None,
    item_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    sql = """SELECT id, conversation_id, item_type, title, content, source_url,
                    metadata, status, created_at, updated_at
             FROM saved_items WHERE 1=1"""
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if item_type:
        sql += " AND item_type = ?"
        params.append(item_type)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": row[0],
            "conversation_id": row[1],
            "item_type": row[2],
            "title": row[3],
            "content": row[4][:200],
            "source_url": row[5],
            "metadata": json.loads(row[6]) if row[6] else None,
            "status": row[7],
            "created_at": row[8],
            "updated_at": row[9],
        }
        for row in rows
    ]


def search_saved_items(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict]:
    """Hybrid search over saved inbox items (`history_saved_items`).

    `filters()` is always passed even with no arguments: for this collection it
    never returns `None`, because "no status given" means "everything except
    dismissed" — the same default the legacy `status != 'dismissed'` clause had.
    Dropping it would start surfacing dismissed items.
    """
    adapters, _ = _backend()
    items = _hybrid_search(conn, SAVED_ITEMS, query, limit,
                           adapters[SAVED_ITEMS].filters())
    _restore_octavius_item_shape(conn, items)
    return items


def _restore_octavius_item_shape(conn: sqlite3.Connection, items: list[dict]) -> None:
    """Put back the two things the shared shape does not carry.

    `shape_hit` is written for the MCP `search_inbox` tool, which has no
    `conversation_id`; octavius's inbox rows always had one, so it is re-read
    here rather than quietly dropped from the API response. `content` is also
    re-clipped to this app's snippet length — note it is the *matching chunk*
    now, not `content[:200]` from position 0, which is the point of the move.
    """
    ids = [it["id"] for it in items if it.get("id") is not None]
    mapping: dict = {}
    if ids:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, conversation_id FROM saved_items WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        mapping = {row[0]: row[1] for row in rows}
    for item in items:
        item["conversation_id"] = mapping.get(item.get("id"))
        if isinstance(item.get("content"), str):
            item["content"] = item["content"][:SAVED_ITEM_SNIPPET]


def get_saved_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute(
        """SELECT id, conversation_id, item_type, title, content, source_url,
                  metadata, status, created_at, updated_at
           FROM saved_items WHERE id = ?""",
        (item_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "conversation_id": row[1],
        "item_type": row[2],
        "title": row[3],
        "content": row[4],
        "source_url": row[5],
        "metadata": json.loads(row[6]) if row[6] else None,
        "status": row[7],
        "created_at": row[8],
        "updated_at": row[9],
    }


def update_saved_item_status(conn: sqlite3.Connection, item_id: int, status: str) -> bool:
    cursor = conn.execute(
        "UPDATE saved_items SET status = ?, updated_at = ? WHERE id = ?",
        (status, now_iso(), item_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def set_item_chat_conversation(conn: sqlite3.Connection, item_id: int, conversation_id: int):
    conn.execute(
        "UPDATE saved_items SET chat_conversation_id = ? WHERE id = ?",
        (conversation_id, item_id),
    )
    conn.commit()


def get_item_chat_conversation_id(conn: sqlite3.Connection, item_id: int) -> int | None:
    row = conn.execute(
        "SELECT chat_conversation_id FROM saved_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    return row[0] if row and row[0] else None


def get_memory_watermark(conn: sqlite3.Connection, conversation_id: int) -> int:
    """Highest message id already pushed to the memory service for this
    conversation (``conversations.last_extracted_message_id``). The watermark
    is Octavius-owned — the memory service never sees message ids.

    These helpers lived in ``memory.store`` until the memory service was
    extracted to the agent-memory repo; they operate purely on Octavius's own
    tables, so they belong here.
    """
    row = conn.execute(
        "SELECT last_extracted_message_id FROM conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    return (row[0] or 0) if row else 0


def set_memory_watermark(conn: sqlite3.Connection, conversation_id: int,
                         message_id: int) -> None:
    conn.execute(
        "UPDATE conversations SET last_extracted_message_id = ? WHERE id = ?",
        (message_id, conversation_id),
    )


def messages_after_watermark(conn: sqlite3.Connection, conversation_id: int,
                             watermark: int) -> tuple[list[dict], int]:
    """User+assistant turns with id > watermark. Returns (messages, max_id_seen).

    TRUST BOUNDARY: role is restricted to user/assistant here — tool results
    (untrusted email/web/file bodies) are never handed to fact extraction.
    """
    rows = conn.execute(
        """SELECT id, role, content FROM messages
           WHERE conversation_id = ? AND id > ?
             AND role IN ('user', 'assistant')
           ORDER BY id ASC""",
        (conversation_id, watermark),
    ).fetchall()
    msgs = [{"id": r[0], "role": r[1], "content": r[2]} for r in rows]
    max_id = max((r[0] for r in rows), default=watermark)
    return msgs, max_id
