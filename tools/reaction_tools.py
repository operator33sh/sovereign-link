"""
tools/reaction_tools.py — send_reaction tool for Luna.

Lets Luna place an emoji reaction on a Telegram message from within a tool call.
"""

from __future__ import annotations

DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "send_reaction",
            "description": (
                "Plaats een emoji-reactie op een Telegram-bericht. "
                "Gebruik dit wanneer de situatie een non-verbale emotionele reactie vraagt "
                "in plaats van (of naast) een tekstantwoord. "
                "Zonder message_id reageert de tool op het laatste bericht van de gebruiker."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "emoji": {
                        "type": "string",
                        "description": (
                            "De emoji voor de reactie, bijv. '❤️', '🔥', '🤔', '🚀', '😂', '👍', '🎯'. "
                            "Gebruik alleen Telegram-ondersteunde reactie-emoji's."
                        ),
                    },
                    "message_id": {
                        "type": "integer",
                        "description": (
                            "Optioneel bericht-ID om op te reageren. "
                            "Standaard: het laatste inkomende bericht van de gebruiker."
                        ),
                    },
                },
                "required": ["emoji"],
            },
        },
    }
]


def _handle_send_reaction(args: dict) -> str:
    import reaction_bridge

    emoji = args.get("emoji", "")
    if not emoji:
        return "Geen emoji opgegeven."

    message_id = args.get("message_id")
    return reaction_bridge.send_reaction(emoji, message_id=message_id)


HANDLERS: dict[str, callable] = {
    "send_reaction": _handle_send_reaction,
}
