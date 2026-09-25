"""Prediction & Settlement Framework — Operational Intelligence tools for Luna.

Three actions:
  claim   — create a falsifiable Claim Note in the vault
  settle  — resolve a due claim and calculate its Brier score
  analyze — aggregate settled claims for a person/entity (Predictability Index)
"""
import logging
import os
import re
from datetime import date, datetime, timezone

import yaml

from tools.vault import VAULT_PATH, list_files, read_vault, write_vault

logger = logging.getLogger(__name__)

_CLAIMS_DIR = "Claims"
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> dict | None:
    """Return parsed YAML frontmatter dict or None."""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return None
    try:
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return None


def _render_frontmatter(meta: dict, body: str) -> str:
    """Serialize frontmatter + body back to markdown."""
    fm = yaml.dump(meta, allow_unicode=True, default_flow_style=False, sort_keys=False).rstrip()
    return f"---\n{fm}\n---\n{body}"


def _find_claim_files() -> list[str]:
    """Return all .md paths under Claims/ in the vault."""
    listing = list_files(_CLAIMS_DIR)
    if listing.startswith("Error") or listing == "Directory is empty.":
        return []
    return [p for p in listing.splitlines() if p.endswith(".md")]


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------

def create_claim(
    claim: str,
    confidence: float,
    settles: str,
    source: str,
    subject: str | None = None,
) -> str:
    """Write a new Claim Note to the vault.

    Args:
        claim:      The specific, falsifiable prediction.
        confidence: Float 0.0–1.0 (Luna's estimated probability of being correct).
        settles:    ISO date string YYYY-MM-DD when to check the outcome.
        source:     The context or trigger that generated this claim.
        subject:    Optional person/entity slug (e.g. 'Extractors/Marco').
                    Used for Predictability Index grouping.
    """
    # Validate
    try:
        confidence = float(confidence)
        if not (0.0 <= confidence <= 1.0):
            return "Error: confidence must be between 0.0 and 1.0"
    except (TypeError, ValueError):
        return "Error: confidence must be a float between 0.0 and 1.0"

    try:
        settles_date = date.fromisoformat(settles)
    except ValueError:
        return f"Error: settles must be a valid ISO date (YYYY-MM-DD), got '{settles}'"

    now = datetime.now(timezone.utc)
    month_tag = now.strftime("#%Y-%m")
    slug = re.sub(r"[^\w\-]", "-", claim[:50].lower()).strip("-")
    file_name = f"{_CLAIMS_DIR}/{settles_date.strftime('%Y-%m-%d')}-{slug}.md"

    meta = {
        "type": "claim",
        "claim": claim,
        "confidence": round(confidence, 4),
        "settles": settles,
        "source": source,
        "subject": subject or "",
        "outcome": None,
        "settled_at": None,
        "brier_score": None,
    }

    body = f"\n# Claim: {claim}\n\n**Confidence:** {confidence:.0%}  \n**Settles:** {settles}  \n**Source:** {source}\n\n{month_tag}\n"
    content = _render_frontmatter(meta, body)

    result = write_vault(file_name, content, ev_tag="#ev-luna-hypo")
    if result.startswith("Error"):
        return result
    return f"[CLAIM] Registered → `{file_name}` | confidence={confidence:.0%} | settles={settles}"


def settle_claim(file_name: str, outcome: bool) -> str:
    """Resolve a claim and write Brier score back to the file.

    Args:
        file_name: Vault path of the claim note (e.g. 'Claims/2026-09-30-...md').
        outcome:   True if the prediction was correct, False otherwise.
    """
    content = read_vault(file_name)
    if content.startswith("Error"):
        return content

    meta = _parse_frontmatter(content)
    if meta is None:
        return f"Error: could not parse frontmatter in '{file_name}'"
    if meta.get("type") != "claim":
        return f"Error: '{file_name}' is not a claim note (type={meta.get('type')!r})"
    if meta.get("outcome") is not None:
        return f"[ALREADY SETTLED] '{file_name}' was settled at {meta.get('settled_at')} with outcome={meta.get('outcome')}"

    confidence = float(meta.get("confidence", 0.5))
    outcome_int = 1 if outcome else 0
    brier_score = round((confidence - outcome_int) ** 2, 6)

    now_iso = datetime.now(timezone.utc).isoformat()
    meta["outcome"] = outcome
    meta["settled_at"] = now_iso
    meta["brier_score"] = brier_score

    # Rebuild body: strip old frontmatter, append settlement block
    body_match = _FRONTMATTER_RE.match(content)
    body = content[body_match.end():] if body_match else content
    gap = "" if body.startswith("\n") else "\n"
    verdict = "CORRECT" if outcome else "INCORRECT"
    body += f"{gap}\n## Settlement\n\n- **Outcome:** {verdict}  \n- **Settled at:** {now_iso}  \n- **Brier score:** {brier_score:.6f}  \n  *(0 = perfect, 1 = worst; lower is better)*\n"

    new_content = _render_frontmatter(meta, body)
    result = write_vault(file_name, new_content, ev_tag="#ev-direct")
    if result.startswith("Error"):
        return result

    gap_label = "overconfident" if confidence > 0.5 and not outcome else ("underconfident" if confidence < 0.5 and outcome else "calibrated")
    return (
        f"[SETTLED] `{file_name}` → outcome={verdict} | "
        f"confidence={confidence:.0%} | Brier={brier_score:.4f} | {gap_label}"
    )


def check_due_claims() -> str:
    """Scan the vault for claims whose settle date has passed but are not yet settled.

    Returns a formatted briefing block listing all due claims.
    """
    today = date.today()
    files = _find_claim_files()
    if not files:
        return "[CLAIMS] No claim notes found in vault."

    due = []
    for f in files:
        content = read_vault(f)
        if content.startswith("Error"):
            continue
        meta = _parse_frontmatter(content)
        if not meta or meta.get("type") != "claim":
            continue
        if meta.get("outcome") is not None:
            continue  # already settled
        try:
            settles_date = date.fromisoformat(str(meta["settles"]))
        except (KeyError, ValueError):
            continue
        if settles_date <= today:
            due.append({
                "file": f,
                "claim": meta.get("claim", "?"),
                "confidence": meta.get("confidence", "?"),
                "settles": meta.get("settles", "?"),
                "subject": meta.get("subject", ""),
            })

    if not due:
        return f"[CLAIMS] No due claims as of {today}. All predictions are still pending or settled."

    lines = [f"[CLAIMS] {len(due)} claim(s) due for settlement as of {today}:\n"]
    for d in due:
        subj = f" | subject={d['subject']}" if d["subject"] else ""
        lines.append(
            f"  • `{d['file']}`\n"
            f"    Claim: {d['claim']}\n"
            f"    Confidence: {float(d['confidence']):.0%} | Due: {d['settles']}{subj}"
        )
    lines.append("\nUse `settle_claim` to resolve each one.")
    return "\n".join(lines)


def predictability_index(subject: str) -> str:
    """Aggregate all settled claims for a subject and generate a Predictability Index.

    Args:
        subject: Person/entity slug to filter on (matched against the 'subject' field).
                 Can be partial (e.g. 'Marco' matches 'Extractors/Marco').
    """
    files = _find_claim_files()
    if not files:
        return f"[ANALYSIS] No claim notes found for subject '{subject}'."

    settled = []
    pending = []

    for f in files:
        content = read_vault(f)
        if content.startswith("Error"):
            continue
        meta = _parse_frontmatter(content)
        if not meta or meta.get("type") != "claim":
            continue
        file_subject = str(meta.get("subject", ""))
        if subject.lower() not in file_subject.lower():
            continue

        if meta.get("outcome") is not None:
            settled.append(meta)
        else:
            pending.append(meta)

    if not settled and not pending:
        return f"[ANALYSIS] No claim notes found for subject matching '{subject}'."

    if not settled:
        return (
            f"[ANALYSIS] {len(pending)} pending claim(s) for '{subject}' — none settled yet. "
            "Settle claims first to generate a Predictability Index."
        )

    total_brier = sum(float(m["brier_score"]) for m in settled)
    mean_brier = total_brier / len(settled)
    correct = sum(1 for m in settled if m.get("outcome") is True)
    accuracy = correct / len(settled)

    # Calibration gap: mean_confidence vs accuracy
    mean_conf = sum(float(m["confidence"]) for m in settled) / len(settled)
    calibration_gap = round(mean_conf - accuracy, 4)
    gap_label = (
        "OVERCONFIDENT" if calibration_gap > 0.1
        else "UNDERCONFIDENT" if calibration_gap < -0.1
        else "WELL-CALIBRATED"
    )

    lines = [
        f"## Predictability Index — {subject}",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Settled claims | {len(settled)} |",
        f"| Pending claims | {len(pending)} |",
        f"| Accuracy | {accuracy:.0%} ({correct}/{len(settled)}) |",
        f"| Mean confidence | {mean_conf:.0%} |",
        f"| Calibration gap | {calibration_gap:+.2%} ({gap_label}) |",
        f"| Mean Brier score | {mean_brier:.4f} |",
        "",
        "### Claim History",
    ]
    for m in settled:
        verdict = "✓" if m.get("outcome") else "✗"
        lines.append(
            f"- [{verdict}] {m.get('claim', '?')} "
            f"(conf={float(m['confidence']):.0%}, Brier={float(m['brier_score']):.4f})"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "create_claim",
            "description": (
                "Operational Intelligence: register a falsifiable behavioral prediction as a Claim Note in the vault. "
                "Use when a predictable pattern is identified — e.g. an Extractor's likely next move. "
                "The claim is stored with a confidence level and a settlement date."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "claim": {
                        "type": "string",
                        "description": "The specific, falsifiable prediction (e.g. 'Marco will attempt re-contact within 14 days of last block').",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Estimated probability of being correct, 0.0 to 1.0.",
                    },
                    "settles": {
                        "type": "string",
                        "description": "ISO date YYYY-MM-DD when to check the outcome.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Context or trigger — what observation led to this prediction.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Optional person/entity slug for grouping (e.g. 'Extractors/Marco').",
                    },
                },
                "required": ["claim", "confidence", "settles", "source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "settle_claim",
            "description": (
                "Operational Intelligence: resolve a due Claim Note by writing the outcome and calculating the Brier score. "
                "Use during daily briefings when a claim's settle date has passed. "
                "Tracks 'The Gap' between Luna's confidence and actual accuracy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "Vault path of the claim note, e.g. 'Claims/2026-09-30-marco-re-contact.md'.",
                    },
                    "outcome": {
                        "type": "boolean",
                        "description": "True if the prediction was correct, False if it was wrong.",
                    },
                },
                "required": ["file_name", "outcome"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_due_claims",
            "description": (
                "Operational Intelligence: scan the vault for Claim Notes whose settlement date has passed "
                "but have not yet been resolved. Run this during daily briefings to surface due predictions."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "predictability_index",
            "description": (
                "Operational Intelligence: aggregate all settled claims for a specific subject (person or entity) "
                "and generate a Predictability Index — accuracy, calibration gap, and mean Brier score. "
                "Use to assess how well-mapped a person's behavioral patterns are."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Person/entity slug to filter on (e.g. 'Marco', 'Extractors/Marco'). Partial match.",
                    },
                },
                "required": ["subject"],
            },
        },
    },
]

HANDLERS = {
    "create_claim": lambda args: create_claim(
        args["claim"],
        args["confidence"],
        args["settles"],
        args["source"],
        args.get("subject"),
    ),
    "settle_claim": lambda args: settle_claim(args["file_name"], args["outcome"]),
    "check_due_claims": lambda args: check_due_claims(),
    "predictability_index": lambda args: predictability_index(args["subject"]),
}
