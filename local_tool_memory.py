"""Local tool handlers for the long-term memory control surface.

remember / forget / correct / what_do_you_know. Each forwards to the shared memory
service over loopback HTTP (the service does the embedding + LLM extraction and
owns the facts). Best-effort, user-facing strings — a down service degrades to a
polite "unavailable" rather than breaking the turn.
"""

from __future__ import annotations

import logging

from memory_client import memory_client

log = logging.getLogger(__name__)

_UNAVAILABLE = "Sorry — my long-term memory service is unreachable right now."


def _conv_key(history_session):
    """Stable conversation key (the Element thread id) for fact provenance."""
    return getattr(history_session, "session_id", None) or "manual"


async def remember(args: dict, history_session=None, mcp_manager=None, session=None) -> str:
    statement = (args.get("statement") or "").strip()
    if not statement:
        return "Error: 'statement' is required."
    if not memory_client.enabled:
        return _UNAVAILABLE
    res = await memory_client.remember(statement, conv_key=_conv_key(history_session))
    if res is None:
        return "Sorry, I hit an error storing that."
    if not res.get("remembered"):
        return ("I couldn't pull a durable fact out of that — try stating it as a "
                "plain fact (e.g. 'I prefer X', 'I live in Y').")
    return "Remembered: " + "; ".join(res["remembered"])


async def forget(args: dict, history_session=None, mcp_manager=None, session=None) -> str:
    query = (args.get("fact") or args.get("query") or "").strip()
    if not query:
        return "Error: 'fact' is required."
    if not memory_client.enabled:
        return _UNAVAILABLE
    res = await memory_client.forget(query)
    if res is None:
        return "Sorry, I hit an error forgetting that."
    if not res.get("forgotten"):
        return f"I don't have a stored fact matching '{query}'."
    return "Forgotten: " + res["forgotten"]


async def correct(args: dict, history_session=None, mcp_manager=None, session=None) -> str:
    old = (args.get("old") or "").strip()
    new = (args.get("new") or "").strip()
    if not (old and new):
        return "Error: both 'old' and 'new' are required."
    if not memory_client.enabled:
        return _UNAVAILABLE
    res = await memory_client.correct(old, new, conv_key=_conv_key(history_session))
    if res is None:
        return "Sorry, I hit an error applying that correction."
    if not res.get("added"):
        return "I couldn't apply that correction."
    was = res.get("corrected_from")
    return f"Updated{f' (was: {was})' if was else ''}."


async def what_do_you_know(args: dict, history_session=None, mcp_manager=None, session=None) -> str:
    about = (args.get("about") or "").strip() or None
    if not memory_client.enabled:
        return _UNAVAILABLE
    res = await memory_client.what_do_you_know(about)
    if res is None:
        return "Sorry, I hit an error reading memory."
    if not res.get("facts"):
        return (f"I don't have anything stored about '{about}'." if about
                else "I don't have any durable facts stored yet.")
    header = f"What I know about '{about}':" if about else "Here's what I durably know:"
    return header + "\n" + "\n".join(f"- {f}" for f in res["facts"])
