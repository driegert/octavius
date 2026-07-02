"""Fact extraction: a user+assistant transcript -> durable SPO triples.

TRUST BOUNDARY (the load-bearing security property of v1): the transcript handed
to the LLM is built from role in {user, assistant} ONLY. Tool results (email/web/
file bodies) are untrusted and are NEVER mined into facts in v1. `untrusted` tier
is therefore never produced here.
"""

import json
import logging
import re
from dataclasses import dataclass

from . import config

log = logging.getLogger(__name__)

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# Only these two tiers can come out of extraction.
_EXTRACT_TIERS = ("asserted", "derived")

SYSTEM_PROMPT = (
    "/no_think\n"
    "You extract DURABLE facts about stable entities from a conversation, for an "
    "assistant's long-term memory. Output ONLY a JSON array; no prose, no markdown.\n\n"
    "Each element: "
    '{"subject": str, "predicate": str, "object": str, '
    '"object_is_entity": bool, "trust_tier": "asserted"|"derived"}.\n\n'
    "Rules:\n"
    "- Emit ONLY durable facts about stable entities, identities, relationships, "
    "preferences, long-running projects, or possessions the user owns or operates "
    "(computers/devices/hardware, infrastructure, tools). Treat each named "
    "machine/device as an entity. NEVER task-specific or ephemeral detail "
    "(today's todo, a one-off question, transient state).\n"
    "- Prefer reusing an existing predicate and an existing entity name (given below).\n"
    "- subject/object are canonical entity names ('Dave', 'Trent University') or, for "
    "a literal object, a short value ('Peterborough', 'uv').\n"
    "- object_is_entity=true only when the object is itself an entity/thing that could "
    "be a subject elsewhere; false for plain literals.\n"
    "- trust_tier='asserted' if the USER stated it directly; 'derived' if it was only "
    "inferred from assistant turns.\n"
    "- If nothing durable is present, output []."
)


@dataclass
class ExtractedFact:
    subject: str
    predicate: str
    object: str
    object_is_entity: bool
    trust_tier: str


def build_memory_transcript(messages: list[dict], *, max_content_chars: int = 1500) -> str:
    """Render ONLY user/assistant turns. The trust boundary in one place."""
    parts = []
    for msg in messages:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if len(content) > max_content_chars:
            content = content[:max_content_chars] + "..."
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


def _context_block(entities: list[str], predicates: list[dict]) -> str:
    ent = ", ".join(entities) if entities else "(none yet)"
    preds = ", ".join(f"{p['name']}({p['cardinality']})" for p in predicates)
    return (
        f"Known entities: {ent}\n"
        f"Known predicates: {preds}\n\n"
        "Conversation:\n"
    )


def _coerce_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def parse_facts(text: str | None) -> list[ExtractedFact]:
    if not text:
        return []
    text = _THINK_RE.sub("", text).strip()
    match = _JSON_ARRAY_RE.search(text)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.debug("Fact extraction returned invalid JSON", exc_info=True)
        return []
    if not isinstance(items, list):
        return []

    facts: list[ExtractedFact] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        subj = str(it.get("subject", "")).strip()
        pred = str(it.get("predicate", "")).strip()
        obj = str(it.get("object", "")).strip()
        if not (subj and pred and obj):
            continue
        # Defensive normalisation. Predicate names: lowercase, snake_case.
        pred = re.sub(r"\s+", "_", pred.lower())
        tier = str(it.get("trust_tier", "derived")).strip().lower()
        if tier not in _EXTRACT_TIERS:
            tier = "derived"
        facts.append(ExtractedFact(
            subject=subj, predicate=pred, object=obj,
            object_is_entity=_coerce_bool(it.get("object_is_entity", False)),
            trust_tier=tier,
        ))
    return facts


def extract_facts(messages: list[dict], *, complete_fn,
                  entities: list[str] | None = None,
                  predicates: list[dict] | None = None) -> list[ExtractedFact]:
    """Run the extractor over a user+assistant transcript.

    complete_fn(system_prompt, user_text) -> str | None  (injected; the real one
    wraps summary_client). Returns [] on empty transcript or LLM failure.
    """
    transcript = build_memory_transcript(messages)
    if not transcript:
        return []
    user_text = _context_block(entities or [], predicates or []) + transcript
    raw = complete_fn(SYSTEM_PROMPT, user_text)
    return parse_facts(raw)
