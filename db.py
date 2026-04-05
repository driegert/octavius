import sqlite3
from contextlib import contextmanager
from pathlib import Path

try:
    import sqlite_vec
except ModuleNotFoundError:  # pragma: no cover - optional in lightweight test environments
    sqlite_vec = None


DEFAULT_DB_PATH = Path(__file__).parent / "octavius_history.db"


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
