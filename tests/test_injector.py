"""Tests voor link_injector.injector — _do_inject en inject_links."""

import os
import pytest

from link_injector.injector import _do_inject, _insert_before_timestamps, inject_links
from link_injector.scanner import _TS_PATTERN


def test_do_inject_creates_section():
    content = "# Note\n\nInhoud.\n"
    result = _do_inject(content, "[[MOC_Bewustzijn]]", "Gerelateerd")
    assert "## Gerelateerd" in result
    assert "[[MOC_Bewustzijn]]" in result


def test_do_inject_appends_to_existing_section():
    content = "# Note\n\nInhoud.\n\n## Gerelateerd\n\n- [[BestaandeLink]]\n"
    result = _do_inject(content, "[[NieuweLink]]", "Gerelateerd")
    assert "[[BestaandeLink]]" in result
    assert "[[NieuweLink]]" in result


def test_inject_links_skips_duplicate(tmp_path, monkeypatch):
    """inject_links slaat een bestand over als de link er al in staat."""
    md = tmp_path / "note.md"
    md.write_text("# Note\n\n## Gerelateerd\n\n- [[Bewustzijn]]\n", encoding="utf-8")

    suggestions = [{"source": "note.md", "target": "Bewustzijn", "reason": "test"}]
    monkeypatch.setattr("link_injector.injector.VAULT_PATH", str(tmp_path))
    injected, skipped, errors, dry_log = inject_links(suggestions, dry_run=False)

    assert injected == 0
    assert skipped == 1
    assert md.read_text(encoding="utf-8").count("[[Bewustzijn]]") == 1


def test_insert_before_timestamps():
    content = "# Note\n\nInhoud.\n\n#2025-01-01 #10 #30\n"
    block = "## Gerelateerd\n\n- [[Link]]\n\n"
    result = _insert_before_timestamps(content, block)
    ts_pos = result.find("#2025-01-01")
    link_pos = result.find("[[Link]]")
    assert link_pos < ts_pos


def test_insert_before_timestamps_no_ts():
    content = "# Note\n\nInhoud.\n"
    block = "## Gerelateerd\n\n- [[Link]]\n\n"
    result = _insert_before_timestamps(content, block)
    assert "[[Link]]" in result


def test_inject_links_dry_run(tmp_path, monkeypatch):
    """inject_links --dry-run mag geen bestanden wijzigen."""
    md = tmp_path / "note.md"
    original = "# Note\n\nInhoud.\n"
    md.write_text(original, encoding="utf-8")

    suggestions = [{"source": "note.md", "target": "Bewustzijn", "reason": "test"}]

    monkeypatch.setattr("link_injector.injector.VAULT_PATH", str(tmp_path))
    injected, skipped, errors, dry_log = inject_links(suggestions, dry_run=True)

    assert injected == 1
    assert errors == 0
    assert md.read_text(encoding="utf-8") == original
