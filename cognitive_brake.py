"""Cognitive Brake — session intensity monitor for Sovereign Link.

Tracks session duration and complex tool-call frequency. When thresholds are
exceeded (adjusted for the user's current mental state from the ACL), a
wellness notification is pushed via write_notification and a Stop Order is
activated — instructing Luna to halt new analytical tasks.

Public API
----------
ensure_monitor_running()   — start background thread (call from llm.run())
record_complex_call(name)  — log a complex tool invocation
stop_order_active()        — True when a Stop Order is in effect
issue_stop_order()         — manually activate Stop Order
clear_stop_order()         — release Stop Order after confirmed rest
reset_session()            — reset all state for a new session
"""

import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tools that count as "complex" cognitive load
# ---------------------------------------------------------------------------

# Keywords that signal heavy psychological content in a user message
_HEAVY_TOPIC_KEYWORDS = frozenset({
    "trauma", "narcis", "narcisme", "narcissisme", "mishandeling", "misbruik",
    "gaslighting", "familieconflict", "familiescène", "hechtingsstoornis",
    "ptss", "ptsd", "dissociati", "suïcid", "suicid", "zelfmoord", "automutilat",
    "schaduwwerk", "innerlijk kind", "seksueel misbruik", "emotioneel misbruik",
    "parentificatie", "generatietrauma",
})

# Substrings that indicate cognitive fatigue, confusion, or self-doubt
_FATIGUE_PATTERNS = (
    "ik weet het niet meer",
    "ik snap het niet",
    "ik ben zo moe",
    "kan niet meer nadenken",
    "mijn hoofd staat",
    "ik ben verward",
    "ik twijfel aan mezelf",
    "ik ben de kluts kwijt",
    "overbelast",
    "uitgeput",
    "het is te veel",
    "ik raak het kwijt",
    "ik kan niet meer",
    "ik ben leeg",
)

COMPLEX_TOOLS = frozenset({
    "search_vault_semantic",
    "spawn_agent",
    "read_vault",
    "list_files",
    "search_timeline",
    "batch_extract_content",
    "crawl_site_structure",
    "get_agent_status",
    "list_agents",
})

# ---------------------------------------------------------------------------
# Built-in defaults — overridden by .system/cognitive_limits.json in vault
# ---------------------------------------------------------------------------

_DEFAULT_LIMITS: dict = {
    "STABILISATIE": {
        "session_warn_minutes": 30,
        "session_stop_minutes": 45,
        "complex_calls_per_window": 5,
        "window_seconds": 300,
        # Emotional/cognitive triggers
        "stabilisatie_streak_stop": 4,       # consecutive STABILISATIE detections
        "heavy_topics_window_seconds": 900,  # 15-min sliding window
        "heavy_topics_stop_count": 2,        # lower threshold: already in stress
        "fatigue_window_seconds": 600,
        "fatigue_stop_count": 2,
    },
    "EXPANSIE": {
        "session_warn_minutes": 75,
        "session_stop_minutes": 90,
        "complex_calls_per_window": 15,
        "window_seconds": 300,
        "stabilisatie_streak_stop": 5,
        "heavy_topics_window_seconds": 900,
        "heavy_topics_stop_count": 3,
        "fatigue_window_seconds": 600,
        "fatigue_stop_count": 3,
    },
    "RECOVERY": {
        "session_warn_minutes": 40,
        "session_stop_minutes": 60,
        "complex_calls_per_window": 8,
        "window_seconds": 300,
        "stabilisatie_streak_stop": 3,
        "heavy_topics_window_seconds": 900,
        "heavy_topics_stop_count": 2,
        "fatigue_window_seconds": 600,
        "fatigue_stop_count": 1,             # single fatigue signal triggers in RECOVERY
    },
    "NEUTRAAL": {
        "session_warn_minutes": 60,
        "session_stop_minutes": 120,
        "complex_calls_per_window": 12,
        "window_seconds": 300,
        "stabilisatie_streak_stop": 4,
        "heavy_topics_window_seconds": 900,
        "heavy_topics_stop_count": 3,
        "fatigue_window_seconds": 600,
        "fatigue_stop_count": 2,
    },
}

# ---------------------------------------------------------------------------
# Module-level state  (protected by _lock)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_session_start: float = time.monotonic()
_complex_call_timestamps: list[float] = []
_stop_order_active: bool = False
_warn_sent: bool = False
_stop_sent: bool = False
_monitor_thread: threading.Thread | None = None

# Emotional / cognitive trigger state
_stabilisatie_streak: int = 0          # consecutive STABILISATIE detections
_heavy_topic_timestamps: list[float] = []  # timestamps of heavy-topic messages
_fatigue_timestamps: list[float] = []      # timestamps of fatigue-signal messages

# Pause enforcement
_MIN_PAUSE_MINUTES: float = 15.0           # absolute floor — never overridable
_required_pause_minutes: float = 15.0     # current required pause (Luna can raise this)
_stop_order_activated_at: float | None = None  # wall-clock time Stop Order fired

# User-defined timer override
_DEFAULT_TIMER_MINUTES: float = 30.0           # hardcoded default session stop threshold
_timer_override_minutes: float | None = _DEFAULT_TIMER_MINUTES  # if set, replaces phase-based stop threshold


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reset_session() -> None:
    """Reset all tracking state — call at the start of a new session."""
    global _session_start, _stop_order_active, _warn_sent, _stop_sent, _stabilisatie_streak
    global _stop_order_activated_at, _required_pause_minutes, _timer_override_minutes
    with _lock:
        _session_start = time.monotonic()
        _complex_call_timestamps.clear()
        _heavy_topic_timestamps.clear()
        _fatigue_timestamps.clear()
        _stabilisatie_streak = 0
        _stop_order_active = False
        _warn_sent = False
        _stop_sent = False
        _stop_order_activated_at = None
        _required_pause_minutes = _MIN_PAUSE_MINUTES
        _timer_override_minutes = _DEFAULT_TIMER_MINUTES
    # Remove persisted state so a future restart starts fresh
    try:
        os.remove(_get_state_path())
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("CognitiveBrake: could not remove session state file", exc_info=True)
    logger.info("CognitiveBrake: session reset")


def record_complex_call(tool_name: str) -> None:
    """Record a complex tool invocation. No-op for non-complex tools."""
    if tool_name not in COMPLEX_TOOLS:
        return
    with _lock:
        _complex_call_timestamps.append(time.monotonic())
    logger.debug("CognitiveBrake: recorded '%s'", tool_name)


def stop_order_active() -> bool:
    """Return True when a Stop Order is currently active."""
    return _stop_order_active


def issue_stop_order() -> None:
    """Programmatically activate a Stop Order."""
    global _stop_order_active
    with _lock:
        _stop_order_active = True
    logger.warning("CognitiveBrake: Stop Order issued manually")


def clear_stop_order() -> str:
    """Release the Stop Order after the required pause has elapsed.

    Returns a human-readable string describing the outcome — either the Stop
    Order was cleared or the pause has not been long enough yet.
    """
    global _session_start, _stop_order_active, _warn_sent, _stop_sent, _stabilisatie_streak
    global _stop_order_activated_at, _required_pause_minutes

    if not _stop_order_active:
        return "Er is geen actieve Stop Order."

    if _stop_order_activated_at is not None:
        elapsed_pause = (time.time() - _stop_order_activated_at) / 60.0
        if elapsed_pause < _required_pause_minutes:
            remaining = _required_pause_minutes - elapsed_pause
            return (
                f"⏳ Stop Order kan nog niet worden vrijgegeven. "
                f"Vereiste pauze: {_required_pause_minutes:.0f} minuten — "
                f"nog {remaining:.0f} minuten te gaan. "
                f"Neem de tijd om te rusten."
            )

    with _lock:
        _session_start = time.monotonic()   # reset timer so session starts fresh
        _stop_order_active = False
        _warn_sent = False
        _stop_sent = False
        _stabilisatie_streak = 0
        _stop_order_activated_at = None
        _complex_call_timestamps.clear()
        _heavy_topic_timestamps.clear()
        _fatigue_timestamps.clear()
    _save_session_state()
    _stop_rest_music()
    logger.info("CognitiveBrake: Stop Order cleared — session timer and counters reset")
    return (
        f"✅ Stop Order vrijgegeven na {_required_pause_minutes:.0f} minuten rust. "
        f"Cognitieve rem opgeheven. Sessietimer herstart. Normale operatie hervat."
    )


def pause_remaining_minutes() -> float:
    """Return minutes remaining before the Stop Order can be cleared.

    Returns 0.0 if no stop order is active or if the required pause has
    already elapsed.
    """
    if not _stop_order_active or _stop_order_activated_at is None:
        return 0.0
    elapsed = (time.time() - _stop_order_activated_at) / 60.0
    return max(0.0, _required_pause_minutes - elapsed)


def set_session_timer(minutes: float) -> str:
    """Override the session stop threshold to a user-defined duration.

    The override persists across Stop Order cycles (clear_stop_order keeps it).
    Only reset_session() clears it. The warn threshold is set to 80% of the value.
    Returns a confirmation string including the computed end time.
    """
    global _timer_override_minutes
    import datetime
    minutes = max(1.0, float(minutes))
    with _lock:
        _timer_override_minutes = minutes
    _save_session_state()
    elapsed = _session_minutes()
    remaining = max(0.0, minutes - elapsed)
    end_time = datetime.datetime.now() + datetime.timedelta(minutes=remaining)
    end_str = end_time.strftime("%H:%M")
    logger.info("CognitiveBrake: timer override set to %.0f min (end ~%s)", minutes, end_str)
    return (
        f"⏱️ Sessietimer ingesteld op {minutes:.0f} minuten. "
        f"Stop Order zal activeren om {end_str} "
        f"(nog {remaining:.0f} minuten)."
    )


def set_pause_duration(minutes: float) -> str:
    """Set the required pause duration before a Stop Order can be cleared.

    The value is clamped to a minimum of 15 minutes (_MIN_PAUSE_MINUTES).
    Luna can use this to increase the rest period for heavy sessions.
    """
    global _required_pause_minutes
    clamped = max(_MIN_PAUSE_MINUTES, float(minutes))
    with _lock:
        _required_pause_minutes = clamped
    _save_session_state()
    logger.info("CognitiveBrake: required pause set to %.0f min", clamped)
    if clamped > minutes:
        return (
            f"Pauzedrempel ingesteld op {clamped:.0f} minuten "
            f"(minimum is {_MIN_PAUSE_MINUTES:.0f} minuten)."
        )
    return f"Pauzedrempel ingesteld op {clamped:.0f} minuten."


def ensure_monitor_running() -> None:
    """Start the background monitor thread if not already alive."""
    global _monitor_thread
    with _lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        t = threading.Thread(target=_monitor_loop, daemon=True, name="CognitiveBrake")
        _monitor_thread = t
    t.start()
    logger.info("CognitiveBrake: monitor thread started")


def record_mental_state(phase: str) -> None:
    """Track the mental state detected for the current message.

    Called from llm.run() after analyze_and_steer(). Increments the
    STABILISATIE streak counter; any other phase resets it.
    """
    global _stabilisatie_streak
    with _lock:
        if phase == "STABILISATIE":
            _stabilisatie_streak += 1
        else:
            _stabilisatie_streak = 0
    logger.debug("CognitiveBrake: mental state=%s streak=%d", phase, _stabilisatie_streak)


def record_message_content(message: str) -> None:
    """Scan a user message for heavy psychological topics and fatigue signals.

    Appends timestamps to the relevant sliding-window lists so that
    _check_thresholds() can evaluate them on the next monitor tick.
    """
    lower = message.lower()
    now = time.monotonic()

    if any(kw in lower for kw in _HEAVY_TOPIC_KEYWORDS):
        with _lock:
            _heavy_topic_timestamps.append(now)
        logger.debug("CognitiveBrake: heavy topic detected in message")

    if any(pat in lower for pat in _FATIGUE_PATTERNS):
        with _lock:
            _fatigue_timestamps.append(now)
        logger.debug("CognitiveBrake: fatigue signal detected in message")

    # Immediately evaluate — content triggers should not wait for the 60s tick
    try:
        _check_thresholds()
    except Exception:
        logger.exception("CognitiveBrake: error in immediate content check")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_limits() -> dict:
    """Load per-state limits from vault config, falling back to built-in defaults."""
    try:
        from tools.vault import VAULT_PATH
        config_path = os.path.join(VAULT_PATH, ".system", "cognitive_limits.json")
        with open(config_path, encoding="utf-8") as f:
            loaded = json.load(f)
        # Merge so missing keys fall back to defaults
        return {
            state: {**defaults, **loaded.get(state, {})}
            for state, defaults in _DEFAULT_LIMITS.items()
        }
    except (FileNotFoundError, json.JSONDecodeError):
        return _DEFAULT_LIMITS
    except Exception:
        return _DEFAULT_LIMITS


def _read_mental_state() -> str:
    """Read the current mental-state phase from mental_state_analyzer.

    Uses current_phase() directly rather than parsing the ACL file — the ACL
    is not updated when the phase returns to NEUTRAAL, so reading it would
    keep returning the last non-NEUTRAAL phase (e.g., STABILISATIE) and cause
    premature Stop Orders.
    """
    try:
        from mental_state_analyzer import current_phase
        return current_phase()
    except Exception:
        logger.debug("CognitiveBrake: could not read mental state", exc_info=True)
    return "NEUTRAAL"


def _session_minutes() -> float:
    return (time.monotonic() - _session_start) / 60.0


def _complex_calls_in_window(window_seconds: int) -> int:
    cutoff = time.monotonic() - window_seconds
    with _lock:
        return sum(1 for t in _complex_call_timestamps if t >= cutoff)


def _get_state_path() -> str:
    """Return path to the persistent session state JSON file."""
    try:
        from tools.vault import RUNTIME_PATH
        return os.path.join(RUNTIME_PATH, "session_state.json")
    except Exception:
        return "/tmp/sovereign_link_session_state.json"


def _load_session_state() -> None:
    """Load persisted session state from disk on startup.

    Resumes the session timer and Stop Order status so that a restart of
    Sovereign Link does not reset the clock.  If the saved session is older
    than _MAX_SESSION_AGE_SECONDS and no Stop Order was active, we start
    fresh — the user has likely slept and recovered.
    """
    global _session_start, _stop_order_active, _warn_sent, _stop_sent, _timer_override_minutes
    _MAX_SESSION_AGE_SECONDS = 8 * 3600  # 8 hours
    path = _get_state_path()
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        start_wall: float | None = state.get("session_start_wall")
        if start_wall is None:
            return
        elapsed_wall = time.time() - start_wall
        stop_was_active = bool(state.get("stop_order_active", False))
        # Start fresh if the session is stale and no active Stop Order
        if elapsed_wall > _MAX_SESSION_AGE_SECONDS and not stop_was_active:
            logger.info("CognitiveBrake: stale session (%.1fh) — starting fresh", elapsed_wall / 3600)
            return
        # Resume the session timer
        with _lock:
            _session_start = time.monotonic() - elapsed_wall
            if stop_was_active:
                _stop_order_active = True
                _stop_sent = True
                _warn_sent = True
                _stop_order_activated_at = state.get("stop_order_activated_at")
            else:
                # Restore warn_sent independently so it doesn't re-fire after restart
                _warn_sent = bool(state.get("warn_sent", False))
            _required_pause_minutes = max(
                _MIN_PAUSE_MINUTES,
                float(state.get("required_pause_minutes", _MIN_PAUSE_MINUTES)),
            )
            override = state.get("timer_override_minutes")
            _timer_override_minutes = float(override) if override is not None else _DEFAULT_TIMER_MINUTES
        logger.info(
            "CognitiveBrake: resumed session (%.1f min elapsed, stop_order=%s)",
            elapsed_wall / 60, stop_was_active,
        )
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("CognitiveBrake: could not load session state", exc_info=True)


def _save_session_state() -> None:
    """Persist current session state to disk."""
    path = _get_state_path()
    elapsed = time.monotonic() - _session_start
    state = {
        "session_start_wall": time.time() - elapsed,
        "stop_order_active": _stop_order_active,
        "stop_order_activated_at": _stop_order_activated_at,
        "required_pause_minutes": _required_pause_minutes,
        "timer_override_minutes": _timer_override_minutes,
        "warn_sent": _warn_sent,
        "saved_at": time.time(),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        logger.debug("CognitiveBrake: could not save session state", exc_info=True)


def _push_notification(message: str) -> None:
    try:
        from tools.notification_tools import write_notification
        write_notification(
            content=message,
            agent_id="cognitive_brake",
            priority="high",
            category="wellness",
        )
    except Exception:
        logger.exception("CognitiveBrake: failed to push notification")


def _start_rest_music() -> None:
    """Start music playback as part of the Stop Order rest protocol."""
    try:
        from tools.music_tools import start_music
        result = start_music()
        logger.info("CognitiveBrake: rest music triggered — %s", result)
    except Exception:
        logger.debug("CognitiveBrake: could not start rest music", exc_info=True)


def _stop_rest_music() -> None:
    """Stop music playback when the Stop Order is cleared."""
    try:
        from tools.music_tools import control_music
        result = control_music({"action": "pause"})
        logger.info("CognitiveBrake: rest music stopped — %s", result)
    except Exception:
        logger.debug("CognitiveBrake: could not stop rest music", exc_info=True)


def _monitor_loop() -> None:
    """Background thread — evaluates thresholds every 60 seconds."""
    while True:
        time.sleep(60)
        try:
            _check_thresholds()
        except Exception:
            logger.exception("CognitiveBrake: error in monitor loop")


def _count_in_window(timestamps: list[float], window_seconds: int) -> int:
    cutoff = time.monotonic() - window_seconds
    return sum(1 for t in timestamps if t >= cutoff)


def _check_thresholds() -> None:
    """Evaluate session against limits and fire notifications when needed."""
    global _stop_order_active, _warn_sent, _stop_sent

    # Already active — nothing to do
    if _stop_order_active:
        return

    limits = _load_limits()
    state = _read_mental_state()
    cfg = limits.get(state, limits["NEUTRAAL"])

    minutes = _session_minutes()
    minutes_int = int(minutes)
    complex_calls = _complex_calls_in_window(cfg["window_seconds"])

    # User-defined override takes priority over phase defaults
    if _timer_override_minutes is not None:
        stop_min = _timer_override_minutes
        warn_min = max(1.0, _timer_override_minutes * 0.8)
    else:
        warn_min = cfg["session_warn_minutes"]
        stop_min = cfg["session_stop_minutes"]
    call_limit: int = cfg["complex_calls_per_window"]

    # ── Trigger 1: Temporal / complex-call density ──────────────────────────
    temporal_stop = minutes >= stop_min or complex_calls >= call_limit * 2
    temporal_warn = minutes >= warn_min or complex_calls >= call_limit

    # ── Trigger 2: Emotional — STABILISATIE streak or heavy topic density ───
    streak_limit: int = cfg.get("stabilisatie_streak_stop", 4)
    heavy_window: int = cfg.get("heavy_topics_window_seconds", 900)
    heavy_limit: int = cfg.get("heavy_topics_stop_count", 3)
    heavy_count = _count_in_window(_heavy_topic_timestamps, heavy_window)
    emotional_stop = _stabilisatie_streak >= streak_limit or heavy_count >= heavy_limit

    # ── Trigger 3: Cognitive fatigue ─────────────────────────────────────────
    fatigue_window: int = cfg.get("fatigue_window_seconds", 600)
    fatigue_limit: int = cfg.get("fatigue_stop_count", 2)
    fatigue_count = _count_in_window(_fatigue_timestamps, fatigue_window)
    cognitive_stop = fatigue_count >= fatigue_limit

    # ── Determine cause label for notification message ───────────────────────
    stop_triggered = temporal_stop or emotional_stop or cognitive_stop

    if stop_triggered:
        # Atomically check-and-set to prevent double-fire from concurrent calls
        with _lock:
            if _stop_sent:
                return
            _stop_order_active = True
            _stop_sent = True
            _warn_sent = True
            _stop_order_activated_at = time.time()

        if emotional_stop:
            if _stabilisatie_streak >= streak_limit:
                cause = (
                    f"Er zijn {_stabilisatie_streak} opeenvolgende hoog-stresssignalen "
                    f"gedetecteerd in jouw berichten ({state} fase)."
                )
            else:
                cause = (
                    f"Er zijn {heavy_count} berichten met zware psychologische inhoud "
                    f"gedetecteerd in de afgelopen {heavy_window // 60} minuten."
                )
        elif cognitive_stop:
            cause = (
                f"Er zijn {fatigue_count} signalen van cognitieve overbelasting "
                f"(vermoeidheid/verwarring) gedetecteerd in de afgelopen {fatigue_window // 60} minuten."
            )
        else:
            cause = (
                f"Je bevindt je nu al {minutes_int} minuten in een intensieve sessie "
                f"({state} fase)."
            )

        _save_session_state()
        _push_notification(
            f"🛑 STOP ORDER — COGNITIEVE REM GEACTIVEERD: {cause} "
            f"Nieuwe analyses en complexe taken zijn tijdelijk geblokkeerd. "
            f"Neem minimaal 15 minuten rust. Zeg 'stop order vrijgeven' om verder te gaan."
        )
        _start_rest_music()
        logger.warning(
            "CognitiveBrake: Stop Order activated (%.1f min, %d complex calls, "
            "streak=%d, heavy=%d, fatigue=%d, state=%s)",
            minutes, complex_calls, _stabilisatie_streak, heavy_count, fatigue_count, state,
        )
        return



# ---------------------------------------------------------------------------
# Module initialisation — resume persisted session on import
# ---------------------------------------------------------------------------

_load_session_state()
