"""Reader document chat and the agent model menu for a WebSocket session.

A mixin composed into ``websocket_session.WebSocketSessionHandler``; the
session state lives on ``self.state`` (see ``websocket_session``).
"""
from __future__ import annotations

import logging

from settings import settings

log = logging.getLogger(__name__)


def reader_model_label(model: str) -> str:
    """The Reader chat menu's label for a pi model id, or the id itself."""
    for m in settings.pi_brain.reader_models:
        if m.get("model") == model:
            return m.get("label") or model
    return model


def chat_model_label(model: str) -> str:
    """The agent menu's label for a pi model id (then the Reader menu's), or the id itself."""
    for m in settings.pi_brain.chat_models:
        if m.get("model") and m.get("model") == model:
            return m.get("label") or model
    return reader_model_label(model)


def chat_model_choice(model_id) -> dict:
    """The agent menu entry for ``model_id``; unknown or missing: the first (default)."""
    models = settings.pi_brain.chat_models or [{"id": "auto", "label": "Auto", "model": None}]
    return next((m for m in models if m.get("id") == model_id), models[0])


def chat_models_payload() -> dict:
    """The agent's model menu, for GET /api/chat-models and the `chat_model` ack."""
    models = settings.pi_brain.chat_models or [{"id": "auto", "label": "Auto", "model": None}]
    return {
        "models": [{"id": m["id"], "label": m.get("label") or m["id"]} for m in models],
        "default": models[0]["id"],
        # The menu only does anything on the pi brain; the native loop has one chain.
        "enabled": settings.pi_brain.enabled,
    }


class DocChatMixin:
    async def handle_doc_chat_open(self, data: dict):
        """Bind this connection to a Reader document's chat (/reader's chat panel).

        One chat per document: the conversation is keyed `reader-doc:<id>` and resumed
        on every open. `new: true` renames the old key aside (it stays in history under
        `reader-doc:<id>:<conversation id>`) and starts a fresh one.
        """
        from contextlib import closing

        from history import get_conversation_messages
        from reader_store import get_document
        try:
            doc_id = int(data.get("doc_id"))
        except (TypeError, ValueError):
            await self.send_payload({"type": "doc_chat_error", "doc_id": data.get("doc_id"), "error": "No document id."})
            return
        with closing(self.state.history.connect()) as conn:
            doc = get_document(conn, doc_id)
        if not doc:
            await self.send_payload({"type": "doc_chat_error", "doc_id": doc_id, "error": "Document not found."})
            return
        await self._abandon_turn()
        await self._discard_pi_process()
        await self.state.history_session.end_async()
        key = f"reader-doc:{doc_id}"
        if data.get("new") is True:
            with closing(self.state.history.connect()) as conn:
                conn.execute(
                    "UPDATE conversations SET session_id = session_id || ':' || id WHERE session_id = ?",
                    (key,),
                )
                conn.commit()
        self.state.history_session = self.state.history.resume_or_start_conversation(
            key,
            source="reader",
            model=settings.llm_chain[0]["model"],
        )
        conv_id = self.state.history_session.conv_id
        msgs = get_conversation_messages(self.state.history_session.conn, conv_id)
        if msgs:
            self.state.conversation.load_from_history(msgs)
        else:
            self.state.conversation.reset()
        self.state.doc_chat_id = doc_id
        self.state.doc_chat_position = None
        choice = self._choose_reader_model(data.get("model"))
        self._request_pi_warm("doc_chat_open", start=True)
        await self.send_payload(
            {
                "type": "doc_chat_opened",
                "doc_id": doc_id,
                "title": doc["title"],
                "conversation_id": conv_id,
                "messages": [
                    {"role": m["role"], "content": m["content"]}
                    for m in msgs
                    if m["role"] in ("user", "assistant") and m.get("content")
                ],
                "models": [{"id": m["id"], "label": m.get("label") or m["id"]} for m in settings.pi_brain.reader_models],
                "model": choice["id"] if choice else None,
            }
        )
        log.info("Reader chat for document %d on conversation %d (%d messages)", doc_id, conv_id, len(msgs))


    def _choose_reader_model(self, model_id) -> dict | None:
        """Point this Reader chat's turns at a menu model (unknown or missing: the default)."""
        models = settings.pi_brain.reader_models
        if not models:
            return None
        choice = next((m for m in models if m.get("id") == model_id), models[0])
        pi_brain = getattr(self.ws.app.state, "pi_brain", None)
        if pi_brain is not None and self.state.history_session is not None:
            pi_brain.prefer(self.state.history_session.conv_id, choice["model"], choice.get("thinking"))
        return choice


    async def handle_doc_chat_model(self, data: dict):
        """Dave picked another model in the Reader chat's menu."""
        if self.state.doc_chat_id is None:
            return
        choice = self._choose_reader_model(data.get("model"))
        if choice is None:
            return
        self._request_pi_warm("doc_chat_model")
        await self.send_payload({"type": "doc_chat_model", "doc_id": self.state.doc_chat_id, "model": choice["id"]})
        log.info("Reader chat for document %d now on %s", self.state.doc_chat_id, choice["model"])


    def _apply_chat_model(self) -> None:
        """Point the current agent conversation's pi turns at Dave's chosen model.

        `prefer` is keyed by conversation id, and the id changes on reset, load and
        delegation pulls (and on a restore that could not resume the record), so this
        runs after each of those and again before every pi turn. A Reader chat keeps its own choice (doc_chat_model); a client that never
        sent `chat_model` is left alone.
        """
        if self.state.chat_model is None or self.state.doc_chat_id is not None:
            return
        pi_brain = getattr(self.ws.app.state, "pi_brain", None)
        if pi_brain is None or self.state.history_session is None:
            return
        choice = chat_model_choice(self.state.chat_model)
        pi_brain.prefer(self.state.history_session.conv_id, choice.get("model"), choice.get("thinking"))


    async def handle_chat_model(self, data: dict):
        """Dave picked a model for the agent (`{"type": "chat_model", "model": <menu id>}`).

        The choice belongs to the connection, not the conversation: it carries over New
        chat, loading a past conversation and reconnect-restore. The ack names what is
        now in effect (an unknown id falls back to the default) and repeats the menu.
        """
        choice = chat_model_choice(data.get("model"))
        self.state.chat_model = choice["id"]
        self._apply_chat_model()
        if self.state.doc_chat_id is None and settings.pi_brain.enabled:
            self._request_pi_warm("chat_model")
        await self.send_payload({
            "type": "chat_model",
            "model": choice["id"],
            "label": choice.get("label") or choice["id"],
            **chat_models_payload(),
        })
        log.info("Agent model set to %s (%s)", choice["id"], choice.get("model") or "shared chain")


    async def handle_doc_chat_position(self, data: dict):
        """Where Dave was listening when he asked: the reader chat's "this"."""
        if self.state.doc_chat_id is None or data.get("doc_id") != self.state.doc_chat_id:
            return
        ci, si = data.get("chunk_index"), data.get("sentence_index")
        if isinstance(ci, int) and isinstance(si, int):
            position = {"chunk_index": ci, "sentence_index": si}
            # A selection: place_context rebuilds its text from the document by index.
            ec, es = data.get("end_chunk_index"), data.get("end_sentence_index")
            if isinstance(ec, int) and isinstance(es, int):
                position.update(end_chunk_index=ec, end_sentence_index=es)
            self.state.doc_chat_position = position
