"""
OpenJev decision client for Sovereign-Link.

Classifies incoming messages before LLM dispatch, enabling fast typed routing
without consuming LLM tokens. Degrades gracefully: if OpenJev is unavailable
or not configured, all calls return None and the normal LLM path runs.

Environment variables:
    OPENJEV_BASE_URL   — base URL of your OpenJev server (e.g. http://localhost:8080
                         or https://api.codiv.ai). Leave unset to disable.
    OPENJEV_API_KEY    — bearer token (required for hosted API, optional for local).
    OPENJEV_MODEL      — model name (default: openjev-latest).
    OPENJEV_TIMEOUT    — HTTP timeout in seconds (default: 5.0).
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BASE_URL: str = os.environ.get("OPENJEV_BASE_URL", "").rstrip("/")
_API_KEY: str = os.environ.get("OPENJEV_API_KEY", "")
_MODEL: str = os.environ.get("OPENJEV_MODEL", "openjev-latest")
_TIMEOUT: float = float(os.environ.get("OPENJEV_TIMEOUT", "5.0"))

ENABLED: bool = bool(_BASE_URL)


def decide_sync(state: str, questions: dict) -> dict | None:
    """
    Synchronous version of decide() using httpx.Client.

    Use in synchronous call paths (e.g. mental_state_analyzer, cognitive_brake).
    Never raises — returns None on any failure.
    """
    if not ENABLED:
        return None
    headers = {"Content-Type": "application/json"}
    if _API_KEY:
        headers["Authorization"] = f"Bearer {_API_KEY}"
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                f"{_BASE_URL}/v1/systemone",
                headers=headers,
                json={"model": _MODEL, "state": state, "questions": questions},
            )
            resp.raise_for_status()
            return resp.json().get("answers", {})
    except Exception:
        logger.debug("OpenJev unavailable (sync) — falling back", exc_info=True)
        return None


async def decide(state: str, questions: dict) -> dict | None:
    """
    POST state + questions to /v1/systemone.

    Returns the ``answers`` dict on success, None on any failure.
    Never raises — the caller is responsible for falling back to LLM-only.
    """
    if not ENABLED:
        return None
    headers = {"Content-Type": "application/json"}
    if _API_KEY:
        headers["Authorization"] = f"Bearer {_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{_BASE_URL}/v1/systemone",
                headers=headers,
                json={"model": _MODEL, "state": state, "questions": questions},
            )
            resp.raise_for_status()
            return resp.json().get("answers", {})
    except Exception:
        logger.debug("OpenJev unavailable — falling back to LLM-only routing", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Request classification
# ---------------------------------------------------------------------------

_REQUEST_TYPE_CRITERIA = {
    "task": "A multi-step task, research assignment, or goal that would benefit from autonomous agent execution",
    "vault_query": "A question about past notes, memories, stored information, or previous conversations",
    "command": "A short operational command such as a toggle, status check, or system control",
    "chat": "Casual conversation, a simple direct question, or anything that needs a direct reply",
}

_CLASSIFICATION_QUESTIONS = {
    "request_type": {
        "type": "choice",
        "instructions": "What type of request is the user making?",
        "criteria": _REQUEST_TYPE_CRITERIA,
    },
    "needs_agent": {
        "type": "noul",
        "instructions": (
            "Should this request be handled by a background agent that can autonomously "
            "execute multiple steps, rather than a single LLM response?"
        ),
    },
    "needs_vault": {
        "type": "noul",
        "instructions": (
            "Does answering this request require searching the user's vault — "
            "past notes, memories, or stored information?"
        ),
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent or time-sensitive is this request?",
        "criteria": [
            "Completely non-urgent, casual or background",
            "Low priority, can wait",
            "Normal conversational priority",
            "Somewhat time-sensitive",
            "Urgent, needs immediate attention",
        ],
    },
}


class RouteHint:
    """Typed routing metadata returned by classify_request()."""

    __slots__ = ("request_type", "needs_agent", "needs_vault", "needs_vault_score", "urgency", "type_confidence")

    def __init__(
        self,
        request_type: str,
        needs_agent: bool,
        needs_vault: bool,
        needs_vault_score: float,
        urgency: float,
        type_confidence: float,
    ) -> None:
        self.request_type: str = request_type
        self.needs_agent: bool = needs_agent
        self.needs_vault: bool = needs_vault
        self.needs_vault_score: float = needs_vault_score  # raw noul probability 0-1
        self.urgency: float = urgency          # expected value on 1-5 scale
        self.type_confidence: float = type_confidence  # 0-1

    def __repr__(self) -> str:
        return (
            f"RouteHint(type={self.request_type!r}, agent={self.needs_agent}, "
            f"vault={self.needs_vault}({self.needs_vault_score:.2f}), urgency={self.urgency:.1f}, "
            f"conf={self.type_confidence:.2f})"
        )


# ---------------------------------------------------------------------------
# Mental state classification
# ---------------------------------------------------------------------------

_MENTAL_STATE_QUESTIONS = {
    "phase": {
        "type": "choice",
        "instructions": "What is the user's current mental/emotional state based on their message?",
        "criteria": {
            "STABILISATIE": "Anxiety, stress, urgency, hyper-arousal, overwhelm, or panic",
            "EXPANSIE": "Flow, euphoria, creative energy, enthusiasm, or visionary thinking",
            "RECOVERY": "Lethargy, emptiness, apathy, fatigue, or indifference",
            "NEUTRAAL": "No significant emotional signal — functional, informational, or neutral",
        },
    },
    "clarity": {
        "type": "score",
        "instructions": "How clear and focused is the user's thinking?",
        "criteria": [
            "Very foggy or confused",
            "Mostly unclear",
            "Neutral clarity",
            "Mostly clear",
            "Very sharp and focused",
        ],
    },
    "groundedness": {
        "type": "score",
        "instructions": "How grounded and stable does the user seem emotionally?",
        "criteria": [
            "Completely ungrounded or dysregulated",
            "Mostly ungrounded",
            "Neutral",
            "Mostly grounded",
            "Very grounded and stable",
        ],
    },
    "emotional_activation": {
        "type": "score",
        "instructions": "What is the user's emotional activation or arousal level?",
        "criteria": [
            "Very low or flat",
            "Low",
            "Moderate",
            "High",
            "Very high or activated",
        ],
    },
}

# Maps Jev 1-5 score to the DriftGovernor 0-1 float scale
_SCORE_TO_FLOAT = {1: 0.1, 2: 0.3, 3: 0.5, 4: 0.7, 5: 0.9}


class MentalStateResult:
    """Result of classify_mental_state_sync()."""

    __slots__ = ("phase", "confidence", "clarity", "groundedness", "emotional_activation")

    def __init__(
        self,
        phase: str,
        confidence: float,
        clarity: float,
        groundedness: float,
        emotional_activation: float,
    ) -> None:
        self.phase: str = phase
        self.confidence: float = confidence
        self.clarity: float = clarity
        self.groundedness: float = groundedness
        self.emotional_activation: float = emotional_activation

    def __repr__(self) -> str:
        return (
            f"MentalStateResult(phase={self.phase!r}, conf={self.confidence:.2f}, "
            f"clarity={self.clarity:.2f}, ground={self.groundedness:.2f}, "
            f"activation={self.emotional_activation:.2f})"
        )


def classify_mental_state_sync(user_text: str) -> "MentalStateResult | None":
    """
    Classify the user's mental state via Jev (synchronous).

    Returns a MentalStateResult with phase + DriftGovernor-ready float dimensions,
    or None when Jev is disabled or unreachable.
    """
    answers = decide_sync(user_text, _MENTAL_STATE_QUESTIONS)
    if answers is None:
        return None

    phase_ans = answers.get("phase", {})
    phase: str = phase_ans.get("choice", "NEUTRAAL")
    if phase not in ("STABILISATIE", "EXPANSIE", "RECOVERY", "NEUTRAAL"):
        phase = "NEUTRAAL"
    confidence: float = phase_ans.get("confidence", 0.0)

    def _score(key: str) -> float:
        raw = answers.get(key, {}).get("score", 3.0)
        # Jev scores are expected values on the 1-5 scale; map to 0-1
        return max(0.0, min(1.0, (raw - 1) / 4))

    return MentalStateResult(
        phase=phase,
        confidence=confidence,
        clarity=_score("clarity"),
        groundedness=_score("groundedness"),
        emotional_activation=_score("emotional_activation"),
    )


async def classify_request(user_text: str) -> RouteHint | None:
    """
    Classify a user message and return a RouteHint.

    Returns None when OpenJev is disabled or unreachable.
    Callers must treat None as "no routing information — proceed normally".
    """
    answers = await decide(user_text, _CLASSIFICATION_QUESTIONS)
    if answers is None:
        return None

    rt = answers.get("request_type", {})
    request_type: str = rt.get("choice", "chat")
    type_confidence: float = rt.get("confidence", 0.0)

    needs_agent: bool = answers.get("needs_agent", {}).get("noul", 0.0) > 0.6
    needs_vault_score: float = answers.get("needs_vault", {}).get("noul", 0.0)
    needs_vault: bool = needs_vault_score > 0.6
    urgency: float = answers.get("urgency", {}).get("score", 3.0)

    return RouteHint(
        request_type=request_type,
        needs_agent=needs_agent,
        needs_vault=needs_vault,
        needs_vault_score=needs_vault_score,
        urgency=urgency,
        type_confidence=type_confidence,
    )
