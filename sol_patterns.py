"""
sol_patterns.py — SoL-Pi Architectural Patterns

Four context-efficiency patterns for Sovereign Link:

  1. Action Fusion            — prompt-level instruction to batch related tool calls
  2. Online Context Compact   — summarize dropped history into a compact state block
  3. ObservationPack          — synthesize multiple tool outputs into one message
  4. Evidence-Preserving Reducer — truncate aggressively, always keep sources/dates/IDs
"""

import re
from datetime import datetime, timezone

# ── Pattern 1: Action Fusion ──────────────────────────────────────────────

ACTION_FUSION_PROMPT = """
## Action Fusion — Efficiënte Tool-Uitvoering
Wanneer je meerdere gerelateerde tool-calls moet doen, geef ze allemaal terug in ÉÉN
LLM-response als een lijst van parallel_tool_calls:
- Lees-operaties (read_vault, search_vault_semantic, list_files_paged) → altijd bundelen
- Schrijf-operaties die onafhankelijk zijn van elkaars resultaat → tegelijk uitvoeren
- Vermijd één-voor-één sequential tool calls die als batch kunnen worden uitgegeven
Doel: minimaliseer het aantal LLM round-trips door gerelateerde acties te fuseren.
"""


# ── Pattern 4: Evidence-Preserving Reducer ───────────────────────────────

_EVIDENCE_PATTERNS = [
    re.compile(r'(?:https?://|www\.)\S+'),                                           # URLs
    re.compile(r'\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?)?'),# ISO dates
    re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'),  # UUIDs
    re.compile(r'(?:^|[\s"])(/[\w./\-]+\.(?:md|json|txt|py|yaml|yml|log))', re.M),  # file paths
    re.compile(r'(?:id|ID|tweet_id|message_id|event_id|entry_id)[=:\s]+[\w\-]+'),   # named IDs
]


def extract_evidence(text: str) -> list[str]:
    """Extract critical references from text: URLs, dates, IDs, file paths."""
    found = []
    for pattern in _EVIDENCE_PATTERNS:
        found.extend(m if isinstance(m, str) else m.group(0) for m in pattern.findall(text))
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique = []
    for item in found:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def evidence_preserving_truncate(text: str, max_chars: int) -> str:
    """
    Truncate *text* to *max_chars* but prepend a preserved-evidence header
    so that sources, timestamps, and IDs are never silently lost.
    """
    if len(text) <= max_chars:
        return text
    evidence = extract_evidence(text)
    truncated = text[:max_chars] + "\n[…gekort]"
    if evidence:
        ev_header = "**[Bewijs behouden]** " + " · ".join(evidence[:8])
        return ev_header + "\n" + truncated
    return truncated


# ── Pattern 2: Online Context Compact ────────────────────────────────────

_COMPACT_MARKER = "## Context Compact"


def _build_compact_block(dropped_messages: list) -> str:
    """Build a structured compact-state block from a list of dropped messages."""
    if not dropped_messages:
        return ""

    tool_names: list[str] = []
    text_snippets: list[str] = []
    evidence: list[str] = []

    for msg in dropped_messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if isinstance(content, str) and content:
            evidence.extend(extract_evidence(content))
            first_line = content.strip().split("\n")[0][:120]
            if first_line and role in ("tool", "assistant"):
                text_snippets.append(first_line)
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {}).get("name", "")
                if fn:
                    tool_names.append(fn)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M UTC")
    lines = [
        _COMPACT_MARKER,
        f"_Gecomprimeerd op {timestamp} — {len(dropped_messages)} berichten samengevoegd_",
    ]
    if tool_names:
        deduped_tools = list(dict.fromkeys(tool_names))
        lines.append(f"**Uitgevoerde tools:** {', '.join(deduped_tools)}")
    if evidence:
        unique_ev = list(dict.fromkeys(ev.strip() for ev in evidence))[:10]
        lines.append(f"**Bewaard bewijs:** {' · '.join(unique_ev)}")
    if text_snippets:
        lines.append("**Samenvatting:**")
        for s in text_snippets[:5]:
            lines.append(f"- {s}")
    lines.append("")
    return "\n".join(lines)


def compact_context(messages: list, keep_count: int = 6) -> tuple[list, str]:
    """
    Drop the oldest non-system messages, preserving evidence in a compact block.

    Returns:
        (new_messages, compact_block)  — compact_block is "" if nothing was dropped.
    """
    system_msgs = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    if len(rest) <= keep_count:
        return messages, ""

    dropped = rest[:-keep_count]
    kept = rest[-keep_count:]

    compact_block = _build_compact_block(dropped)
    compact_msg = {"role": "system", "content": compact_block}
    return system_msgs + [compact_msg] + kept, compact_block


# ── Pattern 3: ObservationPack ────────────────────────────────────────────

def pack_observations(tool_calls: list, tool_results: list[str]) -> str:
    """
    Synthesize multiple tool results into one ObservationPack summary message.

    Returns a formatted string (for injection as a user message), or "" if
    fewer than two results were collected (no benefit packing a single result).
    """
    paired = list(zip(tool_calls, tool_results))
    if len(paired) <= 1:
        return ""

    timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [
        f"**[ObservationPack — {timestamp} — {len(paired)} tools uitgevoerd]**",
        "",
    ]

    for i, (tc, result) in enumerate(paired, 1):
        fn_name = tc.get("function", {}).get("name", f"tool_{i}")
        snippet = (result or "").strip()[:200].replace("\n", " ")
        evidence = extract_evidence(result or "")
        ev_str = " · ".join(evidence[:4]) if evidence else ""

        lines.append(f"**{i}. {fn_name}:**")
        if snippet:
            lines.append(f"> {snippet}")
        if ev_str:
            lines.append(f"> _Bewijs: {ev_str}_")
        lines.append("")

    return "\n".join(lines)
