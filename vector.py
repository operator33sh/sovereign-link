import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import chromadb
import httpx

logger = logging.getLogger(__name__)

EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
CHROMA_PATH = os.environ.get("CHROMA_PATH", os.path.expanduser("~/.sovereign-link/chroma"))
VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")

AGENT_TEMP_DIR = ".agent_temp"    # transient working memory — never indexed
SYSTEM_MEMORY_DIR = "system_memory"  # project-root system files — never indexed

CHUNK_SIZE = 1500   # ~375 tokens
CHUNK_OVERLAP = 150
DISTANCE_THRESHOLD = 0.8  # cosine distance above this = not relevant

_chroma = chromadb.PersistentClient(path=CHROMA_PATH)
_collection = _chroma.get_or_create_collection(
    "vault_cosine",
    metadata={"hnsw:space": "cosine"},
)

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


def index_file(file_name: str, content: str, timestamp: str | None = None) -> None:
    if file_name.startswith(AGENT_TEMP_DIR + "/") or file_name == AGENT_TEMP_DIR:
        return  # transient working memory — never indexed

    if timestamp is None:
        timestamp = datetime.now().isoformat()

    existing = _collection.get(where={"file_name": file_name})
    if existing["ids"]:
        _collection.delete(ids=existing["ids"])

    chunks = _chunk(content, file_name)
    if not chunks:
        return

    for chunk in chunks:
        chunk["metadata"]["timestamp"] = timestamp

    embeddings = [_embed(c["text"]) for c in chunks]
    _collection.add(
        ids=[c["id"] for c in chunks],
        embeddings=embeddings,
        documents=[c["text"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )
    with _recently_indexed_lock:
        _recently_indexed[file_name] = time.time()


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


_DATE_TAG_RE = re.compile(r"#(\d{4}-\d{2}-\d{2})")


def _extract_date_filter(query: str) -> tuple[str, str | None, bool]:
    """Parse #YYYY-MM-DD tag from query string.

    Returns (cleaned_query, date_string_or_None, has_topic).
    - date_string: used as a metadata range filter on 'timestamp' (ISO format)
    - has_topic: True if there are meaningful keywords beyond the date tag
    The tag is stripped from the query so embeddings are not polluted.
    """
    m = _DATE_TAG_RE.search(query)
    if not m:
        return query, None, True
    date_str = m.group(1)
    clean = _DATE_TAG_RE.sub("", query).strip()
    has_topic = bool(clean)
    return clean, date_str, has_topic


def _build_where(date_filter: str | None, path_prefix: str | None) -> dict | None:
    """Build a ChromaDB where clause from optional date and path filters.

    ChromaDB string metadata only supports $eq/$ne/$in/$nin and $contains.
    $gte/$lt require numeric values, so we use $contains for date matching.
    The timestamp field is stored as an ISO string (e.g. '2026-08-23T14:30:00'),
    so $contains on 'YYYY-MM-DD' reliably matches all chunks from that date.
    """
    conditions = []
    if date_filter:
        conditions.append({"timestamp": {"$contains": date_filter}})
    if path_prefix:
        conditions.append({"file_name": {"$contains": path_prefix}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def search_vault_semantic(query: str, n_results: int = 5, path_prefix: str | None = None) -> str:
    """Semantic search returning formatted text chunks.

    Date-tag handling (#YYYY-MM-DD):
    - If the query contains ONLY a date tag (no topic keywords), a pure
      metadata get() is performed — no semantic scoring, no distance filter.
      All indexed chunks from that date are returned, ordered by file/chunk.
    - If the query contains a date tag AND topic keywords, a semantic query
      is run with the date as a metadata pre-filter and a relaxed distance
      threshold so date-scoped results are not over-suppressed.

    Chunks without a date tag are filtered by the standard DISTANCE_THRESHOLD.
    """
    total = _collection.count()
    if total == 0:
        return "Vector index is leeg. Voer eerst ingest.py uit."

    clean_query, date_filter, has_topic = _extract_date_filter(query)
    where_clause = _build_where(date_filter, path_prefix)

    # --- Date-only query: pure metadata lookup, no semantic scoring ---
    if date_filter and not has_topic:
        get_kwargs: dict = {"include": ["documents", "metadatas"]}
        if where_clause:
            get_kwargs["where"] = where_clause
        try:
            raw = _collection.get(**get_kwargs)
        except Exception:
            logger.exception("search_vault_semantic: date-only get() failed")
            return "Fout bij ophalen van datum-gefilterde resultaten."

        docs = raw.get("documents", [])
        metas = raw.get("metadatas", [])
        if not docs:
            return f"Geen fragmenten gevonden voor {date_filter}."

        parts = []
        for doc, meta in zip(docs[:n_results * 3], metas):  # cap output
            if path_prefix and not meta.get("file_name", "").startswith(path_prefix):
                continue
            ts = meta.get("timestamp", "")
            header = f"[{meta['file_name']} | {ts}]" if ts else f"[{meta['file_name']}]"
            parts.append(f"{header}\n{doc}")
            if len(parts) >= n_results:
                break

        if not parts:
            return f"Geen fragmenten gevonden voor {date_filter}."
        return "\n\n---\n\n".join(parts)

    # --- Semantic query (with optional date pre-filter) ---
    semantic_query = clean_query if has_topic else query
    query_embedding = _embed(semantic_query)

    # Relax distance threshold when a date filter narrows the search space
    threshold = DISTANCE_THRESHOLD if not date_filter else min(DISTANCE_THRESHOLD * 1.5, 1.2)

    kwargs: dict = {
        "query_embeddings": [query_embedding],
        "n_results": min(n_results, total),
        "include": ["documents", "metadatas", "distances"],
    }
    if where_clause:
        kwargs["where"] = where_clause

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
        if dist > threshold:
            continue  # suppress irrelevant results rather than hallucinate proximity
        ts = meta.get("timestamp", "")
        header = f"[{meta['file_name']} | {ts}]" if ts else f"[{meta['file_name']}]"
        parts.append(f"{header}\n{doc}")

    if not parts:
        return "Geen relevante fragmenten gevonden."

    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Filesystem watcher — indexes any .md file written outside write_vault
# ---------------------------------------------------------------------------

_watcher_started = False
_watcher_lock = threading.Lock()
_debounce_delay = 2.0  # seconds to wait after last event before indexing

# Suppress duplicate watcher events for files already indexed by write_vault()
_recently_indexed: dict[str, float] = {}
_recently_indexed_lock = threading.Lock()
_recently_indexed_ttl = _debounce_delay + 1.0  # skip watcher if indexed within this window


def _index_path(path: Path, event_type: str = "modified") -> None:
    """Index a single vault file by its absolute path."""
    vault = Path(VAULT_PATH)
    try:
        rel = str(path.relative_to(vault))
        if rel.startswith(AGENT_TEMP_DIR + "/") or rel.startswith(AGENT_TEMP_DIR + "\\"):
            return  # transient working memory — skip indexing
        with _recently_indexed_lock:
            last = _recently_indexed.get(rel, 0.0)
        if time.time() - last < _recently_indexed_ttl:
            return  # already indexed by write_vault() — skip duplicate watcher event
        content = path.read_text(encoding="utf-8")
        if content.strip():
            mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
            index_file(rel, content, mtime)
            logger.info("Vault %s → indexed: %s", event_type, rel)
    except Exception:
        logger.exception("Watcher failed to index %s", path)


def _debounced_index(path: Path, event_type: str = "modified") -> None:
    """Sleep briefly then index — absorbs rapid successive writes to the same file."""
    time.sleep(_debounce_delay)
    _index_path(path, event_type)


def start_vault_watcher() -> None:
    """Start a background watchdog Observer on VAULT_PATH (idempotent)."""
    global _watcher_started
    with _watcher_lock:
        if _watcher_started:
            return
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class _VaultHandler(FileSystemEventHandler):
                def _handle(self, event, event_type: str):
                    if event.is_directory:
                        return
                    path = Path(event.src_path)
                    if path.suffix == ".md":
                        logger.info("Vault %s detected: %s", event_type, path.name)
                        threading.Thread(
                            target=_debounced_index,
                            args=(path, event_type),
                            daemon=True,
                        ).start()

                def on_modified(self, event):
                    self._handle(event, "modified")

                def on_created(self, event):
                    self._handle(event, "created")

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
