#!/usr/bin/env python3
"""Vault indexing tool.

Usage:
    python ingest.py           # start filesystem watcher only
    python ingest.py --rescan  # full vault rescan, then exit
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
_TIMETAG_IN_CONTENT = re.compile(r"#\d{4}-\d{2}")


def _date_from_filename(path: Path) -> datetime | None:
    m = _DATE_IN_NAME.search(path.stem)
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

        if not _TIMETAG_IN_CONTENT.search(content):
            date_dt = _date_from_filename(path)
            source = "filename"
            if date_dt is None:
                date_dt = _date_from_git(path, vault)
                source = "git"
            if date_dt:
                time_tag = date_dt.strftime("#%Y-%m")
                content = f"{time_tag}\n\n{content}"
                print(f"[{i}/{len(md_files)}]   → injected {time_tag} (from {source})")
            else:
                print(f"[{i}/{len(md_files)}]   → no date found, skipping tag injection")

        index_file(rel, content, mtime)

    print("\nDone! Vault fully indexed.")


if __name__ == "__main__":
    if "--rescan" in sys.argv:
        ingest_all()
    else:
        start_vault_watcher()
        print(f"Watching {VAULT_PATH} for changes (Ctrl+C to stop)...")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Watcher stopped.")
