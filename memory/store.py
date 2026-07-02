"""Low-level memory persistence primitives.

Every function takes an open sqlite3 connection (the same conversation-history DB).
No LLM, no network here — that lives in extract/synthesis. Embeddings are injected
as an ``embed_fn(text) -> bytes | None`` so the module unit-tests offline.
"""

import sqlite3
from datetime import datetime, timezone

from . import config


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- Entities & predicates ---------------------------------------------------

def resolve_entity(conn: sqlite3.Connection, name: str) -> str:
    """Map an alias to its canonical string; otherwise return the trimmed name."""
    if name is None:
        return name
    name = name.strip()
    row = conn.execute(
        "SELECT canonical FROM entity_aliases WHERE alias = ?", (name,)
    ).fetchone()
    return row[0] if row else name


def add_alias(conn: sqlite3.Connection, alias: str, canonical: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO entity_aliases (alias, canonical) VALUES (?, ?)",
        (alias.strip(), canonical.strip()),
    )
    conn.commit()


def register_predicate(conn: sqlite3.Connection, name: str,
                       cardinality: str = "multi",
                       description: str | None = None) -> str:
    """Ensure a predicate exists (FK target). Novel predicates default to 'multi'
    (additive, never clobbers). Returns the effective cardinality."""
    conn.execute(
        "INSERT OR IGNORE INTO predicates (name, cardinality, description) VALUES (?, ?, ?)",
        (name, cardinality, description),
    )
    row = conn.execute(
        "SELECT cardinality FROM predicates WHERE name = ?", (name,)
    ).fetchone()
    return row[0] if row else cardinality


def predicate_cardinality(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT cardinality FROM predicates WHERE name = ?", (name,)
    ).fetchone()
    return row[0] if row else None


def predicate_registry(conn: sqlite3.Connection) -> list[dict]:
    return [
        {"name": r[0], "cardinality": r[1], "description": r[2]}
        for r in conn.execute(
            "SELECT name, cardinality, description FROM predicates ORDER BY name"
        )
    ]


def known_entities(conn: sqlite3.Connection, limit: int = 200) -> list[str]:
    """Canonical subjects/entity-objects currently in live facts, for extractor reuse."""
    rows = conn.execute(
        """SELECT subject AS e FROM memory_facts WHERE valid_until IS NULL
           UNION
           SELECT object AS e FROM memory_facts
           WHERE valid_until IS NULL AND object_is_entity = 1
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [r[0] for r in rows]


# --- Facts -------------------------------------------------------------------

def _embed_text(subject: str, predicate: str, obj: str) -> str:
    return f"{subject} {predicate} {obj}"


def store_fact_embedding(conn: sqlite3.Connection, fact_id: int,
                         subject: str, predicate: str, obj: str, embed_fn) -> None:
    emb = embed_fn(_embed_text(subject, predicate, obj))
    if emb is None:
        return
    conn.execute("DELETE FROM fact_embeddings WHERE fact_id = ?", (fact_id,))
    conn.execute(
        "INSERT INTO fact_embeddings (fact_id, embedding) VALUES (?, ?)",
        (fact_id, emb),
    )


def find_live_exact(conn: sqlite3.Connection, subject: str, predicate: str,
                    obj: str) -> int | None:
    row = conn.execute(
        """SELECT id FROM memory_facts
           WHERE subject = ? AND predicate = ? AND object = ? AND valid_until IS NULL
           LIMIT 1""",
        (subject, predicate, obj),
    ).fetchone()
    return row[0] if row else None


def find_live_for_subject_predicate(conn: sqlite3.Connection, subject: str,
                                    predicate: str) -> list[dict]:
    rows = conn.execute(
        """SELECT id, object FROM memory_facts
           WHERE subject = ? AND predicate = ? AND valid_until IS NULL""",
        (subject, predicate),
    ).fetchall()
    return [{"id": r[0], "object": r[1]} for r in rows]


def find_near_dup(conn: sqlite3.Connection, subject: str, predicate: str, obj: str,
                  embed_fn, threshold: float = config.NEAR_DUP_DISTANCE) -> int | None:
    """A LIVE fact with the SAME subject+predicate whose embedding is within
    `threshold` of this triple — i.e. literal-object drift ('uv' vs 'the uv tool')."""
    qbytes = embed_fn(_embed_text(subject, predicate, obj))
    if qbytes is None:
        return None
    row = conn.execute(
        """SELECT mf.id, vec_distance_cosine(fe.embedding, ?) AS distance
           FROM memory_facts mf
           JOIN fact_embeddings fe ON mf.id = fe.fact_id
           WHERE mf.valid_until IS NULL AND mf.subject = ? AND mf.predicate = ?
           ORDER BY distance ASC LIMIT 1""",
        (qbytes, subject, predicate),
    ).fetchone()
    if row and row[1] is not None and row[1] <= threshold:
        return row[0]
    return None


def find_tombstone(conn: sqlite3.Connection, subject: str, predicate: str,
                   obj: str) -> int | None:
    """A FORGOTTEN fact (valid_until set, superseded_by NULL) with this exact triple.
    Used to stop the re-extraction loop from resurrecting a deliberately forgotten fact."""
    row = conn.execute(
        """SELECT id FROM memory_facts
           WHERE subject = ? AND predicate = ? AND object = ?
             AND valid_until IS NOT NULL AND superseded_by IS NULL
           ORDER BY valid_until DESC LIMIT 1""",
        (subject, predicate, obj),
    ).fetchone()
    return row[0] if row else None


def insert_fact(conn: sqlite3.Connection, subject: str, predicate: str, obj: str,
                object_is_entity: bool, trust_tier: str, now: str) -> int:
    cur = conn.execute(
        """INSERT INTO memory_facts
           (subject, predicate, object, object_is_entity, trust_tier,
            valid_from, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (subject, predicate, obj, 1 if object_is_entity else 0, trust_tier, now, now),
    )
    return cur.lastrowid


def add_source(conn: sqlite3.Connection, fact_id: int, conversation_id: int,
               asserted_at: str) -> bool:
    """Record provenance. Idempotent (PK). Returns True if this was a NEW source conv."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO memory_fact_sources
           (fact_id, conversation_id, asserted_at) VALUES (?, ?, ?)""",
        (fact_id, conversation_id, asserted_at),
    )
    return cur.rowcount > 0


def source_count(conn: sqlite3.Connection, fact_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM memory_fact_sources WHERE fact_id = ?", (fact_id,)
    ).fetchone()[0]


def newest_source_at(conn: sqlite3.Connection, fact_id: int) -> str | None:
    row = conn.execute(
        "SELECT MAX(asserted_at) FROM memory_fact_sources WHERE fact_id = ?",
        (fact_id,),
    ).fetchone()
    return row[0] if row else None


def upgrade_tier(conn: sqlite3.Connection, fact_id: int, tier: str, now: str) -> None:
    """Promote a fact's trust tier (derived -> asserted) when the user states it."""
    conn.execute(
        "UPDATE memory_facts SET trust_tier = ?, updated_at = ? WHERE id = ?",
        (tier, now, fact_id),
    )


def supersede(conn: sqlite3.Connection, old_id: int, new_id: int | None,
              now: str) -> None:
    """Retire a fact. new_id set => superseded by a newer value; None => forgotten."""
    conn.execute(
        """UPDATE memory_facts
           SET valid_until = ?, superseded_by = ?, updated_at = ?
           WHERE id = ? AND valid_until IS NULL""",
        (now, new_id, now, old_id),
    )
    # A retired fact must not surface in retrieval/near-dup.
    conn.execute("DELETE FROM fact_embeddings WHERE fact_id = ?", (old_id,))


def recompute_confidence(conn: sqlite3.Connection, fact_id: int,
                         now: str | None = None) -> float:
    """confidence = tier_base + reinforcement(#distinct sources) - recency_penalty."""
    row = conn.execute(
        "SELECT trust_tier FROM memory_facts WHERE id = ?", (fact_id,)
    ).fetchone()
    if not row:
        return 0.0
    tier = row[0]
    base = config.TIER_BASE.get(tier, 0.5)
    n = source_count(conn, fact_id)
    reinforce = min(config.REINFORCE_CAP, config.REINFORCE_STEP * max(0, n - 1))

    penalty = 0.0
    newest = _parse_iso(newest_source_at(conn, fact_id))
    ref = _parse_iso(now) or datetime.now(timezone.utc)
    if newest is not None:
        age_days = max(0.0, (ref - newest).total_seconds() / 86400.0)
        decay = 0.5 ** (age_days / config.RECENCY_HALFLIFE_DAYS)
        penalty = config.RECENCY_MAX_PENALTY * (1.0 - decay)

    conf = base + reinforce - penalty
    conf = max(base * 0.5, min(0.99, conf))
    conn.execute(
        "UPDATE memory_facts SET confidence = ?, updated_at = ? WHERE id = ?",
        (conf, now or now_iso(), fact_id),
    )
    return conf


# --- Watermark ---------------------------------------------------------------

def get_watermark(conn: sqlite3.Connection, conversation_id: int) -> int:
    row = conn.execute(
        "SELECT last_extracted_message_id FROM conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    return (row[0] or 0) if row else 0


def set_watermark(conn: sqlite3.Connection, conversation_id: int,
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
