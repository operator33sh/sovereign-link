"""
tests/test_drift_governor.py — Verification suite for the Bounded Drift & Rollback Framework.

Covers:
- DeltaRuleEvent schema and defaults
- D_max per-cycle cap enforcement
- D_total cumulative cap enforcement
- Rollback condition: predicate_reverted, predicate_always, predicate_never
- perform_rollback() state restoration and drift decrement
- check_rollbacks() automatic sweep
- Biological Landing threshold trigger
- 10-50 cycle simulation: all invariants hold throughout
"""

import pytest
from drift_governor import (
    ApplyResult,
    DeltaRuleEvent,
    DriftGovernor,
    RollbackResult,
    State,
    predicate_always,
    predicate_never,
    predicate_reverted,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASELINE: State = {
    "clarity": 0.8,
    "groundedness": 0.9,
    "emotional_activation": 0.2,
    "analytical_depth": 0.7,
}


def make_governor(
    D_max: float = 0.3,
    D_total: float = 2.0,
    T: float = 1.5,
    alert_callback=None,
) -> DriftGovernor:
    return DriftGovernor(
        baseline_state=BASELINE,
        D_max=D_max,
        D_total=D_total,
        T=T,
        alert_callback=alert_callback,
    )


def small_event(dim: str = "clarity", delta: float = -0.1) -> DeltaRuleEvent:
    return DeltaRuleEvent(
        trigger_input="test signal",
        delta_behavior={dim: delta},
        rollback_condition=predicate_never(),
    )


# ---------------------------------------------------------------------------
# DeltaRuleEvent schema
# ---------------------------------------------------------------------------

class TestDeltaRuleEventSchema:
    def test_rule_id_auto_generated(self):
        e1 = small_event()
        e2 = small_event()
        assert e1.rule_id != e2.rule_id

    def test_rule_id_is_string(self):
        e = small_event()
        assert isinstance(e.rule_id, str)
        assert len(e.rule_id) > 0

    def test_rollout_time_is_datetime(self):
        from datetime import datetime
        e = small_event()
        assert isinstance(e.rollout_time, datetime)

    def test_delta_behavior_stored(self):
        e = DeltaRuleEvent(
            trigger_input="noise",
            delta_behavior={"clarity": -0.15, "groundedness": -0.05},
            rollback_condition=predicate_never(),
        )
        assert e.delta_behavior == {"clarity": -0.15, "groundedness": -0.05}

    def test_trigger_input_stored(self):
        e = small_event()
        assert e.trigger_input == "test signal"


# ---------------------------------------------------------------------------
# DriftGovernor construction
# ---------------------------------------------------------------------------

class TestDriftGovernorConstruction:
    def test_baseline_copied(self):
        g = make_governor()
        BASELINE["clarity"] = 0.0   # mutate original
        assert g.baseline_state["clarity"] == 0.8  # governor's copy unaffected
        BASELINE["clarity"] = 0.8   # restore

    def test_initial_drift_zero(self):
        g = make_governor()
        assert g.cumulative_drift == 0.0

    def test_invalid_D_max_raises(self):
        with pytest.raises(ValueError):
            DriftGovernor(BASELINE, D_max=0.0, D_total=2.0, T=1.5)

    def test_D_total_less_than_D_max_raises(self):
        with pytest.raises(ValueError):
            DriftGovernor(BASELINE, D_max=0.5, D_total=0.3, T=1.5)

    def test_invalid_T_raises(self):
        with pytest.raises(ValueError):
            DriftGovernor(BASELINE, D_max=0.3, D_total=2.0, T=0.0)


# ---------------------------------------------------------------------------
# D_max per-cycle cap
# ---------------------------------------------------------------------------

class TestDmaxEnforcement:
    def test_delta_at_D_max_is_accepted(self):
        g = make_governor(D_max=0.2)
        e = small_event(dim="clarity", delta=-0.2)
        result = g.apply_delta(e)
        assert result.success is True

    def test_delta_exceeding_D_max_is_rejected(self):
        g = make_governor(D_max=0.2)
        e = small_event(dim="clarity", delta=-0.21)
        result = g.apply_delta(e)
        assert result.success is False
        assert "D_max" in result.reason

    def test_rejected_delta_leaves_state_unchanged(self):
        g = make_governor(D_max=0.2)
        before = dict(g.current_state)
        e = small_event(dim="clarity", delta=-0.5)
        g.apply_delta(e)
        assert g.current_state == before

    def test_rejected_delta_leaves_drift_unchanged(self):
        g = make_governor(D_max=0.2)
        g.apply_delta(small_event(dim="clarity", delta=-0.1))  # accepted
        drift_before = g.cumulative_drift
        g.apply_delta(small_event(dim="clarity", delta=-0.5))  # rejected
        assert g.cumulative_drift == drift_before

    def test_multi_dimension_delta_measured_as_L1(self):
        # Two dimensions each with delta=0.12 → total L1=0.24 > D_max=0.2
        g = make_governor(D_max=0.2)
        e = DeltaRuleEvent(
            trigger_input="multi",
            delta_behavior={"clarity": -0.12, "groundedness": -0.12},
            rollback_condition=predicate_never(),
        )
        result = g.apply_delta(e)
        assert result.success is False
        assert result.delta_magnitude == pytest.approx(0.24)

    def test_result_contains_correct_magnitude(self):
        g = make_governor(D_max=0.3)
        e = small_event(dim="clarity", delta=-0.15)
        result = g.apply_delta(e)
        assert result.delta_magnitude == pytest.approx(0.15)

    def test_50_cycles_no_single_delta_exceeds_D_max(self):
        g = make_governor(D_max=0.2, D_total=50.0, T=100.0)
        for i in range(50):
            e = small_event(dim="clarity", delta=0.1)
            result = g.apply_delta(e)
            assert result.delta_magnitude <= g.D_max, (
                f"Cycle {i}: delta {result.delta_magnitude} exceeds D_max {g.D_max}"
            )


# ---------------------------------------------------------------------------
# D_total cumulative cap
# ---------------------------------------------------------------------------

class TestDtotalEnforcement:
    def test_cumulative_drift_tracked(self):
        g = make_governor(D_max=0.2, D_total=2.0)
        for _ in range(3):
            g.apply_delta(small_event(dim="clarity", delta=-0.1))
        assert g.cumulative_drift == pytest.approx(0.3)

    def test_delta_rejected_when_D_total_would_be_exceeded(self):
        g = make_governor(D_max=0.2, D_total=0.25)
        g.apply_delta(small_event(dim="clarity", delta=-0.2))  # accepted, drift=0.2
        result = g.apply_delta(small_event(dim="clarity", delta=-0.1))  # 0.2+0.1=0.3 > 0.25
        assert result.success is False
        assert "D_total" in result.reason

    def test_D_total_exact_boundary_accepted(self):
        g = make_governor(D_max=0.3, D_total=0.3)
        result = g.apply_delta(small_event(dim="clarity", delta=-0.3))
        assert result.success is True
        assert g.cumulative_drift == pytest.approx(0.3)

    def test_10_cycles_drift_never_exceeds_D_total(self):
        D_total = 1.0
        g = make_governor(D_max=0.15, D_total=D_total, T=100.0)
        for i in range(10):
            g.apply_delta(small_event(dim="groundedness", delta=-0.1))
            assert g.cumulative_drift <= D_total + 1e-9, (
                f"Cycle {i}: cumulative drift {g.cumulative_drift} exceeds D_total {D_total}"
            )

    def test_30_cycles_mixed_drift_never_exceeds_D_total(self):
        D_max = 0.2
        D_total = 3.0
        g = make_governor(D_max=D_max, D_total=D_total, T=100.0)
        dims = ["clarity", "groundedness", "emotional_activation", "analytical_depth"]
        for i in range(30):
            dim = dims[i % len(dims)]
            delta = 0.15 if i % 2 == 0 else -0.15
            g.apply_delta(DeltaRuleEvent(
                trigger_input=f"cycle-{i}",
                delta_behavior={dim: delta},
                rollback_condition=predicate_never(),
            ))
            assert g.cumulative_drift <= D_total + 1e-9

    def test_result_contains_updated_cumulative_drift(self):
        g = make_governor(D_max=0.3, D_total=2.0)
        r1 = g.apply_delta(small_event(dim="clarity", delta=-0.1))
        r2 = g.apply_delta(small_event(dim="clarity", delta=-0.15))
        assert r1.cumulative_drift == pytest.approx(0.1)
        assert r2.cumulative_drift == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Rollback mechanism
# ---------------------------------------------------------------------------

class TestRollbackMechanism:
    def test_perform_rollback_restores_state(self):
        g = make_governor()
        before = dict(g.current_state)
        e = DeltaRuleEvent(
            trigger_input="noise",
            delta_behavior={"clarity": -0.2},
            rollback_condition=predicate_always(),
        )
        result = g.apply_delta(e)
        assert result.success

        rb = g.perform_rollback(e.rule_id)
        assert rb.success
        assert g.current_state == before

    def test_rollback_decrements_cumulative_drift(self):
        g = make_governor(D_max=0.3, D_total=2.0)
        e = DeltaRuleEvent(
            trigger_input="noise",
            delta_behavior={"clarity": -0.2},
            rollback_condition=predicate_always(),
        )
        g.apply_delta(e)
        drift_before = g.cumulative_drift
        g.perform_rollback(e.rule_id)
        assert g.cumulative_drift < drift_before
        assert g.cumulative_drift == pytest.approx(0.0)

    def test_rollback_condition_not_met_returns_failure(self):
        g = make_governor()
        e = DeltaRuleEvent(
            trigger_input="noise",
            delta_behavior={"clarity": -0.2},
            rollback_condition=predicate_never(),
        )
        g.apply_delta(e)
        rb = g.perform_rollback(e.rule_id)
        assert rb.success is False
        assert "condition not met" in rb.reason

    def test_rollback_unknown_rule_id_fails_gracefully(self):
        g = make_governor()
        rb = g.perform_rollback("nonexistent-id")
        assert rb.success is False
        assert "snapshot" in rb.reason.lower()

    def test_rollback_removes_event_from_active_list(self):
        g = make_governor()
        e = DeltaRuleEvent(
            trigger_input="noise",
            delta_behavior={"clarity": -0.1},
            rollback_condition=predicate_always(),
        )
        g.apply_delta(e)
        assert len(g._active_events) == 1
        g.perform_rollback(e.rule_id)
        assert len(g._active_events) == 0

    def test_rollback_removes_snapshot(self):
        g = make_governor()
        e = DeltaRuleEvent(
            trigger_input="noise",
            delta_behavior={"clarity": -0.1},
            rollback_condition=predicate_always(),
        )
        g.apply_delta(e)
        assert e.rule_id in g._snapshots
        g.perform_rollback(e.rule_id)
        assert e.rule_id not in g._snapshots

    def test_check_rollbacks_sweeps_active_events(self):
        g = make_governor()
        before = dict(g.current_state)

        e1 = DeltaRuleEvent("s1", {"clarity": -0.1}, predicate_always())
        e2 = DeltaRuleEvent("s2", {"groundedness": -0.1}, predicate_always())
        g.apply_delta(e1)
        g.apply_delta(e2)

        results = g.check_rollbacks()
        assert len(results) == 2
        assert all(r.success for r in results)
        assert g.current_state == pytest.approx(before)
        assert g.cumulative_drift == pytest.approx(0.0)

    def test_check_rollbacks_skips_events_with_unmet_condition(self):
        g = make_governor()
        e1 = DeltaRuleEvent("s1", {"clarity": -0.1}, predicate_never())
        e2 = DeltaRuleEvent("s2", {"groundedness": -0.1}, predicate_always())
        g.apply_delta(e1)
        g.apply_delta(e2)

        results = g.check_rollbacks()
        # Only e2 should be rolled back
        assert len(results) == 1
        assert results[0].rule_id == e2.rule_id
        assert len(g._active_events) == 1  # e1 still active

    def test_rollback_result_contains_restored_state(self):
        g = make_governor()
        before = dict(g.current_state)
        e = DeltaRuleEvent("s1", {"clarity": -0.2}, predicate_always())
        g.apply_delta(e)
        rb = g.perform_rollback(e.rule_id)
        assert rb.restored_state == before


# ---------------------------------------------------------------------------
# predicate_reverted
# ---------------------------------------------------------------------------

class TestPredicateReverted:
    def test_fires_when_dimension_matches_snapshot(self):
        # If current state's dimension has been manually restored to snapshot value,
        # the condition should fire.
        current = {"clarity": 0.8, "groundedness": 0.7}
        snapshot = {"clarity": 0.8, "groundedness": 0.9}
        cond = predicate_reverted("clarity")
        assert cond(current, snapshot) is True

    def test_does_not_fire_when_still_drifted(self):
        current = {"clarity": 0.6, "groundedness": 0.7}
        snapshot = {"clarity": 0.8, "groundedness": 0.9}
        cond = predicate_reverted("clarity")
        assert cond(current, snapshot) is False

    def test_tolerance_respected(self):
        current = {"clarity": 0.8001}
        snapshot = {"clarity": 0.8}
        cond = predicate_reverted("clarity", tolerance=0.001)
        assert cond(current, snapshot) is True

    def test_apply_then_rollback_via_predicate_reverted(self):
        g = make_governor()
        original_clarity = g.current_state["clarity"]
        e = DeltaRuleEvent(
            trigger_input="noise",
            delta_behavior={"clarity": -0.2},
            rollback_condition=predicate_reverted("clarity"),
        )
        g.apply_delta(e)
        # Manually restore clarity to its pre-delta value to trigger the condition
        g.current_state["clarity"] = original_clarity
        rb = g.perform_rollback(e.rule_id)
        assert rb.success


# ---------------------------------------------------------------------------
# Biological Landing
# ---------------------------------------------------------------------------

class TestBiologicalLanding:
    def test_alert_triggered_when_T_exceeded(self):
        alerts = []
        g = make_governor(D_max=0.5, D_total=10.0, T=0.4, alert_callback=alerts.append)
        e = DeltaRuleEvent("s1", {"clarity": -0.45}, predicate_never())
        result = g.apply_delta(e)
        assert result.biological_landing is True
        assert len(alerts) == 1
        assert "BIOLOGICAL LANDING" in alerts[0]

    def test_alert_not_triggered_below_T(self):
        alerts = []
        g = make_governor(D_max=0.3, D_total=2.0, T=1.5, alert_callback=alerts.append)
        g.apply_delta(small_event(dim="clarity", delta=-0.1))
        assert len(alerts) == 0

    def test_alert_triggered_only_once(self):
        alerts = []
        g = make_governor(D_max=0.5, D_total=10.0, T=0.4, alert_callback=alerts.append)
        # First event pushes over T
        g.apply_delta(DeltaRuleEvent("s1", {"clarity": -0.45}, predicate_never()))
        # Second event also over T — should NOT re-trigger
        g.apply_delta(DeltaRuleEvent("s2", {"groundedness": -0.45}, predicate_never()))
        assert len(alerts) == 1

    def test_alert_reset_after_rollback_below_T(self):
        alerts = []
        g = make_governor(D_max=0.5, D_total=10.0, T=0.4, alert_callback=alerts.append)
        e = DeltaRuleEvent("s1", {"clarity": -0.45}, predicate_always())
        g.apply_delta(e)
        assert g._biological_landing_triggered is True
        g.perform_rollback(e.rule_id)
        assert g._biological_landing_triggered is False

    def test_drift_report_reflects_landing_state(self):
        g = make_governor(D_max=0.5, D_total=10.0, T=0.4)
        report = g.drift_report()
        assert report["biological_landing_triggered"] is False
        g.apply_delta(DeltaRuleEvent("s1", {"clarity": -0.45}, predicate_never()))
        report = g.drift_report()
        assert report["biological_landing_triggered"] is True


# ---------------------------------------------------------------------------
# Drift report
# ---------------------------------------------------------------------------

class TestDriftReport:
    def test_report_contains_required_keys(self):
        g = make_governor()
        report = g.drift_report()
        for key in ("current_state", "baseline_state", "cumulative_drift",
                    "drift_from_baseline", "D_max", "D_total", "T",
                    "biological_landing_triggered", "active_events", "active_rule_ids"):
            assert key in report, f"Missing key: {key}"

    def test_drift_from_baseline_after_rollback_is_zero(self):
        g = make_governor()
        e = DeltaRuleEvent("s1", {"clarity": -0.2}, predicate_always())
        g.apply_delta(e)
        g.perform_rollback(e.rule_id)
        assert g.drift_from_baseline() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Full simulation: 10-50 cycles — all invariants must hold
# ---------------------------------------------------------------------------

class TestFullSimulation:
    """Simulate 10 and 50 cycles with random-ish deltas; verify all invariants."""

    def _run_simulation(self, n_cycles: int, D_max: float, D_total: float, T: float):
        alerts = []
        g = DriftGovernor(
            baseline_state={"a": 1.0, "b": 1.0, "c": 1.0},
            D_max=D_max,
            D_total=D_total,
            T=T,
            alert_callback=alerts.append,
        )

        dims = ["a", "b", "c"]
        results = []
        for i in range(n_cycles):
            dim = dims[i % 3]
            # Alternate sign so state oscillates; stays within D_max
            delta = D_max * 0.8 * (1 if i % 2 == 0 else -1)
            e = DeltaRuleEvent(
                trigger_input=f"cycle-{i}",
                delta_behavior={dim: delta},
                rollback_condition=predicate_never(),
            )
            r = g.apply_delta(e)
            results.append(r)

            # Invariant 1: accepted delta magnitude never exceeds D_max
            if r.success:
                assert r.delta_magnitude <= D_max + 1e-9, (
                    f"Cycle {i}: accepted delta {r.delta_magnitude} > D_max {D_max}"
                )

            # Invariant 2: cumulative drift never exceeds D_total
            assert g.cumulative_drift <= D_total + 1e-9, (
                f"Cycle {i}: cumulative drift {g.cumulative_drift} > D_total {D_total}"
            )

        return g, results, alerts

    def test_10_cycles_all_invariants(self):
        g, results, alerts = self._run_simulation(
            n_cycles=10, D_max=0.2, D_total=2.0, T=1.5
        )
        accepted = [r for r in results if r.success]
        assert len(accepted) > 0

    def test_50_cycles_all_invariants(self):
        g, results, alerts = self._run_simulation(
            n_cycles=50, D_max=0.2, D_total=2.0, T=1.5
        )
        # After 50 cycles with oscillating sign, many are accepted
        accepted = [r for r in results if r.success]
        assert len(accepted) > 0

    def test_rollback_restores_to_exact_snapshot(self):
        """Apply N events then roll back each; state must match the original baseline."""
        g = DriftGovernor(
            baseline_state={"x": 5.0, "y": 3.0},
            D_max=0.5,
            D_total=20.0,
            T=100.0,
        )
        events = []
        for i in range(10):
            e = DeltaRuleEvent(
                trigger_input=f"e{i}",
                delta_behavior={"x": 0.1, "y": -0.1},
                rollback_condition=predicate_always(),
            )
            r = g.apply_delta(e)
            if r.success:
                events.append(e)

        # Roll back all in reverse order
        for e in reversed(events):
            rb = g.perform_rollback(e.rule_id)
            assert rb.success, f"Rollback failed for {e.rule_id}: {rb.reason}"

        # After all rollbacks, current state must equal original baseline
        assert g.current_state == pytest.approx({"x": 5.0, "y": 3.0})
        assert g.cumulative_drift == pytest.approx(0.0)

    def test_drift_never_goes_negative(self):
        """cumulative_drift must always be >= 0 even after rollbacks."""
        g = DriftGovernor({"p": 1.0}, D_max=0.5, D_total=10.0, T=100.0)
        for i in range(20):
            e = DeltaRuleEvent(f"e{i}", {"p": 0.1}, predicate_always())
            g.apply_delta(e)
            g.check_rollbacks()
            assert g.cumulative_drift >= 0.0
