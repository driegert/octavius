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


def _content_snippet(content: str | None, length: int = 150) -> str:
    if not content:
        return ""
    flat = " ".join(content.split())
    if len(flat) <= length:
        return flat
    return flat[:length].rstrip() + "…"


def save_to_stash(args: dict, session=None, _mcp_manager=None) -> str:
    from history import save_item

    title = args.get("title", "")
    content = args.get("content", "")
    item_type = args.get("item_type", "note")
    if not title or not content:
        return "Error: title and content are required."

    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    conversation_id = session.conv_id if session else None
    item_id = save_item(
        conn=conn,
        item_type=item_type,
        title=title,
        content=content,
        conversation_id=conversation_id,
        source_url=args.get("source_url"),
        metadata=args.get("metadata"),
    )
    return f"Saved to stash (item #{item_id}): {title}"


def read_item_content(args: dict, session=None, _mcp_manager=None) -> str:
    from history import get_saved_item

    item_id = args.get("item_id")
    if not item_id:
        return "Error: item_id is required."

    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    item = get_saved_item(conn, item_id)
    if not item:
        return f"Error: stash item {item_id} not found."

    content = item.get("content", "")
    offset = args.get("offset", 0)
    limit = args.get("limit", 4000)
    chunk = content[offset:offset + limit]
    total_len = len(content)
    remaining = max(0, total_len - offset - limit)
    return (
        f"[Item #{item_id}: {total_len} chars total, showing {offset}-{offset + len(chunk)}]"
        f"\n\n{chunk}"
        + (
            f"\n\n[{remaining} more characters available — use offset={offset + limit} to continue]"
            if remaining > 0 else "\n\n[End of content]"
        )
    )


def list_stash_items(args: dict, session=None, _mcp_manager=None) -> str:
    from history_store import list_saved_items

    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    status = args.get("status", "pending")
    if status == "all":
        status = None
    item_type = args.get("item_type")
    limit = max(1, min(int(args.get("limit", 20)), 50))

    items = list_saved_items(conn, status=status, item_type=item_type, limit=limit)
    if not items:
        filters = []
        if status:
            filters.append(f"status={status}")
        if item_type:
            filters.append(f"type={item_type}")
        suffix = f" ({', '.join(filters)})" if filters else ""
        return f"No stash items found{suffix}."

    header_filters = []
    if status:
        header_filters.append(f"status={status}")
    if item_type:
        header_filters.append(f"type={item_type}")
    header_suffix = f" ({', '.join(header_filters)})" if header_filters else ""
    lines = [f"Stash items{header_suffix}, showing {len(items)}:"]
    for item in items:
        age = _format_age(item.get("created_at"))
        snippet = _content_snippet(item.get("content"))
        lines.append(
            f"#{item['id']} [{item['status']}/{item['item_type']}] ({age}) {item['title']}"
        )
        if snippet:
            lines.append(f"    {snippet}")
    return "\n".join(lines)
