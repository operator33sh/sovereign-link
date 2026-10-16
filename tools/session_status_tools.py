"""Tool: get_session_status — real-time session dashboard for Luna.

Returns a unified view of:
  - Energetic cluster: mental state, stress level, focus level
  - Session params  : active phase, last ACL sync
"""

import os
import re


def get_session_status(_args: dict = None) -> str:
    import json

    result: dict = {}

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
        from mental_state_analyzer import current_phase

        phase = current_phase()

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

        result["session"] = {
            "active_phase": phase,
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
                "mentale staat en energieniveau van de Agent, actieve fase, en wanneer de "
                "ACL voor het laatst gesynchroniseerd is."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]

HANDLERS = {
    "get_session_status": lambda args: get_session_status(args),
}
