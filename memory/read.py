"""Read path: per-turn fact retrieval + the maintained profile document.

All retrieval filters to LIVE (valid_until IS NULL) and trust_tier != 'untrusted'.
The profile string returned here is what gets folded into messages[0] (see Step 4).
"""

import sqlite3

from . import config, store


def _row_to_fact(row) -> dict:
    return {
        "id": row[0], "subject": row[1], "predicate": row[2], "object": row[3],
        "object_is_entity": bool(row[4]), "confidence": row[5], "trust_tier": row[6],
    }


def retrieve_facts(conn: sqlite3.Connection, query_text: str, *, embed_fn,
                   k: int = config.RETRIEVAL_K,
                   threshold: float = config.RETRIEVAL_DISTANCE,
                   exclude_ids: set[int] | None = None) -> list[dict]:
    """KNN over live, trusted facts for the current user turn. Empty on embed failure."""
    if not query_text or not query_text.strip():
        return []
    qbytes = embed_fn(query_text)
    if qbytes is None:
        return []
    rows = conn.execute(
        """SELECT mf.id, mf.subject, mf.predicate, mf.object, mf.object_is_entity,
                  mf.confidence, mf.trust_tier,
                  vec_distance_cosine(fe.embedding, ?) AS distance
           FROM memory_facts mf
           JOIN fact_embeddings fe ON mf.id = fe.fact_id
           WHERE mf.valid_until IS NULL AND mf.trust_tier != 'untrusted'
           ORDER BY distance ASC
           LIMIT ?""",
        (qbytes, max(k * 3, k)),
    ).fetchall()
    exclude_ids = exclude_ids or set()
    out = []
    for r in rows:
        if r[7] is None or r[7] > threshold:
            continue
        if r[0] in exclude_ids:
            continue
        out.append(_row_to_fact(r))
        if len(out) >= k:
            break
    return out


def live_facts(conn: sqlite3.Connection, *, confidence_floor: float = 0.0,
               subject: str | None = None) -> list[dict]:
    sql = ("""SELECT id, subject, predicate, object, object_is_entity, confidence, trust_tier
              FROM memory_facts
              WHERE valid_until IS NULL AND trust_tier != 'untrusted' AND confidence >= ?""")
    params: list = [confidence_floor]
    if subject:
        sql += " AND subject = ?"
        params.append(subject)
    sql += " ORDER BY subject, confidence DESC"
    return [_row_to_fact(r) for r in conn.execute(sql, params).fetchall()]


def _humanize_predicate(pred: str) -> str:
    return pred.replace("_", " ")


def format_fact_line(fact: dict) -> str:
    return f"{fact['subject']} {_humanize_predicate(fact['predicate'])} {fact['object']}"


def render_identity_block(conn: sqlite3.Connection, *,
                          confidence_floor: float = config.PROFILE_CONFIDENCE_FLOOR) -> str:
    """Block 1 — deterministic, rebuilt from live facts at injection time.

    Grouped per subject: 'Dave: lives in Peterborough; researches multitaper; uses uv.'
    """
    facts = live_facts(conn, confidence_floor=confidence_floor)
    if not facts:
        return ""
    by_subject: dict[str, list[str]] = {}
    for f in facts:
        phrase = f"{_humanize_predicate(f['predicate'])} {f['object']}"
        by_subject.setdefault(f["subject"], []).append(phrase)
    lines = []
    for subject, phrases in by_subject.items():
        lines.append(f"- {subject}: " + "; ".join(phrases) + ".")
    return "\n".join(lines)


def facts_by_ids(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT id, subject, predicate, object, object_is_entity, confidence, trust_tier
            FROM memory_facts WHERE id IN ({placeholders})""",
        ids,
    ).fetchall()
    return [_row_to_fact(r) for r in rows]


def get_profile_themes(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT content FROM memory_profile WHERE id = 1"
    ).fetchone()
    return row[0] if row and row[0] else None


def render_profile(conn: sqlite3.Connection, *,
                   confidence_floor: float = config.PROFILE_CONFIDENCE_FLOOR) -> str:
    """The full always-on memory block for messages[0]: identity (live) + themes (rollup)."""
    identity = render_identity_block(conn, confidence_floor=confidence_floor)
    themes = get_profile_themes(conn)
    if not identity and not themes:
        return ""
    sections = ["## What you know about the user (long-term memory)"]
    if identity:
        sections.append(identity)
    if themes:
        sections.append("Recent themes: " + themes)
    return "\n".join(sections)
