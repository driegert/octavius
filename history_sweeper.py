"""Background repair for embeddings that never landed.

Message embeds are detached from the turn path (see
``history_enrichment.spawn_embedding``), so an embedder outage, a process
restart, or a full backlog leaves rows with no vector. Nothing else notices:
semantic search just quietly returns less. This module finds those rows and
re-embeds them.

The pending marker is the *absence* of a row in the vec0 table — no extra
bookkeeping table, and ``store_embedding_bytes`` is DELETE-then-INSERT, so a
sweep that overlaps a live embed converges on the same vector rather than
conflicting.

Lives in its own module rather than in ``history_enrichment``: that one is
content generation imported by both ``history`` and ``history_store``, and a
lifespan-aware forever-loop there would risk an import cycle.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db import connect_db
from history_enrichment import embed_text_async, store_embedding_bytes

log = logging.getLogger(__name__)

MESSAGE_BATCH_LIMIT = 50
SUMMARY_BATCH_LIMIT = 25
# Comfortably longer than a live embed's worst case (attempts x endpoints x
# timeout), so the sweeper doesn't race one that is still in flight.
MIN_AGE_SECONDS = 120
PAUSE_BETWEEN_ROWS = 0.2
SWEEP_INTERVAL_SECONDS = 900
INITIAL_DELAY_SECONDS = 30


def _cutoff(min_age_seconds: int) -> str:
    """An ISO-8601 UTC cutoff in exactly the format history._now() writes.

    Must match: the comparison against messages.created_at is lexical. SQLite's
    own datetime('now') is space-separated and would not order correctly against
    the stored 'T'-separated values.
    """
    return (datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds)).isoformat()


def find_unembedded_messages(conn, *, limit=MESSAGE_BATCH_LIMIT, min_age_seconds=MIN_AGE_SECONDS):
    """Messages that should have a vector and don't.

    The role filter is load-bearing, not cosmetic: tool results are recorded as
    role='tool' and are deliberately never embedded, so without it every pass
    would re-select the entire tool-call history and never converge.
    """
    rows = conn.execute(
        """
        SELECT id, content FROM messages
         WHERE role IN ('user', 'assistant')
           AND content <> ''
           AND created_at < ?
           AND id NOT IN (SELECT message_id FROM message_embeddings)
         ORDER BY id DESC
         LIMIT ?
        """,
        (_cutoff(min_age_seconds), limit),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def find_unembedded_summaries(conn, *, limit=SUMMARY_BATCH_LIMIT):
    """Summaries the summariser marked worth indexing that have no vector.

    ``indexed IS NULL`` means legacy/unknown — those predate the flag and are
    left alone, because a conversation the summariser deliberately skipped is
    indistinguishable from one whose embed failed.
    """
    rows = conn.execute(
        """
        SELECT id, summary FROM conversations
         WHERE indexed = 1
           AND summary IS NOT NULL
           AND summary <> ''
           AND id NOT IN (SELECT conversation_id FROM summary_embeddings)
         ORDER BY id DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def _summary_unchanged(conn, conv_id: int, text: str) -> bool:
    """Is this summary still the one we embedded, and still meant to be indexed?"""
    row = conn.execute(
        "SELECT summary, indexed FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    return bool(row) and row[0] == text and row[1] == 1


async def _embed_rows(conn, rows, table: str, id_col: str, guard=None) -> tuple[int, bool]:
    """Returns (repaired, aborted). Aborts the pass on the first hard failure.

    `guard` is re-checked *after* the embed, immediately before the write. The
    embed is an await of network latency, and a conversation can be summarised
    again during it (every Matrix thread is resumed in place). Writing back
    unconditionally would insert a vector for superseded text — and because the
    pending marker is the absence of a row, that vector would then look complete
    forever, which is the exact staleness this sweeper exists to repair.
    """
    repaired = 0
    for row_id, text in rows:
        emb = await embed_text_async(text)
        if emb is None:
            # None means the whole chain failed. Grinding through the rest of the
            # batch would just burn the timeout budget per row against dead hosts.
            log.warning("Embedding unavailable; aborting sweep after %d %s row(s)", repaired, table)
            return repaired, True
        if guard is not None and not guard(conn, row_id, text):
            log.info("Skipping %s row %d: it changed while we were embedding it", table, row_id)
            continue
        if store_embedding_bytes(conn, table, id_col, row_id, emb):
            repaired += 1
        await asyncio.sleep(PAUSE_BETWEEN_ROWS)
    return repaired, False


async def sweep_once(db_path) -> dict:
    """One pass over both backlogs. Never raises."""
    result = {"messages": 0, "summaries": 0, "aborted": False}
    try:
        with connect_db(Path(db_path)) as conn:
            messages = find_unembedded_messages(conn)
            summaries = find_unembedded_summaries(conn)
            if not messages and not summaries:
                return result
            log.info(
                "Embedding sweep: %d message(s), %d summary(ies) pending",
                len(messages), len(summaries),
            )
            result["messages"], aborted = await _embed_rows(
                conn, messages, "message_embeddings", "message_id"
            )
            if aborted:
                result["aborted"] = True
                return result
            result["summaries"], aborted = await _embed_rows(
                conn, summaries, "summary_embeddings", "conversation_id",
                guard=_summary_unchanged,
            )
            result["aborted"] = aborted
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Embedding sweep failed")
    return result


async def run_sweeper(
    db_path,
    *,
    interval: float = SWEEP_INTERVAL_SECONDS,
    initial_delay: float = INITIAL_DELAY_SECONDS,
) -> None:
    """Sweep forever. Cancelled on shutdown; one bad pass never kills the loop."""
    try:
        await asyncio.sleep(initial_delay)
        while True:
            result = await sweep_once(db_path)
            if result["messages"] or result["summaries"]:
                log.info(
                    "Embedding sweep repaired %d message(s), %d summary(ies)%s",
                    result["messages"],
                    result["summaries"],
                    " (aborted early)" if result["aborted"] else "",
                )
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        log.info("Embedding sweeper stopped")
        raise
