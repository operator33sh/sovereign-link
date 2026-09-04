"""Tests voor link_injector.scanner — strip_all_links en build_vault_map."""

import os
import tempfile
import pytest

from link_injector.scanner import _strip_wikilinks, strip_all_links, build_vault_map, VAULT_PATH


def test_strip_wikilinks_removes_links():
    content = "Zie ook [[Bewustzijn]] en [[Fractalisme]]."
    result = _strip_wikilinks(content)
    assert "[[" not in result
    assert "]]" not in result


def test_strip_wikilinks_removes_empty_gerelateerd():
    content = "## Gerelateerd\n\n## Volgende sectie\n"
    result = _strip_wikilinks(content)
    assert "Gerelateerd" not in result


def test_strip_wikilinks_collapses_blank_lines():
    content = "Alinea 1.\n\n\n\nAlinea 2.\n"
    result = _strip_wikilinks(content)
    assert "\n\n\n" not in result


def test_strip_all_links_dry_run(tmp_path, monkeypatch):
    """strip_all_links --dry-run mag geen bestanden wijzigen."""
    md = tmp_path / "test.md"
    original = "Tekst met [[Link]].\n"
    md.write_text(original, encoding="utf-8")

    monkeypatch.setattr("link_injector.scanner.VAULT_PATH", str(tmp_path))
    count = strip_all_links(dry_run=True)

    assert count == 1
    assert md.read_text(encoding="utf-8") == original


def test_strip_all_links_modifies(tmp_path, monkeypatch):
    md = tmp_path / "test.md"
    md.write_text("Tekst met [[Link]].\n", encoding="utf-8")

    monkeypatch.setattr("link_injector.scanner.VAULT_PATH", str(tmp_path))
    count = strip_all_links(dry_run=False)

    assert count == 1
    assert "[[" not in md.read_text(encoding="utf-8")


def test_build_vault_map_basic(tmp_path, monkeypatch):
    (tmp_path / "note1.md").write_text("# Bewustzijn\nIets over bewustzijn.\n", encoding="utf-8")
    (tmp_path / "note2.md").write_text("# Fractalisme\nPatronen in systemen.\n", encoding="utf-8")

    monkeypatch.setattr("link_injector.scanner.VAULT_PATH", str(tmp_path))
    entries = build_vault_map()

    assert len(entries) == 2
    titles = {e["title"] for e in entries}
    assert "Bewustzijn" in titles
    assert "Fractalisme" in titles


def test_build_vault_map_skips_hidden(tmp_path, monkeypatch):
    (tmp_path / "visible.md").write_text("# Zichtbaar\n", encoding="utf-8")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.md").write_text("# Verborgen\n", encoding="utf-8")

    monkeypatch.setattr("link_injector.scanner.VAULT_PATH", str(tmp_path))
    entries = build_vault_map()

    assert len(entries) == 1
    assert entries[0]["title"] == "Zichtbaar"
