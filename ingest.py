#!/usr/bin/env python3
"""Vault indexing tool.

Usage:
    python ingest.py            # start filesystem watcher only
    python ingest.py --rescan   # full vault rescan, then exit
    python ingest.py --backfill # fix timeline dates from #YYYY-MM-DD tags, then exit
"""
import logging
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from vector import index_file, start_vault_watcher, VAULT_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


_DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")
# Specific #YYYY-MM-DD hashtag — the canonical vault date tag
_DATETAG_IN_CONTENT = re.compile(r"#(\d{4}-\d{2}-\d{2})\b")


def _date_from_filename(path: Path) -> datetime | None:
    m = _DATE_IN_NAME.search(path.stem)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            pass
    return None


def _date_from_content(content: str) -> datetime | None:
    """Return the first #YYYY-MM-DD tag found in content, or None."""
    m = _DATETAG_IN_CONTENT.search(content)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            pass
    return None


def _date_from_git(path: Path, vault: Path) -> datetime | None:
    try:
        rel = str(path.relative_to(vault))
        result = subprocess.run(
            ["git", "-C", str(vault), "log", "--diff-filter=A", "--follow",
             "--format=%aI", "--", rel],
            capture_output=True, text=True, timeout=5,
        )
        first_line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if first_line:
            return datetime.fromisoformat(first_line)
    except Exception:
        pass
    return None


def ingest_all() -> None:
    vault = Path(VAULT_PATH)
    if not vault.exists():
        print(f"Vault not found: {VAULT_PATH}")
        sys.exit(1)

    md_files = sorted(vault.rglob("*.md"))
    print(f"Found {len(md_files)} markdown files in {VAULT_PATH}")

    for i, path in enumerate(md_files, 1):
        rel = str(path.relative_to(vault))
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            print(f"[{i}/{len(md_files)}] Skipping empty: {rel}")
            continue
        print(f"[{i}/{len(md_files)}] Indexing: {rel}")

        # Use mtime only as fallback for the vector DB timestamp field
        mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat()

        # Priority 1: #YYYY-MM-DD tag already in content
        date_dt = _date_from_content(content)
        source = "content-tag"

        if date_dt is None:
            # Priority 2: date from filename
            date_dt = _date_from_filename(path)
            source = "filename"

        if date_dt is None:
            # Priority 3: date from git history
            date_dt = _date_from_git(path, vault)
            source = "git"

        if date_dt is None:
            # No date found — warn and leave content untagged
            logging.warning(
                "[%d/%d] %s: no #YYYY-MM-DD tag found — add one for proper "
                "chronological indexing (falling back to filesystem date)",
                i, len(md_files), rel,
            )
        elif not _DATETAG_IN_CONTENT.search(content):
            # Inject #YYYY-MM-DD tag when content doesn't already have one
            date_tag = date_dt.strftime("#%Y-%m-%d")
            content = f"{date_tag}\n\n{content}"
            print(f"[{i}/{len(md_files)}]   → injected {date_tag} (from {source})")

        index_file(rel, content, mtime)

        try:
            from timeline import index_file as _timeline_index
            _timeline_index(rel, content, mtime)
        except Exception as e:
            print(f"[{i}/{len(md_files)}]   ⚠ timeline index failed: {e}")

    print("\nDone! Vault fully indexed (ChromaDB + SQLite timeline).")


if __name__ == "__main__":
    if "--rescan" in sys.argv:
        ingest_all()
    elif "--backfill" in sys.argv:
        from timeline import backfill_dates
        stats = backfill_dates(VAULT_PATH)
        print(
            f"\nBackfill complete: updated={stats['updated']} skipped={stats['skipped']} "
            f"warned={stats['warned']} errors={stats['errors']}"
        )
    else:
        start_vault_watcher()
        print(f"Watching {VAULT_PATH} for changes (Ctrl+C to stop)...")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Watcher stopped.")
