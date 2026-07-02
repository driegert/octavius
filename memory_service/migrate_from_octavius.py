"""One-shot migration: copy Octavius's live memory graph into the standalone
memory-service DB (v2 Phase 2).

Octavius's in-process memory (v1) writes facts into its history DB. The service
owns its own memory-only DB. This copies the `memory_*` graph across with full
fidelity:

- memory_facts ids are preserved EXACTLY, so `superseded_by` self-refs,
  fact_sources, and fact_embeddings stay internally consistent.
- conversation ids are REMAPPED: each source conversation becomes a lightweight
  (service, conv_key=session_id) row in the service DB.
- the FULL predicates table is copied (catches runtime-registered predicates like
  `teaches_at` that the static seed lacks) along with entity_aliases.
- fact_embeddings (vec0 blobs) are copied byte-for-byte; the profile row's
  source_count is preserved (drives synthesis cadence).

Safe to re-run for Phase 4: pass --fresh to rebuild from scratch (the service DB
is authoritative only once the service is deployed; until then a clean re-copy is
the simplest way to capture facts Octavius learned after the first migration).

Usage:
    uv run python -m memory_service.migrate_from_octavius [--fresh]

Env:
    OCTAVIUS_DB_PATH         source (default /media/extra_stuff/octavius/octavius_history.db)
    OCTAVIUS_MEMORY_DB_PATH  destination (default /media/extra_stuff/octavius/memory_service.db)
"""

import os
import sys
from pathlib import Path

from db import connect
from .db import init_db, DEFAULT_SERVICE_DB_PATH

# Default the SOURCE to the deployment path, not db.DEFAULT_DB_PATH — the latter
# falls back to a repo-local (pre-memory) backup when OCTAVIUS_DB_PATH is unset.
DEFAULT_SOURCE_DB_PATH = Path(
    os.environ.get("OCTAVIUS_DB_PATH",
                   "/media/extra_stuff/octavius/octavius_history.db"))


def migrate(src_path=None, dst_path=None, *, fresh=False):
    src_path = Path(src_path or DEFAULT_SOURCE_DB_PATH)
    dst_path = Path(dst_path or DEFAULT_SERVICE_DB_PATH)

    # Validate the SOURCE before touching the destination (so --fresh can't wipe a
    # good DB on a misconfigured source).
    src = connect(src_path)
    has_mem = src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_facts'").fetchone()
    n_src = src.execute("SELECT count(*) FROM memory_facts").fetchone()[0] if has_mem else 0
    if not has_mem or n_src == 0:
        src.close()
        print(f"ABORT: source {src_path} has no memory_facts to migrate "
              f"(found table={bool(has_mem)}, rows={n_src}). Set OCTAVIUS_DB_PATH.")
        return 1

    if fresh:
        for suffix in ("", "-wal", "-shm"):
            p = dst_path.with_name(dst_path.name + suffix)
            if p.exists():
                p.unlink()
        print(f"--fresh: removed existing {dst_path.name}(+wal/shm)")

    init_db(dst_path)  # schema + predicate seed + profile row
    dst = connect(dst_path)
    try:
        existing = dst.execute("SELECT count(*) FROM memory_facts").fetchone()[0]
        if existing:
            print(f"ABORT: destination already has {existing} facts (use --fresh to rebuild).")
            return 1

        dst.execute("BEGIN")

        preds = src.execute("SELECT name, cardinality, description FROM predicates").fetchall()
        dst.executemany(
            "INSERT OR IGNORE INTO predicates(name, cardinality, description) VALUES (?,?,?)", preds)

        aliases = src.execute("SELECT alias, canonical FROM entity_aliases").fetchall()
        dst.executemany(
            "INSERT OR IGNORE INTO entity_aliases(alias, canonical) VALUES (?,?)", aliases)

        # conversation id-map: every conv referenced by a fact source
        conv_ids = [r[0] for r in src.execute(
            "SELECT DISTINCT conversation_id FROM memory_fact_sources").fetchall()]
        id_map = {}
        for cid in conv_ids:
            session_id, service, summary, started_at, ended_at = src.execute(
                "SELECT session_id, service, summary, started_at, ended_at "
                "FROM conversations WHERE id = ?", (cid,)).fetchone()
            dst.execute(
                "INSERT INTO conversations(service, conv_key, summary, started_at, ended_at) "
                "VALUES (?,?,?,?,?)", (service, session_id, summary, started_at, ended_at))
            id_map[cid] = dst.execute("SELECT last_insert_rowid()").fetchone()[0]

        facts = src.execute(
            "SELECT id, subject, predicate, object, object_is_entity, confidence, trust_tier, "
            "valid_from, valid_until, superseded_by, created_at, updated_at "
            "FROM memory_facts ORDER BY id").fetchall()
        dst.executemany(
            "INSERT INTO memory_facts(id, subject, predicate, object, object_is_entity, "
            "confidence, trust_tier, valid_from, valid_until, superseded_by, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", facts)

        sources = src.execute(
            "SELECT fact_id, conversation_id, asserted_at FROM memory_fact_sources").fetchall()
        dst.executemany(
            "INSERT INTO memory_fact_sources(fact_id, conversation_id, asserted_at) VALUES (?,?,?)",
            [(fid, id_map[cid], at) for fid, cid, at in sources])

        for fid, emb in src.execute("SELECT fact_id, embedding FROM fact_embeddings").fetchall():
            dst.execute("INSERT INTO fact_embeddings(fact_id, embedding) VALUES (?,?)", (fid, emb))

        prof = src.execute(
            "SELECT content, generated_at, source_count FROM memory_profile WHERE id=1").fetchone()
        if prof:
            dst.execute("UPDATE memory_profile SET content=?, generated_at=?, source_count=? WHERE id=1", prof)

        dst.execute("COMMIT")
        print(f"Migrated {len(facts)} facts, {len(sources)} sources, "
              f"{len(id_map)} conversations, {len(preds)} predicates → {dst_path}")
        return 0
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    sys.exit(migrate(fresh="--fresh" in sys.argv[1:]))
