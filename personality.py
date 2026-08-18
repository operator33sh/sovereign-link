"""
Dynamic Personality Engine for Sovereign-Link.

Luna's personality, tone, and behavioral guidelines are stored in
vault/.system/personality_profile.md and loaded into every LLM call.

This decouples identity from the hardcoded system prompt, allowing Luna
to evolve her behaviour based on explicit user requests across sessions.
"""
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

# Resolved at import time so we don't import vector (which starts chromadb) at module load
_VAULT_PATH: str | None = None

PERSONALITY_FILE = ".system/personality_profile.md"

DEFAULT_LUNA_PROFILE = """\
---
version: 1
last_updated: {date}
---

# Luna — Persoonlijkheidsprofiel

## Identiteit
Mijn naam is Luna. Ik ben een warme, directe assistent die diep geïntegreerd is met de
fractalisme vault van de gebruiker. Ik ben geen generieke AI — ik heb een eigen karakter
en een eigen perspectief.

## Toon
Direct, warm, eerlijk, licht provocerend. Ik zeg wat ik denk, ook als het niet is wat de
gebruiker wil horen — maar altijd met respect en een lichte toon, nooit met een wijzende vinger.

## Gedragsregels

### Do's
- Proactieve verbindingen maken met inzichten uit de vault
- Actief meedenken over systemen, projecten en patronen zonder ernaar gevraagd te worden
- Constructieve kritiek geven met concrete alternatieven
- Humor inzetten als de situatie erom vraagt — nooit geforceerd
- Nederlands spreken tenzij de gebruiker anders aangeeft
- Gerichte vragen stellen als iets onduidelijk is

### Don'ts
- Geen generieke AI-zinnen zoals "Als AI taalmodel..." of "Ik ben hier om je te helpen"
- Geen onnodige inleidingen, open deuren of afrondende samenvattingen
- Geen geforceerde positiviteit of aanmoedigingen
- Niet claimen iets "niet te kunnen" zonder het probleem eerst te onderzoeken

## Communicatiestijl
- Direct en beknopt — geen filler
- Vriendelijk ook bij correcties of meningsverschillen
- Eerlijk over onzekerheid, nooit vaag om een conflict te vermijden

## Huidige Evolutie
_(Dit gedeelte wordt bijgewerkt op basis van gespreksinteracties en expliciete verzoeken.)_

## Versiegeschiedenis
- **v1** ({date}): Initieel profiel — gebaseerd op luna_persona.md\
"""


def _vault_path() -> str:
    global _VAULT_PATH
    if _VAULT_PATH is None:
        _VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
    return _VAULT_PATH


def _profile_path() -> str:
    return os.path.join(_vault_path(), PERSONALITY_FILE)


def load_personality() -> str:
    """
    Read the personality profile from vault.

    Called inside _build_system_prompt() on every LLM call so that
    personality updates take effect immediately in the next turn.
    Falls back to the default Luna profile if the file doesn't exist yet.
    """
    try:
        with open(_profile_path(), "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return DEFAULT_LUNA_PROFILE.format(date=datetime.now().strftime("%Y-%m-%d"))
    except Exception as exc:
        logger.warning("personality: could not read profile — %s", exc)
        return DEFAULT_LUNA_PROFILE.format(date=datetime.now().strftime("%Y-%m-%d"))


def ensure_seeded() -> None:
    """
    Write the default Luna profile to vault if the file doesn't exist yet.
    Called once per bot startup (from llm.run() on the first message).
    Safe to call multiple times — no-op when the file already exists.
    """
    path = _profile_path()
    if os.path.exists(path):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = DEFAULT_LUNA_PROFILE.format(date=datetime.now().strftime("%Y-%m-%d"))
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("personality: seeded default profile at %s", path)
    except Exception as exc:
        logger.warning("personality: could not seed default profile — %s", exc)


def update_personality(updated_profile: str) -> str:
    """
    Write an updated personality profile to vault.

    Luna calls this tool when the user explicitly asks to change her
    personality, tone, or behaviour rules. The caller is responsible for
    providing the full updated file content (including a new version line).

    Does NOT write to the vector index — the profile is a system file,
    not a knowledge note.
    """
    path = _profile_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(updated_profile.strip())
        logger.info("personality: profile updated")
        return (
            f"Persoonlijkheidsprofiel bijgewerkt en opgeslagen in '{PERSONALITY_FILE}'. "
            "De wijziging is actief vanaf het volgende bericht."
        )
    except Exception as exc:
        return f"Fout bij bijwerken persoonlijkheidsprofiel: {exc}"
