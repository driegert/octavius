"""User-facing control surface (registered as local tools in Step 6).

remember            -> assert facts directly (highest trust).
forget / correct    -> soft-delete (tombstone), never hard-delete; the re-extraction
                       loop honours the tombstone so a forgotten fact can't resurrect.
what_do_you_know    -> list live facts (doubles as a recall_facts fallback).
"""

import logging

from . import config, read, store
from .extract import ExtractedFact
from .reconcile import reconcile_facts

log = logging.getLogger(__name__)

FORGET_DISTANCE = 0.5  # how close a fact must be to a forget/correct query to match


def remember(conn, statement: str, *, conversation_id: int, embed_fn, complete_fn) -> dict:
    """Dave asserts a fact in natural language. Extracted, forced to 'asserted',
    high trust. Bypasses the forget tombstone (a deliberate user act may resurrect)."""
    from .extract import extract_facts
    facts = extract_facts(
        [{"role": "user", "content": statement}],
        complete_fn=complete_fn,
        entities=store.known_entities(conn),
        predicates=store.predicate_registry(conn),
    )
    for f in facts:
        f.trust_tier = "asserted"
    res = reconcile_facts(conn, facts, conversation_id, embed_fn=embed_fn,
                          respect_tombstones=False)
    touched = read.facts_by_ids(conn, res.touched_ids)
    return {
        "remembered": [read.format_fact_line(f) for f in touched],
        "added": res.added, "reinforced": res.reinforced, "superseded": res.superseded,
    }


def _match_live(conn, query: str, embed_fn, *, threshold=FORGET_DISTANCE) -> dict | None:
    matches = read.retrieve_facts(conn, query, embed_fn=embed_fn, k=1, threshold=threshold)
    return matches[0] if matches else None


def forget(conn, query: str, *, embed_fn) -> dict:
    """Soft-delete the best-matching live fact (tombstone: valid_until set,
    superseded_by NULL). Idempotent-ish; returns what was forgotten."""
    fact = _match_live(conn, query, embed_fn)
    if not fact:
        return {"forgotten": None, "note": "no matching fact"}
    store.supersede(conn, fact["id"], None, store.now_iso())
    conn.commit()
    return {"forgotten": read.format_fact_line(fact)}


def correct(conn, old: str, new: str, *, conversation_id: int, embed_fn, complete_fn) -> dict:
    """Replace: forget the old fact, assert the new one, link supersession."""
    target = _match_live(conn, old, embed_fn)
    now = store.now_iso()
    from .extract import extract_facts
    facts = extract_facts(
        [{"role": "user", "content": new}],
        complete_fn=complete_fn,
        entities=store.known_entities(conn),
        predicates=store.predicate_registry(conn),
    )
    for f in facts:
        f.trust_tier = "asserted"
    res = reconcile_facts(conn, facts, conversation_id, embed_fn=embed_fn,
                          respect_tombstones=False, now=now)
    new_id = res.touched_ids[-1] if res.touched_ids else None
    if target:
        store.supersede(conn, target["id"], new_id, now)
        conn.commit()
    return {
        "corrected_from": read.format_fact_line(target) if target else None,
        "added": res.added, "reinforced": res.reinforced,
    }


def what_do_you_know(conn, about: str | None = None, *, embed_fn=None,
                     limit: int = 30) -> dict:
    """List live facts. With `about`, semantic-filter (needs embed_fn); else all."""
    if about and embed_fn is not None:
        facts = read.retrieve_facts(conn, about, embed_fn=embed_fn, k=limit,
                                    threshold=config.RETRIEVAL_DISTANCE)
    else:
        facts = read.live_facts(conn)[:limit]
    return {"facts": [read.format_fact_line(f) for f in facts], "count": len(facts)}
