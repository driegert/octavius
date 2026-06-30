"""Reconcile extracted facts into the store.

For each incoming triple, in order:
  1. alias-resolve subject/object; register a novel predicate (default 'multi').
  2. forget-guard: if this exact triple was deliberately FORGOTTEN, skip it
     (the re-extraction loop must not resurrect a forgotten fact).
  3. exact live match  -> reinforce (add source, maybe upgrade tier).
  4. near-dup live match (same subj+pred, literal drift) -> reinforce existing.
  5. functional predicate with a different live object -> supersede old, insert new.
  6. otherwise (multi, or new) -> insert new.
Confidence is recomputed (never LLM-guessed) for every touched fact.
"""

import logging
from dataclasses import dataclass, field

from . import config, store
from .extract import ExtractedFact

log = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    added: int = 0
    reinforced: int = 0
    superseded: int = 0
    skipped: int = 0
    touched_ids: list[int] = field(default_factory=list)


def _reinforce(conn, fact_id, conversation_id, tier, now, result):
    is_new_source = store.add_source(conn, fact_id, conversation_id, now)
    # A user assertion upgrades a previously merely-derived fact.
    if tier == "asserted":
        cur = conn.execute(
            "SELECT trust_tier FROM memory_facts WHERE id = ?", (fact_id,)
        ).fetchone()
        if cur and cur[0] == "derived":
            store.upgrade_tier(conn, fact_id, "asserted", now)
    store.recompute_confidence(conn, fact_id, now)
    if is_new_source:
        result.reinforced += 1
    else:
        result.skipped += 1
    result.touched_ids.append(fact_id)


def reconcile_facts(conn, facts: list[ExtractedFact], conversation_id: int, *,
                    embed_fn, now: str | None = None,
                    respect_tombstones: bool = True,
                    near_dup_threshold: float = config.NEAR_DUP_DISTANCE) -> ReconcileResult:
    now = now or store.now_iso()
    result = ReconcileResult()

    for f in facts:
        subject = store.resolve_entity(conn, f.subject)
        obj = store.resolve_entity(conn, f.object) if f.object_is_entity else f.object.strip()
        predicate = f.predicate
        cardinality = store.register_predicate(conn, predicate)

        # 2. forget-guard
        if respect_tombstones and store.find_tombstone(conn, subject, predicate, obj):
            result.skipped += 1
            continue

        # 3. exact live match
        exact = store.find_live_exact(conn, subject, predicate, obj)
        if exact is not None:
            _reinforce(conn, exact, conversation_id, f.trust_tier, now, result)
            continue

        # 4. near-dup (same subject+predicate, literal-object drift)
        dup = store.find_near_dup(conn, subject, predicate, obj, embed_fn,
                                  threshold=near_dup_threshold)
        if dup is not None:
            _reinforce(conn, dup, conversation_id, f.trust_tier, now, result)
            continue

        # 5. functional supersession
        new_id = store.insert_fact(
            conn, subject, predicate, obj, f.object_is_entity, f.trust_tier, now
        )
        if cardinality == "functional":
            for live in store.find_live_for_subject_predicate(conn, subject, predicate):
                if live["id"] != new_id:
                    store.supersede(conn, live["id"], new_id, now)
                    result.superseded += 1

        # 6. insert (finalise the new fact)
        store.add_source(conn, new_id, conversation_id, now)
        store.store_fact_embedding(conn, new_id, subject, predicate, obj, embed_fn)
        store.recompute_confidence(conn, new_id, now)
        result.added += 1
        result.touched_ids.append(new_id)

    conn.commit()
    return result
