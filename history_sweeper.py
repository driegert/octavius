"""RETIRED 2026-09-15 — background repair for legacy embeddings.

This module backfilled the legacy vec0 tables (``message_embeddings``,
``summary_embeddings``) for rows whose detached embed never landed. Both the
detached embeds and those tables are gone. ``history-index.timer`` runs
``hybrid-corpus run history`` every 15 minutes, which fingerprints ``messages``,
``conversations`` and ``saved_items``, re-embeds anything new or changed, and
heals its own embed debt through the ``embed_pending`` flag in
``history_*_source_state``. That covers ``saved_items`` as well, which this
sweeper never did.

The file is kept rather than deleted so the retirement stays discoverable from
the import graph, not just from a commit message. Both entry points raise
``RuntimeError``: nothing should call them, and a silent no-op would hide a
caller that still does.

The selection helpers (``find_unembedded_messages``,
``find_unembedded_summaries``) and the embed loop (``_embed_rows``) are gone
outright — they queried tables that no longer exist, so there is nothing for
them to be honest about.
"""

import logging

log = logging.getLogger(__name__)

_RETIRED = (
    "history_sweeper was retired on 2026-09-15. Embedding and embed-debt "
    "healing for the octavius history database belong to history-index.timer "
    "(`hybrid-corpus run history`, every 15 minutes), which covers messages, "
    "summaries and saved items. Nothing in the app should start a sweeper."
)


async def sweep_once(db_path) -> dict:
    """Retired 2026-09-15 in favour of history-index.timer. Raises RuntimeError.

    Formerly: one pass over the message and summary embed backlogs, repairing
    rows that had no vector in the legacy tables.
    """
    raise RuntimeError(_RETIRED)


async def run_sweeper(db_path, **kwargs) -> None:
    """Retired 2026-09-15 in favour of history-index.timer. Raises RuntimeError.

    Formerly: an asyncio task started from ``main.py``'s lifespan that called
    ``sweep_once`` every 15 minutes for the life of the process.
    """
    raise RuntimeError(_RETIRED)
