import os

import chromadb
import httpx

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
CHROMA_PATH = os.environ.get("CHROMA_PATH", os.path.expanduser("~/.sovereign-link/chroma"))
VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")

CHUNK_SIZE = 1500   # ~375 tokens
CHUNK_OVERLAP = 150

_chroma = chromadb.PersistentClient(path=CHROMA_PATH)
_collection = _chroma.get_or_create_collection("vault")

_embed_client = httpx.Client(timeout=60.0)


def _embed(text: str) -> list:
    response = _embed_client.post(
        f"{OLLAMA_BASE_URL}/api/embeddings",
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


def search_vault_semantic(query: str, n_results: int = 5) -> str:
    total = _collection.count()
    if total == 0:
        return "Vector index is leeg. Voer eerst ingest.py uit."

    query_embedding = _embed(query)
    results = _collection.query(
        query_embeddings=[query_embedding],
        n_results=min(n_results, total),
        include=["documents", "metadatas"],
    )

    if not results["documents"][0]:
        return "Geen relevante fragmenten gevonden."

    parts = []
    for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
        parts.append(f"[{meta['file_name']}]\n{doc}")

    return "\n\n---\n\n".join(parts)
