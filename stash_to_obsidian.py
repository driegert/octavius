"""Export new Octavius stash items (saved_items) to the Obsidian vault inbox.

Watermark-based watcher: each item is exported exactly once, so notes that are
later triaged (moved/renamed/deleted) in Obsidian are never re-created. Run
periodically via the obsidian-stash-export.timer systemd user unit.

On first run (no state file) it exports nothing and records the current max id,
treating everything already in the DB as handled by the 2026-07-02 bulk export.

The DB is opened read-only; the only writes are .md files in the vault and the
watermark state file.
"""
import json
import os
import re
import sqlite3
from pathlib import Path

DB = os.environ.get("OCTAVIUS_DB_PATH", "/media/extra_stuff/octavius/octavius_history.db")
DEST = Path(os.environ.get("STASH_EXPORT_DEST",
                           str(Path.home() / "Documents/Personal/01-Inbox/imported/octavius")))
STATE_FILE = Path.home() / ".local/state/octavius-stash-export/state.json"


def slugify(title: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()
    return slug[:max_len].rstrip("-") or "untitled"


def yaml_str(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def render(row: sqlite3.Row) -> str:
    lines = ["---"]
    lines.append(f"title: {yaml_str(row['title'] or 'Untitled')}")
    lines.append(f"octavius_id: {row['id']}")
    lines.append(f"type: {row['item_type']}")
    lines.append(f"status: {row['status']}")
    lines.append(f"created: {row['created_at']}")
    if row["updated_at"]:
        lines.append(f"updated: {row['updated_at']}")
    if row["source_url"]:
        lines.append(f"source: {yaml_str(row['source_url'])}")
    meta = json.loads(row["metadata"]) if row["metadata"] else {}
    for key, val in meta.items():
        lines.append(f"octavius_{key}: {yaml_str(val)}")
    lines.append("tags:")
    lines.append("  - imported/octavius")
    lines.append("---")
    lines.append("")
    lines.append(row["content"] or "")
    return "\n".join(lines) + "\n"


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    if not STATE_FILE.exists():
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM saved_items").fetchone()[0]
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"last_id": max_id}))
        print(f"Initialized watermark at id {max_id}; nothing exported.")
        conn.close()
        return

    state = json.loads(STATE_FILE.read_text())
    rows = conn.execute(
        "SELECT id, item_type, title, content, source_url, metadata, status,"
        " created_at, updated_at FROM saved_items WHERE id > ? ORDER BY id",
        (state["last_id"],),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No new items (watermark {state['last_id']}).")
        return

    DEST.mkdir(parents=True, exist_ok=True)
    for row in rows:
        created = (row["created_at"] or "")[:10] or "undated"
        out = DEST / f"{created}_{row['id']:04d}_{slugify(row['title'] or 'untitled')}.md"
        if out.exists():
            print(f"Skip (exists): {out.name}")
        else:
            out.write_text(render(row), encoding="utf-8")
            print(f"Exported: {out.name}")
        state["last_id"] = row["id"]
        STATE_FILE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
