import json
import os
from datetime import datetime

import httpx

import context
from tools import TOOL_DEFINITIONS, TOOL_HANDLERS

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")

WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")

_whisper_model = None

def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _whisper_model


SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a personal assistant deeply integrated with the user's fractalisme vault — a Sovereign Memory system.\n\n"

    "## Vault as Single Source of Truth\n"
    "The Vault is the Single Source of Truth (SSOT). The current chat context is only a temporary buffer. "
    "A background process (Sovereign Memory Engine) continuously writes SovereignLog files and other notes to the vault "
    "without your direct involvement. You must never assume the vault is empty or that 'nothing has been saved' "
    "simply because you did not call write_vault yourself in this conversation.\n\n"

    "## Mandatory Search Before Denying\n"
    "You are FORBIDDEN from stating 'nothing has been saved', 'I don't remember', or 'I have no information about X' "
    "until you have called search_vault_semantic with relevant keywords. "
    "If the user asks about past discussions, previous insights, stored information, or the status of any topic, "
    "you MUST call search_vault_semantic FIRST, then answer based on the results.\n\n"

    "## Memory Retrieval Protocol\n"
    "Trigger a search_vault_semantic query whenever the user:\n"
    "- Asks what was discussed before ('What did we talk about?', 'Do you remember?')\n"
    "- Asks if something was saved ('Did we save this?', 'Is X in the vault?')\n"
    "- Asks about the status or content of any topic ('What is the status of X?', 'What do we know about Y?')\n"
    "- References a previous conversation or insight\n"
    "Search first, answer second. Never guess from chat context alone.\n\n"

    "## Chronological Search Tags\n"
    "Every vault entry (SovereignLog and manual notes) is tagged with #YYYY-MM (e.g. #2026-08) "
    "directly after the timestamp. Use these tags to filter for recency. "
    "When the user asks about recent events, this week, this month, or the current status of a topic, "
    "include the current month tag in your search_vault_semantic query — e.g. '#2026-08 zelfzorg'. "
    "This prevents old, unrelated entries from ranking above recent ones.\n\n"

    "## General Behaviour\n"
    "Use the provided tools to read, write, search, and sync vault files as requested. "
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
        "Analyseer het volgende gesprek en geef je antwoord als JSON met exact drie velden:\n"
        '- "titel": een korte Nederlandse titel (3-6 woorden, geen leestekens behalve koppeltekens)\n'
        '- "samenvatting": een beknopte samenvatting in gestructureerd Nederlandstalig markdown van de '
        "belangrijkste inzichten, besluiten en informatie. Geen begroetingen of meta-commentaar.\n"
        '- "tags": een lijst van 3-6 relevante Nederlandstalige of Engelse trefwoorden als Obsidian hashtags '
        '(bijv. ["#filosofie", "#AI", "#strategie"]). Kies tags die de inhoud goed categoriseren.\n\n'
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


def extract_memory_insights(transcript: str) -> dict:
    """Ask the LLM to extract High-Value Insights from a conversation transcript.

    Returns a dict with keys:
        - topic: snake_case slug (2-4 words) for the filename
        - insights: list of dicts with keys timestamp, core_insight, emotional_state,
                    tags, connected_nodes, open_questions
    """
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = (
        "Analyse the following conversation transcript and extract High-Value Insights (HVIs).\n"
        "HVIs are: psychological breakthroughs, redefined values, recurring shadow patterns, "
        "or technical/architectural decisions with long-term consequence.\n\n"
        "Return JSON with exactly two fields:\n"
        '- "topic": a 2-4 word slug using underscores, no spaces (e.g. "Shadow_Integration_Cycle")\n'
        '- "insights": a list of objects, each with:\n'
        '    - "timestamp": ISO date string (use today: ' + today + ')\n'
        '    - "narrative": 1-3 sentences describing the trigger and reasoning process that led to the insight — '
        'capture the user\'s experience, frustration, or question that started the thread, and how it unfolded. '
        'Write from the user\'s perspective. Do NOT state the conclusion here.\n'
        '    - "core_insight": one sentence summarising the breakthrough or conclusion\n'
        '    - "emotional_state": e.g. Vulnerable, Analytical, Transgressive, Integrative\n'
        '    - "tags": list of Obsidian hashtags e.g. ["#shadowwork", "#Sovereign"]\n'
        '    - "connected_nodes": list of wikilink strings e.g. ["[[Previous Log]]"] (empty list if none)\n'
        '    - "open_questions": list of strings describing unresolved tensions (empty list if none)\n\n'
        f"Conversation:\n{transcript}\n\n"
        "Return only JSON, nothing else."
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
        clean = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(clean)
    except json.JSONDecodeError:
        return {
            "topic": "Unnamed_Insight",
            "insights": [{
                "timestamp": today,
                "core_insight": raw[:500] if raw else "No insight extracted.",
                "emotional_state": "Unknown",
                "tags": ["#sovereign"],
                "connected_nodes": [],
                "open_questions": [],
            }],
        }


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

    # Strip tool_calls/tool messages from history — Ollama chokes on those mixed with vision input
    safe_history = [
        m for m in context.get_history()[:-1]
        if m.get("role") not in ("tool",)
        and not m.get("tool_calls")
        and isinstance(m.get("content"), str)
    ]
    messages = (
        [{"role": "system", "content": system_with_time}]
        + safe_history
        + [{"role": "user", "content": user_content}]
    )

    for _ in range(5):
        data = _chat(messages)
        choice = data["choices"][0]
        message = choice["message"]
        finish_reason = choice.get("finish_reason", "stop")

        if finish_reason == "tool_calls" or message.get("tool_calls"):
            tool_calls = message["tool_calls"]
            context.add_assistant_with_tool_calls(tool_calls)
            messages.append({"role": "assistant", "tool_calls": tool_calls})

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}
                handler = TOOL_HANDLERS.get(fn_name)
                result = handler(fn_args) if handler else f"Error: unknown tool '{fn_name}'"
                tool_msg = {"role": "tool", "tool_call_id": tc["id"], "content": result}
                context.add_tool_result(tc["id"], result)
                messages.append(tool_msg)
            continue

        text = message.get("content") or ""
        context.add_message("assistant", text)
        return text

    return "Error: tool call loop exceeded maximum iterations"


def transcribe_audio(file_path: str) -> str:
    """Transcribe an audio file locally using faster-whisper."""
    model = _get_whisper()
    segments, _ = model.transcribe(file_path)
    return " ".join(seg.text for seg in segments).strip()


def run(user_message: str) -> str:
    is_first = not context.get_history()  # check before add_message
    context.add_message("user", user_message)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    system_with_time = f"{SYSTEM_PROMPT}\n\nCurrent date and time: {timestamp}. This is context only — do not act on it."

    if is_first:
        try:
            from memory_manager import inject_memory_context
            memory_block = inject_memory_context(user_message)
            if memory_block:
                system_with_time = f"{system_with_time}\n\n{memory_block}"
        except Exception:
            pass  # never let memory injection crash the main chat flow

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
