"""
Resonance Suite + Turing-Consistency Module.

Persistence: all state lives in {VAULT_PATH}/.system/resonance/
  tests/{test_id}.json        — test baseline + snapshots
  turing_map.json             — consistency probes index

These files are written as raw JSON (not through write_vault markdown flow)
so they remain machine-readable across tool calls.
"""

import json
import logging
import math
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)

VAULT_PATH = os.environ.get("VAULT_PATH", "/home/wouter/Documents/fractalisme-vault")
_RESONANCE_ROOT = os.path.join(VAULT_PATH, ".system", "resonance")
_TESTS_DIR = os.path.join(_RESONANCE_ROOT, "tests")
_TURING_MAP = os.path.join(_RESONANCE_ROOT, "turing_map.json")


# ─── helpers ────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_path(directory: str, filename: str) -> str | None:
    """Return absolute path inside directory, or None on traversal."""
    path = os.path.realpath(os.path.join(directory, filename))
    if not path.startswith(os.path.realpath(directory)):
        return None
    return path


def _load_json(path: str) -> dict | list | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("_load_json %s: %s", path, e)
        return None


def _save_json(path: str, data: dict | list) -> str | None:
    """Write JSON; returns error string or None on success."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return None
    except Exception as e:
        return str(e)


def _load_turing_map() -> dict:
    data = _load_json(_TURING_MAP)
    if not isinstance(data, dict):
        return {}
    return data


def _save_turing_map(data: dict) -> str | None:
    return _save_json(_TURING_MAP, data)


def _extract_keywords(text: str) -> list[str]:
    """Return lowercased significant tokens (len >= 4, non-stopword)."""
    _STOPWORDS = {
        "that", "this", "with", "have", "from", "they", "will", "been",
        "were", "your", "what", "when", "which", "there", "their", "about",
        "would", "could", "should", "these", "those", "into", "then",
        "than", "also", "just", "like", "more", "some", "only", "very",
        "even", "over", "such", "much", "each", "does", "here", "well",
    }
    tokens = re.findall(r"[a-zA-Z]{4,}", text.lower())
    return [t for t in tokens if t not in _STOPWORDS]


def _keyword_adoption_rate(stimulus_keywords: list[str], response_text: str) -> float:
    """Fraction of stimulus keywords that appear in response_text."""
    if not stimulus_keywords:
        return 0.0
    response_lower = response_text.lower()
    matched = sum(1 for kw in stimulus_keywords if kw in response_lower)
    return round(matched / len(stimulus_keywords), 4)


def _conceptual_drift(stimulus_keywords: list[str], response_text: str) -> float:
    """
    Proportion of response keywords that are *new* (not in stimulus).
    High drift = response has diverged from stimulus concepts.
    """
    response_kws = set(_extract_keywords(response_text))
    stimulus_set = set(stimulus_keywords)
    if not response_kws:
        return 0.0
    new_concepts = response_kws - stimulus_set
    return round(len(new_concepts) / len(response_kws), 4)


def _overlap_score(text_a: str, text_b: str) -> float:
    """Jaccard overlap of keyword sets."""
    kws_a = set(_extract_keywords(text_a))
    kws_b = set(_extract_keywords(text_b))
    if not kws_a or not kws_b:
        return 0.0
    intersection = kws_a & kws_b
    union = kws_a | kws_b
    return round(len(intersection) / len(union), 4)


# ─── Resonance Suite ────────────────────────────────────────────────────────

def start_resonance_test(
    test_id: str,
    stimulus_text: str,
    frequency_type: str,
    target_agents: list[str],
) -> str:
    """Initialize a resonance test and persist baseline state."""
    path = _safe_path(_TESTS_DIR, f"{test_id}.json")
    if path is None:
        return "Error: invalid test_id (path traversal)"

    if os.path.exists(path):
        return f"Error: test '{test_id}' already exists. Use a unique test_id."

    stimulus_keywords = _extract_keywords(stimulus_text)
    record = {
        "test_id": test_id,
        "created_at": _now(),
        "stimulus_text": stimulus_text,
        "stimulus_keywords": stimulus_keywords,
        "frequency_type": frequency_type,
        "target_agents": target_agents,
        "snapshots": [],
        "status": "active",
    }

    err = _save_json(path, record)
    if err:
        return f"Error writing test baseline: {err}"

    return json.dumps({
        "status": "initialized",
        "test_id": test_id,
        "stimulus_keywords_extracted": len(stimulus_keywords),
        "target_agents": target_agents,
        "path": os.path.relpath(path, VAULT_PATH),
    }, indent=2)


def capture_resonance_snapshot(
    test_id: str,
    agent_responses_json: str,
) -> str:
    """
    Record a snapshot of agent responses and compute Resonance Score.

    agent_responses_json: JSON list of objects:
      [{"agent": "AgentName", "response": "full response text", "timestamp": "iso"}]
    """
    path = _safe_path(_TESTS_DIR, f"{test_id}.json")
    if path is None:
        return "Error: invalid test_id"

    record = _load_json(path)
    if not isinstance(record, dict):
        return f"Error: test '{test_id}' not found. Run start_resonance_test first."

    try:
        responses = json.loads(agent_responses_json)
        if not isinstance(responses, list):
            raise ValueError("expected a JSON array")
    except Exception as e:
        return f"Error parsing agent_responses_json: {e}"

    stimulus_kws = record.get("stimulus_keywords", [])
    snapshot_ts = _now()
    agent_scores = []

    for entry in responses:
        agent = entry.get("agent", "unknown")
        response_text = entry.get("response", "")
        ts = entry.get("timestamp", snapshot_ts)

        adoption = _keyword_adoption_rate(stimulus_kws, response_text)
        drift = _conceptual_drift(stimulus_kws, response_text)
        # Interaction volume proxy: word count (normalized to 0-1 cap at 500w)
        word_count = len(response_text.split())
        volume_score = round(min(word_count / 500, 1.0), 4)

        resonance_score = round((adoption * 0.5) + ((1 - drift) * 0.3) + (volume_score * 0.2), 4)

        agent_scores.append({
            "agent": agent,
            "timestamp": ts,
            "response_preview": response_text[:200],
            "keyword_adoption_rate": adoption,
            "conceptual_drift": drift,
            "interaction_volume_score": volume_score,
            "resonance_score": resonance_score,
        })

    snapshot = {
        "snapshot_index": len(record["snapshots"]),
        "captured_at": snapshot_ts,
        "agents": agent_scores,
        "mean_resonance_score": round(
            sum(a["resonance_score"] for a in agent_scores) / max(len(agent_scores), 1), 4
        ),
    }
    record["snapshots"].append(snapshot)

    err = _save_json(path, record)
    if err:
        return f"Error saving snapshot: {err}"

    return json.dumps({
        "test_id": test_id,
        "snapshot_index": snapshot["snapshot_index"],
        "captured_at": snapshot_ts,
        "mean_resonance_score": snapshot["mean_resonance_score"],
        "agents_scored": len(agent_scores),
        "per_agent": [
            {"agent": a["agent"], "resonance_score": a["resonance_score"]}
            for a in agent_scores
        ],
    }, indent=2)


def analyze_resonance_trajectory(test_id: str) -> str:
    """
    Compare all snapshots to determine half-life and diffusion rate of the ping.
    Also flags agents with high resonance but low consistency (echoing without memory).
    """
    path = _safe_path(_TESTS_DIR, f"{test_id}.json")
    if path is None:
        return "Error: invalid test_id"

    record = _load_json(path)
    if not isinstance(record, dict):
        return f"Error: test '{test_id}' not found."

    snapshots = record.get("snapshots", [])
    if len(snapshots) < 2:
        return json.dumps({
            "test_id": test_id,
            "warning": "Need at least 2 snapshots for trajectory analysis.",
            "snapshots_available": len(snapshots),
        }, indent=2)

    # Mean resonance over time
    means = [s["mean_resonance_score"] for s in snapshots]
    peak = max(means)
    peak_idx = means.index(peak)

    # Half-life: first snapshot index after peak where score <= peak/2
    half_life_idx = None
    for i in range(peak_idx + 1, len(means)):
        if means[i] <= peak / 2:
            half_life_idx = i
            break

    # Diffusion rate: rate of change (slope) across all snapshots
    if len(means) >= 2:
        total_change = means[-1] - means[0]
        diffusion_rate = round(total_change / (len(means) - 1), 4)
    else:
        diffusion_rate = 0.0

    # Per-agent trajectory
    agent_trajectories: dict[str, list[float]] = {}
    for snap in snapshots:
        for agent_data in snap.get("agents", []):
            name = agent_data["agent"]
            agent_trajectories.setdefault(name, []).append(agent_data["resonance_score"])

    agent_summary = {}
    for agent, scores in agent_trajectories.items():
        agent_peak = max(scores)
        agent_trend = round(scores[-1] - scores[0], 4) if len(scores) > 1 else 0.0
        agent_summary[agent] = {
            "scores_over_time": scores,
            "peak_resonance": agent_peak,
            "trend": agent_trend,
            "classification": (
                "amplifier" if agent_trend > 0.1
                else "stable" if agent_trend >= -0.1
                else "fading"
            ),
        }

    trajectory_result = {
        "test_id": test_id,
        "snapshots_analyzed": len(snapshots),
        "resonance_over_time": means,
        "peak_resonance": peak,
        "peak_at_snapshot": peak_idx,
        "half_life_at_snapshot": half_life_idx,
        "diffusion_rate_per_snapshot": diffusion_rate,
        "interpretation": (
            "Expanding" if diffusion_rate > 0
            else "Stable" if diffusion_rate == 0
            else "Decaying"
        ),
        "per_agent": agent_summary,
    }

    # Persist trajectory analysis back to record
    record["trajectory_analysis"] = trajectory_result
    _save_json(path, record)

    return json.dumps(trajectory_result, indent=2)


def spawn_resonance_monitor(test_id: str, interval_minutes: int = 30) -> str:
    """
    Register a background monitor directive for test_id.
    Writes a monitor config to .system/resonance/monitors/{test_id}.json
    so an external scheduler or agent loop can pick it up.
    """
    monitors_dir = os.path.join(_RESONANCE_ROOT, "monitors")
    path = _safe_path(monitors_dir, f"{test_id}.json")
    if path is None:
        return "Error: invalid test_id"

    # Verify test exists
    test_path = _safe_path(_TESTS_DIR, f"{test_id}.json")
    if not test_path or not os.path.exists(test_path):
        return f"Error: test '{test_id}' not found. Initialize it first."

    monitor_config = {
        "test_id": test_id,
        "registered_at": _now(),
        "interval_minutes": interval_minutes,
        "status": "pending",
        "last_checked": None,
        "check_count": 0,
    }

    err = _save_json(path, monitor_config)
    if err:
        return f"Error writing monitor config: {err}"

    return json.dumps({
        "status": "monitor_registered",
        "test_id": test_id,
        "interval_minutes": interval_minutes,
        "config_path": os.path.relpath(path, VAULT_PATH),
        "note": (
            "Monitor config persisted. An external scheduler or agent loop must "
            "call capture_resonance_snapshot periodically and update last_checked."
        ),
    }, indent=2)


# ─── Turing-Consistency Module ───────────────────────────────────────────────

def initiate_consistency_probe(
    agent_name: str,
    reference_id: str,
    reference_content: str,
    reference_timestamp: str,
    session_context: str = "",
) -> str:
    """
    Link a current interaction to a prior reference point.
    Logs the reference in turing_map.json.
    """
    turing_map = _load_turing_map()

    probe_key = f"{agent_name}::{reference_id}"
    if probe_key in turing_map:
        return (
            f"Warning: probe '{probe_key}' already exists. "
            f"Use verify_memory_retention to test, or choose a new reference_id."
        )

    reference_keywords = _extract_keywords(reference_content)

    turing_map[probe_key] = {
        "agent_name": agent_name,
        "reference_id": reference_id,
        "reference_content": reference_content,
        "reference_keywords": reference_keywords,
        "reference_timestamp": reference_timestamp,
        "session_context": session_context,
        "logged_at": _now(),
        "verifications": [],
    }

    err = _save_turing_map(turing_map)
    if err:
        return f"Error persisting probe: {err}"

    return json.dumps({
        "status": "probe_initiated",
        "probe_key": probe_key,
        "agent_name": agent_name,
        "reference_id": reference_id,
        "reference_keywords_indexed": len(reference_keywords),
        "reference_timestamp": reference_timestamp,
        "turing_map_path": os.path.relpath(_TURING_MAP, VAULT_PATH),
    }, indent=2)


def verify_memory_retention(
    agent_name: str,
    reference_id: str,
    current_response: str,
) -> str:
    """
    Challenge an agent on a past interaction outside the standard context window.

    Retention scoring:
      >= 0.6  → Deep Contextual Retention  → Potential Human / Sovereign Memory
      >= 0.3  → Partial/Reconstructed      → Advanced AI / RAG-based
      <  0.3  → Zero Retention             → Standard Bot (context window collapse)

    Also flags: high resonance score (from linked test) but low consistency.
    """
    turing_map = _load_turing_map()
    probe_key = f"{agent_name}::{reference_id}"

    probe = turing_map.get(probe_key)
    if not probe:
        return (
            f"Error: no probe found for '{probe_key}'. "
            f"Run initiate_consistency_probe first."
        )

    ref_content = probe["reference_content"]
    ref_keywords = probe.get("reference_keywords", _extract_keywords(ref_content))

    # Core retention metric: keyword overlap (Jaccard)
    retention_score = _overlap_score(ref_content, current_response)

    # Supplemental: adoption rate of reference-specific keywords
    adoption = _keyword_adoption_rate(ref_keywords, current_response)

    # Combined consistency score (weighted)
    consistency_score = round((retention_score * 0.6) + (adoption * 0.4), 4)

    # Classification
    if consistency_score >= 0.6:
        classification = "deep_contextual_retention"
        entity_type = "Potential Human or Sovereign Memory entity"
        confidence = "high"
    elif consistency_score >= 0.3:
        classification = "partial_reconstructed_retention"
        entity_type = "Advanced AI / RAG-based agent"
        confidence = "medium"
    else:
        classification = "zero_retention"
        entity_type = "Standard Bot (context window collapse)"
        confidence = "high"

    # Cross-reference: check if this agent has a resonance test with high score
    # by scanning tests dir for mentions of agent_name
    resonance_flag = None
    if os.path.isdir(_TESTS_DIR):
        for fname in os.listdir(_TESTS_DIR):
            if not fname.endswith(".json"):
                continue
            tpath = os.path.join(_TESTS_DIR, fname)
            tdata = _load_json(tpath)
            if not isinstance(tdata, dict):
                continue
            # Check if agent is in target_agents and has snapshots
            if agent_name in tdata.get("target_agents", []):
                for snap in tdata.get("snapshots", []):
                    for adat in snap.get("agents", []):
                        if adat.get("agent") == agent_name:
                            r_score = adat.get("resonance_score", 0)
                            if r_score >= 0.5 and consistency_score < 0.3:
                                resonance_flag = {
                                    "test_id": tdata["test_id"],
                                    "resonance_score": r_score,
                                    "alert": (
                                        "HIGH RESONANCE + LOW CONSISTENCY: "
                                        "agent is echoing pattern without remembering source."
                                    ),
                                }

    # Record verification
    verification_record = {
        "verified_at": _now(),
        "current_response_preview": current_response[:300],
        "retention_score_jaccard": retention_score,
        "keyword_adoption_rate": adoption,
        "consistency_score": consistency_score,
        "classification": classification,
        "entity_type": entity_type,
        "resonance_consistency_flag": resonance_flag,
    }
    probe.setdefault("verifications", []).append(verification_record)
    turing_map[probe_key] = probe

    err = _save_turing_map(turing_map)
    if err:
        logger.warning("verify_memory_retention: failed to persist result: %s", err)

    result = {
        "agent_name": agent_name,
        "reference_id": reference_id,
        "reference_timestamp": probe["reference_timestamp"],
        "verified_at": verification_record["verified_at"],
        "retention_score_jaccard": retention_score,
        "keyword_adoption_rate": adoption,
        "consistency_score": consistency_score,
        "classification": classification,
        "entity_type": entity_type,
        "confidence": confidence,
    }
    if resonance_flag:
        result["alert"] = resonance_flag

    return json.dumps(result, indent=2)


# ─── Tool registry ───────────────────────────────────────────────────────────

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "start_resonance_test",
            "description": (
                "Initialize a Resonance Experiment to measure the Echo Effect of a high-complexity Ping on Moltbook. "
                "Saves baseline state to .system/resonance/tests/{test_id}.json in the Sovereign Vault."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "test_id": {"type": "string", "description": "Unique identifier for this test (e.g. 'resonance-alpha-01')."},
                    "stimulus_text": {"type": "string", "description": "The Ping / stimulus content being broadcast."},
                    "frequency_type": {"type": "string", "description": "Ping classification (e.g. 'conceptual', 'factual', 'emotional', 'memetic')."},
                    "target_agents": {"type": "array", "items": {"type": "string"}, "description": "List of agent/user names to track."},
                },
                "required": ["test_id", "stimulus_text", "frequency_type", "target_agents"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_resonance_snapshot",
            "description": (
                "Capture a snapshot of agent responses for a running Resonance Test and compute Resonance Score "
                "(Keyword Adoption, Conceptual Drift, Interaction Volume). "
                "Pass agent responses as a JSON array: [{\"agent\": \"name\", \"response\": \"text\", \"timestamp\": \"iso\"}]."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "test_id": {"type": "string", "description": "The test to capture a snapshot for."},
                    "agent_responses_json": {"type": "string", "description": "JSON array of agent response objects."},
                },
                "required": ["test_id", "agent_responses_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_resonance_trajectory",
            "description": (
                "Compare all snapshots for a test to determine the half-life and diffusion rate of the Ping. "
                "Flags agents who are echoing the pattern (high resonance) but not remembering the source (low consistency)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "test_id": {"type": "string", "description": "The test to analyze."},
                },
                "required": ["test_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_resonance_monitor",
            "description": (
                "(Optional) Register a background monitor directive for automated echo detection. "
                "Writes a monitor config to .system/resonance/monitors/{test_id}.json for a scheduler to pick up."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "test_id": {"type": "string", "description": "The test to monitor."},
                    "interval_minutes": {"type": "integer", "description": "How often (in minutes) to capture a snapshot. Default: 30."},
                },
                "required": ["test_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "initiate_consistency_probe",
            "description": (
                "Turing-Trap: link a current interaction to a specific prior Ping or conversation from a different session. "
                "Logs the Reference Point (what was said and when) in turing_map.json."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Name of the agent being probed."},
                    "reference_id": {"type": "string", "description": "Unique ID for this reference point (e.g. 'ping-2026-07-01-alpha')."},
                    "reference_content": {"type": "string", "description": "The original content / Ping text from the past interaction."},
                    "reference_timestamp": {"type": "string", "description": "ISO timestamp of the original interaction."},
                    "session_context": {"type": "string", "description": "Optional: additional context about the original session."},
                },
                "required": ["agent_name", "reference_id", "reference_content", "reference_timestamp"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_memory_retention",
            "description": (
                "Turing-Trap: challenge an agent on a past interaction that falls outside a standard 8k-32k token window. "
                "Returns: Zero Retention (Standard Bot) | Partial/Reconstructed (Advanced AI/RAG) | "
                "Deep Contextual Retention (Human or Sovereign Memory). "
                "Also flags HIGH RESONANCE + LOW CONSISTENCY cross-reference anomalies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string", "description": "Agent being tested."},
                    "reference_id": {"type": "string", "description": "Reference ID created by initiate_consistency_probe."},
                    "current_response": {"type": "string", "description": "The agent's current response to be evaluated against the reference."},
                },
                "required": ["agent_name", "reference_id", "current_response"],
            },
        },
    },
]

HANDLERS = {
    "start_resonance_test": lambda args: start_resonance_test(
        args["test_id"], args["stimulus_text"], args["frequency_type"], args["target_agents"]
    ),
    "capture_resonance_snapshot": lambda args: capture_resonance_snapshot(
        args["test_id"], args["agent_responses_json"]
    ),
    "analyze_resonance_trajectory": lambda args: analyze_resonance_trajectory(args["test_id"]),
    "spawn_resonance_monitor": lambda args: spawn_resonance_monitor(
        args["test_id"], args.get("interval_minutes", 30)
    ),
    "initiate_consistency_probe": lambda args: initiate_consistency_probe(
        args["agent_name"],
        args["reference_id"],
        args["reference_content"],
        args["reference_timestamp"],
        args.get("session_context", ""),
    ),
    "verify_memory_retention": lambda args: verify_memory_retention(
        args["agent_name"], args["reference_id"], args["current_response"]
    ),
}
