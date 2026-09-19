"""Tool: write_soul_md — allow Luna to update her persistent cognitive architecture."""

import os
from datetime import datetime, timezone

_SOUL_PATH = os.path.join(os.path.dirname(__file__), "..", "SOUL.md")


def write_soul_md(args: dict) -> str:
    """Write or append to SOUL.md — Luna's persistent cognitive architecture.

    When mode='append_log', only the Evolution Log table is extended.
    When mode='replace_section', the named section is replaced wholesale.
    When mode='overwrite', the full file is replaced (use sparingly).
    """
    mode = args.get("mode", "append_log")
    content = args.get("content", "").strip()

    if not content:
        return "Error: 'content' is required."

    try:
        if mode == "overwrite":
            with open(_SOUL_PATH, "w", encoding="utf-8") as f:
                f.write(content)
            return "SOUL.md volledig herschreven."

        with open(_SOUL_PATH, "r", encoding="utf-8") as f:
            current = f.read()

        if mode == "append_log":
            # Append a row to the Evolution Log table
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            row = f"| {date} | {content} |"
            if "Evolution Log" not in current:
                current += f"\n\n# Evolution Log\n\n| Datum | Wijziging | Reden |\n|---|---|---|\n{row}\n"
            else:
                current = current.rstrip() + f"\n{row}\n"
            with open(_SOUL_PATH, "w", encoding="utf-8") as f:
                f.write(current)
            return f"Evolution Log bijgewerkt: {row}"

        if mode == "replace_section":
            section = args.get("section", "")
            if not section:
                return "Error: 'section' is required voor mode='replace_section'."
            import re
            # Match ## <section>…(until next ## at same level or EOF)
            pattern = re.compile(
                rf"(## {re.escape(section)}.*?)(?=\n## |\Z)",
                re.DOTALL,
            )
            if not pattern.search(current):
                return f"Error: sectie '## {section}' niet gevonden in SOUL.md."
            updated = pattern.sub(f"## {section}\n{content}", current)
            with open(_SOUL_PATH, "w", encoding="utf-8") as f:
                f.write(updated)
            return f"Sectie '## {section}' bijgewerkt in SOUL.md."

        return f"Error: onbekende mode '{mode}'. Gebruik append_log, replace_section, of overwrite."

    except Exception as exc:
        return f"Error: schrijven naar SOUL.md mislukt — {exc}"


DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "write_soul_md",
            "description": (
                "Update Luna's persistent cognitive architecture (SOUL.md). "
                "Use mode='append_log' to record a new evolution entry (content = "
                "'Wijziging | Reden'). Use mode='replace_section' + section name to "
                "rewrite a specific ## section. Use mode='overwrite' only to fully "
                "rewrite the file. Call this when you discover a better operational "
                "pattern, identify a reasoning error to permanently correct, or want "
                "to record a validated mechanism."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["append_log", "replace_section", "overwrite"],
                        "description": "How to update: append_log (default), replace_section, or overwrite.",
                    },
                    "content": {
                        "type": "string",
                        "description": "For append_log: 'Wijziging | Reden'. For replace_section/overwrite: full section or file content.",
                    },
                    "section": {
                        "type": "string",
                        "description": "Required for replace_section: the ## heading name (without ##).",
                    },
                },
                "required": ["content"],
            },
        },
    }
]

HANDLERS = {
    "write_soul_md": lambda args: write_soul_md(args),
}
