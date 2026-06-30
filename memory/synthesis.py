"""Global synthesis — Block 2 of the profile (themes/direction rollup).

Full rebuild (never incremental: incremental compounds drift, the same reason we
skipped a real graph). Triggered by an event counter, not a scheduler: each salient
conversation close bumps source_count; crossing the threshold rebuilds on next close.
"""

import logging

from . import config, store

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "/no_think\n"
    "You maintain a short 'recent themes' note for an assistant's memory of one user. "
    "Given recent conversation summaries and frequent topic tags, write 2-4 sentences "
    "on what the user has been working on and where their focus is heading. "
    "Be concrete (name projects/topics). No preamble, no markdown, no lists."
)


def bump_source_count(conn) -> int:
    conn.execute(
        "UPDATE memory_profile SET source_count = source_count + 1 WHERE id = 1"
    )
    conn.commit()
    return conn.execute(
        "SELECT source_count FROM memory_profile WHERE id = 1"
    ).fetchone()[0]


def should_synthesize(conn, threshold: int = config.SYNTHESIS_THRESHOLD) -> bool:
    row = conn.execute(
        "SELECT source_count FROM memory_profile WHERE id = 1"
    ).fetchone()
    return bool(row) and row[0] >= threshold


def _recent_summaries(conn, limit: int) -> list[str]:
    rows = conn.execute(
        """SELECT summary FROM conversations
           WHERE summary IS NOT NULL AND summary != ''
           ORDER BY COALESCE(ended_at, started_at) DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [r[0] for r in rows]


def _top_tags(conn, limit: int = 15) -> list[str]:
    rows = conn.execute(
        """SELECT t.name, COUNT(*) c FROM tags t
           JOIN conversation_tags ct ON t.id = ct.tag_id
           GROUP BY t.name ORDER BY c DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [r[0] for r in rows]


def synthesize_profile(conn, *, complete_fn,
                       window: int = config.SYNTHESIS_WINDOW) -> str | None:
    """Rebuild Block 2 from recent summaries + top tags. Resets the counter."""
    summaries = _recent_summaries(conn, window)
    if not summaries:
        return None
    tags = _top_tags(conn)
    user_text = (
        "Frequent topics: " + (", ".join(tags) if tags else "(none)") + "\n\n"
        "Recent conversation summaries (newest first):\n"
        + "\n".join(f"- {s}" for s in summaries)
    )
    themes = complete_fn(SYSTEM_PROMPT, user_text)
    if themes:
        themes = themes.strip()
    conn.execute(
        """UPDATE memory_profile
           SET content = ?, generated_at = ?, source_count = 0 WHERE id = 1""",
        (themes or None, store.now_iso()),
    )
    conn.commit()
    return themes


def maybe_synthesize(conn, *, complete_fn,
                     threshold: int = config.SYNTHESIS_THRESHOLD) -> str | None:
    """Convenience: synthesize only if the counter has crossed the threshold."""
    if should_synthesize(conn, threshold):
        return synthesize_profile(conn, complete_fn=complete_fn)
    return None
