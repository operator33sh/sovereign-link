#!/usr/bin/env python3
"""Index all vault .md files into ChromaDB.

Usage:
    python ingest.py           # one-shot full index
    python ingest.py --watch   # full index then watch for changes
"""
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from vector import index_file, start_vault_watcher, VAULT_PATH


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
        index_file(rel, content)

    print("\nDone! Vault fully indexed.")


if __name__ == "__main__":
    watch_mode = "--watch" in sys.argv
    ingest_all()
    if watch_mode:
        start_vault_watcher()
        print(f"Watching {VAULT_PATH} for changes (Ctrl+C to stop)...")
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Watcher stopped.")
