"""Reader document storage and metadata helpers."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_document(
    conn: sqlite3.Connection,
    title: str,
    source_type: str,
    source_path: str | None = None,
    saved_item_id: int | None = None,
) -> int:
    """Create a reader_documents row. Returns the document ID."""
    now = _now()
    cursor = conn.execute(
        """INSERT INTO reader_documents
           (title, source_type, source_path, saved_item_id, status, created_at)
           VALUES (?, ?, ?, ?, 'processing', ?)""",
        (title, source_type, source_path, saved_item_id, now),
    )
    conn.commit()
    return cursor.lastrowid


def update_document(conn: sqlite3.Connection, doc_id: int, **kwargs):
    """Update fields on a reader_documents row."""
    kwargs["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [doc_id]
    conn.execute(f"UPDATE reader_documents SET {sets} WHERE id = ?", vals)
    conn.commit()


def get_document(conn: sqlite3.Connection, doc_id: int) -> dict | None:
    row = conn.execute(
        """SELECT id, title, source_type, source_path, saved_item_id,
                  speech_file, original_md_file, chunk_count, status, error,
                  last_chunk, last_sentence, created_at, updated_at
           FROM reader_documents WHERE id = ?""",
        (doc_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "title": row[1], "source_type": row[2],
        "source_path": row[3], "saved_item_id": row[4],
        "speech_file": row[5], "original_md_file": row[6],
        "chunk_count": row[7], "status": row[8], "error": row[9],
        "last_chunk": row[10], "last_sentence": row[11],
        "created_at": row[12], "updated_at": row[13],
    }


def list_documents(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT id, title, source_type, chunk_count, status, error, created_at
           FROM reader_documents
           ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {"id": r[0], "title": r[1], "source_type": r[2],
         "chunk_count": r[3], "status": r[4], "error": r[5], "created_at": r[6]}
        for r in rows
    ]


def delete_document(conn: sqlite3.Connection, doc_id: int) -> bool:
    doc = get_document(conn, doc_id)
    if not doc:
        return False
    if doc["speech_file"]:
        path = Path(doc["speech_file"])
        if path.exists():
            path.unlink()
    conn.execute("DELETE FROM reader_documents WHERE id = ?", (doc_id,))
    conn.commit()
    return True


def fail_stale_processing_documents(
    conn: sqlite3.Connection,
    error_message: str = "Document processing was interrupted before completion.",
) -> int:
    """Mark orphaned processing rows as failed on startup."""
    cursor = conn.execute(
        """UPDATE reader_documents
           SET status = 'failed', error = ?, updated_at = ?
           WHERE status = 'processing'""",
        (error_message, _now()),
    )
    conn.commit()
    return cursor.rowcount


def load_speech_data(doc: dict) -> dict | None:
    """Load the speech JSON file for a document."""
    speech_file = doc.get("speech_file")
    if not speech_file:
        return None
    path = Path(speech_file)
    if not path.exists():
        return None
    return json.loads(path.read_text())
