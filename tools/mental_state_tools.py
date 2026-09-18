"""Tool: get_mental_state — expose the current analyzed mental state to Luna."""


def get_mental_state(_args: dict = None) -> str:
    from mental_state_analyzer import current_phase, drift_report

    phase = current_phase()
    drift = drift_report()

    phase_labels = {
        "STABILISATIE": "🔴 Stabilisatie (Angst / Stress / Hyper-arousal)",
        "EXPANSIE":     "🟢 Expansie (Flow / Euforie)",
        "RECOVERY":     "🟡 Recovery (Lethargie / Leegte)",
        "NEUTRAAL":     "⚪ Neutraal (geen significant signaal gedetecteerd)",
    }

    lines = [
        f"**Huidige mentale staat:** {phase_labels.get(phase, phase)}",
    ]

    if drift:
        state = drift["current_state"]
        lines += [
            "",
            "**DriftGovernor dimensies:**",
            f"- Clarity:              {state.get('clarity', 0):.2f}",
            f"- Groundedness:         {state.get('groundedness', 0):.2f}",
            f"- Emotional activation: {state.get('emotional_activation', 0):.2f}",
            "",
            f"**Cumulatieve drift:** {drift['cumulative_drift']:.3f} / {drift['D_total']} max",
            f"**Biological Landing:** {'⚠️ getriggerd' if drift['biological_landing_triggered'] else 'niet actief'}",
        ]

    return "\n".join(lines)


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_mental_state",
            "description": (
                "Return the current mental state of the user as inferred from their "
                "recent typing patterns (syntactic drift, lexical choices, interaction "
                "rhythm). Also shows the DriftGovernor dimension scores. Call this when "
                "the user asks about their current state, energy level, or how they come "
                "across based on their messages."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]

HANDLERS = {
    "get_mental_state": lambda args: get_mental_state(args),
}
