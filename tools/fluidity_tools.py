"""
tools/fluidity_tools.py — Fluidity Layer runtime controls.

Exposes `set_fluidity` so Luna can adjust output smoothness per session
in response to user requests like "schrijf vloeiender" or "minder smooth".
"""

from __future__ import annotations


def _handle_set_fluidity(args: dict) -> str:
    import fluidity

    results: list[str] = []

    if "strength" in args:
        try:
            s = float(args["strength"])
        except (TypeError, ValueError):
            return f"Ongeldige waarde voor strength: {args['strength']!r} — verwacht een getal tussen 0.0 en 1.0."
        results.append(fluidity.set_strength(s))

    feature_map = {
        "narrative": "narrative",
        "mirroring": "mirroring",
        "anti_cliche": "anti_cliche",
        "stream_pacing": "stream_pacing",
    }
    feature_kwargs: dict[str, bool] = {}
    for arg_key, feat_key in feature_map.items():
        if arg_key in args:
            val = args[arg_key]
            if isinstance(val, str):
                val = val.lower() in ("true", "1", "yes", "aan", "on")
            feature_kwargs[feat_key] = bool(val)

    if feature_kwargs:
        results.append(fluidity.set_feature(**feature_kwargs))

    if not results:
        cfg = fluidity.get_config()
        return (
            f"Fluidity Layer status — "
            f"strength: {cfg.strength:.2f}, "
            f"narrative: {cfg.narrative_formatting}, "
            f"mirroring: {cfg.mirroring}, "
            f"anti_cliche: {cfg.anti_cliche}, "
            f"stream_pacing: {cfg.stream_pacing}."
        )

    return " ".join(results)


DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "set_fluidity",
            "description": (
                "Pas de Fluidity Layer aan — de module die de output vloeiender en minder "
                "mechanisch maakt. Gebruik dit als de gebruiker vraagt om vloeiender, "
                "staccato-vrijer of juist meer gestructureerd te schrijven. "
                "Roep zonder argumenten aan om de huidige status te zien."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "strength": {
                        "type": "number",
                        "description": (
                            "Algemene sterkte van de Fluidity Layer (0.0 = uit, 1.0 = maximaal). "
                            "Schaal alle effecten proportioneel."
                        ),
                    },
                    "narrative": {
                        "type": "boolean",
                        "description": "Schakel de narratieve opmaakrichtlijn in de system prompt in/uit.",
                    },
                    "mirroring": {
                        "type": "boolean",
                        "description": "Schakel het Mirroring Protocol (ritme-synchronisatie) in/uit.",
                    },
                    "anti_cliche": {
                        "type": "boolean",
                        "description": "Schakel de post-processing anti-cliché filter in/uit.",
                    },
                    "stream_pacing": {
                        "type": "boolean",
                        "description": "Schakel de perceptuele token-pacing voor SSE streaming in/uit.",
                    },
                },
                "required": [],
            },
        },
    }
]

HANDLERS: dict[str, callable] = {
    "set_fluidity": _handle_set_fluidity,
}
