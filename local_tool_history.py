from __future__ import annotations

from datetime import datetime, timezone

# Local tool results bypass the MCP 4000-char truncator, so read_conversation
# enforces its own per-page budget to protect the context window.
PAGE_CHAR_BUDGET = 3500
# A single turn can dwarf the page budget (e.g. a matrix turn with inlined
# PDF markdown), so individual messages are capped too.
MESSAGE_CHAR_CAP = 1500

_SOURCES = ("voice", "matrix", "text")


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


def _format_local(iso_ts: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render a stored UTC ISO timestamp in the server's local timezone."""
    if not iso_ts:
        return "?"
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return "?"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone().strftime(fmt)


def _normalize_since(raw: str) -> str | None:
    """Turn a user-facing date/datetime into a UTC ISO lower bound.

    A bare date ("2026-07-20") means local midnight, so 'today' works the
    way Dave means it rather than shifting by the UTC offset.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.astimezone()
    return ts.astimezone(timezone.utc).isoformat()


def _format_conversation_lines(results: list[dict]) -> list[str]:
    lines = []
    for r in results:
        started = _format_local(r.get("started_at"))
        age = _format_age(r.get("started_at"))
        source = r.get("source") or "?"
        tags = r.get("tags") or []
        tag_suffix = f" [{', '.join(tags)}]" if tags else ""
        summary = r.get("summary") or "(no summary)"
        lines.append(
            f"#{r['conversation_id']} ({source}, {started}, {age}){tag_suffix} {summary}"
        )
    return lines


def search_conversation_history(args: dict, session=None, _mcp_manager=None) -> str:
    from history_store import list_conversations, search_conversations

    query = (args.get("query") or "").strip()
    source = (args.get("source") or "").strip() or None
    if source and source not in _SOURCES:
        return f"Error: source must be one of {', '.join(_SOURCES)}."
    since = None
    if args.get("since"):
        since = _normalize_since(str(args["since"]))
        if since is None:
            return "Error: since must be an ISO date like 2026-07-20."
    if not query and not (source or since):
        return "Error: provide a query, or a source/since filter to list recent conversations."

    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    limit = max(1, min(int(args.get("limit", 5)), 20))

    if query:
        results = search_conversations(
            conn, query, service="octavius", limit=limit, source=source, since=since
        )
    else:
        results = list_conversations(
            conn, service="octavius", source=source, since=since, limit=limit
        )
    current_conv_id = getattr(session, "conv_id", None) if session else None
    if current_conv_id is not None:
        results = [r for r in results if r.get("conversation_id") != current_conv_id]

    filter_bits = []
    if source:
        filter_bits.append(f"source={source}")
    if since:
        filter_bits.append(f"since {_format_local(since)}")
    filter_desc = f" ({', '.join(filter_bits)})" if filter_bits else ""

    if not results:
        if query:
            return (
                f"No prior conversations matched '{query}'{filter_desc}. "
                "(Retrieval-only chats are intentionally not indexed; "
                "try a source/since listing without a query.)"
            )
        return f"No prior conversations found{filter_desc}."

    if query:
        header = f"Prior conversations matching '{query}'{filter_desc}, showing {len(results)}:"
    else:
        header = f"Recent conversations{filter_desc}, showing {len(results)}:"
    lines = [header] + _format_conversation_lines(results)
    lines.append("Use read_conversation with a #id to pull in the full transcript.")
    return "\n".join(lines)


def read_conversation(args: dict, session=None, _mcp_manager=None) -> str:
    from history_store import get_conversation, get_conversation_messages

    try:
        conversation_id = int(args.get("conversation_id"))
    except (TypeError, ValueError):
        return "Error: conversation_id is required and must be an integer."
    page = max(1, int(args.get("page", 1)))

    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    current_conv_id = getattr(session, "conv_id", None) if session else None
    if current_conv_id is not None and conversation_id == current_conv_id:
        return "Error: that is the current conversation — its history is already in context."

    meta = get_conversation(conn, conversation_id)
    if meta is None:
        return f"Error: no conversation #{conversation_id} found."

    messages = get_conversation_messages(conn, conversation_id)
    turns = [m for m in messages if m.get("role") in ("user", "assistant")]
    if not turns:
        return f"Conversation #{conversation_id} has no user/assistant messages."

    formatted = []
    for m in turns:
        speaker = "Dave" if m["role"] == "user" else "Octavius"
        when = _format_local(m.get("created_at"), "%H:%M")
        content = (m.get("content") or "").strip()
        if len(content) > MESSAGE_CHAR_CAP:
            content = content[:MESSAGE_CHAR_CAP] + " … [message truncated]"
        formatted.append(f"[{when}] {speaker}: {content}")

    # Page 1 is the most recent window; higher pages walk back in time.
    # Chunks are built from the end so the newest turns always land on page 1.
    pages: list[tuple[int, int]] = []  # (start_idx, end_idx) inclusive
    end = len(formatted) - 1
    while end >= 0:
        size = 0
        start = end
        while start >= 0:
            entry_len = len(formatted[start]) + 1
            if size + entry_len > PAGE_CHAR_BUDGET and start != end:
                break
            size += entry_len
            start -= 1
        pages.append((start + 1, end))
        end = start
    total_pages = len(pages)
    if page > total_pages:
        return (
            f"Error: conversation #{conversation_id} has only {total_pages} "
            f"page(s) of transcript."
        )
    start, end = pages[page - 1]

    started = _format_local(meta.get("started_at"))
    source = meta.get("source") or "?"
    header = [
        f"Conversation #{conversation_id} ({source}) — started {started}, "
        f"{len(turns)} messages."
    ]
    if meta.get("summary"):
        header.append(f"Summary: {meta['summary']}")
    header.append(
        f"Showing messages {start + 1}-{end + 1} of {len(turns)} "
        f"(page {page} of {total_pages}; higher pages are earlier)."
    )
    body = formatted[start : end + 1]
    footer = []
    if page < total_pages:
        footer.append(
            f"Earlier messages available: call read_conversation with page={page + 1}."
        )
    return "\n".join(header + body + footer)
