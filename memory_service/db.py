"""Connection + schema init for the memory service's OWN database.

Reuses Octavius's `db.connect` (WAL + foreign_keys + sqlite-vec load) but against
a separate DB file. Override the path with OCTAVIUS_MEMORY_DB_PATH.
"""

import os
from pathlib import Path

from db import connect as _connect

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

DEFAULT_SERVICE_DB_PATH = Path(
    os.environ.get("OCTAVIUS_MEMORY_DB_PATH",
                   "/media/extra_stuff/octavius/memory_service.db")
)


def connect(db_path: Path | None = None):
    return _connect(db_path or DEFAULT_SERVICE_DB_PATH)


def init_db(db_path: Path | None = None) -> None:
    """Create the service schema if absent. Closes its own setup connection;
    request handlers each open their own short-lived connection."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
    finally:
        conn.close()
