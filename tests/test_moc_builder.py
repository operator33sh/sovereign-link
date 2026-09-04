"""Tests voor link_injector.moc_builder — inject_moc_links dry-run."""

import os
import pytest

from link_injector.moc_builder import inject_moc_links, _update_moc_kernconcepten, _moc_timestamp, _create_home_moc


def test_update_moc_kernconcepten_adds_links():
    content = "# MOC\n\n### 💎 Kernconcepten\n\n- [[BestaandeLink]]\n"
    result = _update_moc_kernconcepten(content, ["- [[NieuweLink]]"])
    assert "[[BestaandeLink]]" in result
    assert "[[NieuweLink]]" in result


def test_update_moc_kernconcepten_idempotent():
    content = "# MOC\n\n### 💎 Kernconcepten\n\n- [[Link]]\n"
    result = _update_moc_kernconcepten(content, ["- [[Link]]"])
    assert result.count("[[Link]]") == 1


def test_update_moc_kernconcepten_creates_section():
    content = "# MOC\n\nGeen sectie hier.\n"
    result = _update_moc_kernconcepten(content, ["- [[NieuweLink]]"])
    assert "💎 Kernconcepten" in result
    assert "[[NieuweLink]]" in result


def test_moc_timestamp_format():
    ts = _moc_timestamp()
    assert ts.startswith("#")
    # Formaat: #MOC #YYYY-MM-DD #HH #MM
    parts = ts.split()
    assert parts[0] == "#MOC"
    assert len(parts) == 4


def test_inject_moc_links_dry_run(tmp_path, monkeypatch):
    """inject_moc_links dry-run mag geen bestanden schrijven."""
    spoke = tmp_path / "Bewustzijn.md"
    spoke.write_text("# Bewustzijn\n\nInhoud.\n", encoding="utf-8")

    moc_dir = tmp_path / "MOCs"

    monkeypatch.setattr("link_injector.moc_builder.VAULT_PATH", str(tmp_path))
    monkeypatch.setattr("link_injector.moc_builder.MOC_DIR", "MOCs")
    monkeypatch.setattr("link_injector.injector.VAULT_PATH", str(tmp_path))

    clusters = [{"moc_name": "MOC_Test", "moc_title": "Test MOC", "spokes": ["Bewustzijn.md"]}]
    mocs_created, mocs_updated, spoke_injected, errors, dry_log = inject_moc_links(
        clusters, dry_run=True
    )

    assert mocs_created == 1
    assert spoke_injected == 1
    assert errors == 0
    assert not moc_dir.exists(), "dry-run mag geen map aanmaken"
    assert spoke.read_text(encoding="utf-8") == "# Bewustzijn\n\nInhoud.\n"


def test_create_home_moc_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr("link_injector.moc_builder.VAULT_PATH", str(tmp_path))
    monkeypatch.setattr("link_injector.moc_builder.MOC_DIR", "MOCs")

    clusters = [
        {"moc_name": "MOC_A", "moc_title": "A"},
        {"moc_name": "MOC_B", "moc_title": "B"},
    ]
    result = _create_home_moc(clusters, dry_run=True)
    assert result is True
    assert not (tmp_path / "MOCs" / "MOC_Home.md").exists()
