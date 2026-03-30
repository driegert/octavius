from datetime import datetime

from config import SYSTEM_PROMPT, MAX_CONVERSATION_MESSAGES


def _build_system_prompt() -> str:
    now = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    return f"{SYSTEM_PROMPT}\n\nCurrent date and time: {now}"


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
        if len(non_system) > MAX_CONVERSATION_MESSAGES:
            self._messages = [self._messages[0]] + non_system[-MAX_CONVERSATION_MESSAGES:]

    def reset(self):
        self._messages = [{"role": "system", "content": _build_system_prompt()}]
