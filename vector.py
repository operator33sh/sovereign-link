import logging
import os
import threading
import time
from pathlib import Path

import chromadb
import httpx

logger = logging.getLogger(__name__)

EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
CHROMA_PATH = os.environ.get("CHROMA_PATH", os.path.expanduser("~/.sovereign-link/chroma"))
VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")

CHUNK_SIZE = 1500   # ~375 tokens
CHUNK_OVERLAP = 150
DISTANCE_THRESHOLD = 0.8  # cosine distance above this = not relevant

_chroma = chromadb.PersistentClient(path=CHROMA_PATH)
_collection = _chroma.get_or_create_collection("vault")

_embed_client = httpx.Client(timeout=60.0)


def _embed(text: str) -> list:
    response = _embed_client.post(
        f"{EMBED_BASE_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
    )
    response.raise_for_status()
    return response.json()["embedding"]


def _chunk(text: str, file_name: str) -> list:
    chunks = []
    start = 0
    i = 0
    while start < len(text):
        chunk_text = text[start : start + CHUNK_SIZE]
        chunks.append({
            "id": f"{file_name}::{i}",
            "text": chunk_text,
            "metadata": {"file_name": file_name, "chunk": i},
        })
        start += CHUNK_SIZE - CHUNK_OVERLAP
        i += 1
    return chunks


def index_file(file_name: str, content: str) -> None:
    existing = _collection.get(where={"file_name": file_name})
    if existing["ids"]:
        _collection.delete(ids=existing["ids"])

    chunks = _chunk(content, file_name)
    if not chunks:
        return

    embeddings = [_embed(c["text"]) for c in chunks]
    _collection.add(
        ids=[c["id"] for c in chunks],
        embeddings=embeddings,
        documents=[c["text"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )


def random_chunk() -> str | None:
    """Return a random document chunk from the vault collection, or None if empty."""
    import random
    total = _collection.count()
    if total == 0:
        return None
    offset = random.randint(0, total - 1)
    result = _collection.get(limit=1, offset=offset, include=["documents"])
    docs = result.get("documents", [])
    return docs[0] if docs else None


def search_vault_files(query: str, n_results: int = 5, path_prefix: str | None = None) -> list[str]:
    """Return unique file names of the most semantically related vault notes."""
    total = _collection.count()
    if total == 0:
        return []

    query_embedding = _embed(query)
    kwargs: dict = {
        "query_embeddings": [query_embedding],
        "n_results": min(n_results, total),
        "include": ["metadatas"],
    }
    if path_prefix:
        try:
            kwargs["where"] = {"file_name": {"$contains": path_prefix}}
        except Exception:
            pass

    results = _collection.query(**kwargs)

    seen: set = set()
    files: list = []
    for meta in results["metadatas"][0]:
        name = meta["file_name"]
        if path_prefix and not name.startswith(path_prefix):
            continue
        if name not in seen:
            seen.add(name)
            files.append(name)
    return files


def search_vault_semantic(query: str, n_results: int = 5, path_prefix: str | None = None) -> str:
    """Semantic search returning formatted text chunks.

    Chunks with a cosine distance above DISTANCE_THRESHOLD are suppressed
    so the assistant never receives hallucinated proximity results.
    """
    total = _collection.count()
    if total == 0:
        return "Vector index is leeg. Voer eerst ingest.py uit."

    query_embedding = _embed(query)
    kwargs: dict = {
        "query_embeddings": [query_embedding],
        "n_results": min(n_results, total),
        "include": ["documents", "metadatas", "distances"],
    }
    if path_prefix:
        try:
            kwargs["where"] = {"file_name": {"$contains": path_prefix}}
        except Exception:
            pass

    results = _collection.query(**kwargs)

    if not results["documents"][0]:
        return "Geen relevante fragmenten gevonden."

    parts = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        if path_prefix and not meta["file_name"].startswith(path_prefix):
            continue
        if dist > DISTANCE_THRESHOLD:
            continue  # suppress irrelevant results rather than hallucinate proximity
        parts.append(f"[{meta['file_name']}]\n{doc}")

    if not parts:
        return "Geen relevante fragmenten gevonden."

    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Filesystem watcher — indexes any .md file written outside write_vault
# ---------------------------------------------------------------------------

_watcher_started = False
_watcher_lock = threading.Lock()
_debounce_delay = 2.0  # seconds to wait after last event before indexing


def _index_path(path: Path) -> None:
    """Index a single vault file by its absolute path."""
    vault = Path(VAULT_PATH)
    try:
        rel = str(path.relative_to(vault))
        content = path.read_text(encoding="utf-8")
        if content.strip():
            index_file(rel, content)
            logger.info("Watcher indexed: %s", rel)
    except Exception:
        logger.exception("Watcher failed to index %s", path)


def _debounced_index(path: Path) -> None:
    """Sleep briefly then index — absorbs rapid successive writes to the same file."""
    time.sleep(_debounce_delay)
    _index_path(path)


def start_vault_watcher() -> None:
    """Start a background watchdog Observer on VAULT_PATH (idempotent)."""
    global _watcher_started
    with _watcher_lock:
        if _watcher_started:
            return
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

            class _VaultHandler(FileSystemEventHandler):
                def _handle(self, event):
                    if event.is_directory:
                        return
                    path = Path(event.src_path)
                    if path.suffix == ".md":
                        threading.Thread(
                            target=_debounced_index,
                            args=(path,),
                            daemon=True,
                        ).start()

                def on_modified(self, event):
                    self._handle(event)

                def on_created(self, event):
                    self._handle(event)

            observer = Observer()
            observer.schedule(_VaultHandler(), VAULT_PATH, recursive=True)
            observer.daemon = True
            observer.start()
            _watcher_started = True
            logger.info("Vault watcher started on %s", VAULT_PATH)
        except ImportError:
            logger.warning("watchdog not installed — filesystem watcher disabled")
        except Exception:
            logger.exception("Failed to start vault watcher")
