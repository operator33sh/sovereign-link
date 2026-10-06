"""
skills_loader.py — SKILLS.md subsystem for Sovereign-Link.

Responsibilities:
  1. Scan ./skills/*/SKILLS.md (and extra dirs from SKILLS_DIRS env var).
  2. Parse YAML frontmatter (name, description, version, tools, dependencies)
     and separate it from the markdown body.
  3. Build a SkillManifest (names + descriptions only) for system-prompt injection.
  4. Expose load_skill(name) → full markdown body + context files for on-demand
     injection into the context window.

Extra skill directories: set SKILLS_DIRS in .env as a colon-separated list of
absolute paths, e.g. SKILLS_DIRS=/opt/skills:/home/user/my-skills
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# Project-local skills directory (relative to this file).
_DEFAULT_SKILLS_DIR = Path(__file__).parent / "skills"

# ──────────────────────────────────────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class SkillMeta:
    name: str
    description: str
    version: str = "1.0.0"
    tools: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    # Absolute path to the SKILLS.md file this was loaded from.
    source_path: str = ""


@dataclass
class Skill:
    meta: SkillMeta
    # Full markdown body (everything after the frontmatter block).
    body: str
    # Dict of {filename: content} for any extra files in the skill directory.
    context_files: dict[str, str] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# Frontmatter parser
# ──────────────────────────────────────────────────────────────────────────────

_FRONTMATTER_SENTINEL = "---"


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split a SKILLS.md string into (raw_yaml, markdown_body).

    Raises ValueError if frontmatter delimiters are missing or malformed.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != _FRONTMATTER_SENTINEL:
        raise ValueError("SKILLS.md must begin with '---' (YAML frontmatter delimiter)")

    # Find the closing '---'
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip() == _FRONTMATTER_SENTINEL:
            raw_yaml = "".join(lines[1:i])
            body = "".join(lines[i + 1:]).lstrip("\n")
            return raw_yaml, body

    raise ValueError("SKILLS.md frontmatter is never closed (missing closing '---')")


def parse_skills_md(text: str, source_path: str = "") -> Skill:
    """Parse the full text of a SKILLS.md file into a Skill dataclass.

    Raises ValueError for invalid/missing frontmatter fields.
    """
    raw_yaml, body = _split_frontmatter(text)

    try:
        fm = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML frontmatter parse error: {exc}") from exc

    if not isinstance(fm, dict):
        raise ValueError("Frontmatter must be a YAML mapping (key: value pairs)")

    name = fm.get("name", "").strip()
    if not name:
        raise ValueError("Frontmatter missing required field: 'name'")

    description = fm.get("description", "").strip()
    if not description:
        raise ValueError(f"Skill '{name}': frontmatter missing required field: 'description'")

    meta = SkillMeta(
        name=name,
        description=description,
        version=str(fm.get("version", "1.0.0")),
        tools=list(fm.get("tools") or []),
        dependencies=list(fm.get("dependencies") or []),
        source_path=source_path,
    )
    return Skill(meta=meta, body=body)


# ──────────────────────────────────────────────────────────────────────────────
# Directory scanner
# ──────────────────────────────────────────────────────────────────────────────


def _extra_skill_dirs() -> list[Path]:
    """Return additional skill directories from the SKILLS_DIRS environment variable."""
    raw = os.environ.get("SKILLS_DIRS", "").strip()
    if not raw:
        return []
    dirs = []
    for part in raw.split(":"):
        p = Path(part.strip())
        if p.is_dir():
            dirs.append(p)
        else:
            logger.warning("SKILLS_DIRS: '%s' is not a directory, skipping", p)
    return dirs


def _load_context_files(skill_dir: Path) -> dict[str, str]:
    """Read all non-SKILLS.md files in a skill directory as context.

    Only reads .md, .txt, .json, .yaml, .yml, .toml files to avoid
    accidentally loading binaries.
    """
    allowed_suffixes = {".md", ".txt", ".json", ".yaml", ".yml", ".toml"}
    result: dict[str, str] = {}
    for f in sorted(skill_dir.iterdir()):
        if f.name == "SKILLS.md" or f.suffix.lower() not in allowed_suffixes:
            continue
        try:
            result[f.name] = f.read_text(encoding="utf-8")
        except Exception:
            logger.debug("Could not read context file '%s'", f)
    return result


def scan_skills(extra_dirs: list[Path] | None = None) -> list[Skill]:
    """Traverse all skill directories and return a list of parsed Skills.

    Directories searched (in order):
      1. ./skills/ (project-local)
      2. Any directories in SKILLS_DIRS env var
      3. extra_dirs argument (for testing/programmatic use)

    Skills with duplicate names: last one wins (with a warning).
    Malformed SKILLS.md files are skipped (with an error log).
    """
    search_dirs = [_DEFAULT_SKILLS_DIR] + _extra_skill_dirs() + (extra_dirs or [])

    seen: dict[str, Skill] = {}

    for base_dir in search_dirs:
        if not base_dir.is_dir():
            continue
        for skills_md in sorted(base_dir.rglob("SKILLS.md")):
            try:
                text = skills_md.read_text(encoding="utf-8")
                skill = parse_skills_md(text, source_path=str(skills_md))
                skill.context_files = _load_context_files(skills_md.parent)
                if skill.meta.name in seen:
                    logger.warning(
                        "Duplicate skill name '%s': overwriting '%s' with '%s'",
                        skill.meta.name,
                        seen[skill.meta.name].meta.source_path,
                        skills_md,
                    )
                seen[skill.meta.name] = skill
                logger.debug("Loaded skill '%s' from %s", skill.meta.name, skills_md)
            except Exception as exc:
                logger.error("Failed to load skill from '%s': %s", skills_md, exc)

    return list(seen.values())


# ──────────────────────────────────────────────────────────────────────────────
# Manifest generator + system-prompt formatter
# ──────────────────────────────────────────────────────────────────────────────


def build_manifest(skills: list[Skill]) -> str:
    """Compile a lightweight manifest: names and descriptions only.

    Returns the manifest as a formatted XML-style system-prompt block,
    or an empty string if there are no skills.
    """
    if not skills:
        return ""

    lines = ["<available_skills>"]
    for skill in sorted(skills, key=lambda s: s.meta.name):
        lines.append(f"  <skill name=\"{skill.meta.name}\" version=\"{skill.meta.version}\">")
        lines.append(f"    {skill.meta.description}")
        if skill.meta.tools:
            lines.append(f"    requires_tools: {', '.join(skill.meta.tools)}")
        lines.append("  </skill>")
    lines.append("</available_skills>")

    block = "\n".join(lines)
    return (
        "\n\n---\n\n"
        "## Beschikbare Skills\n\n"
        "Je hebt toegang tot de volgende uitbreidbare skills. "
        "Gebruik `load_skill` om de volledige instructies van een skill in te laden "
        "wanneer de gebruiker vraagt om die functionaliteit te gebruiken.\n\n"
        + block
    )


# ──────────────────────────────────────────────────────────────────────────────
# On-demand skill loader (used by the load_skill tool)
# ──────────────────────────────────────────────────────────────────────────────


def load_skill(name: str) -> Skill:
    """Return the full Skill object for the given name.

    Rescans the skills directories on every call so newly added skills are
    picked up without a bot restart.

    Raises KeyError if no skill with that name exists.
    """
    skills = scan_skills()
    index = {s.meta.name: s for s in skills}
    if name not in index:
        available = ", ".join(sorted(index)) or "(none)"
        raise KeyError(
            f"Skill '{name}' not found. Available skills: {available}"
        )
    return index[name]


def format_skill_for_context(skill: Skill) -> str:
    """Format a full Skill (body + context files) for injection into the context window."""
    parts = [
        f"## Skill: {skill.meta.name} (v{skill.meta.version})\n",
        f"**Description:** {skill.meta.description}\n",
    ]
    if skill.meta.tools:
        parts.append(f"**Required tools:** {', '.join(skill.meta.tools)}\n")
    if skill.meta.dependencies:
        parts.append(f"**Dependencies:** {', '.join(skill.meta.dependencies)}\n")
    parts.append("\n### Instructions\n")
    parts.append(skill.body)

    if skill.context_files:
        parts.append("\n### Context Files\n")
        for fname, content in skill.context_files.items():
            parts.append(f"\n#### {fname}\n```\n{content}\n```\n")

    return "".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# Module-level cached manifest (refreshed once per process start)
# ──────────────────────────────────────────────────────────────────────────────

_cached_manifest: Optional[str] = None


def get_manifest() -> str:
    """Return the cached system-prompt manifest, building it on first call."""
    global _cached_manifest
    if _cached_manifest is None:
        try:
            skills = scan_skills()
            _cached_manifest = build_manifest(skills)
        except Exception:
            logger.exception("skills_loader: failed to build manifest")
            _cached_manifest = ""
    return _cached_manifest


def invalidate_manifest_cache() -> None:
    """Force manifest regeneration on the next get_manifest() call."""
    global _cached_manifest
    _cached_manifest = None
