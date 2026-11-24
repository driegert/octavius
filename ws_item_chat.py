"""Inbox item chat frames for a WebSocket session.

A mixin composed into ``websocket_session.WebSocketSessionHandler``; the
session state lives on ``self.state`` (see ``websocket_session``).
"""
from __future__ import annotations

import logging
import time

from conversation import Conversation
from settings import settings

log = logging.getLogger(__name__)


_ITEM_CHAT_MAX_CHARS = 6000


def build_item_chat_context(item: dict, item_id: int) -> str:
    # The saved_items stack is retiring and this legacy "chat about a saved item"
    # flow no longer has a fetch tool, so inline the (capped) content directly.
    content = item.get("content") or ""
    if len(content) > _ITEM_CHAT_MAX_CHARS:
        body = content[:_ITEM_CHAT_MAX_CHARS] + "\n\n...(content truncated)"
    else:
        body = content
    return (
        f"\n\nYou are discussing saved item #{item_id} with Dave.\n"
        f"Title: {item['title']}\nType: {item['item_type']}\n\n"
        f"Content:\n{body}"
    )


def create_item_conversation(item: dict, item_id: int) -> Conversation:
    conversation = Conversation()
    conversation._messages[0]["content"] += build_item_chat_context(item, item_id)
    return conversation


class ItemChatMixin:
    async def handle_item_chat_load(self, data: dict):
        from history import (
            get_conversation_messages,
            get_item_chat_conversation_id,
            get_saved_item,
            set_item_chat_conversation,
        )
        item_id = data.get("item_id")
        with self.state.history.connect() as conn:
            item = get_saved_item(conn, item_id)
            chat_conv_id = get_item_chat_conversation_id(conn, item_id)
            msgs = get_conversation_messages(conn, chat_conv_id) if chat_conv_id else []

        if not item:
            await self.send_payload(
                {
                    "type": "item_chat_status",
                    "item_id": item_id,
                    "text": "Item not found.",
                }
            )
            return

        if chat_conv_id:
            conversation = create_item_conversation(item, item_id)
            for message in msgs:
                if message["role"] in ("user", "assistant") and message.get("content"):
                    conversation._messages.append({"role": message["role"], "content": message["content"]})
            conversation.trim()
            self.state.item_conversations[item_id] = conversation
            history_session = self.state.history.start_conversation(
                service="octavius",
                source="inbox_chat",
                model=settings.llm_chain[0]["model"],
            )
            self.state.item_history_sessions[item_id] = history_session
            with self.state.history.connect() as conn:
                set_item_chat_conversation(conn, item_id, history_session.conv_id)
            history_pairs = [
                {"role": message["role"], "content": message["content"]}
                for message in msgs
                if message["role"] in ("user", "assistant") and message.get("content")
            ]
            await self.send_payload(
                {
                    "type": "item_chat_loaded",
                    "item_id": item_id,
                    "messages": history_pairs,
                }
            )
            return

        self.state.item_conversations[item_id] = create_item_conversation(item, item_id)
        history_session = self.state.history.start_conversation(
            service="octavius",
            source="inbox_chat",
            model=settings.llm_chain[0]["model"],
        )
        self.state.item_history_sessions[item_id] = history_session
        with self.state.history.connect() as conn:
            set_item_chat_conversation(conn, item_id, history_session.conv_id)
        await self.send_payload({"type": "item_chat_loaded", "item_id": item_id, "messages": []})


    async def handle_item_chat(self, data: dict):
        from agent import stream_agent_turn
        item_id = data.get("item_id")
        user_text = data.get("text", "").strip()
        if not user_text or item_id not in self.state.item_conversations:
            return

        conversation = self.state.item_conversations[item_id]
        history_session = self.state.item_history_sessions.get(item_id)
        await self.send_payload({"type": "item_chat_status", "item_id": item_id, "text": "Thinking..."})
        if history_session:
            await history_session.add_message_async(role="user", content=user_text)

        turn_start = time.monotonic()

        async def item_status_cb(text: str):
            await self.send_payload({"type": "item_chat_status", "item_id": item_id, "text": text})

        full_parts = []
        try:
            async for sentence in stream_agent_turn(
                conversation,
                self.state.mcp_manager,
                user_text,
                status_callback=item_status_cb,
                history_session=history_session,
                session=self,
                source="inbox_chat",
            ):
                full_parts.append(sentence)
        except Exception as exc:
            log.exception("Item chat agent failed")
            await self.send_payload(
                {
                    "type": "item_chat_response",
                    "item_id": item_id,
                    "text": f"Error: {exc}",
                }
            )
            return

        full_reply = "".join(full_parts).strip()
        if history_session and full_reply:
            latency_ms = int((time.monotonic() - turn_start) * 1000)
            await history_session.add_message_async(
                role="assistant",
                content=full_reply,
                model=settings.llm_chain[0]["model"],
                latency_ms=latency_ms,
            )

        await self.send_payload(
            {
                "type": "item_chat_response",
                "item_id": item_id,
                "text": full_reply or "I'm not sure how to respond to that.",
            }
        )


    async def handle_item_chat_reset(self, data: dict):
        from history import get_saved_item, set_item_chat_conversation
        item_id = data.get("item_id")
        old_session = self.state.item_history_sessions.pop(item_id, None)
        if old_session:
            await old_session.end_async()
        self.state.item_conversations.pop(item_id, None)

        with self.state.history.connect() as conn:
            item = get_saved_item(conn, item_id)
        if item:
            conversation = create_item_conversation(item, item_id)
            self.state.item_conversations[item_id] = conversation
            history_session = self.state.history.start_conversation(
                service="octavius",
                source="inbox_chat",
                model=settings.llm_chain[0]["model"],
            )
            self.state.item_history_sessions[item_id] = history_session
            with self.state.history.connect() as conn:
                set_item_chat_conversation(conn, item_id, history_session.conv_id)

        await self.send_payload({"type": "item_chat_loaded", "item_id": item_id, "messages": []})
