import json
import logging
import os
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MAX_MESSAGES = 20
PERSIST_MESSAGES = 10  # 5 heen-en-weer exchanges
HISTORY_FILE = os.environ.get("HISTORY_FILE", os.path.join(os.path.dirname(__file__), "conversation_history.json"))

# Single-user session context held in memory
_history: deque = deque(maxlen=MAX_MESSAGES)


def _save() -> None:
    try:
        recent = list(_history)[-PERSIST_MESSAGES:]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(recent, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Failed to save conversation history")


def _load() -> None:
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            messages = json.load(f)
        for msg in messages:
            _history.append(msg)
        logger.info("Restored %d messages from %s", len(messages), HISTORY_FILE)
    except Exception:
        logger.exception("Failed to load conversation history")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_message(role: str, content: str) -> None:
    _history.append({"role": role, "content": content, "timestamp": _now()})
    _save()


_MAX_TOOL_CONTENT = 8_000  # chars — prevents history bloat from large vault searches


def add_tool_result(tool_call_id: str, content: str) -> None:
    if len(content) > _MAX_TOOL_CONTENT:
        content = content[:_MAX_TOOL_CONTENT] + "\n[…gekort]"
    _history.append({"role": "tool", "tool_call_id": tool_call_id, "content": content, "timestamp": _now()})
    _save()


def add_assistant_with_tool_calls(tool_calls: list) -> None:
    _history.append({"role": "assistant", "tool_calls": tool_calls, "timestamp": _now()})
    _save()


def get_history() -> list:
    result = []
    for msg in _history:
        entry = {k: v for k, v in msg.items() if k != "timestamp"}
        ts = msg.get("timestamp")
        if ts and isinstance(entry.get("content"), str):
            entry["content"] = f"{entry['content']}\n[{ts}]"
        result.append(entry)
    return result


def clear() -> None:
    _history.clear()
    _save()


# Restore history on module load
_load()
