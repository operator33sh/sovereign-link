#!/usr/bin/env python3
"""One-shot script to index all vault .md files into ChromaDB."""
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from vector import index_file, VAULT_PATH


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
    ingest_all()
