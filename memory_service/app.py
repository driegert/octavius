"""Memory service — FastAPI over the `memory/` module.

The cross-harness memory brain. Agents push closed (user+assistant) transcripts to
POST /conversations and query GET /profile, GET /facts; the four control tools are
POST /facts/{remember,forget,correct} + GET /facts/all.

Endpoints are sync `def` so FastAPI runs them in a threadpool — the underlying
sqlite + sync embed/LLM calls never block the event loop, and each request gets its
own connection (sqlite connections are single-thread).
"""

from contextlib import closing

from fastapi import FastAPI, Request
from pydantic import BaseModel

import memory
from memory import store

from . import db


# --- request models ----------------------------------------------------------

class PushConversation(BaseModel):
    service: str
    conv_key: str
    transcript: list[dict] = []          # [{role: user|assistant, content}]
    summary: str | None = None
    tags: list[str] = []
    index: bool = True
    ended_at: str | None = None


class RememberBody(BaseModel):
    service: str = "octavius"
    conv_key: str = "manual"
    statement: str


class ForgetBody(BaseModel):
    query: str


class CorrectBody(BaseModel):
    service: str = "octavius"
    conv_key: str = "manual"
    old: str
    new: str


# --- helpers -----------------------------------------------------------------

def _conn(request: Request):
    return db.connect(request.app.state.db_path)


def _resolve_conversation(conn, service: str, conv_key: str,
                          summary: str | None = None,
                          ended_at: str | None = None) -> int:
    now = store.now_iso()
    row = conn.execute(
        "SELECT id FROM conversations WHERE service = ? AND conv_key = ?",
        (service, conv_key),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE conversations SET summary = COALESCE(?, summary), ended_at = ? WHERE id = ?",
            (summary, ended_at or now, row[0]),
        )
        return row[0]
    cur = conn.execute(
        "INSERT INTO conversations (service, conv_key, summary, started_at, ended_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (service, conv_key, summary, now, ended_at),
    )
    return cur.lastrowid


def _store_tags(conn, conv_id: int, tags: list[str]) -> None:
    for name in tags:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if row:
            conn.execute(
                "INSERT OR IGNORE INTO conversation_tags (conversation_id, tag_id) VALUES (?, ?)",
                (conv_id, row[0]),
            )


def _store_summary_embedding(conn, conv_id: int, summary: str) -> None:
    emb = memory.default_embed_fn(summary)
    if emb is None:
        return
    conn.execute("DELETE FROM summary_embeddings WHERE conversation_id = ?", (conv_id,))
    conn.execute(
        "INSERT INTO summary_embeddings (conversation_id, embedding) VALUES (?, ?)",
        (conv_id, emb),
    )


# --- app ---------------------------------------------------------------------

def create_app(db_path=None) -> FastAPI:
    app = FastAPI(title="Octavius memory service")
    app.state.db_path = db_path or db.DEFAULT_SERVICE_DB_PATH
    db.init_db(app.state.db_path)

    @app.get("/healthz")
    def healthz(request: Request):
        with closing(_conn(request)) as conn:
            n = conn.execute("SELECT COUNT(*) FROM memory_facts WHERE valid_until IS NULL").fetchone()[0]
        return {"ok": True, "live_facts": n}

    @app.post("/conversations")
    def push_conversation(body: PushConversation, request: Request):
        with closing(_conn(request)) as conn:
            conv_id = _resolve_conversation(conn, body.service, body.conv_key,
                                            body.summary, body.ended_at)
            _store_tags(conn, conv_id, body.tags)
            out = {"conversation_id": conv_id, "added": 0, "reinforced": 0, "superseded": 0}
            if body.index:
                if body.summary:
                    _store_summary_embedding(conn, conv_id, body.summary)
                if body.transcript:
                    res = memory.extract_and_reconcile(conn, body.transcript, conv_id)
                    out.update(added=res.added, reinforced=res.reinforced,
                               superseded=res.superseded)
                memory.bump_source_count(conn)
                memory.maybe_synthesize(conn, complete_fn=memory.default_complete_fn)
            conn.commit()
        return out

    @app.get("/profile")
    def get_profile(request: Request):
        with closing(_conn(request)) as conn:
            return {"profile": memory.render_profile(conn)}

    @app.get("/facts")
    def get_facts(request: Request, q: str, k: int = memory.config.RETRIEVAL_K):
        with closing(_conn(request)) as conn:
            facts = memory.retrieve_facts(conn, q, embed_fn=memory.default_embed_fn, k=k)
            return {"facts": [memory.read.format_fact_line(f) for f in facts], "raw": facts}

    @app.get("/facts/all")
    def get_all_facts(request: Request, about: str | None = None):
        with closing(_conn(request)) as conn:
            return memory.what_do_you_know(conn, about, embed_fn=memory.default_embed_fn)

    @app.post("/facts/remember")
    def post_remember(body: RememberBody, request: Request):
        with closing(_conn(request)) as conn:
            conv_id = _resolve_conversation(conn, body.service, body.conv_key)
            conn.commit()
            return memory.remember(conn, body.statement, conversation_id=conv_id,
                                   embed_fn=memory.default_embed_fn,
                                   complete_fn=memory.default_complete_fn)

    @app.post("/facts/forget")
    def post_forget(body: ForgetBody, request: Request):
        with closing(_conn(request)) as conn:
            return memory.forget(conn, body.query, embed_fn=memory.default_embed_fn)

    @app.post("/facts/correct")
    def post_correct(body: CorrectBody, request: Request):
        with closing(_conn(request)) as conn:
            conv_id = _resolve_conversation(conn, body.service, body.conv_key)
            conn.commit()
            return memory.correct(conn, body.old, body.new, conversation_id=conv_id,
                                  embed_fn=memory.default_embed_fn,
                                  complete_fn=memory.default_complete_fn)

    return app
