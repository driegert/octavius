from datetime import datetime

from settings import settings


def _build_system_prompt() -> str:
    now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    return f"{settings.system_prompt}\n\nCurrent date and time: {now}"


class Conversation:
    def __init__(self):
        self._messages: list[dict] = [{"role": "system", "content": _build_system_prompt()}]

    def add_user(self, content: str | list[dict]):
        """Add a user turn. ``content`` is normally plain text, but may be an
        OpenAI-style content array (``[{"type": "text", ...}, {"type":
        "image_url", ...}]``) for a multimodal (image) turn. Multimodal
        content is expected to be downgraded back to text via
        ``replace_last_user_content`` once the turn completes — see
        agent.py's ``use_vision`` handling in ``stream_agent_turn``.
        """
        self._messages.append({"role": "user", "content": content})

    def replace_last_user_content(self, text: str) -> None:
        """Downgrade the most recent user message's content to plain text.

        Used after a multimodal (image) turn finishes: the content array
        (which holds a base64 data URL) is swapped for a short text
        placeholder. This keeps later turns on the default text-only chain
        (the simpler of the two documented options — see agent.py), keeps
        conversation payloads from growing unbounded with inline image
        bytes, and ensures only text ever reaches persisted history / the
        memory extractor's trust boundary.
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

    def load_from_history(self, messages: list[dict]):
        """Restore conversation state from history DB messages.

        Accepts the format returned by history.get_conversation_messages().
        Skips tool-role messages (they were part of the agent loop, the LLM
        doesn't need them to continue the conversation).
        """
        self._messages = [{"role": "system", "content": _build_system_prompt()}]
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                self._messages.append({"role": role, "content": content})
        self.trim()
