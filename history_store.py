import json
import sqlite3
from datetime import datetime, timezone

from history_enrichment import embed_text, store_embedding


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def search_messages_text(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict]:
    pattern = f"%{query}%"
    rows = conn.execute(
        """SELECT id, conversation_id, role, content, created_at, model
           FROM messages
           WHERE content LIKE ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (pattern, limit),
    ).fetchall()
    return [
        {
            "message_id": row[0], "conversation_id": row[1], "role": row[2],
            "content": row[3][:300], "created_at": row[4], "model": row[5],
        }
        for row in rows
    ]


def search_messages(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict]:
    query_bytes = embed_text(query)
    if query_bytes is None:
        return search_messages_text(conn, query, limit)

    rows = conn.execute(
        """SELECT m.id, m.conversation_id, m.role, m.content, m.created_at,
                  m.model, vec_distance_cosine(me.embedding, ?) as distance
           FROM messages m
           JOIN message_embeddings me ON m.id = me.message_id
           WHERE me.embedding IS NOT NULL
           ORDER BY distance ASC
           LIMIT ?""",
        (query_bytes, limit),
    ).fetchall()
    return [
        {
            "message_id": row[0], "conversation_id": row[1], "role": row[2],
            "content": row[3][:300], "created_at": row[4], "model": row[5],
            "distance": row[6],
        }
        for row in rows
    ]


def _conversation_tags(conn: sqlite3.Connection, conversation_id: int) -> list[str]:
    rows = conn.execute(
        """SELECT t.name FROM tags t
           JOIN conversation_tags ct ON t.id = ct.tag_id
           WHERE ct.conversation_id = ?""",
        (conversation_id,),
    ).fetchall()
    return [row[0] for row in rows]


def search_conversations(
    conn: sqlite3.Connection,
    query: str,
    service: str | None = None,
    limit: int = 10,
) -> list[dict]:
    query_bytes = embed_text(query)
    if query_bytes is None:
        pattern = f"%{query}%"
        sql = """SELECT id, session_id, started_at, ended_at, service, source,
                        summary, model, message_count
                 FROM conversations
                 WHERE summary LIKE ?"""
        params: list = [pattern]
        if service:
            sql += " AND service = ?"
            params.append(service)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    else:
        sql = """SELECT c.id, c.session_id, c.started_at, c.ended_at,
                        c.service, c.source, c.summary, c.model, c.message_count,
                        vec_distance_cosine(se.embedding, ?) as distance
                 FROM conversations c
                 JOIN summary_embeddings se ON c.id = se.conversation_id
                 WHERE se.embedding IS NOT NULL"""
        params = [query_bytes]
        if service:
            sql += " AND c.service = ?"
            params.append(service)
        sql += " ORDER BY distance ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()

    results = []
    for row in rows:
        item = {
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
        if len(row) > 9:
            item["distance"] = row[9]
        results.append(item)
    return results


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
    embed_text_value = f"{title}\n{content[:500]}"
    store_embedding(conn, "saved_item_embeddings", "saved_item_id", item_id, embed_text_value)
    return item_id


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
    query_bytes = embed_text(query)
    if query_bytes is None:
        pattern = f"%{query}%"
        rows = conn.execute(
            """SELECT id, conversation_id, item_type, title, content, source_url,
                      metadata, status, created_at
               FROM saved_items
               WHERE (title LIKE ? OR content LIKE ?) AND status != 'dismissed'
               ORDER BY created_at DESC LIMIT ?""",
            (pattern, pattern, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.id, s.conversation_id, s.item_type, s.title, s.content,
                      s.source_url, s.metadata, s.status, s.created_at,
                      vec_distance_cosine(e.embedding, ?) as distance
               FROM saved_items s
               JOIN saved_item_embeddings e ON s.id = e.saved_item_id
               WHERE s.status != 'dismissed'
               ORDER BY distance ASC LIMIT ?""",
            (query_bytes, limit),
        ).fetchall()
    results = []
    for row in rows:
        item = {
            "id": row[0],
            "conversation_id": row[1],
            "item_type": row[2],
            "title": row[3],
            "content": row[4][:200],
            "source_url": row[5],
            "metadata": json.loads(row[6]) if row[6] else None,
            "status": row[7],
            "created_at": row[8],
        }
        if len(row) > 9:
            item["distance"] = row[9]
        results.append(item)
    return results


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


def get_stats(conn: sqlite3.Connection) -> dict:
    total_convs = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    total_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    total_tool_calls = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    total_embeddings = conn.execute("SELECT COUNT(*) FROM message_embeddings").fetchone()[0]
    by_service = conn.execute("SELECT service, COUNT(*) FROM conversations GROUP BY service").fetchall()
    by_source = conn.execute("SELECT source, COUNT(*) FROM conversations GROUP BY source").fetchall()
    by_role = conn.execute("SELECT role, COUNT(*) FROM messages GROUP BY role").fetchall()
    top_tools = conn.execute(
        """SELECT tool_name, COUNT(*) as cnt FROM tool_calls
           GROUP BY tool_name ORDER BY cnt DESC LIMIT 10"""
    ).fetchall()
    top_tags = conn.execute(
        """SELECT t.name, COUNT(*) as cnt FROM tags t
           JOIN conversation_tags ct ON t.id = ct.tag_id
           GROUP BY t.name ORDER BY cnt DESC LIMIT 10"""
    ).fetchall()
    return {
        "total_conversations": total_convs,
        "total_messages": total_msgs,
        "total_tool_calls": total_tool_calls,
        "total_embeddings": total_embeddings,
        "embedding_coverage": f"{total_embeddings / total_msgs * 100:.1f}%" if total_msgs else "0%",
        "conversations_by_service": {row[0]: row[1] for row in by_service},
        "conversations_by_source": {row[0]: row[1] for row in by_source},
        "messages_by_role": {row[0]: row[1] for row in by_role},
        "top_tools": {row[0]: row[1] for row in top_tools},
        "top_tags": {row[0]: row[1] for row in top_tags},
    }
