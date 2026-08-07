#!/usr/bin/env python3
"""Vault indexing tool.

Usage:
    python ingest.py           # start filesystem watcher only
    python ingest.py --rescan  # full vault rescan, then exit
"""
import logging
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
        mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
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
