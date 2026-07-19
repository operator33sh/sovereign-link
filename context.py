from collections import deque

MAX_MESSAGES = 20

# Single-user session context held in memory
_history: deque = deque(maxlen=MAX_MESSAGES)


def add_message(role: str, content: str) -> None:
    _history.append({"role": role, "content": content})


def add_tool_result(tool_call_id: str, content: str) -> None:
    _history.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})


def add_assistant_with_tool_calls(tool_calls: list) -> None:
    _history.append({"role": "assistant", "tool_calls": tool_calls})


def get_history() -> list:
    return list(_history)


def clear() -> None:
    _history.clear()
