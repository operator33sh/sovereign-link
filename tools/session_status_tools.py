"""Tool: get_session_status — real-time session dashboard for Luna.

Returns a unified view of:
  - Timer cluster   : elapsed/remaining time, stop order status
  - Energetic cluster: mental state, stress level, focus level
  - Session params  : active phase, last ACL sync, cognitive load indicators
"""

import os
import re
import time


def get_session_status(_args: dict = None) -> str:
    import json

    result: dict = {}

    # ── Timer cluster ─────────────────────────────────────────────────────────
    try:
        import cognitive_brake

        phase_for_limits = cognitive_brake._read_mental_state()
        limits = cognitive_brake._load_limits()
        cfg = limits.get(phase_for_limits, limits["NEUTRAAL"])

        # Mirror priority logic from _check_thresholds
        override = cognitive_brake._timer_override_minutes
        if override is not None:
            stop_min = override
            warn_min = max(1.0, override * 0.8)
        else:
            stop_min = cfg["session_stop_minutes"]
            warn_min = cfg["session_warn_minutes"]

        elapsed_min = cognitive_brake._session_minutes()
        remaining_min = max(0.0, stop_min - elapsed_min)

        def _fmt(minutes: float) -> str:
            h, m = divmod(int(minutes), 60)
            return f"{h}u {m:02d}m" if h else f"{m}m"

        result["timer"] = {
            "elapsed": _fmt(elapsed_min),
            "remaining": _fmt(remaining_min),
            "current_threshold_minutes": stop_min,
            "warn_threshold_minutes": warn_min,
            "timer_override_active": override is not None,
            "stop_order_status": "ACTIVE" if cognitive_brake.stop_order_active() else "INACTIVE",
        }
    except Exception as exc:
        result["timer"] = {"error": str(exc)}

    # ── Energetic cluster ────────────────────────────────────────────────────
    try:
        from mental_state_analyzer import current_phase, drift_report

        phase = current_phase()
        phase_labels = {
            "STABILISATIE": "Stress / Hyper-arousal",
            "EXPANSIE":     "Flow / Expansie",
            "RECOVERY":     "Lethargie / Leegte",
            "NEUTRAAL":     "Neutraal",
        }
        drift = drift_report()
        state = drift.get("current_state", {}) if drift else {}

        emotional_activation = state.get("emotional_activation", None)
        clarity = state.get("clarity", None)

        # Map 0-1 float to label
        def _intensity_label(val: float | None) -> str:
            if val is None:
                return "onbekend"
            if val >= 0.7:
                return f"hoog ({val:.2f})"
            if val >= 0.4:
                return f"gemiddeld ({val:.2f})"
            return f"laag ({val:.2f})"

        result["energetic"] = {
            "current_mental_state": phase_labels.get(phase, phase),
            "phase_code": phase,
            "stress_level": _intensity_label(emotional_activation),
            "focus_level": _intensity_label(clarity),
            "groundedness": _intensity_label(state.get("groundedness")),
            "cumulative_drift": round(drift.get("cumulative_drift", 0), 3) if drift else None,
            "biological_landing_triggered": drift.get("biological_landing_triggered", False) if drift else False,
        }
    except Exception as exc:
        result["energetic"] = {"error": str(exc)}

    # ── Session parameters ───────────────────────────────────────────────────
    try:
        from tools.vault import RUNTIME_PATH

        # Read last_sync from ACL file
        last_sync = "onbekend"
        acl_path = os.path.join(RUNTIME_PATH, "active_briefing.md")
        try:
            with open(acl_path, encoding="utf-8") as f:
                acl_content = f.read()
            m = re.search(r"_Last updated:\s*([^\n_]+)_", acl_content)
            if m:
                last_sync = m.group(1).strip()
        except FileNotFoundError:
            last_sync = "geen ACL actief"

        # Cognitive load indicators from cognitive_brake
        import cognitive_brake as _cb
        heavy_count = _cb._count_in_window(
            _cb._heavy_topic_timestamps,
            _cb._DEFAULT_LIMITS.get(phase_for_limits, _cb._DEFAULT_LIMITS["NEUTRAAL"])
            .get("heavy_topics_window_seconds", 900),
        )
        fatigue_count = _cb._count_in_window(
            _cb._fatigue_timestamps,
            _cb._DEFAULT_LIMITS.get(phase_for_limits, _cb._DEFAULT_LIMITS["NEUTRAAL"])
            .get("fatigue_window_seconds", 600),
        )

        result["session"] = {
            "active_phase": phase_for_limits,
            "stabilisatie_streak": _cb._stabilisatie_streak,
            "heavy_topic_messages_in_window": heavy_count,
            "fatigue_signals_in_window": fatigue_count,
            "last_acl_sync": last_sync,
        }
    except Exception as exc:
        result["session"] = {"error": str(exc)}

    return json.dumps(result, ensure_ascii=False, indent=2)


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_session_status",
            "description": (
                "Geeft een volledig real-time overzicht van de huidige sessie: "
                "verstreken en resterende tijd tot Stop Order, mentale staat en energieniveau "
                "van de Agent, actieve fase, cognitieve belasting, en wanneer de ACL voor "
                "het laatst gesynchroniseerd is. "
                "Gebruik dit om proactief te interveniëren wanneer de tijd of het energieniveau "
                "een kritieke drempel nadert."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]

HANDLERS = {
    "get_session_status": lambda args: get_session_status(args),
}
