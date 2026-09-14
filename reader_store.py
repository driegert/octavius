"""Reader document storage and metadata helpers."""

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from settings import settings

APPENDIX_DIRNAME = "appendices"


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


def list_documents(
    conn: sqlite3.Connection,
    limit: int = 50,
    status: str | None = None,
) -> list[dict]:
    sql = """SELECT id, title, source_type, chunk_count, status, error, created_at
             FROM reader_documents"""
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
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
    clear_appendices(doc_id)
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
    return json.loads(path.read_text(encoding="utf-8"))



def claim_document_for_processing(conn: sqlite3.Connection, doc_id: int) -> bool:
    """Atomically flip a document to `processing` unless it already is.

    A read-then-update lets two concurrent appends (or an append racing a
    retry) both pass the status check and launch competing background jobs;
    the conditional UPDATE makes exactly one of them win. Returns False when
    the row is missing or already processing.
    """
    cursor = conn.execute(
        """UPDATE reader_documents SET status = 'processing', error = NULL, updated_at = ?
           WHERE id = ? AND status != 'processing'""",
        (_now(), doc_id),
    )
    conn.commit()
    return cursor.rowcount == 1


# ---------------------------------------------------------------------------
# Appendices: text appended to a document after its original ingest.
#
# Kept as plain files under <reader_dir>/appendices/<doc_id>/ rather than a
# schema change: the directory is discoverable from the id alone, and the
# sequence number in the filename fixes the order. They exist so that a RETRY
# (which replays the original source — re-downloads the URL, re-reads the
# file) can fold the appended text back in instead of silently dropping it.
# A `REPLACED` marker in the directory records that the original source was
# discarded, so a retry must ignore it rather than resurrect it.
# ---------------------------------------------------------------------------

REPLACED_MARKER = "REPLACED"


def appendix_dir(doc_id: int) -> Path:
    return Path(settings.reader.directory) / APPENDIX_DIRNAME / str(doc_id)


def _seq_of(path: Path) -> int | None:
    prefix = path.stem.split("-", 1)[0]
    return int(prefix) if prefix.isdigit() else None


def _appendix_files(doc_id: int) -> list[Path]:
    directory = appendix_dir(doc_id)
    if not directory.is_dir():
        return []
    # Only NNN-*.md files are appendices; anything else (an editor swap file,
    # a stray note) is ignored rather than crashing every load for this doc.
    numbered = [(seq, p) for p in directory.glob("*.md") if (seq := _seq_of(p)) is not None]
    return [p for _, p in sorted(numbered)]


def save_appendix(doc_id: int, text: str, replace: bool = False) -> Path:
    """Persist one appended block and return its path. Order is the sequence
    number in the filename, so a later append always sorts after an earlier one.

    With `replace`, the new block becomes the document's only appendix and a
    REPLACED marker is left so retries skip the original source. The new file
    is written BEFORE the old ones are removed: a crash in between leaves
    extra (older) blocks, never a document with nothing behind it.
    """
    directory = appendix_dir(doc_id)
    directory.mkdir(parents=True, exist_ok=True)
    existing = _appendix_files(doc_id)
    seq = (_seq_of(existing[-1]) + 1) if existing else 1
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{seq:03d}-{stamp}.md"
    path.write_text(text, encoding="utf-8")
    if replace:
        for old in existing:
            old.unlink(missing_ok=True)
        (directory / REPLACED_MARKER).touch()
    return path


def is_replaced(doc_id: int) -> bool:
    return (appendix_dir(doc_id) / REPLACED_MARKER).exists()


def load_appendices(doc_id: int) -> list[str]:
    """Appended blocks in append order (empty when nothing was appended)."""
    return [p.read_text(encoding="utf-8") for p in _appendix_files(doc_id)]


def clear_appendices(doc_id: int) -> None:
    shutil.rmtree(appendix_dir(doc_id), ignore_errors=True)
