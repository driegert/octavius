from datetime import datetime

from settings import settings


def _build_system_prompt() -> str:
    now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    return f"{settings.system_prompt}\n\nCurrent date and time: {now}"


class Conversation:
    def __init__(self):
        self._messages: list[dict] = [{"role": "system", "content": _build_system_prompt()}]

    def add_user(self, text: str):
        self._messages.append({"role": "user", "content": text})

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
