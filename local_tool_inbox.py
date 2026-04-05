from __future__ import annotations


def save_to_inbox(args: dict, session=None) -> str:
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
    return f"Saved to inbox (item #{item_id}): {title}"


def read_item_content(args: dict, session=None) -> str:
    from history import get_saved_item

    item_id = args.get("item_id")
    if not item_id:
        return "Error: item_id is required."

    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    item = get_saved_item(conn, item_id)
    if not item:
        return f"Error: inbox item {item_id} not found."

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
