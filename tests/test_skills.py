"""Unit tests for the SKILLS.md subsystem (skills_loader + load_skill tool)."""

import os
import sys
import textwrap
from pathlib import Path

import pytest

# Ensure the project root is on sys.path when running tests directly.
sys.path.insert(0, str(Path(__file__).parent.parent))

import skills_loader
from skills_loader import (
    Skill,
    SkillMeta,
    _split_frontmatter,
    build_manifest,
    format_skill_for_context,
    parse_skills_md,
    scan_skills,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

VALID_SKILLS_MD = textwrap.dedent("""\
    ---
    name: test_skill
    description: "A test skill for unit testing."
    version: 2.1.0
    tools:
      - read_vault
      - write_vault
    dependencies:
      - requests
    ---

    # Test Skill

    Do the thing.
""")

MINIMAL_SKILLS_MD = textwrap.dedent("""\
    ---
    name: minimal
    description: Minimal skill with no optional fields.
    ---

    Body text.
""")


# ──────────────────────────────────────────────────────────────────────────────
# _split_frontmatter
# ──────────────────────────────────────────────────────────────────────────────

class TestSplitFrontmatter:
    def test_splits_correctly(self):
        raw_yaml, body = _split_frontmatter(VALID_SKILLS_MD)
        assert "name: test_skill" in raw_yaml
        assert "# Test Skill" in body

    def test_missing_opening_delimiter_raises(self):
        with pytest.raises(ValueError, match="---"):
            _split_frontmatter("name: foo\ndescription: bar\n---\nBody")

    def test_missing_closing_delimiter_raises(self):
        with pytest.raises(ValueError, match="closed"):
            _split_frontmatter("---\nname: foo\n")

    def test_empty_body_is_ok(self):
        text = "---\nname: foo\ndescription: bar\n---\n"
        _, body = _split_frontmatter(text)
        assert body == ""


# ──────────────────────────────────────────────────────────────────────────────
# parse_skills_md
# ──────────────────────────────────────────────────────────────────────────────

class TestParseSkillsMd:
    def test_parses_full_skill(self):
        skill = parse_skills_md(VALID_SKILLS_MD)
        assert skill.meta.name == "test_skill"
        assert skill.meta.description == "A test skill for unit testing."
        assert skill.meta.version == "2.1.0"
        assert skill.meta.tools == ["read_vault", "write_vault"]
        assert skill.meta.dependencies == ["requests"]
        assert "# Test Skill" in skill.body

    def test_parses_minimal_skill(self):
        skill = parse_skills_md(MINIMAL_SKILLS_MD)
        assert skill.meta.name == "minimal"
        assert skill.meta.version == "1.0.0"
        assert skill.meta.tools == []
        assert skill.meta.dependencies == []
        assert "Body text." in skill.body

    def test_missing_name_raises(self):
        text = "---\ndescription: A skill.\n---\nBody"
        with pytest.raises(ValueError, match="name"):
            parse_skills_md(text)

    def test_missing_description_raises(self):
        text = "---\nname: foo\n---\nBody"
        with pytest.raises(ValueError, match="description"):
            parse_skills_md(text)

    def test_invalid_yaml_raises(self):
        text = "---\n: bad: yaml: [\n---\nBody"
        with pytest.raises(ValueError, match="YAML"):
            parse_skills_md(text)

    def test_source_path_stored(self):
        skill = parse_skills_md(MINIMAL_SKILLS_MD, source_path="/some/path/SKILLS.md")
        assert skill.meta.source_path == "/some/path/SKILLS.md"


# ──────────────────────────────────────────────────────────────────────────────
# scan_skills (filesystem integration — uses tmp_path fixture)
# ──────────────────────────────────────────────────────────────────────────────

class TestScanSkills:
    def _write_skill(self, base: Path, skill_name: str, content: str) -> Path:
        skill_dir = base / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILLS.md").write_text(content)
        return skill_dir

    def test_scans_single_skill(self, tmp_path):
        self._write_skill(tmp_path, "alpha", VALID_SKILLS_MD)
        skills = scan_skills(extra_dirs=[tmp_path])
        names = {s.meta.name for s in skills}
        assert "test_skill" in names

    def test_scans_multiple_skills(self, tmp_path):
        self._write_skill(tmp_path, "alpha", VALID_SKILLS_MD)
        self._write_skill(tmp_path, "beta", MINIMAL_SKILLS_MD)
        skills = scan_skills(extra_dirs=[tmp_path])
        names = {s.meta.name for s in skills}
        assert "test_skill" in names
        assert "minimal" in names

    def test_malformed_skill_skipped(self, tmp_path):
        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        (bad_dir / "SKILLS.md").write_text("no frontmatter at all")
        self._write_skill(tmp_path, "good", MINIMAL_SKILLS_MD)
        skills = scan_skills(extra_dirs=[tmp_path])
        names = {s.meta.name for s in skills}
        assert "minimal" in names
        assert len([s for s in skills if s.meta.name == "bad"]) == 0

    def test_context_files_loaded(self, tmp_path):
        skill_dir = self._write_skill(tmp_path, "ctx_skill", MINIMAL_SKILLS_MD)
        (skill_dir / "extra.md").write_text("Extra context.")
        skills = scan_skills(extra_dirs=[tmp_path])
        skill = next(s for s in skills if s.meta.name == "minimal")
        assert "extra.md" in skill.context_files
        assert skill.context_files["extra.md"] == "Extra context."

    def test_nonexistent_extra_dir_ignored(self, tmp_path):
        fake = tmp_path / "nonexistent"
        skills = scan_skills(extra_dirs=[fake])  # should not raise
        assert isinstance(skills, list)

    def test_duplicate_name_last_wins(self, tmp_path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir(); dir_b.mkdir()
        (dir_a / "SKILLS.md").write_text(
            "---\nname: dup\ndescription: Version A.\n---\nA body"
        )
        (dir_b / "SKILLS.md").write_text(
            "---\nname: dup\ndescription: Version B.\n---\nB body"
        )
        skills = scan_skills(extra_dirs=[tmp_path])
        dups = [s for s in skills if s.meta.name == "dup"]
        assert len(dups) == 1
        assert dups[0].meta.description == "Version B."


# ──────────────────────────────────────────────────────────────────────────────
# build_manifest
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildManifest:
    def _make_skill(self, name: str, description: str, tools: list[str] | None = None) -> Skill:
        meta = SkillMeta(name=name, description=description, tools=tools or [])
        return Skill(meta=meta, body="Body.")

    def test_empty_list_returns_empty_string(self):
        assert build_manifest([]) == ""

    def test_contains_skill_names(self):
        skills = [self._make_skill("foo", "Foo skill"), self._make_skill("bar", "Bar skill")]
        manifest = build_manifest(skills)
        assert "foo" in manifest
        assert "bar" in manifest

    def test_contains_descriptions(self):
        skills = [self._make_skill("foo", "Does the foo thing")]
        manifest = build_manifest(skills)
        assert "Does the foo thing" in manifest

    def test_xml_block_present(self):
        skills = [self._make_skill("foo", "A skill")]
        manifest = build_manifest(skills)
        assert "<available_skills>" in manifest
        assert "</available_skills>" in manifest

    def test_tools_listed_when_present(self):
        skills = [self._make_skill("foo", "A skill", tools=["read_vault"])]
        manifest = build_manifest(skills)
        assert "read_vault" in manifest

    def test_sorted_alphabetically(self):
        skills = [self._make_skill("zebra", "Z skill"), self._make_skill("apple", "A skill")]
        manifest = build_manifest(skills)
        assert manifest.index("apple") < manifest.index("zebra")


# ──────────────────────────────────────────────────────────────────────────────
# format_skill_for_context
# ──────────────────────────────────────────────────────────────────────────────

class TestFormatSkillForContext:
    def test_includes_name(self):
        skill = parse_skills_md(VALID_SKILLS_MD)
        output = format_skill_for_context(skill)
        assert "test_skill" in output

    def test_includes_body(self):
        skill = parse_skills_md(VALID_SKILLS_MD)
        output = format_skill_for_context(skill)
        assert "# Test Skill" in output

    def test_includes_context_files(self):
        skill = parse_skills_md(MINIMAL_SKILLS_MD)
        skill.context_files = {"notes.md": "Some notes."}
        output = format_skill_for_context(skill)
        assert "notes.md" in output
        assert "Some notes." in output


# ──────────────────────────────────────────────────────────────────────────────
# load_skill tool function
# ──────────────────────────────────────────────────────────────────────────────

class TestLoadSkillTool:
    def test_missing_skill_name_returns_error(self):
        from tools.skill_tools import load_skill
        result = load_skill({})
        assert "required" in result.lower() or "error" in result.lower()

    def test_unknown_skill_name_returns_error(self, tmp_path, monkeypatch):
        # Point the scanner at an empty directory so no skills are found.
        monkeypatch.setattr(skills_loader, "_DEFAULT_SKILLS_DIR", tmp_path)
        from tools.skill_tools import load_skill
        result = load_skill({"skill_name": "nonexistent_skill"})
        assert "not found" in result.lower() or "error" in result.lower()

    def test_valid_skill_returns_instructions(self, tmp_path, monkeypatch):
        # Create a real skill file and point the scanner at it.
        skill_dir = tmp_path / "myskill"
        skill_dir.mkdir()
        (skill_dir / "SKILLS.md").write_text(VALID_SKILLS_MD)
        monkeypatch.setattr(skills_loader, "_DEFAULT_SKILLS_DIR", tmp_path)
        # Invalidate cache so the fresh directory is picked up.
        skills_loader.invalidate_manifest_cache()
        from tools.skill_tools import load_skill
        result = load_skill({"skill_name": "test_skill"})
        assert "test_skill" in result
        assert "# Test Skill" in result
