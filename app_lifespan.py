"""Startup/shutdown for the FastAPI app.

`main.create_app` injects `mcp_manager_factory`, `db_init`, `db_path` and
`verify_search` on `app.state`; the lifespan reads them from there.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from history import HistoryRecorder
from history_enrichment import drain_inflight
from pi_brain import PiProcessManager
from pi_tools_server import PiToolsServer
from reader_store import fail_stale_processing_documents, purge_temporary_documents
from settings import settings
from subagent_dispatcher import SubagentDispatcher
import tools as local_tools

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    mcp_manager = app.state.mcp_manager_factory(settings.mcp_servers)
    history_conn = app.state.db_init()
    db_path = app.state.db_path
    history = HistoryRecorder(db_path)
    stale_count = fail_stale_processing_documents(history_conn)
    try:
        purge_temporary_documents(history_conn)
    except Exception:  # a sweep must never stop startup; the next listing retries it
        log.warning("Could not purge temporary reader documents on startup", exc_info=True)
    app.state.mcp_manager = mcp_manager
    app.state.history = history
    app.state.subagent_dispatcher = SubagentDispatcher(settings.subagent_llm_chain)
    log.info(
        "Subagent dispatcher ready: %s",
        app.state.subagent_dispatcher.snapshot(),
    )
    if stale_count:
        log.warning("Marked %d stale reader document(s) as failed on startup", stale_count)
    # Loud, at startup: conversation/inbox search reads sidecar tables that
    # history-index.timer maintains in the file sites.toml names. If this app
    # opens a different file, or the sidecars are missing or L2-metric, search
    # would keep answering with stale or magnitude-ranked results instead of
    # failing. Injected so tests can run against a throwaway database.
    app.state.verify_search(history_conn, db_path)
    history_conn.close()
    log.info("Connecting MCP servers...")
    await mcp_manager.connect_all()
    log.info("MCP ready — %d tools available", len(mcp_manager.tools))
    for issue in local_tools.validate_local_tool_registry():
        log.warning("%s", issue)
    local_names = {t["function"]["name"] for t in local_tools.TOOLS}
    overlap = local_names & mcp_manager.get_registered_tool_names()
    if overlap:
        log.warning(
            "Local tool name(s) collide with MCP tools (local handler wins): %s",
            sorted(overlap),
        )
    # The embedding sweeper started here until 2026-09-15. It backfilled the
    # legacy vec0 tables; history-index.timer (`hybrid-corpus run history`,
    # every 15 min) owns freshness and embed-debt healing now, for all three
    # history collections rather than just two.
    # OCTAVIUS_BRAIN=pi: voice/text turns run on pi processes (pi_brain.py).
    app.state.pi_brain = None
    if settings.pi_brain.enabled:
        app.state.pi_brain = PiProcessManager(settings.pi_brain)
        # Octavius's own tools for pi, on a loopback-only port (never on this app).
        # If it cannot start, pi still runs, without those tools.
        app.state.pi_tools = PiToolsServer(port=settings.pi_brain.tools_port)
        try:
            await app.state.pi_tools.start()
            app.state.pi_brain.tools_server = app.state.pi_tools
        except Exception:
            log.exception("pi tools server failed to start; pi runs without Octavius's tools")
            app.state.pi_tools = None
        app.state.pi_brain.start_reaper()
        # Warm system prompt + tools in the background (pi /warm, never awaited).
        app.state.pi_brain.warm_static_prefix("startup")
        log.info("Brain: pi (%s via %s)", settings.pi_brain.model, settings.pi_brain.launcher)
    yield
    if app.state.pi_brain is not None:
        await app.state.pi_brain.shutdown()
    if getattr(app.state, "pi_tools", None) is not None:
        await app.state.pi_tools.stop()
    log.info("Shutting down MCP...")
    await mcp_manager.disconnect_all()
    # Nothing spawns detached embeds any more (see history_enrichment), so this
    # drains an empty set. Kept because it is the shutdown hook's only await on
    # in-flight background work and costs nothing when there is none.
    await drain_inflight()
