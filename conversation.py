import base64
import logging
import mimetypes
from datetime import datetime
from pathlib import Path

from settings import settings

log = logging.getLogger(__name__)

# Cap on how many of the most recent image attachments get rehydrated back
# into base64 content arrays when restoring a conversation from history (see
# load_from_history). Keeps the first payload of a re-attached, image-heavy
# thread bounded; older images stay as plain-text placeholders.
MAX_REHYDRATED_IMAGES = 3


def _build_system_prompt() -> str:
    now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    return f"{settings.system_prompt}\n\nCurrent date and time: {now}"


class Conversation:
    def __init__(self):
        self._messages: list[dict] = [{"role": "system", "content": _build_system_prompt()}]
        self.has_images = False

    def add_user(self, content: str | list[dict]):
        """Add a user turn. ``content`` is normally plain text, but may be an
        OpenAI-style content array (``[{"type": "text", ...}, {"type":
        "image_url", ...}]``) for a multimodal (image) turn. When ``content``
        is a list, ``has_images`` is set True and stays True for the rest of
        the thread (until ``reset()``): once a thread has seen an image, it
        stays on the vision chain and keeps the image content array in
        memory — see agent.py's ``use_vision`` handling in
        ``stream_agent_turn``.
        """
        if isinstance(content, list):
            self.has_images = True
        self._messages.append({"role": "user", "content": content})

    def replace_last_user_content(self, text: str) -> None:
        """Replace the most recent user message's content with plain text.

        Not called automatically anymore — image-bearing turns now keep
        their content array in memory for the life of the thread (see
        ``add_user``). Kept as a utility for callers that need to swap a
        message's content out explicitly (e.g. a rollback path).
        """
        for message in reversed(self._messages):
            if message.get("role") == "user":
                message["content"] = text
                return

    def add_assistant(self, text: str):
        self._messages.append({"role": "assistant", "content": text})

    def add_tool_call(self, tool_call_id: str, name: str, arguments_str: str):
        self._messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments_str},
                    }
                ],
            }
        )

    def add_tool_result(self, tool_call_id: str, content: str):
        self._messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
            }
        )

    def get_messages(self) -> list[dict]:
        return list(self._messages)

    def trim(self):
        """Keep system prompt + last N messages to stay within context."""
        non_system = self._messages[1:]
        if len(non_system) > settings.max_conversation_messages:
            self._messages = [self._messages[0]] + non_system[-settings.max_conversation_messages:]

    def reset(self):
        self._messages = [{"role": "system", "content": _build_system_prompt()}]
        self.has_images = False

    def load_from_history(self, messages: list[dict]):
        """Restore conversation state from history DB messages.

        Accepts the format returned by history_store.get_conversation_messages().
        Skips tool-role messages (they were part of the agent loop, the LLM
        doesn't need them to continue the conversation).

        User messages carrying an image attachment (``message["attachments"]``,
        see history_store.get_conversation_messages) are rehydrated back into
        an OpenAI-style multimodal content array by reading the spooled image
        file off disk and base64-encoding it, so a re-attached thread doesn't
        forget an image it already saw. Only the ``MAX_REHYDRATED_IMAGES``
        most recent image attachments in the window are rehydrated — older
        ones stay as their plain-text placeholder. A missing file, read
        error, or non-image mime type degrades silently back to the
        placeholder text (logged at debug level only; message bodies/captions
        are never logged). ``has_images`` is set True only when at least one
        rehydration actually succeeds.
        """
        self._messages = [{"role": "system", "content": _build_system_prompt()}]
        self.has_images = False

        image_message_indices = [
            i for i, msg in enumerate(messages)
            if msg.get("role") == "user"
            and any(a.get("type") == "image" for a in (msg.get("attachments") or []))
        ]
        rehydrate_indices = set(image_message_indices[-MAX_REHYDRATED_IMAGES:])

        for i, msg in enumerate(messages):
            role = msg.get("role")
            content = msg.get("content", "")
            if role not in ("user", "assistant") or not content:
                continue
            if role == "user" and i in rehydrate_indices:
                content = self._rehydrate_image_content(content, msg.get("attachments") or [])
            self._messages.append({"role": role, "content": content})
        self.trim()

    def _rehydrate_image_content(self, text: str, attachments: list[dict]):
        """Best-effort: turn a text placeholder + image attachment row back
        into a multimodal content array. Returns ``text`` unchanged on any
        failure (missing file, unreadable, non-image mime)."""
        image_attachment = next(
            (a for a in attachments if a.get("type") == "image"), None
        )
        if image_attachment is None:
            return text
        reference = image_attachment.get("reference")
        try:
            path = Path(reference)
            mime, _ = mimetypes.guess_type(reference)
            if not mime or not mime.startswith("image/"):
                return text
            if not path.exists():
                return text
            data = path.read_bytes()
        except (OSError, TypeError, ValueError):
            log.debug(
                "Failed to rehydrate image attachment during history re-attach",
                exc_info=True,
            )
            return text

        b64 = base64.b64encode(data).decode("ascii")
        self.has_images = True
        return [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]
