"""Sovereign Memory Vault — MCP Server.

Exposes 5 tools and 1 priority resource over the Model Context Protocol (stdio).
Wraps the existing vector.py, timeline.py, and tools/vault.py modules directly
so there is zero duplication of ChromaDB, embedding, or vault I/O logic.
"""
from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUNTIME_BRIEFING = os.path.join(_PROJECT_ROOT, ".runtime", "active_briefing.md")

server = MCPServer(
    "sovereign-vault",
    instructions=(
        "Access the Sovereign Memory Vault: read/write Markdown files, "
        "run semantic search via ChromaDB (nomic-embed-text embeddings), "
        "and query the SQLite timeline index. "
        "Always call search_semantic before answering from your own memory."
    ),
)


# ── Tool 1: read_document ─────────────────────────────────────────────────────

@server.tool()
def read_document(path: str) -> str:
    """Read a Markdown file from the vault by its relative path.

    Examples: '10_Kern/Inzichten/some-note.md', '.system/active_briefing.md'
    Returns the raw file content or an error message if the file is not found.
    """
    from tools.vault import read_vault
    return read_vault(path)


# ── Tool 2: search_semantic ───────────────────────────────────────────────────

@server.tool()
def search_semantic(query: str, n_results: int = 5) -> str:
    """Semantic search over the vault using ChromaDB and nomic-embed-text embeddings.

    Supports #YYYY-MM-DD date tags in the query for temporal filtering.
    Returns the most relevant document fragments with [file | timestamp] citations.
    """
    import vector
    return vector.search_vault_semantic(query, n_results)


# ── Tool 3: write_document ────────────────────────────────────────────────────

@server.tool()
def write_document(path: str, content: str, ev_tag: str = "#ev-direct") -> str:
    """Write or update a Markdown file in the vault with Sovereign Truth Protocol tagging.

    ev_tag must be one of:
      #ev-direct    — directly observed fact
      #ev-derived   — logically inferred
      #ev-reported  — reported by a third party
      #ev-assumed   — working assumption
      #ev-luna-hypo — Luna's hypothesis

    Auto-injects YAML frontmatter, #YYYY-MM-DD time tags, MOC links,
    ChromaDB indexing, and SQLite timeline entry on every write.
    """
    from tools.vault import write_vault
    return write_vault(path, content, ev_tag=ev_tag)


# ── Tool 4: list_vault_structure ──────────────────────────────────────────────

@server.tool()
def list_vault_structure(directory: str = "") -> str:
    """Recursively list all Markdown files in a vault directory.

    Pass an empty string for the full vault root.
    Examples: '', '10_Kern/', '30_Intelligence/', '.system/'
    """
    from tools.vault import list_files
    return list_files(directory)


# ── Tool 5: query_timeline ────────────────────────────────────────────────────

@server.tool()
def query_timeline(date_range: str, keyword: str = "") -> str:
    """Query the SQLite timeline index by date with optional full-text keyword filtering.

    date_range formats:
      Single day : 'YYYY-MM-DD'               e.g. '2026-09-09'
      Range      : 'YYYY-MM-DD/YYYY-MM-DD'    e.g. '2026-09-01/2026-09-09'

    keyword filters results by full-text search within the entry content.
    Returns matching vault entries with file paths, dates, and content snippets.
    """
    import timeline as tl

    kw = keyword.strip() or None

    if "/" in date_range:
        date_from, date_to = (p.strip() for p in date_range.split("/", 1))
        results = tl.search_by_range(date_from, date_to, query=kw, n=20)
    else:
        results = tl.search_by_date(date_range.strip(), query=kw, n=20)

    if not results:
        suffix = f" met keyword '{keyword}'" if keyword else ""
        return f"Geen resultaten gevonden voor '{date_range}'{suffix}."

    lines = []
    for r in results:
        snippet = (r.get("content") or "")[:200].replace("\n", " ")
        lines.append(f"**{r['file_path']}** ({r['date']})\n{snippet}")
    return "\n\n".join(lines)


# ── Resource: active_briefing ─────────────────────────────────────────────────

@server.resource("vault://briefing")
def active_briefing() -> str:
    """Active Context Layer (ACL) — highest-priority operational briefing.

    Automatically updated by Luna to reflect the user's current mental/emotional
    state, operational constraints, active definitions, and focus points.
    Always available to the LLM without an explicit tool call.
    """
    try:
        with open(_RUNTIME_BRIEFING, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
