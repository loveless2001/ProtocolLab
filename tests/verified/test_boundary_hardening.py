"""Boundary and shell hardening tests based on independent review probes."""

from typing import Any
import pytest
from pydantic import ValidationError

from protocollab.actor.budget import DiagnosticBudgetAllocation, StageBudgetLimits
from protocollab.verified.lifecycle_owner import VerifiedLifecycleOwner
from protocollab.verified.protocol import (
    Charge,
    ChargeKind,
    LedgerState,
    LifecycleMode,
    MAX_SAFE_INT,
    StageLimit,
    VerdictKind,
)


class DummyStore:
    def __init__(self):
        self.records = []

    def get(self, table: str, key: str):
        return None

    def set(self, table: str, key: str, payload: Any, kind: str):
        self.records.append((table, key, payload, kind))


class FaultyBridge:
    def __init__(self, should_fail: bool = False):
        self.events = []
        self.should_fail = should_fail

    def init(self, run_id, config_hash, agg_max_calls, agg_max_tokens, stage_limits):
        return LedgerState(
            run_id=run_id,
            config_hash=config_hash,
            agg_max_calls=agg_max_calls,
            agg_max_tokens=agg_max_tokens,
            stage_limits=stage_limits,
        )

    def apply(self, state, event):
        self.events.append(event)
        if self.should_fail:
            raise RuntimeError("SIMULATED_BRIDGE_UNAVAILABLE")
        from types import SimpleNamespace
        return SimpleNamespace(
            state=state,
            verdict=SimpleNamespace(kind=VerdictKind.ACCEPTED, intent=None, reason=None),
        )


def test_charge_validation_negative_tokens():
    with pytest.raises(ValidationError):
        Charge.model_validate({"kind": "Pending", "tokens": -1})


def test_charge_validation_missing_pending_tokens():
    with pytest.raises(ValidationError):
        Charge.model_validate({"kind": "Pending"})


def test_charge_validation_missing_settled_fields():
    with pytest.raises(ValidationError):
        Charge.model_validate({"kind": "Settled"})


def test_charge_validation_valid_variants():
    c_pending = Charge.model_validate({"kind": "Pending", "tokens": 100})
    assert c_pending.tokens == 100

    c_settled = Charge.model_validate(
        {"kind": "Settled", "input_tokens": 50, "output_tokens": 25, "receipt_hash": "h1"}
    )
    assert c_settled.input_tokens == 50

    c_released = Charge.model_validate({"kind": "Released", "evidence_hash": "ev1"})
    assert c_released.evidence_hash == "ev1"


def test_stage_limit_max_safe_integer():
    # Exactly MAX_SAFE_INT passes
    sl_ok = StageLimit(stage="s1", max_calls=1, max_tokens=MAX_SAFE_INT)
    assert sl_ok.max_tokens == MAX_SAFE_INT

    # Beyond MAX_SAFE_INT is rejected to prevent JavaScript float precision loss
    with pytest.raises(ValidationError):
        StageLimit(stage="s1", max_calls=1, max_tokens=MAX_SAFE_INT + 2)


def test_lifecycle_owner_interleaved_stage_routing():
    alloc = DiagnosticBudgetAllocation(
        stages={
            "stage1": StageBudgetLimits(max_calls=5, max_tokens=500),
            "stage2": StageBudgetLimits(max_calls=5, max_tokens=500),
        },
        aggregate_max_calls=10,
        aggregate_max_tokens=1000,
    )
    bridge = FaultyBridge()
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="test_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge,
    )

    # Reserve stage1, then reserve stage2
    owner.reserve("stage1", 160, req_id="r1")
    owner.reserve("stage2", 160, req_id="r2")

    # Complete stage1 without passing req_id explicitly: must route to r1!
    owner.record_completed("stage1", 60, 10, 160)
    assert bridge.events[-1]["req_id"] == "r1"
    assert bridge.events[-1]["kind"] == "SettleUsage"


def test_lifecycle_owner_shadow_bridge_failure_does_not_crash_caller():
    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=5, max_tokens=500)},
        aggregate_max_calls=10,
        aggregate_max_tokens=1000,
    )
    bridge = FaultyBridge(should_fail=True)
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="test_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.SHADOW,
        bridge=bridge,
    )

    # In shadow mode, even though bridge throws RuntimeError, caller does not crash
    owner.reserve("stage1", 160, req_id="r1")
    owner.record_dispatched("stage1", req_id="r1")
    owner.record_completed("stage1", 60, 10, 160, req_id="r1")
    owner.record_timeout("stage1", 160, req_id="r1")
    owner.record_failed("stage1", 160, req_id="r1")


def test_lifecycle_owner_duplicate_completion_routes_to_same_stage():
    alloc = DiagnosticBudgetAllocation(
        stages={
            "stage1": StageBudgetLimits(max_calls=5, max_tokens=500),
            "stage2": StageBudgetLimits(max_calls=5, max_tokens=500),
        },
        aggregate_max_calls=10,
        aggregate_max_tokens=1000,
    )
    bridge = FaultyBridge()
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="test_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge,
    )

    owner.reserve("stage1", 160, req_id="r1")
    owner.reserve("stage2", 160, req_id="r2")
    owner.record_completed("stage1", 60, 10, 160)
    assert bridge.events[-1]["req_id"] == "r1"

    # Replay completion on stage1 must route to r1, NOT cross-stage leak to r2!
    owner.record_completed("stage1", 60, 10, 160)
    assert bridge.events[-1]["req_id"] == "r1"


def test_lifecycle_owner_same_stage_fifo_routing():
    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=5, max_tokens=500)},
        aggregate_max_calls=10,
        aggregate_max_tokens=1000,
    )
    bridge = FaultyBridge()
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="test_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge,
    )

    owner.reserve("stage1", 160, req_id="r1")
    owner.reserve("stage1", 160, req_id="r2")

    # First completion routes to r1 (FIFO)
    owner.record_completed("stage1", 60, 10, 160)
    assert bridge.events[-1]["req_id"] == "r1"

    # Second completion routes to r2
    owner.record_completed("stage1", 60, 10, 160)
    assert bridge.events[-1]["req_id"] == "r2"


def test_lifecycle_owner_shadow_init_failure_contained():
    class InitFailingBridge:
        def init(self, **kwargs):
            raise RuntimeError("SIMULATED_KERNEL_STARTUP_FAILURE")

        def apply(self, state, event):
            pass

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=5, max_tokens=500)},
        aggregate_max_calls=10,
        aggregate_max_tokens=1000,
    )
    # Does not raise in SHADOW mode
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="test_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.SHADOW,
        bridge=InitFailingBridge(),
    )
    assert owner.bend_state is not None


def test_mutation_classifier_distinguishes_genuine_from_accidental_errors():
    def classify(rc, stdout, stderr):
        output = stdout + stderr
        if rc in (139, 134, -11, -6) or rc != 1:
            return False
        if "All terms check." in stdout:
            return False
        out_lower = output.lower()
        return "expected" in out_lower and "observed" in out_lower

    # Genuine proof mismatch: caught
    assert classify(1, "", "Error: expected True observed False") is True
    # Accidental syntax/unannotated literal: rejected (not caught as mutation)
    assert classify(1, "", "Error: cannot infer an unannotated literal") is False
    # Missing import: rejected
    assert classify(1, "", "Error: imported module not found") is False
    # Toolchain crash: rejected
    assert classify(139, "", "Segmentation fault") is False
    # Success without magic string: rejected
    assert classify(0, "checked successfully", "") is False
