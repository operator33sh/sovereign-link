"""
Sovereign Memory Engine — Memory Manager

Standalone CLI script and importable module for the Extract→Synthesize→Store→Sync pipeline.

Usage (CLI):
    python memory_manager.py transcript.txt
    cat chat.txt | python memory_manager.py

Usage (module):
    from memory_manager import run_memory_pipeline, inject_memory_context
"""
import os
import sys
from datetime import datetime
from pathlib import Path

MEMORY_DIR = "memory"  # relative to VAULT_PATH; used as path_prefix in vector search
VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
_MAX_INJECT_CHARS = 2000  # cap injected memory to avoid context window bloat

MEMORY_PROTOCOL_CONTENT = """\
# Sovereign Memory Protocol

## Purpose
This document defines how AI agents interact with the Sovereign Memory Engine (SME)
in the fractalisme-vault. The SME captures High-Value Insights (HVIs) from
conversations and surfaces them at session start to maintain continuity.

## Memory Log Location
All memory logs are stored in: `memory/YYYY-MM-DD_TOPIC_SovereignLog.md`

## HVI Criteria
Extract insights that fall into one or more of these categories:
- Psychological breakthroughs or shadow work integrations
- Redefined values or identity shifts
- Recurring shadow patterns (behaviours, defences, projections)
- Technical or architectural decisions with long-term consequence

## Memory Log Schema
Each insight block uses this exact structure:

```
- Timestamp: [ISO Date]
- Core Insight: [one sentence]
- Emotional/Psychological State: [e.g. Vulnerable, Analytical, Transgressive, Integrative]
- Related Tags: [#shadowwork #Sovereign #AI_Architecture]
- Connected Nodes: [[wikilink to related log]]
- Open Questions:
  - [unresolved tension as bullet]
```

## Session Continuity
At the start of each session, the SME performs a semantic search over `memory/`
and injects the top 3 most relevant memory logs into the LLM system prompt.
This means the assistant begins each conversation with awareness of relevant prior insights.

## Triggering Memory Extraction
- `/memory` Telegram command: processes the current conversation
- CLI: `python memory_manager.py transcript.txt` or pipe via stdin
- Module: `from memory_manager import run_memory_pipeline`
"""


def format_memory_note(
    topic: str,
    insights: list[dict],
    related_files: list[str],
) -> tuple[str, str]:
    """Format a list of HVI dicts into a Sovereign Memory Log markdown note.

    Returns:
        (content, filename) where filename follows YYYY-MM-DD_TOPIC_SovereignLog.md
    """
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"{today}_{topic}_SovereignLog.md"

    related_links = [
        f"[[{f[:-3] if f.endswith('.md') else f}]]"
        for f in related_files
        if f != f"{MEMORY_DIR}/{filename}"
    ]

    lines = [f"## Sovereign Memory Log — {topic.replace('_', ' ')}\n"]

    for i, insight in enumerate(insights, start=1):
        tags = insight.get("tags", [])
        tags_str = " ".join(t if t.startswith("#") else f"#{t}" for t in tags if isinstance(t, str))

        nodes = insight.get("connected_nodes", [])
        if related_links and not nodes:
            nodes = related_links[:3]
        nodes_str = "  ".join(nodes) if nodes else "—"

        questions = insight.get("open_questions", [])
        questions_block = (
            "\n".join(f"  - {q}" for q in questions)
            if questions
            else "  - (none)"
        )

        lines.append(f"### Insight {i}\n")
        lines.append(f"- Timestamp: {insight.get('timestamp', today)}")
        lines.append(f"- Core Insight: {insight.get('core_insight', '')}")
        lines.append(f"- Emotional/Psychological State: {insight.get('emotional_state', 'Unknown')}")
        lines.append(f"- Related Tags: {tags_str}")
        lines.append(f"- Connected Nodes: {nodes_str}")
        lines.append(f"- Open Questions:\n{questions_block}")
        lines.append("")

    content = "\n".join(lines)
    return content, filename


def inject_memory_context(query: str, n_results: int = 3) -> str | None:
    """Semantic search vault/memory/ and return a formatted context block for the LLM system prompt.

    Returns None if the memory folder is empty or has no relevant results.
    """
    import vector

    result = vector.search_vault_semantic(query, n_results=n_results, path_prefix=f"{MEMORY_DIR}/")

    # Guard against empty-index or no-results messages from search_vault_semantic
    if not result or "leeg" in result or "Geen" in result:
        return None

    # Cap injected content to avoid bloating the context window
    if len(result) > _MAX_INJECT_CHARS:
        result = result[:_MAX_INJECT_CHARS] + "\n…[truncated]"

    return (
        "=== Sovereign Memory Context ===\n"
        "The following are your most relevant past memory logs for this session:\n\n"
        f"{result}\n\n"
        "=== End Memory Context ==="
    )


def _bootstrap_protocol() -> None:
    """Write MEMORY_PROTOCOL.md to the vault if it doesn't exist yet."""
    protocol_path = Path(VAULT_PATH) / "MEMORY_PROTOCOL.md"
    if not protocol_path.exists():
        try:
            protocol_path.write_text(MEMORY_PROTOCOL_CONTENT, encoding="utf-8")
        except OSError:
            pass  # non-fatal


def run_memory_pipeline(transcript: str) -> dict:
    """Extract HVIs from a transcript, write a memory log, and sync the vault.

    Args:
        transcript: Raw conversation text (role-prefixed lines or plain text).

    Returns:
        dict with keys: file_name, write_result, sync_result, topic
    """
    import llm
    from tools import write_vault, sync_vault
    import vector

    # Step 1: Extract High-Value Insights via LLM
    extracted = llm.extract_memory_insights(transcript)
    topic = extracted.get("topic", "Unnamed_Insight")
    insights = extracted.get("insights", [])
    if not insights:
        insights = [{
            "timestamp": datetime.now().strftime("%Y-%m-%d"),
            "core_insight": "No insights extracted from this session.",
            "emotional_state": "Neutral",
            "tags": ["#sovereign"],
            "connected_nodes": [],
            "open_questions": [],
        }]

    # Step 2: Find related memory logs for wikilinks
    primary_insight = insights[0].get("core_insight", topic)
    related_files = vector.search_vault_files(
        primary_insight, n_results=5, path_prefix=f"{MEMORY_DIR}/"
    )

    # Step 3: Format the markdown note
    content, filename = format_memory_note(topic, insights, related_files)
    vault_path = f"{MEMORY_DIR}/{filename}"

    # Step 4: Write to vault (write_vault handles dir creation + ChromaDB indexing)
    write_result = write_vault(vault_path, content)

    # Step 5: Git commit/push
    sync_result = sync_vault()

    # Bootstrap MEMORY_PROTOCOL.md on first run
    _bootstrap_protocol()

    return {
        "file_name": vault_path,
        "write_result": write_result,
        "sync_result": sync_result,
        "topic": topic,
    }


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    if len(sys.argv) > 1:
        transcript = Path(sys.argv[1]).read_text(encoding="utf-8")
    else:
        transcript = sys.stdin.read()

    if not transcript.strip():
        print("Error: no transcript provided. Pass a file path or pipe via stdin.", file=sys.stderr)
        sys.exit(1)

    result = run_memory_pipeline(transcript)
    print(f"Memory log saved: {result['file_name']}")
    print(result["write_result"])
    print(result["sync_result"])
