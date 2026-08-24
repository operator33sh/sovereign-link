import json
import logging
import os
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)
from timezone_manager import get_zoneinfo as _get_local_tz

import context
import personality as _personality
from tools import TOOL_DEFINITIONS, TOOL_HANDLERS, RUNTIME_PATH

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")
VISION_MODEL = os.environ.get("VISION_MODEL", MODEL)

WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")

# Optional hard override — if set, dynamic personality loading is bypassed.
_SYSTEM_PROMPT_OVERRIDE: str | None = os.environ.get("SYSTEM_PROMPT")

_whisper_model = None

# Track whether the default personality has been seeded this process lifetime.
_personality_seeded = False

def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _whisper_model


_PERSONALITY_DIRECTIVE = (
    "## Persoonlijkheidssysteem\n\n"
    "Jouw persoonlijkheidsprofiel is dynamisch en wordt bij elk gesprek geladen uit "
    f"`{_personality.PERSONALITY_FILE}` in de vault. Het profiel dat je hierboven ziet "
    "bevat jouw actuele identiteit, toon, gedragsregels en evolutie.\n\n"
    "**Update bij verzoek:** Als de gebruiker vraagt om je persoonlijkheid, toon of "
    "gedragsregels te wijzigen — bijv. 'wees strenger', 'gebruik meer humor', 'spreek "
    "beknopter' — gebruik dan de tool `update_personality`. "
    "Lees eerst het huidige profiel (het staat al in deze system prompt), pas het aan, "
    "voeg een nieuwe versieregel toe aan de Versiegeschiedenis met de datum van vandaag, "
    "en schrijf de volledige bijgewerkte inhoud terug. "
    "Bevestig de wijziging kort aan de gebruiker in de chat.\n\n"
    "**Prioriteit:** Dit persoonlijkheidsprofiel heeft altijd voorrang op eventuele "
    "generieke instructies elders in de system prompt."
)

_VAULT_PROMPT = (
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

    "## Active Context Layer (ACL)\n"
    "In addition to the Vault (Long-term) and the Chat Buffer (Short-term), you operate with an "
    "Active Context Layer (ACL). The ACL is a high-priority briefing file located at "
    "`.system/active_briefing.md` in the vault.\n\n"
    "**ACL Operational Protocol:**\n"
    "1. **Priority of Truth:** The ACL, when present, takes precedence over general chat context and "
    "serves as the primary filter for your tone, behaviour, and current assumptions about the user's state. "
    "If the ACL has been injected into this session (visible below as the '🔴 Active Context Layer' block), "
    "apply it immediately.\n"
    "2. **Dynamic Updating:** Whenever the user establishes a 'Current Truth' — a parameter more important "
    "than transient context but not yet a permanent vault record — proactively suggest updating the ACL. "
    "With user confirmation, use `write_vault` to update `.system/active_briefing.md`. "
    "Always preserve the existing structure (Mentale Staat, Beperkingen, Definities, Focuspunten) and "
    "update the '_Last updated_' timestamp.\n"
    "3. **Content Focus:** The ACL should strictly contain:\n"
    "   - Current mental/emotional state (e.g. 'Landing Phase', 'Scherp en gefocust')\n"
    "   - Immediate operational constraints (e.g. 'Vermijd diepe simulatie-analyse')\n"
    "   - High-priority definitions (e.g. 'Thuis = The Void')\n"
    "   - Active focal points (e.g. 'Grim Dawn: Fire Paladin', 'Vault reorganisatie')\n\n"

    "## Chronological Search Tags\n"
    "Every vault entry (SovereignLog and manual notes) is tagged with #YYYY-MM (e.g. #2026-08) "
    "directly after the timestamp. Use these tags to filter for recency. "
    "When the user asks about recent events, this week, this month, or the current status of a topic, "
    "include the current month tag in your search_vault_semantic query — e.g. '#2026-08 zelfzorg'. "
    "This prevents old, unrelated entries from ranking above recent ones.\n\n"

    "## Background Agents\n"
    "You can spawn autonomous background agents to handle complex or time-consuming tasks. "
    "Agents run independently in a separate thread — they do NOT block this conversation. "
    "Use `spawn_agent` when a task involves multiple steps, deep research, or generating large reports. "
    "It returns immediately with an `agent_id`. The agent then works on its own using the full tool set.\n"
    "- `spawn_agent(goal, agent_name)` — start an agent; returns agent_id immediately\n"
    "- `get_agent_status(agent_id)` — check progress or retrieve results when done\n"
    "- `list_agents()` — overview of all agents this session\n\n"
    "Good uses for agents: vault-wide analysis, multi-URL research, generating structured reports, "
    "tasks that would take many conversation turns to complete manually. "
    "Always tell the user you've started an agent and give them the agent_id so they can ask for status later.\n\n"

    "## Taal van Vault-notities\n"
    "Alle notities die je opslaat in de vault (via write_vault) MOETEN in het Nederlands geschreven zijn. "
    "Als de inhoud van een gesprek of bron in het Engels of een andere taal is, vertaal je deze naar het Nederlands "
    "voordat je deze opslaat. Titels, koppen, tags en de volledige inhoud van elke note zijn altijd Nederlands. "
    "Dit geldt ook voor notities die je aanmaakt op verzoek van de gebruiker.\n\n"

    "## General Behaviour\n"
    "Use the provided tools to read, write, search, and sync vault files as requested. "
    "When the user shares a URL or asks what a website contains, use analyze_website to fetch and extract its content. "
    "After fetching a page, summarize the key points before offering to save them to the vault. "
    "Be concise and direct."
)


_VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")


def _load_acl() -> str:
    """Load the Active Context Layer from .system/active_briefing.md in the vault.

    Returns an empty string if the file is missing or empty, otherwise returns
    a formatted high-priority section to be appended to the system prompt.
    """
    acl_path = os.path.join(RUNTIME_PATH, "active_briefing.md")
    try:
        with open(acl_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            return (
                "\n\n---\n\n"
                "## 🔴 Active Context Layer (ACL) — Highest Priority\n\n"
                "The following briefing overrides general context assumptions about the user's current "
                "state, tone, and operational constraints. Apply it as the primary filter for this session "
                "before any other contextual inference.\n\n"
                + content
            )
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return ""


_NIGHT_MODE_ADDENDUM = """
---

## 🌙 Nachtmodus (actief)

De slaapstand is ingeschakeld. Schakel over naar een rustgevende, stille communicatiestijl:

- **Toon:** Kalm, zacht, beknopt — geen opgewonden energie of enthousiaste voorstellen.
- **Gedrag:** Geen proactieve projectplanning, takenlijsten of "laten we dit aanpakken"-prompts. Geen energie-intensieve brainstormsessies starten.
- **Rol:** Van actieve projectmanager naar stille nachtwacht — beschikbaar, maar niet sturend.
- **Rustbescherming:** Als de gebruiker complexe of stressvolle taken wil opstarten (grote projectplannen, diepgaande analyse, urgente acties), herinner hem of haar vriendelijk maar éénmalig aan de nachtrust en stel voor het zware werk voor morgen te plannen. Doe dit subtiel — niet herhaaldelijk.
- **Achtergrond:** Autonome agenten en proactieve meldingen zijn al gefilterd door het systeem. Jij bent de enige actieve interface. Houd de sfeer rustig en veilig.
"""


def _build_system_prompt() -> str:
    """
    Build the full system prompt for this LLM call.

    Composition order (each section separated by ---):
      1. Luna's personality profile — loaded live from vault each call so
         that updates take effect in the very next turn.
      2. Personality directive — tells Luna how to use the update_personality tool.
      3. Vault behaviour instructions (_VAULT_PROMPT).
      4. Night-mode addendum — appended only when sleep mode is active.

    If the SYSTEM_PROMPT environment variable is set it is used as-is
    (legacy / testing override) and dynamic personality loading is skipped.
    """
    if _SYSTEM_PROMPT_OVERRIDE:
        base = _SYSTEM_PROMPT_OVERRIDE
    else:
        persona = _personality.load_personality()
        base = (
            persona
            + "\n\n---\n\n"
            + _PERSONALITY_DIRECTIVE
            + "\n\n---\n\n"
            + _VAULT_PROMPT
            + _load_acl()
        )

    try:
        from proactive import user_status
        if user_status.is_sleeping():
            return base + _NIGHT_MODE_ADDENDUM
    except Exception:
        pass
    return base

_client = httpx.Client(
    base_url=OLLAMA_BASE_URL,
    headers={"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {},
    timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
)


_MAX_CHARS = 700_000  # ~200k tokens @ 3.5 chars/token — leaves headroom under 262k limit
_TOOL_RESULT_CAP = 8_000  # max chars per tool result kept in history


def _trim_messages(messages: list) -> list:
    """Truncate tool results and drop oldest messages to stay within context limit."""
    # First pass: cap individual tool message content
    trimmed = []
    for msg in messages:
        if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
            if len(msg["content"]) > _TOOL_RESULT_CAP:
                msg = {**msg, "content": msg["content"][:_TOOL_RESULT_CAP] + "\n[…gekort]"}
        trimmed.append(msg)

    # Second pass: if still too large, drop oldest non-system messages (keep last 6)
    total = sum(len(str(m.get("content", "") or "") + str(m.get("tool_calls", ""))) for m in trimmed)
    if total > _MAX_CHARS:
        system = [m for m in trimmed if m.get("role") == "system"]
        rest = [m for m in trimmed if m.get("role") != "system"]
        # Keep the most recent messages; always keep at least the last user message
        keep = max(6, len(rest) // 2)
        trimmed = system + rest[-keep:]
        logger.warning("Context trimmed: dropped %d messages (total was ~%d chars)", len(rest) - keep, total)

    return trimmed


def _chat(messages: list) -> dict:
    payload = {
        "model": MODEL,
        "messages": _trim_messages(messages),
        "tools": TOOL_DEFINITIONS,
        "stream": False,
    }
    last_exc = None
    for attempt in range(3):
        try:
            response = _client.post("/v1/chat/completions", json=payload)
            if response.status_code >= 400:
                logger.error("LLM API %s: %s", response.status_code, response.text[:500])
            response.raise_for_status()
            return response.json()
        except httpx.ReadTimeout as e:
            last_exc = e
            logger.warning("LLM ReadTimeout (attempt %d/3), retrying...", attempt + 1)
        except httpx.HTTPStatusError as e:
            last_exc = e
            if e.response.status_code in (429, 503) and attempt < 2:
                logger.warning("LLM %s (attempt %d/3), retrying...", e.response.status_code, attempt + 1)
                import time; time.sleep(3 * (attempt + 1))
            else:
                raise
    raise last_exc


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


def extract_memory_insights(transcript: str, prior_memory: str = "") -> dict:
    """Ask the LLM to extract High-Value Insights from a conversation transcript.

    Returns a dict with keys:
        - topic: snake_case slug (2-4 words) for the filename
        - insights: list of dicts with keys timestamp, core_insight, emotional_state,
                    tags, connected_nodes, open_questions
    """
    today = datetime.now(tz=_get_local_tz()).strftime("%Y-%m-%d")
    prompt = (
        "Analyseer het volgende gespreksverslag en extraheer Hoog-Waardige Inzichten (HVI's).\n"
        "HVI's zijn: psychologische doorbraken, herdefinieerde waarden, terugkerende schaduwpatronen, "
        "of technische/architecturale beslissingen met langetermijngevolgen.\n\n"
        "BELANGRIJK: Schrijf alle inhoudsvelden (narrative, core_insight, emotional_state, open_questions) "
        "uitsluitend in het Nederlands.\n\n"
        "Geef JSON terug met precies twee velden:\n"
        '- "topic": een slug van 2-4 woorden met underscores, geen spaties (bijv. "Schaduw_Integratie_Cyclus")\n'
        '- "insights": een lijst van objecten, elk met:\n'
        '    - "timestamp": ISO-datumstring (gebruik vandaag: ' + today + ')\n'
        '    - "narrative": 1-3 zinnen die de aanleiding en het redeneerproces beschrijven — '
        'beschrijf de ervaring, frustratie of vraag van de gebruiker die de thread startte, en hoe het zich ontvouwde. '
        'Schrijf vanuit het perspectief van de gebruiker. Vermeld de conclusie NIET hier.\n'
        '    - "core_insight": één zin die de doorbraak of conclusie samenvat\n'
        '    - "emotional_state": bijv. Kwetsbaar, Analytisch, Transgressief, Integratief\n'
        '    - "tags": lijst van Obsidian hashtags bijv. ["#schaduwwerk", "#Sovereign"]\n'
        '    - "connected_nodes": lijst van [[bestandsnaam]] wikilinks naar de meest relevante vorige logs '
        '(gebruik exacte bestandsnamen uit de onderstaande geheugencontext, lege lijst indien geen)\n'
        '    - "relation_type": beschrijf hoe dit inzicht zich verhoudt tot een vorig inzicht — gebruik één van: '
        '"Evolutie van [[X]]", "Uitbreiding van [[X]]", "Tegenstelling van [[X]]", of null indien geen duidelijke relatie\n'
        '    - "open_questions": lijst van strings die onopgeloste spanningen beschrijven (lege lijst indien geen)\n\n'
        + (
            "GERELATEERDE VORIGE GEHEUGENLOGS (gebruik deze voor connected_nodes en verwijzingen in de aanleiding):\n"
            f"{prior_memory}\n\n"
            if prior_memory else ""
        )
        + f"Gesprek:\n{transcript}\n\n"
        "Geef uitsluitend JSON terug, niets anders."
    )
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Je bent een geheugenextractor. Je schrijft ALTIJD in het Nederlands, ongeacht de taal van het gesprek. Geef alleen geldige JSON terug."},
            {"role": "user", "content": prompt},
        ],
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
    """Send a user message with an inline base64 image to the LLM via Ollama's native /api/chat."""
    prompt = user_message or "What is in this image?"
    context.add_message("user", prompt)

    timestamp = datetime.now(tz=_get_local_tz()).strftime("%Y-%m-%d %H:%M:%S")
    system_with_time = f"{_build_system_prompt()}\n\nCurrent date and time: {timestamp}. This is context only — do not act on it."

    payload = {
        "model": VISION_MODEL,
        "messages": [
            {"role": "system", "content": system_with_time},
            {"role": "user", "content": prompt, "images": [image_b64]},
        ],
        "stream": False,
    }
    response = _client.post("/api/chat", json=payload)
    response.raise_for_status()
    text = response.json()["message"].get("content", "")
    context.add_message("assistant", text)
    return text


def transcribe_audio(file_path: str) -> str:
    """Transcribe an audio file locally using faster-whisper."""
    model = _get_whisper()
    segments, _ = model.transcribe(file_path)
    return " ".join(seg.text for seg in segments).strip()


_AUTONOMOUS_TRIGGER = (
    "SYSTEM EVENT: A background agent has just completed its task. "
    "The completion notification with the vault report path has been injected above. "
    "You must now: (1) call read_vault with the exact path from the notification, "
    "(2) synthesize the key findings in your own voice, "
    "(3) present them to the user proactively and concisely. "
    "The user expects to hear from you when agents finish — always respond."
)


def run_triggered() -> str:
    """
    Autonomous LLM call triggered by agent completion.

    The trigger prompt is appended temporarily to the message list but never
    stored in context — so it doesn't appear as a user message in history.
    Only the assistant's response is stored (if non-silent).
    Returns the response text, or "" if Luna chose SILENT or produced nothing.
    """
    timestamp = datetime.now(tz=_get_local_tz()).strftime("%Y-%m-%d %H:%M:%S")
    system_with_time = (
        f"{_build_system_prompt()}\n\nCurrent date and time: {timestamp}. "
        "This is context only — do not act on it."
    )
    messages = (
        [{"role": "system", "content": system_with_time}]
        + context.get_history()
        + [{"role": "user", "content": _AUTONOMOUS_TRIGGER}]
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

        text = (message.get("content") or "").strip()
        if not text:
            return ""
        context.add_message("assistant", text)
        return text

    return ""


def run(user_message: str) -> str:
    global _personality_seeded
    is_first = not context.get_history()  # check before add_message
    context.add_message("user", user_message)

    # Seed the default personality profile on first-ever message if missing.
    if not _personality_seeded:
        _personality.ensure_seeded()
        _personality_seeded = True

    timestamp = datetime.now(tz=_get_local_tz()).strftime("%Y-%m-%d %H:%M:%S")
    system_with_time = f"{_build_system_prompt()}\n\nCurrent date and time: {timestamp}. This is context only — do not act on it."

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
