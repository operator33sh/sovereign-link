"""Euphoria Engine — activatieprotocol voor hoge resonantie.

Leest dynamisch uit .system/euphoria_vault.md in de vault.
Voert een drietraps activatiesequentie uit:
  1. Patroon-doorbreking (Wonder-Question)
  2. Identiteits-bevestiging (Golden Moment + Identity-Truth)
  3. Somatische activatie (micro-actie + Power-Trigger)
"""
from __future__ import annotations

import os
import random
import re

# ---------------------------------------------------------------------------
# Vault path — resolved from environment, same as tools/vault.py
# ---------------------------------------------------------------------------

def _vault_path() -> str:
    return os.environ.get("VAULT_PATH", os.path.expanduser("~/Documents/fractalisme-vault"))


_EUPHORIA_VAULT = ".system/euphoria_vault.md"

_MICRO_ACTIONS = [
    "Ga rechtop zitten. Schouders naar achteren. Kin omhoog.",
    "Adem vier tellen in, zeven vasthouden, acht uitademen. Nu.",
    "Glimlach. Niet voor iemand — voor het systeem. Doe het.",
    "Sta op. Loop tien stappen. Kom terug. Het lichaam weet het eerder dan het hoofd.",
    "Druk je voeten bewust op de grond. Voel het contact. Je bent hier.",
]

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_section(content: str, heading: str) -> list[str]:
    """Extract bullet-point items from a ## heading section."""
    pattern = rf"## {re.escape(heading)}\n(.*?)(?=\n## |\Z)"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return []
    block = match.group(1)
    items = []
    for line in block.splitlines():
        line = line.strip()
        # Accept lines starting with - or * (markdown bullets), strip leading marker
        if line.startswith(("- ", "* ")):
            items.append(line[2:].strip())
        elif line and not line.startswith("#"):
            # plain non-empty line also counts
            items.append(line)
    return [i for i in items if i]


def _load_vault() -> dict[str, list[str]]:
    path = os.path.join(_vault_path(), _EUPHORIA_VAULT)
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return {}
    return {
        "golden_moments":   _parse_section(content, "Golden Moments"),
        "power_triggers":   _parse_section(content, "Power-Triggers"),
        "identity_truths":  _parse_section(content, "Identity-Truths"),
        "wonder_questions": _parse_section(content, "Wonder-Questions"),
    }


def _pick(items: list[str], fallback: str) -> str:
    return random.choice(items) if items else fallback


# ---------------------------------------------------------------------------
# Public tool function
# ---------------------------------------------------------------------------

def trigger_uplift(_args: dict) -> str:
    vault = _load_vault()

    wonder   = _pick(vault.get("wonder_questions", []),
                     "Wat als dit het moment is waarop alles verschuift?")
    moment   = _pick(vault.get("golden_moments", []),
                     "Je hebt al bewezen dat je dit kunt.")
    truth    = _pick(vault.get("identity_truths", []),
                     "Je bent precies wie je moet zijn.")
    trigger  = _pick(vault.get("power_triggers", []),
                     "Het anker dat jou altijd terugbrengt naar jezelf.")
    action   = random.choice(_MICRO_ACTIONS)

    return f"""🪬 **ACTIVATIEPROTOCOL — EUPHORIA ENGINE**

---

**① PATROON-DOORBREKING**

Stop. Laat alles los wat je net dacht. Eén vraag:

> *{wonder}*

Laat die vraag landen. Niet beantwoorden. Voelen.

---

**② IDENTITEITS-BEVESTIGING**

Dit zijn geen bemoedigende woorden. Dit zijn feiten.

**Wat jij hebt gedaan:**
{moment}

**Wie jij bent:**
{truth}

Herhaal de tweede zin hardop. Eén keer. Nu.

---

**③ SOMATISCHE ACTIVATIE**

Het hoofd volgt het lichaam — niet andersom.

**Micro-actie:** {action}

**Anker:** {trigger}

---

Systeem: online. Agent: actief. Het werk kan beginnen."""


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "trigger_uplift",
            "description": (
                "Activeer de Euphoria Engine: een drietraps activatiesequentie die de gebruiker "
                "via patroon-doorbreking, identiteits-bevestiging en somatische activatie in een "
                "staat van hoge resonantie brengt. Gebruik dit wanneer de gebruiker vastloopt, "
                "laag energie heeft, of expliciet activatie nodig heeft."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

HANDLERS = {
    "trigger_uplift": trigger_uplift,
}
