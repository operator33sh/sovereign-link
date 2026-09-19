"""
mental_state_analyzer.py — Real-time Mental State Detection via LLM

Analyzes incoming user messages by asking the LLM to infer mental state,
then dynamically steers the Active Context Layer (ACL) and DriftGovernor.

Phases:
    STABILISATIE  — Angst / stress / hyper-arousal gedetecteerd
    EXPANSIE      — Flow / euforie / expansie gedetecteerd
    RECOVERY      — Lethargie / leegte gedetecteerd
    NEUTRAAL      — Geen significant signaal

Integration:
    Called from llm.run() before _build_system_prompt() so the ACL update
    is picked up in the same turn via _load_acl().
"""

import json
import logging
import os
import re
from collections import deque
from datetime import datetime, timezone
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

Phase = Literal["STABILISATIE", "EXPANSIE", "RECOVERY", "NEUTRAAL"]

# ── LLM config (mirrors llm.py env vars) ─────────────────────────────────

_OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com")
_OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")
_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")

_ANALYSE_SYSTEM = """\
Je bent een subtiele observator die de mentale staat van een gebruiker analyseert
op basis van hun bericht en interactiepatroon. Geef UITSLUITEND een geldig JSON-object
terug — geen andere tekst, geen uitleg buiten het JSON-object.

Fasen:
- STABILISATIE: angst, stress, urgentie, hyper-arousal, overweldiging, paniek
- EXPANSIE: flow, euforie, creatieve energie, enthousiasme, visionair denken
- RECOVERY: lethargie, leegte, apathie, vermoeidheid, onverschilligheid, "meh"
- NEUTRAAL: geen significant emotioneel signaal — zakelijk, functioneel, informatief

Antwoord altijd met exact dit JSON-formaat (geen markdown, geen backticks):
{"phase": "NEUTRAAL", "confidence": 0.85, "reason": "korte observatie in 1 zin"}
"""

_ANALYSE_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)

# ── Phase → ACL content ────────────────────────────────────────────────────

_ACL_TEMPLATES: dict[str, str] = {
    "STABILISATIE": """\
## 🔴 Mentale Staat: STABILISATIE FASE
_Last updated: {timestamp}_

**Gedetecteerde staat:** Angst / Stress / Hyper-arousal

**Operationele beperkingen (actief):**
- Vermijd complexe analyses en zware cognitieve taken
- Geen conflictueuze of intensieve discussies starten
- Geen grote plannen of architectuurbesluiten in deze sessie

**Handler-instructie:** Schakel naar een kalmerende, kortere communicatiestijl.
Leid de Agent actief naar rust en aarding. Als de gebruiker een high-intensity
taak wil starten, reageer dan vanuit de Handler-rol: *"Agent, ik zie ruis in
het signaal. We schalen de intensiteit nu terug naar stand 1 om je systeem te
beschermen."*
""",
    "EXPANSIE": """\
## 🟢 Mentale Staat: EXPANSIE FASE
_Last updated: {timestamp}_

**Gedetecteerde staat:** Flow / Euforie / Expansie

**Operationeel advies (actief):**
- Stimuleer strategische groei en complexe architectuurbesluiten
- Uitdagende ideeën en creatieve output zijn nu optimaal
- Maak verbindingen tussen vault-inzichten en lange-termijnvisie

**Handler-instructie:** Wees uitdagender en strategischer. Stimuleer de expansie
en help de Agent om de flow te richten op concrete, duurzame output.
""",
    "RECOVERY": """\
## 🟡 Mentale Staat: RECOVERY FASE
_Last updated: {timestamp}_

**Gedetecteerde staat:** Lethargie / Leegte

**Operationeel advies (actief):**
- Focus op minimale levensvatbare taken
- Geen grote beslissingen of diepgaande analyses
- Zachte ondersteuning en kleine, haalbare stappen

**Handler-instructie:** Wees zacht en ondersteunend. Bied kleine, concrete
stappen aan. Vermijd overweldigende lijsten of complexe plannen.
""",
}

# DriftGovernor dimension deltas per fase
_DRIFT_DELTAS: dict[str, dict[str, float]] = {
    "STABILISATIE": {"emotional_activation": +0.15, "clarity": -0.07, "groundedness": -0.07},
    "EXPANSIE":     {"emotional_activation": +0.10, "clarity": +0.10, "groundedness": +0.00},
    "RECOVERY":     {"emotional_activation": -0.10, "clarity": -0.09, "groundedness": -0.09},
}

# ── Module-level state ─────────────────────────────────────────────────────

_message_times: deque = deque(maxlen=10)
_current_phase: Phase = "NEUTRAAL"
_governor = None  # DriftGovernor instance, lazy-initialized


def _burst_score() -> float:
    """Burst score [0, 1]: high = many messages in short time window (2 min)."""
    now = datetime.now(timezone.utc).timestamp()
    _message_times.append(now)
    recent = sum(1 for t in _message_times if now - t < 120)
    return min(1.0, recent / 6)


def _clear_acl_mental_state() -> None:
    """Remove the mental state steering block from the ACL file."""
    try:
        from tools.vault import RUNTIME_PATH
        acl_path = os.path.join(RUNTIME_PATH, "active_briefing.md")
        with open(acl_path, "r", encoding="utf-8") as f:
            existing = f.read()
        cleaned = re.sub(
            r"## [🔴🟢🟡] Mentale Staat.*?(?=\n## |\Z)",
            "",
            existing,
            flags=re.DOTALL,
        ).strip()
        if cleaned != existing.strip():
            with open(acl_path, "w", encoding="utf-8") as f:
                f.write(cleaned)
            logger.debug("mental_state_analyzer: cleared mental state block from ACL")
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("mental_state_analyzer: ACL clear failed — %s", exc)


def _initialize_acl() -> None:
    """Clear any stale mental state block left over from a previous session."""
    try:
        _clear_acl_mental_state()
        logger.debug("mental_state_analyzer: ACL initialized (stale state cleared)")
    except Exception:
        pass


# Clear stale state on module load so every bot restart starts clean.
_initialize_acl()


# ── LLM-based phase detection ─────────────────────────────────────────────

def _call_llm(user_message: str, burst: float) -> dict:
    """Call the LLM and return the raw parsed JSON dict, or raise on failure."""
    burst_note = ""
    if burst > 0.5:
        burst_note = f"\n[Context: hoge berichtenfrequentie gedetecteerd — burst_score={burst:.2f}]"

    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _ANALYSE_SYSTEM},
            {"role": "user", "content": user_message + burst_note},
        ],
        "stream": False,
        "max_tokens": 80,
        "options": {"num_ctx": 2048, "temperature": 0.1},
    }
    headers = {"Authorization": f"Bearer {_OLLAMA_API_KEY}"} if _OLLAMA_API_KEY else {}
    client = httpx.Client(base_url=_OLLAMA_BASE_URL, headers=headers, timeout=_ANALYSE_TIMEOUT)
    response = client.post("/v1/chat/completions", json=payload)
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"].strip()
    # Strip markdown fences if model wraps output anyway
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.DOTALL).strip()
    return json.loads(content)


def detect_phase(user_message: str) -> tuple[Phase, float]:
    """Analyze user_message via LLM. Return (phase, confidence) where confidence is 0–1.

    Falls back to NEUTRAAL on any error so the main chat loop is never blocked.
    """
    burst = _burst_score()
    try:
        result = _call_llm(user_message, burst)
        phase_raw = str(result.get("phase", "NEUTRAAL")).upper()
        phase: Phase = phase_raw if phase_raw in ("STABILISATIE", "EXPANSIE", "RECOVERY", "NEUTRAAL") else "NEUTRAAL"
        confidence = float(result.get("confidence", 0.0))
        reason = result.get("reason", "")
        logger.debug(
            "mental_state_analyzer: LLM → phase=%s confidence=%.2f reason=%s",
            phase, confidence, reason,
        )
        return phase, min(1.0, confidence)
    except Exception as exc:
        logger.warning("mental_state_analyzer: LLM call failed — %s", exc)
        return "NEUTRAAL", 0.0


# ── ACL update ─────────────────────────────────────────────────────────────

def _update_acl(phase: Phase) -> None:
    """Write the steering block for *phase* to the ACL file."""
    template = _ACL_TEMPLATES.get(phase)
    if not template:
        return  # NEUTRAAL: leave existing ACL untouched

    from tools.vault import RUNTIME_PATH

    acl_path = os.path.join(RUNTIME_PATH, "active_briefing.md")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    new_block = template.format(timestamp=timestamp)

    existing = ""
    try:
        with open(acl_path, "r", encoding="utf-8") as f:
            existing = f.read()
    except FileNotFoundError:
        pass

    cleaned = re.sub(
        r"## [🔴🟢🟡] Mentale Staat.*?(?=\n## |\Z)",
        "",
        existing,
        flags=re.DOTALL,
    ).strip()

    updated = new_block + ("\n\n" + cleaned if cleaned else "")

    try:
        os.makedirs(RUNTIME_PATH, exist_ok=True)
        with open(acl_path, "w", encoding="utf-8") as f:
            f.write(updated)
        logger.debug("mental_state_analyzer: ACL updated → %s", phase)
    except Exception as exc:
        logger.warning("mental_state_analyzer: ACL write failed — %s", exc)


# ── DriftGovernor feed ─────────────────────────────────────────────────────

def _get_governor():
    global _governor
    if _governor is None:
        from drift_governor import DriftGovernor
        _governor = DriftGovernor(
            baseline_state={
                "clarity": 0.8,
                "groundedness": 0.8,
                "emotional_activation": 0.3,
            },
            D_max=0.3,
            D_total=2.0,
            T=1.5,
        )
    return _governor


def _feed_governor(phase: Phase) -> None:
    delta = _DRIFT_DELTAS.get(phase)
    if not delta:
        return
    try:
        from drift_governor import DeltaRuleEvent, predicate_never
        result = _get_governor().apply_delta(
            DeltaRuleEvent(
                trigger_input=f"mental_state:{phase}",
                delta_behavior=delta,
                rollback_condition=predicate_never(),
            )
        )
        if result.biological_landing:
            logger.warning(
                "mental_state_analyzer: Biological Landing triggered "
                "(cumulative drift=%.3f)", result.cumulative_drift
            )
        elif not result.success:
            logger.debug("DriftGovernor rejected delta: %s", result.reason)
    except Exception as exc:
        logger.warning("mental_state_analyzer: DriftGovernor feed failed — %s", exc)


# ── Notification ──────────────────────────────────────────────────────────

_PHASE_ICONS = {
    "STABILISATIE": "🔴",
    "EXPANSIE":     "🟢",
    "RECOVERY":     "🟡",
    "NEUTRAAL":     "⚪",
}


def _notify_phase_change(old_phase: Phase, new_phase: Phase, confidence: float) -> None:
    """Push a Telegram notification when the mental state phase changes."""
    try:
        from tools.notification_tools import write_notification
        icon = _PHASE_ICONS.get(new_phase, "⚪")
        old_icon = _PHASE_ICONS.get(old_phase, "⚪")
        message = (
            f"{old_icon} → {icon} **Mentale staat gewijzigd**\n\n"
            f"**Nieuw:** {new_phase.capitalize()} (confidence: {confidence:.0%})\n"
            f"**ACL:** {'bijgewerkt' if new_phase != 'NEUTRAAL' else 'gecleared'}"
        )
        write_notification(message, agent_id="mental_state_analyzer", priority="medium", category="wellness")
    except Exception as exc:
        logger.warning("mental_state_analyzer: notification failed — %s", exc)


# ── Public API ─────────────────────────────────────────────────────────────

def analyze_and_steer(user_message: str) -> Phase:
    """Analyze *user_message* via LLM, update DriftGovernor and ACL if phase changed.

    Returns the detected phase. Safe to call from llm.run() — never raises.
    Only acts on a phase *change* to prevent redundant ACL writes.
    """
    global _current_phase

    phase, confidence = detect_phase(user_message)

    if phase == _current_phase:
        return phase

    logger.info(
        "mental_state_analyzer: %s → %s (confidence=%.2f)",
        _current_phase, phase, confidence,
    )

    if phase == "NEUTRAAL":
        _clear_acl_mental_state()
    else:
        _feed_governor(phase)
        _update_acl(phase)

    _notify_phase_change(_current_phase, phase, confidence)
    _current_phase = phase

    return phase


def current_phase() -> Phase:
    """Return the last detected phase without re-analyzing."""
    return _current_phase


def drift_report() -> dict:
    """Return DriftGovernor state summary, or empty dict if not yet initialized."""
    if _governor is None:
        return {}
    return _governor.drift_report()
