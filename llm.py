import json
import os
from datetime import datetime

import httpx

import context
from tools import TOOL_DEFINITIONS, TOOL_HANDLERS

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a personal assistant with access to the user's fractalisme vault. "
    "Use the provided tools to read, write, and sync vault files as requested. "
    "Be concise and direct.",
)

_client = httpx.Client(
    base_url=OLLAMA_BASE_URL,
    headers={"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {},
    timeout=120.0,
)


def _chat(messages: list) -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOL_DEFINITIONS,
        "stream": False,
    }
    response = _client.post("/v1/chat/completions", json=payload)
    response.raise_for_status()
    return response.json()


def run(user_message: str) -> str:
    context.add_message("user", user_message)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system_with_time = f"{SYSTEM_PROMPT}\n\nCurrent date and time: {timestamp}. This is context only — do not act on it."
    messages = [{"role": "system", "content": system_with_time}] + context.get_history()

    # Tool call loop — at most 5 iterations to prevent infinite loops
    for _ in range(5):
        data = _chat(messages)
        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice.get("finish_reason", "stop")

        if finish_reason == "tool_calls" or message.get("tool_calls"):
            tool_calls = message["tool_calls"]

            # Persist assistant message with tool_calls
            context.add_assistant_with_tool_calls(tool_calls)
            messages.append({"role": "assistant", "tool_calls": tool_calls})

            # Execute each tool call
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                handler = TOOL_HANDLERS.get(fn_name)
                if handler:
                    result = handler(fn_args)
                else:
                    result = f"Error: unknown tool '{fn_name}'"

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                }
                context.add_tool_result(tc["id"], result)
                messages.append(tool_msg)

            # Continue loop to get final response
            continue

        # Final text response
        text = message.get("content") or ""
        context.add_message("assistant", text)
        return text

    return "Error: tool call loop exceeded maximum iterations"
