from __future__ import annotations

from datetime import datetime, timezone


def _format_age(iso_ts: str | None) -> str:
    if not iso_ts:
        return "?"
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return "?"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def search_conversation_history(args: dict, session=None, _mcp_manager=None) -> str:
    from history_store import search_conversations

    query = (args.get("query") or "").strip()
    if not query:
        return "Error: query is required."

    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    limit = max(1, min(int(args.get("limit", 5)), 20))

    results = search_conversations(conn, query, service="octavius", limit=limit)
    current_conv_id = getattr(session, "conv_id", None) if session else None
    if current_conv_id is not None:
        results = [r for r in results if r.get("conversation_id") != current_conv_id]

    if not results:
        return (
            f"No prior conversations matched '{query}'. "
            "(Retrieval-only chats are intentionally not indexed.)"
        )

    lines = [f"Prior conversations matching '{query}', showing {len(results)}:"]
    for r in results:
        age = _format_age(r.get("started_at"))
        tags = r.get("tags") or []
        tag_suffix = f" [{', '.join(tags)}]" if tags else ""
        summary = r.get("summary") or "(no summary)"
        lines.append(f"#{r['conversation_id']} ({age}){tag_suffix} {summary}")
    return "\n".join(lines)
