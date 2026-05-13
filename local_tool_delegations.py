"""Local-tool handlers for inspecting and pulling parked delegations.

These give the voice agent a way to react to "let's go over those emails now"
style instructions: list what's ready, pick a handle, pull it into the
running conversation. The new-conversation flow is intentionally UI-only
(see websocket_session._spawn_seeded_conversation) — swapping the session
state out from under a mid-turn agent is fragile.
"""

from __future__ import annotations

import json


async def list_pending_delegations(args: dict, history_session=None, mcp_manager=None, session=None) -> str:
    """Return a JSON summary of currently parked delegations.

    Filters: pass {"status": "ready"} to show only completed ones, or
    {"domain": "email"} to narrow by domain. With no filters, all records
    (running, ready, failed) are returned.
    """
    if session is None:
        return "Error: delegation session unavailable."

    status_filter = args.get("status")
    domain_filter = args.get("domain")

    items = []
    for handle, record in session.state.delegations.items():
        if status_filter and record.status != status_filter:
            continue
        if domain_filter and record.domain != domain_filter:
            continue
        items.append({
            "handle": handle,
            "domain": record.domain,
            "submitted_task": record.submitted_task,
            "status": record.status,
            "preview": record.preview,
            "error": record.error,
        })

    # Newest-first; more useful when there are several.
    items.sort(key=lambda x: x["handle"], reverse=True)
    return json.dumps({"count": len(items), "delegations": items})


async def pull_delegation(args: dict, history_session=None, mcp_manager=None, session=None) -> str:
    """Pull a ready delegation into the running conversation.

    Accepts either an explicit handle, or a domain (in which case the most
    recent ready delegation in that domain is selected). Returns the
    specialist's reply as the tool observation so the agent's current turn
    can read and summarize it.
    """
    if session is None:
        return "Error: delegation session unavailable."

    handle = (args.get("handle") or "").strip()
    domain = (args.get("domain") or "").strip()

    if not handle and not domain:
        return "Error: provide handle or domain."

    if not handle:
        # Pick the most recent ready record for the requested domain.
        candidates = [
            (h, r) for h, r in session.state.delegations.items()
            if r.domain == domain and r.status == "ready"
        ]
        if not candidates:
            return f"No ready {domain} delegation to pull. Use list_pending_delegations to check status."
        candidates.sort(key=lambda x: x[1].created_at, reverse=True)
        handle = candidates[0][0]

    # Voice path: agent is mid-turn and wants the result text as observation.
    return await session.pull_delegation(handle=handle, mode="merge", via="voice")
