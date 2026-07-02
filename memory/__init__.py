"""Octavius long-term memory (v1, graph-lite).

Self-contained module: its public functions ARE the future memory-service API.
Everything is pure-Python over a sqlite3 connection plus two injected callables —
  embed_fn(text) -> bytes | None
  complete_fn(system_prompt, user_text) -> str | None
— so the module unit-tests offline. `default_embed_fn` / `default_complete_fn`
wrap Octavius's real clients; callers in the agent pass those.

Public API
----------
write:   extract_facts, reconcile_facts, extract_and_reconcile
read:    retrieve_facts, render_profile, render_identity_block, live_facts
synth:   bump_source_count, should_synthesize, synthesize_profile, maybe_synthesize
tools:   remember, forget, correct, what_do_you_know
boundary: build_memory_transcript  (user+assistant only)
"""

import logging

from . import config, read, reconcile, store, synthesis, tools
from .extract import (ExtractedFact, build_memory_transcript, extract_facts,
                      parse_facts)
from .reconcile import ReconcileResult, reconcile_facts
from .read import (facts_by_ids, live_facts, render_identity_block,
                   render_profile, retrieve_facts)
from .synthesis import (bump_source_count, maybe_synthesize, should_synthesize,
                        synthesize_profile)
from .tools import correct, forget, remember, what_do_you_know

log = logging.getLogger(__name__)

__all__ = [
    "config", "store", "read", "reconcile", "synthesis", "tools",
    "ExtractedFact", "ReconcileResult",
    "build_memory_transcript", "extract_facts", "parse_facts", "reconcile_facts",
    "extract_and_reconcile",
    "retrieve_facts", "render_profile", "render_identity_block", "live_facts",
    "facts_by_ids",
    "bump_source_count", "should_synthesize", "synthesize_profile", "maybe_synthesize",
    "remember", "forget", "correct", "what_do_you_know",
    "default_embed_fn", "default_complete_fn",
]


# --- Real-client adapters (kept thin; the only coupling to Octavius services) ---

def default_embed_fn(text: str):
    """Bridge to Octavius's embedding client. Returns float32 bytes or None."""
    from history_enrichment import embed_text
    return embed_text(text)


def default_complete_fn(system_prompt: str, user_text: str):
    """Bridge to Octavius's summary LLM. Returns visible completion text or None."""
    from service_clients import summary_client
    from settings import settings
    payload = {
        "model": settings.summary_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "max_tokens": 1024,
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    import re
    text = summary_client.complete(payload, timeout=settings.summary_timeout)
    if not text:
        return None
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip() or None


def extract_and_reconcile(conn, messages, conversation_id, *, embed_fn=None,
                          complete_fn=None, now=None):
    """One-call write path: extract durable facts from user+assistant turns and
    reconcile them. Used by the conversation-close hook (Step 3)."""
    embed_fn = embed_fn or default_embed_fn
    complete_fn = complete_fn or default_complete_fn
    facts = extract_facts(
        messages, complete_fn=complete_fn,
        entities=store.known_entities(conn),
        predicates=store.predicate_registry(conn),
    )
    if not facts:
        return ReconcileResult()
    return reconcile_facts(conn, facts, conversation_id, embed_fn=embed_fn, now=now)
