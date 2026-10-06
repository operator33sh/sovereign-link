"""skill_tools.py — load_skill tool for the Sovereign-Link agent harness.

Exposes a single `load_skill` tool that reads the full instructions of a named
skill from the ./skills/ directory (or any configured external directory) and
returns them directly into the context window.
"""

from __future__ import annotations


def load_skill(args: dict) -> str:
    """Load the full instructions of a named skill into the context window.

    Args:
        args: dict with key 'skill_name' (str, required).

    Returns:
        Formatted skill instructions as a string, ready for context injection.
    """
    skill_name = (args.get("skill_name") or "").strip()
    if not skill_name:
        return "Error: 'skill_name' parameter is required."

    try:
        import skills_loader
        skill = skills_loader.load_skill(skill_name)
        return skills_loader.format_skill_for_context(skill)
    except KeyError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("load_skill: unexpected error for '%s'", skill_name)
        return f"Error loading skill '{skill_name}': {exc}"


DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "Load the full instructions of a named skill into the context window. "
                "Use this when the user asks to use a specific skill by name, or when a task "
                "matches a skill listed in <available_skills>. "
                "Returns the complete skill instructions including parameters and examples."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "The exact name of the skill to load (as listed in <available_skills>).",
                    }
                },
                "required": ["skill_name"],
            },
        },
    }
]

HANDLERS: dict[str, callable] = {
    "load_skill": lambda args: load_skill(args),
}
