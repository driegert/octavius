import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

try:
    import sqlite_vec
except ModuleNotFoundError:  # pragma: no cover - optional in lightweight test environments
    sqlite_vec = None


# The history/memory DB can grow large once cross-harness aggregation is on, so its
# location is overridable. Point OCTAVIUS_DB_PATH at a roomy local ext4 volume (e.g.
# /media/extra_stuff/octavius/octavius_history.db). Must be a POSIX/ext4 mount — SQLite
# WAL corrupts on NTFS/exFAT/network filesystems. Ensure the mount is up before the
# service starts (systemd RequiresMountsFor=). Default: alongside the code.
_DB_ENV = os.environ.get("OCTAVIUS_DB_PATH")
DEFAULT_DB_PATH = Path(_DB_ENV).expanduser() if _DB_ENV else Path(__file__).parent / "octavius_history.db"


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if sqlite_vec is not None:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    return conn


@contextmanager
def connect_db(db_path: Path = DEFAULT_DB_PATH):
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
