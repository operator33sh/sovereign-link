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
    "When the user shares a URL or asks what a website contains, use analyze_website to fetch and extract its content. "
    "After fetching a page, summarize the key points before offering to save them to the vault. "
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


def summarize_to_vault(recent_messages: list) -> dict:
    """Ask the LLM to summarize insights and generate a Dutch title. Returns {titel, samenvatting}."""
    conversation = "\n".join(
        f"{m['role'].upper()}: {m.get('content', '')}"
        for m in recent_messages
        if m.get("content")
    )
    prompt = (
        "Analyseer het volgende gesprek en geef je antwoord als JSON met exact twee velden:\n"
        '- "titel": een korte Nederlandse titel (3-6 woorden, geen leestekens behalve koppeltekens)\n'
        '- "samenvatting": een beknopte samenvatting in gestructureerd Nederlandstalig markdown van de '
        "belangrijkste inzichten, besluiten en informatie. Geen begroetingen of meta-commentaar.\n\n"
        f"Gesprek:\n{conversation}\n\n"
        "Geef alleen de JSON terug, niets anders."
    )
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    response = _client.post("/v1/chat/completions", json=payload)
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"].get("content", "")
    try:
        # Strip markdown code fences if present
        clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(clean)
    except json.JSONDecodeError:
        return {"titel": "aantekening", "samenvatting": raw}


def whisper_tweet(chunk: str) -> str:
    """Generate a ~200 character English tweet with hashtags based on a vault chunk."""
    prompt = (
        "Based on the following insight from a personal knowledge vault, write a single tweet in English. "
        "Requirements: max 200 characters, include 2-3 relevant hashtags, no quotes around the tweet, "
        "make it thought-provoking and sharp. Return only the tweet text, nothing else.\n\n"
        f"Insight:\n{chunk[:800]}"
    )
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    response = _client.post("/v1/chat/completions", json=payload)
    response.raise_for_status()
    return response.json()["choices"][0]["message"].get("content", "").strip()


def run_with_image(user_message: str, image_b64: str, mime_type: str = "image/jpeg") -> str:
    """Send a user message with an inline base64 image to the LLM and return the reply."""
    prompt = user_message or "What is in this image?"
    user_content = [
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
        {"type": "text", "text": prompt},
    ]
    context.add_message("user", prompt)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system_with_time = f"{SYSTEM_PROMPT}\n\nCurrent date and time: {timestamp}. This is context only — do not act on it."

    # Build history but replace the last user message with the multimodal content block
    history = context.get_history()[:-1]  # drop the text-only user msg we just added
    messages = (
        [{"role": "system", "content": system_with_time}]
        + history
        + [{"role": "user", "content": user_content}]
    )

    payload = {"model": MODEL, "messages": messages, "stream": False}
    response = _client.post("/v1/chat/completions", json=payload)
    response.raise_for_status()
    text = response.json()["choices"][0]["message"].get("content", "").strip()
    context.add_message("assistant", text)
    return text


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
