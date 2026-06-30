"""Local tool handlers for the long-term memory control surface.

remember / forget / correct / what_do_you_know. Each runs its blocking work
(embedding, and for remember/correct an LLM extraction) off the event loop in a
worker thread with its OWN short-lived connection opened from the history session's
db_path (sqlite connections are single-thread; never touch the live session.conn
from a thread). Best-effort, user-facing strings.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


def _ctx(history_session):
    return (getattr(history_session, "db_path", None),
            getattr(history_session, "conv_id", None))


async def remember(args: dict, history_session=None, mcp_manager=None, session=None) -> str:
    statement = (args.get("statement") or "").strip()
    if not statement:
        return "Error: 'statement' is required."
    db_path, conv_id = _ctx(history_session)
    if db_path is None or conv_id is None:
        return "Error: no conversation context available."
    return await asyncio.to_thread(_remember_sync, db_path, conv_id, statement)


def _remember_sync(db_path, conv_id, statement) -> str:
    import memory
    from db import connect
    conn = connect(db_path)
    try:
        res = memory.remember(conn, statement, conversation_id=conv_id,
                              embed_fn=memory.default_embed_fn,
                              complete_fn=memory.default_complete_fn)
        if not res.get("remembered"):
            return ("I couldn't pull a durable fact out of that — try stating it as a "
                    "plain fact (e.g. 'I prefer X', 'I live in Y').")
        return "Remembered: " + "; ".join(res["remembered"])
    except Exception:
        log.warning("remember failed", exc_info=True)
        return "Sorry, I hit an error storing that."
    finally:
        conn.close()


async def forget(args: dict, history_session=None, mcp_manager=None, session=None) -> str:
    query = (args.get("fact") or args.get("query") or "").strip()
    if not query:
        return "Error: 'fact' is required."
    db_path, _ = _ctx(history_session)
    if db_path is None:
        return "Error: no conversation context available."
    return await asyncio.to_thread(_forget_sync, db_path, query)


def _forget_sync(db_path, query) -> str:
    import memory
    from db import connect
    conn = connect(db_path)
    try:
        res = memory.forget(conn, query, embed_fn=memory.default_embed_fn)
        if not res.get("forgotten"):
            return f"I don't have a stored fact matching '{query}'."
        return "Forgotten: " + res["forgotten"]
    except Exception:
        log.warning("forget failed", exc_info=True)
        return "Sorry, I hit an error forgetting that."
    finally:
        conn.close()


async def correct(args: dict, history_session=None, mcp_manager=None, session=None) -> str:
    old = (args.get("old") or "").strip()
    new = (args.get("new") or "").strip()
    if not (old and new):
        return "Error: both 'old' and 'new' are required."
    db_path, conv_id = _ctx(history_session)
    if db_path is None or conv_id is None:
        return "Error: no conversation context available."
    return await asyncio.to_thread(_correct_sync, db_path, conv_id, old, new)


def _correct_sync(db_path, conv_id, old, new) -> str:
    import memory
    from db import connect
    conn = connect(db_path)
    try:
        res = memory.correct(conn, old, new, conversation_id=conv_id,
                             embed_fn=memory.default_embed_fn,
                             complete_fn=memory.default_complete_fn)
        if not res.get("added"):
            return "I couldn't apply that correction."
        was = res.get("corrected_from")
        return f"Updated{f' (was: {was})' if was else ''}."
    except Exception:
        log.warning("correct failed", exc_info=True)
        return "Sorry, I hit an error applying that correction."
    finally:
        conn.close()


async def what_do_you_know(args: dict, history_session=None, mcp_manager=None, session=None) -> str:
    about = (args.get("about") or "").strip() or None
    db_path, _ = _ctx(history_session)
    if db_path is None:
        return "Error: no conversation context available."
    return await asyncio.to_thread(_wdyk_sync, db_path, about)


def _wdyk_sync(db_path, about) -> str:
    import memory
    from db import connect
    conn = connect(db_path)
    try:
        res = memory.what_do_you_know(conn, about, embed_fn=memory.default_embed_fn)
        if not res["facts"]:
            return (f"I don't have anything stored about '{about}'." if about
                    else "I don't have any durable facts stored yet.")
        header = f"What I know about '{about}':" if about else "Here's what I durably know:"
        return header + "\n" + "\n".join(f"- {f}" for f in res["facts"])
    except Exception:
        log.warning("what_do_you_know failed", exc_info=True)
        return "Sorry, I hit an error reading memory."
    finally:
        conn.close()
