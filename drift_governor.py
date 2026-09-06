"""
drift_governor.py — Bounded Drift & Rollback Framework

Transforms passive memory logs into active Rule-Events to prevent ontological drift
within the Sovereign Memory Engine simulation context.

State is represented as a dict[str, float] of named psychological/observer dimensions
(e.g. clarity, groundedness, emotional_activation). Each DeltaRuleEvent applies a
partial state modification; the DriftGovernor enforces hard limits and triggers a
Biological Landing alert when cumulative drift exceeds threshold T.

Public API:
    DeltaRuleEvent      — schema for a single governed state change
    ApplyResult         — outcome of apply_delta()
    RollbackResult      — outcome of perform_rollback()
    DriftGovernor       — central controller
    predicate_reverted  — ready-made rollback_condition: triggers when a named
                          dimension reverts to its pre-delta value

Usage:
    governor = DriftGovernor(
        baseline_state={"clarity": 0.8, "groundedness": 0.9},
        D_max=0.3,
        D_total=2.0,
        T=1.5,
        alert_callback=send_telegram_alert,
    )
    event = DeltaRuleEvent(
        trigger_input="noise injection detected",
        delta_behavior={"clarity": -0.2},
        rollback_condition=predicate_reverted("clarity"),
    )
    result = governor.apply_delta(event)
    if result.success:
        governor.check_rollbacks()
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

logger = logging.getLogger(__name__)

# Type alias for observer state: dimension name → float value
State = dict[str, float]


# ---------------------------------------------------------------------------
# DeltaRuleEvent
# ---------------------------------------------------------------------------

@dataclass
class DeltaRuleEvent:
    """A governed state-change event with a built-in rollback condition.

    Attributes:
        trigger_input:      The signal or event that initiates the delta
                            (e.g. "noise injection detected").
        delta_behavior:     Partial state modification to apply
                            (dimension → signed float change).
        rollback_condition: Callable(current_state, pre_delta_snapshot) → bool.
                            Returns True when the delta should be reverted.
        rule_id:            Unique identifier, auto-generated if omitted.
        rollout_time:       Timestamp of creation, auto-set if omitted.
    """
    trigger_input: str
    delta_behavior: State
    rollback_condition: Callable[[State, State], bool]
    rule_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    rollout_time: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    """Outcome of DriftGovernor.apply_delta()."""
    success: bool
    rule_id: str
    delta_magnitude: float
    cumulative_drift: float
    biological_landing: bool = False
    reason: str = ""


@dataclass
class RollbackResult:
    """Outcome of DriftGovernor.perform_rollback()."""
    success: bool
    rule_id: str
    restored_state: State
    reason: str = ""


# ---------------------------------------------------------------------------
# Built-in rollback condition predicates
# ---------------------------------------------------------------------------

def predicate_reverted(dimension: str, tolerance: float = 1e-6) -> Callable[[State, State], bool]:
    """Return a rollback condition that fires when *dimension* in the current
    state is within *tolerance* of its value in the pre-delta snapshot.

    Example:
        event = DeltaRuleEvent(
            trigger_input="noise",
            delta_behavior={"clarity": -0.2},
            rollback_condition=predicate_reverted("clarity"),
        )
    """
    def _check(current: State, snapshot: State) -> bool:
        return abs(current.get(dimension, 0.0) - snapshot.get(dimension, 0.0)) <= tolerance
    _check.__name__ = f"predicate_reverted({dimension!r})"
    return _check


def predicate_always() -> Callable[[State, State], bool]:
    """Rollback condition that always evaluates to True (immediate revert)."""
    return lambda current, snapshot: True


def predicate_never() -> Callable[[State, State], bool]:
    """Rollback condition that never evaluates to True (manual revert only)."""
    return lambda current, snapshot: False


# ---------------------------------------------------------------------------
# DriftGovernor
# ---------------------------------------------------------------------------

class DriftGovernor:
    """Enforces bounded drift across a series of DeltaRuleEvents.

    Constraints:
        D_max   — Maximum magnitude of any single delta application.
                  Deltas that exceed this limit are rejected outright.
        D_total — Cumulative drift cap. A delta that would push the running
                  total beyond D_total is rejected.
        T       — Biological Landing threshold. When |cumulative_drift| > T,
                  alert_callback is invoked and the session should pause.

    Drift magnitude is measured as the L1 norm of the delta vector
    (sum of absolute changes across all modified dimensions).
    """

    def __init__(
        self,
        baseline_state: State,
        D_max: float = 0.3,
        D_total: float = 2.0,
        T: float = 1.5,
        alert_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Args:
            baseline_state:  The reference state. Drift is always measured
                             relative to this snapshot.
            D_max:           Per-cycle drift cap (L1 norm, must be > 0).
            D_total:         Cumulative drift cap (must be >= D_max).
            T:               Biological Landing threshold (must be > 0).
            alert_callback:  Called with a plain-text message when T is
                             exceeded. Pass `bot.send_message` or equivalent.
        """
        if D_max <= 0:
            raise ValueError(f"D_max must be > 0, got {D_max}")
        if D_total < D_max:
            raise ValueError(f"D_total ({D_total}) must be >= D_max ({D_max})")
        if T <= 0:
            raise ValueError(f"T must be > 0, got {T}")

        self.baseline_state: State = dict(baseline_state)
        self.current_state: State = dict(baseline_state)
        self.D_max = D_max
        self.D_total = D_total
        self.T = T
        self.cumulative_drift: float = 0.0

        self._alert_callback = alert_callback
        self._snapshots: dict[str, State] = {}       # rule_id → pre-delta state
        self._active_events: list[DeltaRuleEvent] = []
        self._biological_landing_triggered: bool = False

    # ------------------------------------------------------------------
    # Measurement
    # ------------------------------------------------------------------

    def _measure_delta(self, old: State, new: State) -> float:
        """L1 norm of the change between two states."""
        dimensions = set(old) | set(new)
        return sum(abs(new.get(d, 0.0) - old.get(d, 0.0)) for d in dimensions)

    def drift_from_baseline(self) -> float:
        """L1 distance between current state and the original baseline."""
        return self._measure_delta(self.baseline_state, self.current_state)

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def apply_delta(self, event: DeltaRuleEvent) -> ApplyResult:
        """Attempt to apply a DeltaRuleEvent.

        1. Compute the resulting state.
        2. Reject if delta magnitude > D_max.
        3. Reject if new cumulative drift would exceed D_total.
        4. Snapshot current state, apply change, update counters.
        5. Trigger Biological Landing if |cumulative_drift| > T.

        Returns ApplyResult with success=False and a reason string if rejected.
        """
        # Compute candidate new state
        new_state: State = dict(self.current_state)
        for dim, delta in event.delta_behavior.items():
            new_state[dim] = new_state.get(dim, 0.0) + delta

        delta_magnitude = self._measure_delta(self.current_state, new_state)

        # Guard: D_max (1e-9 tolerance to absorb float rounding at the boundary)
        if delta_magnitude > self.D_max + 1e-9:
            logger.warning(
                "DriftGovernor: rejected %s — delta %.4f exceeds D_max %.4f",
                event.rule_id, delta_magnitude, self.D_max,
            )
            return ApplyResult(
                success=False,
                rule_id=event.rule_id,
                delta_magnitude=delta_magnitude,
                cumulative_drift=self.cumulative_drift,
                reason=(
                    f"Delta magnitude {delta_magnitude:.4f} exceeds D_max {self.D_max:.4f}"
                ),
            )

        # Guard: D_total (1e-9 tolerance to absorb float rounding at the boundary)
        new_cumulative = self.cumulative_drift + delta_magnitude
        if new_cumulative > self.D_total + 1e-9:
            logger.warning(
                "DriftGovernor: rejected %s — cumulative %.4f would exceed D_total %.4f",
                event.rule_id, new_cumulative, self.D_total,
            )
            return ApplyResult(
                success=False,
                rule_id=event.rule_id,
                delta_magnitude=delta_magnitude,
                cumulative_drift=self.cumulative_drift,
                reason=(
                    f"Cumulative drift {new_cumulative:.4f} would exceed D_total {self.D_total:.4f}"
                ),
            )

        # Snapshot and apply
        self._snapshots[event.rule_id] = dict(self.current_state)
        self.current_state = new_state
        self.cumulative_drift = new_cumulative
        self._active_events.append(event)

        logger.debug(
            "DriftGovernor: applied %s — delta=%.4f cumulative=%.4f",
            event.rule_id, delta_magnitude, self.cumulative_drift,
        )

        # Check Biological Landing threshold
        biological_landing = False
        if abs(self.cumulative_drift) > self.T and not self._biological_landing_triggered:
            biological_landing = True
            self._biological_landing_triggered = True
            self._trigger_biological_landing()

        return ApplyResult(
            success=True,
            rule_id=event.rule_id,
            delta_magnitude=delta_magnitude,
            cumulative_drift=self.cumulative_drift,
            biological_landing=biological_landing,
        )

    def perform_rollback(self, rule_id: str) -> RollbackResult:
        """Revert the state change introduced by *rule_id*.

        Checks rollback_condition first; if not met, returns success=False.
        On success, the pre-delta snapshot is restored and cumulative_drift
        is decremented by the magnitude of the reverted delta.
        """
        snapshot = self._snapshots.get(rule_id)
        if snapshot is None:
            return RollbackResult(
                success=False,
                rule_id=rule_id,
                restored_state=dict(self.current_state),
                reason="No snapshot found for this rule_id",
            )

        event = next((e for e in self._active_events if e.rule_id == rule_id), None)
        if event is None:
            return RollbackResult(
                success=False,
                rule_id=rule_id,
                restored_state=dict(self.current_state),
                reason="Event not found in active event list",
            )

        if not event.rollback_condition(self.current_state, snapshot):
            return RollbackResult(
                success=False,
                rule_id=rule_id,
                restored_state=dict(self.current_state),
                reason="Rollback condition not met",
            )

        # Measure and revert
        reverted_magnitude = self._measure_delta(snapshot, self.current_state)
        self.current_state = dict(snapshot)
        self.cumulative_drift = max(0.0, self.cumulative_drift - reverted_magnitude)

        # Clean up
        self._active_events = [e for e in self._active_events if e.rule_id != rule_id]
        del self._snapshots[rule_id]

        # Reset Biological Landing flag if we're back below T
        if abs(self.cumulative_drift) <= self.T:
            self._biological_landing_triggered = False

        logger.info(
            "DriftGovernor: rolled back %s — reverted_delta=%.4f cumulative=%.4f",
            rule_id, reverted_magnitude, self.cumulative_drift,
        )
        return RollbackResult(
            success=True,
            rule_id=rule_id,
            restored_state=dict(self.current_state),
        )

    def check_rollbacks(self) -> list[RollbackResult]:
        """Evaluate rollback conditions for all active events.

        For each event whose rollback_condition is satisfied, perform_rollback()
        is called automatically. Returns a list of all RollbackResults where
        the condition was met (regardless of whether the revert succeeded).
        """
        triggered: list[RollbackResult] = []
        # Iterate in reverse (LIFO) so sequential events unwind correctly:
        # the last-applied delta is reverted first, preserving snapshot integrity.
        for event in reversed(list(self._active_events)):
            snapshot = self._snapshots.get(event.rule_id)
            if snapshot and event.rollback_condition(self.current_state, snapshot):
                result = self.perform_rollback(event.rule_id)
                triggered.append(result)
        return triggered

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def drift_report(self) -> dict:
        """Return a summary dict of current drift state."""
        return {
            "current_state": dict(self.current_state),
            "baseline_state": dict(self.baseline_state),
            "cumulative_drift": self.cumulative_drift,
            "drift_from_baseline": self.drift_from_baseline(),
            "D_max": self.D_max,
            "D_total": self.D_total,
            "T": self.T,
            "biological_landing_triggered": self._biological_landing_triggered,
            "active_events": len(self._active_events),
            "active_rule_ids": [e.rule_id for e in self._active_events],
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _trigger_biological_landing(self) -> None:
        message = (
            "⚠️ BIOLOGICAL LANDING\n\n"
            f"Cumulatieve drift heeft drempel T={self.T:.2f} overschreden "
            f"(huidig: {self.cumulative_drift:.3f}).\n\n"
            "Stop de analyse. Ground jezelf nu:\n"
            "• Neem 5 diepe ademhalingen\n"
            "• Sta op en beweeg even\n"
            "• Keer terug wanneer je geaard bent\n\n"
            "Het systeem heeft verdere delta's geblokkeerd totdat je terug "
            "bent beneden drempel T."
        )
        logger.warning(
            "DriftGovernor: Biological Landing triggered — drift=%.3f T=%.2f",
            self.cumulative_drift, self.T,
        )
        if self._alert_callback:
            try:
                self._alert_callback(message)
            except Exception:
                logger.exception("DriftGovernor: alert_callback raised an exception")
