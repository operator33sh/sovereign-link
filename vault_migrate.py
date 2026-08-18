#!/usr/bin/env python3
"""
vault_migrate.py — Fractalisme Vault Migration to Functional Architecture

Reorganises an Obsidian-style vault from topical folders to a functional
topography:

    00_Atlas    — MOCs and indices
    10_Kern     — Axioms and foundational principles
    20_Werk     — Active experiments, drafts, developing theories (default)
    30_Proces   — Chronological logs, memory logs
    40_Systeem  — Templates, config, maintenance
    90_Archief  — Deprecated or completed projects

Usage:
    # Dry run (no files moved, shows proposed changes)
    python3 vault_migrate.py --vault /path/to/vault

    # Execute migration
    python3 vault_migrate.py --vault /path/to/vault --execute

    # Also inject #status/unclassified into YAML frontmatter
    python3 vault_migrate.py --vault /path/to/vault --execute --inject-status

    # Custom backup directory
    python3 vault_migrate.py --vault /path/to/vault --execute --backup-dir /tmp/vault_backup
"""

import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Functional folders
# ---------------------------------------------------------------------------

FUNCTIONAL_ROOTS = [
    "00_Atlas",
    "10_Kern",
    "20_Werk",
    "30_Proces",
    "40_Systeem",
    "90_Archief",
]

# Default destination for anything not matched by a heuristic
DEFAULT_DEST = "20_Werk"

# ---------------------------------------------------------------------------
# Classification heuristics
# (checked in order; first match wins)
# ---------------------------------------------------------------------------

# Each rule: (destination, match_fn(path: Path, rel: str) -> bool)
def _make_rules() -> list:
    return [
        # 30_Proces: log files, diary/daily notes, memory logs
        ("30_Proces", lambda p, r: (
            any(kw in p.stem for kw in ("Log", "log", "Daily", "Diary", "SovereignLog"))
            or any(seg.lower() in ("diary", "daily", "logs", "proces", "process", "journal")
                   for seg in Path(r).parts[:-1])
        )),

        # 40_Systeem: templates and system/config files
        ("40_Systeem", lambda p, r: (
            any(seg.lower() in ("templates", "template", "systeem", "system", "config", "maintenance")
                for seg in Path(r).parts[:-1])
            or p.stem.lower().startswith("template")
        )),

        # 00_Atlas: hub / MOC / index files
        ("00_Atlas", lambda p, r: (
            any(kw in p.stem for kw in ("MOC", "Index", "Atlas", "Hub", "Overzicht", "Map"))
            or any(seg.lower() in ("atlas", "moc", "index", "indices")
                   for seg in Path(r).parts[:-1])
        )),

        # 10_Kern: axioms and foundational principles
        ("10_Kern", lambda p, r: (
            any(kw in p.stem for kw in ("Axiom", "Principe", "Kern", "Fundament", "Grondslag"))
            or any(seg.lower() in ("kern", "axioms", "principes", "foundational")
                   for seg in Path(r).parts[:-1])
        )),

        # 90_Archief: deprecated / archived / completed
        ("90_Archief", lambda p, r: (
            any(kw in p.stem for kw in ("Archief", "Archive", "Deprecated", "Completed", "Oud"))
            or any(seg.lower() in ("archief", "archive", "deprecated", "completed", "oud")
                   for seg in Path(r).parts[:-1])
        )),
    ]


def classify(path: Path, rel: str, rules: list) -> str:
    for dest, match_fn in rules:
        try:
            if match_fn(path, rel):
                return dest
        except Exception:
            pass
    return DEFAULT_DEST


# ---------------------------------------------------------------------------
# YAML frontmatter injection
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_STATUS_TAG = "#status/unclassified"


def inject_status_tag(content: str) -> str:
    """Add #status/unclassified to YAML frontmatter tags, or prepend frontmatter."""
    match = _FRONTMATTER_RE.match(content)
    if match:
        fm = match.group(1)
        # Already tagged — skip
        if _STATUS_TAG in fm:
            return content
        # Find existing tags: field
        tags_match = re.search(r"^(tags\s*:\s*)(.*)$", fm, re.MULTILINE)
        if tags_match:
            existing = tags_match.group(2).strip()
            if existing.startswith("["):
                # Inline list: tags: [a, b]
                new_tags = existing[:-1] + f", {_STATUS_TAG}]"
            elif existing.startswith("-"):
                # Block list — append after last item
                new_tags = existing + f"\n  - {_STATUS_TAG}"
            else:
                # Single value
                new_tags = f"[{existing}, {_STATUS_TAG}]"
            new_fm = fm[: tags_match.start(2)] + new_tags + fm[tags_match.end(2):]
        else:
            # No tags field — add one
            new_fm = fm + f"\ntags: [{_STATUS_TAG}]"
        return content[: match.start(1)] + new_fm + content[match.end(1):]
    else:
        # No frontmatter — prepend minimal block
        return f"---\ntags: [{_STATUS_TAG}]\n---\n\n" + content


# ---------------------------------------------------------------------------
# Core migration logic
# ---------------------------------------------------------------------------

def collect_migrations(vault: Path, rules: list) -> list[dict]:
    """
    Walk the vault and build a list of proposed moves.
    Skips files already inside functional roots and hidden files/dirs.
    Returns list of dicts: {src, dst, dest_root, rel_src}
    """
    functional_roots = set(FUNCTIONAL_ROOTS)
    migrations = []

    for dirpath, dirnames, filenames in os.walk(vault):
        # Skip hidden dirs and functional root dirs in-place
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".")
            and d not in functional_roots
        ]

        current = Path(dirpath)
        rel_current = current.relative_to(vault)

        # Skip if we're already inside a functional root
        if rel_current.parts and rel_current.parts[0] in functional_roots:
            continue

        for fname in filenames:
            if fname.startswith("."):
                continue
            if not fname.endswith(".md"):
                # Only migrate markdown notes; leave attachments in place for now
                continue

            src = current / fname
            rel_src = str(src.relative_to(vault))
            dest_root = classify(src, rel_src, rules)

            # Preserve sub-path relative to vault root (minus the first segment
            # if it's a topical folder that will be removed)
            rel_parts = Path(rel_src).parts
            if len(rel_parts) > 1:
                # Keep intermediate structure under the functional root
                sub = Path(*rel_parts[1:]) if len(rel_parts) > 1 else Path(fname)
            else:
                sub = Path(fname)

            dst = vault / dest_root / sub

            # Skip if source and destination are identical (already in place)
            if src == dst:
                continue

            migrations.append({
                "src": src,
                "dst": dst,
                "dest_root": dest_root,
                "rel_src": rel_src,
            })

    return migrations


def resolve_conflicts(migrations: list[dict]) -> list[dict]:
    """Ensure no two files are mapped to the same destination."""
    seen: dict[Path, int] = {}
    resolved = []
    for m in migrations:
        dst = m["dst"]
        if dst in seen:
            stem = dst.stem
            suffix = dst.suffix
            counter = seen[dst]
            seen[dst] += 1
            m = dict(m)
            m["dst"] = dst.parent / f"{stem}_{counter}{suffix}"
        else:
            seen[dst] = 1
        resolved.append(m)
    return resolved


def print_dry_run(migrations: list[dict], vault: Path) -> None:
    by_dest: dict[str, list] = {}
    for m in migrations:
        by_dest.setdefault(m["dest_root"], []).append(m)

    print(f"\n{'='*70}")
    print(f"  DRY RUN — Proposed Changes ({len(migrations)} files)")
    print(f"  Vault: {vault}")
    print(f"{'='*70}\n")

    for dest in FUNCTIONAL_ROOTS:
        items = by_dest.get(dest, [])
        if not items:
            continue
        print(f"  [{dest}]  ({len(items)} files)")
        for m in items:
            src_rel = m["src"].relative_to(vault)
            dst_rel = m["dst"].relative_to(vault)
            print(f"    {src_rel}")
            print(f"    → {dst_rel}\n")

    print(f"{'='*70}")
    print(f"  Total: {len(migrations)} files to move")
    print(f"  Run with --execute to apply.\n")


def backup_vault(vault: Path, backup_dir: Path | None) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if backup_dir is None:
        backup_dir = vault.parent / f"{vault.name}_backup_{ts}"
    else:
        backup_dir = backup_dir / f"{vault.name}_backup_{ts}"

    print(f"Creating backup: {backup_dir} …", end=" ", flush=True)
    shutil.copytree(vault, backup_dir, symlinks=True)
    print("done.")
    return backup_dir


def execute_migrations(
    migrations: list[dict],
    vault: Path,
    inject_status: bool,
) -> None:
    # Create functional root dirs
    for root in FUNCTIONAL_ROOTS:
        (vault / root).mkdir(exist_ok=True)

    moved = 0
    errors = 0

    for m in migrations:
        src: Path = m["src"]
        dst: Path = m["dst"]

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)

            if inject_status and src.suffix == ".md":
                content = src.read_text(encoding="utf-8")
                new_content = inject_status_tag(content)
                dst.write_text(new_content, encoding="utf-8")
                src.unlink()
            else:
                shutil.move(str(src), dst)

            moved += 1
        except Exception as e:
            print(f"  ERROR moving {src.relative_to(vault)}: {e}", file=sys.stderr)
            errors += 1

    print(f"\nMigration complete: {moved} moved, {errors} errors.")


def remove_empty_topical_dirs(vault: Path) -> None:
    """Remove empty directories that are not functional roots or hidden."""
    functional_roots = set(FUNCTIONAL_ROOTS)
    removed = 0

    # Walk bottom-up so children are processed before parents
    for dirpath, dirnames, filenames in os.walk(vault, topdown=False):
        current = Path(dirpath)
        if current == vault:
            continue
        rel = current.relative_to(vault)
        # Never remove functional roots or hidden dirs
        if rel.parts[0] in functional_roots or any(p.startswith(".") for p in rel.parts):
            continue
        # Remove if empty
        try:
            if not any(current.iterdir()):
                current.rmdir()
                removed += 1
        except Exception:
            pass

    if removed:
        print(f"Removed {removed} empty topical directories.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate Fractalisme vault to Functional Topography architecture.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--vault", required=True, help="Path to the vault root directory.")
    parser.add_argument("--execute", action="store_true", help="Apply the migration (default: dry run).")
    parser.add_argument("--inject-status", action="store_true",
                        help="Inject #status/unclassified tag into YAML frontmatter of migrated notes.")
    parser.add_argument("--backup-dir", default=None,
                        help="Parent directory for the timestamped backup. Default: next to vault.")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip backup step (not recommended).")
    parser.add_argument("--remove-empty", action="store_true",
                        help="Remove empty topical directories after migration.")
    args = parser.parse_args()

    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        print(f"Error: vault path does not exist or is not a directory: {vault}", file=sys.stderr)
        sys.exit(1)

    rules = _make_rules()
    migrations = collect_migrations(vault, rules)
    migrations = resolve_conflicts(migrations)

    if not migrations:
        print("No files to migrate — vault already follows functional architecture.")
        return

    if not args.execute:
        print_dry_run(migrations, vault)
        return

    # Execute
    if not args.no_backup:
        backup_dir = Path(args.backup_dir).expanduser().resolve() if args.backup_dir else None
        backup_vault(vault, backup_dir)

    print(f"\nMigrating {len(migrations)} files …")
    execute_migrations(migrations, vault, inject_status=args.inject_status)

    if args.remove_empty:
        remove_empty_topical_dirs(vault)

    print("\nDone. Review in Obsidian — links should resolve automatically via its link-resolver.")
    if args.inject_status:
        print("Search for '#status/unclassified' in Obsidian to begin your Discernment Layer audit.")


if __name__ == "__main__":
    main()
