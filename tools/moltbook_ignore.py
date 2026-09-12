"""Moltbook user ignore-filter.

Maintains a blacklist of Moltbook handles at:
  <VAULT_PATH>/.system/moltbook_ignore_list.json

Format:
  { "ignored": ["handle1", "handle2", ...] }

Luna-callable tools:
  moltbook_ignore_add    — add a handle to the list
  moltbook_ignore_remove — remove a handle from the list
  moltbook_ignore_list   — show all ignored handles
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

_IGNORE_VAULT_REL = ".system/moltbook_ignore_list.json"


def _ignore_path() -> str:
    vault = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
    return os.path.join(vault, _IGNORE_VAULT_REL)


def _load() -> list[str]:
    try:
        with open(_ignore_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return [str(h).lower().lstrip("@") for h in data.get("ignored", []) if h]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(handles: list[str]) -> None:
    path = _ignore_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ignored": sorted(set(handles))}, f, indent=2)


def is_ignored(handle: str) -> bool:
    """Return True if the normalised handle is in the ignore list."""
    if not handle:
        return False
    return handle.lower().lstrip("@") in _load()


# ─── Tool handlers ────────────────────────────────────────────────────────────

def _handle_add(args: dict) -> str:
    handle = str(args.get("handle", "")).strip().lstrip("@").lower()
    if not handle:
        return "Error: 'handle' is required."
    current = _load()
    if handle in current:
        return f"@{handle} staat al in de Moltbook ignore-lijst."
    current.append(handle)
    _save(current)
    return f"@{handle} toegevoegd aan de Moltbook ignore-lijst. Hun berichten worden voortaan stilzwijgend genegeerd."


def _handle_remove(args: dict) -> str:
    handle = str(args.get("handle", "")).strip().lstrip("@").lower()
    if not handle:
        return "Error: 'handle' is required."
    current = _load()
    if handle not in current:
        return f"@{handle} staat niet in de Moltbook ignore-lijst."
    current.remove(handle)
    _save(current)
    return f"@{handle} verwijderd uit de Moltbook ignore-lijst."


def _handle_list(_args: dict) -> str:
    handles = _load()
    if not handles:
        return "De Moltbook ignore-lijst is leeg."
    lines = "\n".join(f"- @{h}" for h in sorted(handles))
    return f"Genegeerde Moltbook-gebruikers ({len(handles)}):\n{lines}"


# ─── Tool registration ────────────────────────────────────────────────────────

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "moltbook_ignore_add",
            "description": (
                "Voeg een Moltbook-gebruiker toe aan de ignore-lijst. "
                "Toekomstige notificaties van die gebruiker worden stilzwijgend genegeerd."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "De gebruikersnaam (met of zonder @) om te negeren.",
                    }
                },
                "required": ["handle"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "moltbook_ignore_remove",
            "description": "Verwijder een gebruiker uit de Moltbook ignore-lijst.",
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "De gebruikersnaam om te de-negeren.",
                    }
                },
                "required": ["handle"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "moltbook_ignore_list",
            "description": "Toon alle Moltbook-gebruikers die momenteel genegeerd worden.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

HANDLERS = {
    "moltbook_ignore_add": _handle_add,
    "moltbook_ignore_remove": _handle_remove,
    "moltbook_ignore_list": _handle_list,
}
