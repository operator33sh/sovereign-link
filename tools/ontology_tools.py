"""Ontology tools: schema validation and multi-hop relation traversal for the Sovereign Vault."""
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
ONTOLOGY_PATH = os.path.join(VAULT_PATH, ".system", "ontology.md")

# ---------------------------------------------------------------------------
# Entity type registry — derived from ontology.md, duplicated here for
# fast in-process validation without file I/O on every call.
# ---------------------------------------------------------------------------

ENTITY_TYPES = {
    "Insight", "Concept", "Protocol", "Operation",
    "Analysis", "Agent", "Project", "Log", "Reference",
}

RELATION_TYPES = {
    "supports", "contradicts", "belongs_to", "triggers",
    "defines", "instantiates", "references", "extends",
    "precedes", "resolves",
}

STS_VALID_TAGS = {
    "#ev-direct", "#ev-derived", "#ev-reported", "#ev-assumed", "#ev-luna-hypo",
}

# Required fields per entity type (beyond entity_type + ev_tag which are universal)
_REQUIRED_EXTRA: dict[str, list[str]] = {
    "Protocol":  ["status"],
    "Operation": ["status", "start_date"],
    "Analysis":  ["subject"],
    "Agent":     ["name"],
    "Reference": ["source"],
}

_VALID_STATUS = {
    "Protocol":  {"active", "deprecated", "draft"},
    "Operation": {"active", "completed", "paused", "abandoned"},
    "Project":   {"active", "completed", "paused", "abandoned"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> dict[str, Any]:
    """Extract YAML frontmatter key/value pairs as a flat dict (values as strings)."""
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end == -1:
        return {}
    fm_text = content[3:end]
    result: dict[str, Any] = {}
    for line in fm_text.splitlines():
        if ":" in line and not line.startswith(" ") and not line.startswith("-"):
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip().strip('"').strip("'")
    return result


def _parse_relations(content: str) -> list[dict[str, str]]:
    """
    Parse the `relations:` block in YAML frontmatter.

    Supports:
      relations:
        - type: supports
          target: "Some Note"
    """
    if not content.startswith("---"):
        return []
    end = content.find("\n---", 3)
    if end == -1:
        return []
    fm_text = content[3:end]

    relations: list[dict[str, str]] = []
    in_relations = False
    current: dict[str, str] = {}

    for line in fm_text.splitlines():
        stripped = line.strip()
        if stripped == "relations:":
            in_relations = True
            continue
        if in_relations:
            if stripped.startswith("- type:"):
                if current:
                    relations.append(current)
                current = {"type": stripped[len("- type:"):].strip().strip('"').strip("'")}
            elif stripped.startswith("type:"):
                if current:
                    relations.append(current)
                current = {"type": stripped[len("type:"):].strip().strip('"').strip("'")}
            elif stripped.startswith("target:"):
                current["target"] = stripped[len("target:"):].strip().strip('"').strip("'")
            elif stripped and not stripped.startswith("-") and ":" in stripped and not stripped.startswith(" "):
                # New top-level key — end of relations block
                if current:
                    relations.append(current)
                break

    if current:
        relations.append(current)
    return relations


def _vault_files() -> list[str]:
    """Recursively yield all .md paths relative to VAULT_PATH."""
    result = []
    for root, dirs, files in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                full = os.path.join(root, f)
                result.append(os.path.relpath(full, VAULT_PATH))
    return result


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------

def read_ontology() -> str:
    """Return the full contents of .system/ontology.md."""
    try:
        with open(ONTOLOGY_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "Error: ontology.md niet gevonden in .system/"
    except Exception as e:
        return f"Error: {e}"


def validate_note(entity_type: str, content: str) -> str:
    """
    Validate a note's content against the Sovereign Ontology.

    Checks:
    - entity_type is recognized
    - ev_tag is present and valid
    - Required fields for the entity type are present
    - status values are within allowed set (for typed entities)
    - relation types are recognized

    Returns "OK" if valid, or a list of violations to fix.
    """
    errors: list[str] = []

    # 1. entity_type
    if entity_type not in ENTITY_TYPES:
        errors.append(
            f"Onbekend entity_type: '{entity_type}'. "
            f"Toegestane types: {', '.join(sorted(ENTITY_TYPES))}."
        )

    # 2. Parse frontmatter
    fm = _parse_frontmatter(content)
    fm_entity = fm.get("entity_type", "")
    if fm_entity and fm_entity != entity_type:
        errors.append(
            f"entity_type in frontmatter ('{fm_entity}') "
            f"komt niet overeen met opgegeven type ('{entity_type}')."
        )
    if not fm_entity:
        errors.append("Ontbrekend veld in frontmatter: 'entity_type'.")

    # 3. ev_tag
    ev = fm.get("ev_tag", "")
    if not ev:
        errors.append("Ontbrekend veld in frontmatter: 'ev_tag'. Voeg een STS-tag toe.")
    elif ev not in STS_VALID_TAGS:
        errors.append(
            f"Ongeldig ev_tag: '{ev}'. "
            f"Toegestane waarden: {', '.join(sorted(STS_VALID_TAGS))}."
        )

    # 4. Type-specifieke verplichte velden
    for field in _REQUIRED_EXTRA.get(entity_type, []):
        if not fm.get(field):
            errors.append(f"Ontbrekend verplicht veld voor {entity_type}: '{field}'.")

    # 5. Status validatie
    if entity_type in _VALID_STATUS:
        status = fm.get("status", "")
        allowed = _VALID_STATUS[entity_type]
        if status and status not in allowed:
            errors.append(
                f"Ongeldige status '{status}' voor {entity_type}. "
                f"Toegestaan: {', '.join(sorted(allowed))}."
            )

    # 6. Relaties
    relations = _parse_relations(content)
    for rel in relations:
        rtype = rel.get("type", "")
        if rtype and rtype not in RELATION_TYPES:
            errors.append(
                f"Onbekend relatietype: '{rtype}'. "
                f"Toegestane types: {', '.join(sorted(RELATION_TYPES))}."
            )
        if not rel.get("target"):
            errors.append(f"Relatie van type '{rtype}' mist een 'target' veld.")

    if not errors:
        return "OK"
    return "Ontologie-validatie mislukt:\n" + "\n".join(f"- {e}" for e in errors)


def find_related(file_name: str, relation_type: str | None = None) -> str:
    """
    Multi-hop relation traversal: find all vault notes that declare a relation
    pointing TO `file_name`, or that `file_name` points to.

    If `relation_type` is given, filter to only that relation type.

    Returns a markdown-formatted list with file, relation type and direction.
    """
    if relation_type and relation_type not in RELATION_TYPES:
        return (
            f"Onbekend relatietype: '{relation_type}'. "
            f"Toegestane types: {', '.join(sorted(RELATION_TYPES))}."
        )

    # Normalize target name for matching (strip path prefix and .md)
    target_stem = os.path.splitext(os.path.basename(file_name))[0].lower()

    outbound: list[tuple[str, str, str]] = []   # (rel_type, target, file)
    inbound:  list[tuple[str, str, str]] = []   # (rel_type, source, file)

    # Load the note itself for outbound relations
    source_path = os.path.join(VAULT_PATH, file_name)
    if not source_path.endswith(".md"):
        source_path += ".md"
    try:
        with open(source_path, "r", encoding="utf-8") as f:
            source_content = f.read()
        for rel in _parse_relations(source_content):
            rtype = rel.get("type", "unknown")
            target = rel.get("target", "")
            if relation_type is None or rtype == relation_type:
                outbound.append((rtype, target, file_name))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("find_related: error reading source %s: %s", file_name, e)

    # Scan all vault files for inbound relations pointing to this file
    for rel_path in _vault_files():
        if rel_path == file_name or rel_path == file_name + ".md":
            continue
        full_path = os.path.join(VAULT_PATH, rel_path)
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue
        for rel in _parse_relations(content):
            rtype = rel.get("type", "unknown")
            target = rel.get("target", "")
            target_norm = os.path.splitext(os.path.basename(target))[0].lower()
            if target_norm == target_stem:
                if relation_type is None or rtype == relation_type:
                    inbound.append((rtype, rel_path, rel_path))

    if not outbound and not inbound:
        msg = f"Geen relaties gevonden voor `{file_name}`"
        if relation_type:
            msg += f" met type `{relation_type}`"
        return msg + "."

    lines = [f"## Relaties voor `{file_name}`\n"]

    if outbound:
        lines.append("### Uitgaand (dit bestand → andere notes)")
        for rtype, target, _ in outbound:
            lines.append(f"- `{rtype}` → [[{target}]]")
        lines.append("")

    if inbound:
        lines.append("### Inkomend (andere notes → dit bestand)")
        for rtype, source, _ in inbound:
            lines.append(f"- [[{os.path.splitext(source)[0]}]] `{rtype}` → dit bestand")
        lines.append("")

    return "\n".join(lines)


def get_entity_schema(entity_type: str) -> str:
    """Return a ready-to-use YAML frontmatter template for the given entity type."""
    if entity_type not in ENTITY_TYPES:
        return (
            f"Onbekend entity_type: '{entity_type}'. "
            f"Toegestane types: {', '.join(sorted(ENTITY_TYPES))}."
        )

    base = f"entity_type: {entity_type}\nev_tag: \"#ev-assumed\""
    extras = {
        "Protocol":  "status: draft",
        "Operation": "status: active\nstart_date: \"YYYY-MM-DD\"",
        "Analysis":  "subject: \"naam of situatie\"",
        "Agent":     "name: \"volledige naam\"",
        "Reference": "source: \"URL of boektitel\"",
    }
    extra = extras.get(entity_type, "")
    relations_block = (
        "relations:\n"
        "  - type: belongs_to\n"
        "    target: \"map of domein\""
    )
    parts = ["---", base]
    if extra:
        parts.append(extra)
    parts.append(relations_block)
    parts.append("---")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_ontology",
            "description": (
                "Lees het Sovereign Ontology schema uit .system/ontology.md. "
                "Gebruik dit voor je schrijft om te begrijpen welke entity types, "
                "relatie types en validatieregels van toepassing zijn."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_note",
            "description": (
                "Valideer de inhoud van een note tegen de Sovereign Ontologie voordat je write_vault aanroept. "
                "Geeft 'OK' terug als de note voldoet, anders een lijst met te corrigeren fouten. "
                "Verplicht te gebruiken voor alle semantische notes (Insight, Concept, Protocol, Operation, Analysis, Agent, Project, Reference)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "description": "Het entity type van de note (bijv. 'Insight', 'Protocol', 'Operation').",
                        "enum": sorted(ENTITY_TYPES),
                    },
                    "content": {
                        "type": "string",
                        "description": "De volledige inhoud van de note (inclusief YAML frontmatter).",
                    },
                },
                "required": ["entity_type", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_related",
            "description": (
                "Voer multi-hop redenering uit: vind alle vault-notes die via een relatietype "
                "verbonden zijn aan het opgegeven bestand. "
                "Geeft zowel uitgaande relaties (dit bestand → anderen) als inkomende (anderen → dit bestand). "
                "Gebruik dit voor vragen als: 'welke insights ondersteunen deze operatie?' of "
                "'wat definieert dit concept?'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "Relatief pad naar het vault-bestand (bijv. '40_Operations/MijnOperatie' of 'Sovereignty').",
                    },
                    "relation_type": {
                        "type": "string",
                        "description": "Filter op een specifiek relatietype. Weglaten = alle relatietypes.",
                        "enum": sorted(RELATION_TYPES),
                    },
                },
                "required": ["file_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_schema",
            "description": (
                "Genereer een kant-en-klare YAML frontmatter template voor een gegeven entity type. "
                "Gebruik dit als startpunt bij het aanmaken van een nieuwe semantische note."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "description": "Het gewenste entity type.",
                        "enum": sorted(ENTITY_TYPES),
                    },
                },
                "required": ["entity_type"],
            },
        },
    },
]

def _get_entity_type(args: dict) -> str:
    for key in ("entity_type", "type", "entity", "kind"):
        if key in args:
            return args[key]
    raise KeyError("entity_type")


def _get_content(args: dict) -> str:
    for key in ("content", "note", "text", "body"):
        if key in args:
            return args[key]
    raise KeyError("content")


HANDLERS = {
    "read_ontology": lambda args: read_ontology(),
    "validate_note": lambda args: validate_note(_get_entity_type(args), _get_content(args)),
    "find_related": lambda args: find_related(args.get("file_name") or args.get("filename") or args.get("path", ""), args.get("relation_type")),
    "get_entity_schema": lambda args: get_entity_schema(_get_entity_type(args)),
}
