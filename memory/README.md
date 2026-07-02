# Octavius long-term memory (v1, graph-lite)

Three memory mechanisms over one SQLite store (no Kùzu, no dual store, no query
router). Full design record: `~/ai_chats/matrix_lxc_temp/DESIGN-octavius-memory.md`.

- **Durable facts** — a temporal/provenance-tagged Subject-Predicate-Object table
  (`memory_facts`). An SPO row whose object is an entity *is* a graph edge.
- **Global synthesis** — a maintained `memory_profile` doc: Block 1 (identity,
  rendered deterministically from live facts) + Block 2 (LLM themes rollup).
- **Episodic recall** — the existing `search_conversation_history` tool +
  thread-start proactive injection (lives in the agent, not here).

## Shape

Self-contained, liftable module. Everything is pure Python over a `sqlite3`
connection plus two injected callables —

```
embed_fn(text)               -> bytes | None         # float32 LE, 1024-d
complete_fn(system, user)    -> str | None            # the summary LLM
```

— so the whole module **unit-tests offline** (`tests/test_memory.py`).
`default_embed_fn` / `default_complete_fn` in `__init__.py` are the only coupling
to Octavius's real services; agent callers pass those. The public functions are
intended to become the future memory *service*'s endpoints unchanged.

| file | role |
|------|------|
| `config.py`    | tunable thresholds + the confidence model |
| `store.py`     | DB primitives; the user+assistant-only message reader (trust boundary) |
| `extract.py`   | transcript → SPO triples; `build_memory_transcript` is the trust seam |
| `reconcile.py` | dedup / supersession / provenance / confidence write logic |
| `read.py`      | per-turn `retrieve_facts` + `render_profile` |
| `synthesis.py` | Block-2 rollup on an event counter |
| `tools.py`     | `remember` / `forget` / `correct` / `what_do_you_know` |

## Trust boundary (the load-bearing v1 invariant)

Fact extraction reads **only `user` and `assistant` turns**. Tool results
(email/web/file bodies) are untrusted and are never mined into facts — enforced in
two places: `store.messages_after_watermark` (the write-path source query) and
`extract.build_memory_transcript` (defence in depth). The `untrusted` tier exists
in the schema but is never populated in v1. Do not add a tool/email extraction
path without the quarantine rules in the design doc (§Q8).

## Tuning notes

- **`NEAR_DUP_DISTANCE` (config.py, default `0.12`).** Cosine distance below which
  two facts sharing subject+predicate are merged as literal-object drift ("uv" vs
  "the uv tool") instead of stored as separate rows. **This default is calibrated
  for the real sentence-embedder** (paraphrase distances typically land ~0.05–0.10;
  distinct-but-related facts ~0.2–0.4). The offline tests use a coarse bag-of-words
  fake embedding whose distances are much larger, so they pass an explicit
  `near_dup_threshold` instead of relying on this default — do **not** loosen the
  production constant to satisfy a test. `reconcile_facts(..., near_dup_threshold=)`
  lets you sweep it without editing code. **Validated 2026-06-30** against the live
  embedder: paraphrases ("uv" vs "the uv package manager", "Peterborough" vs
  "Peterborough Ontario") land at 0.035–0.096; distinct facts ("uv" vs "ripgrep",
  "statistics" vs "chemistry") at 0.156–0.236. `0.12` sits in the valley with
  headroom both sides — re-check only if the embedding model changes.
- `RETRIEVAL_DISTANCE` / `RETRIEVAL_K` gate per-turn fact injection; same calibrate
  -against-the-real-embedder caveat applies.
- `SYNTHESIS_THRESHOLD` = salient conversation closes before a Block-2 rebuild.
- Confidence is **derived, never LLM-guessed**: `f(trust_tier, #distinct source
  conversations, recency)` — see `store.recompute_confidence`.

## Database location

The store is the existing conversation-history DB (`schema.sql`), shared with the
memory tables. Override its path with **`OCTAVIUS_DB_PATH`** (see `db.py`) to put it
on a roomy local **ext4** volume — it grows once cross-harness aggregation is on.
Must be a POSIX/ext4 mount (SQLite WAL corrupts on NTFS/exFAT/network FS), and the
mount must be up before `octavius.service` starts (`RequiresMountsFor=`).
