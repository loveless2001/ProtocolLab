"""Boundary and shell hardening tests based on independent review probes."""

from typing import Any

import pytest
from pydantic import ValidationError

from protocollab.actor.budget import DiagnosticBudgetAllocation, StageBudgetLimits
from protocollab.storage import Store
from protocollab.verified.lifecycle_owner import VerifiedLifecycleOwner
from protocollab.verified.protocol import (
    MAX_SAFE_INT,
    Charge,
    LedgerState,
    LifecycleMode,
    StageLimit,
    VerdictKind,
)


class DummyStore(Store):
    """Record writes while exercising the production transaction boundary."""

    def __init__(self):
        super().__init__(":memory:")
        self.records = []

    def set(self, table: str, key: str, payload: Any, kind: str):
        result = super().set(table, key, payload, kind)
        self.records.append((table, key, payload, kind))
        return result


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

        from protocollab.verified.protocol import Charge, ChargeKind, RequestRecord, TransportState

        if event.get("kind") == "Reserve":
            s = state.model_copy(deep=True)
            existing = next((r for r in s.requests if r.req_id == event["req_id"]), None)
            if existing:
                return SimpleNamespace(
                    state=s,
                    verdict=SimpleNamespace(kind=VerdictKind.DUPLICATE_NOOP, intent=None, reason=None),
                )
            r = RequestRecord(
                req_id=event["req_id"],
                stage=event["stage"],
                basis_ref=event.get("basis_ref", ""),
                config_hash=event.get("config_hash", ""),
                max_input=event.get("max_input", 0),
                max_output=event.get("max_output", 0),
                transport=TransportState.PREPARED,
                charge=Charge(
                    kind=ChargeKind.PENDING,
                    tokens=event.get("max_input", 0) + event.get("max_output", 0),
                ),
            )
            s.requests.insert(0, r)
            return SimpleNamespace(
                state=s,
                verdict=SimpleNamespace(kind=VerdictKind.ACCEPTED, intent=None, reason=None),
            )

        if event.get("kind") == "DispatchIntent":
            s = state.model_copy(deep=True)
            existing = next((r for r in s.requests if r.req_id == event["req_id"]), None)
            if existing and existing.transport in (
                TransportState.DISPATCHED_INTENT,
                TransportState.SENT,
                TransportState.RESPONSE_RECEIVED,
            ):
                return SimpleNamespace(
                    state=s,
                    verdict=SimpleNamespace(kind=VerdictKind.DUPLICATE_NOOP, intent=None, reason=None),
                )
            if existing:
                existing.transport = TransportState.DISPATCHED_INTENT
            return SimpleNamespace(
                state=s,
                verdict=SimpleNamespace(kind=VerdictKind.ACCEPTED, intent=None, reason=None),
            )

        if event.get("kind") == "SettleUsage":
            s = state.model_copy(deep=True)
            existing = next((r for r in s.requests if r.req_id == event["req_id"]), None)
            if existing:
                existing.charge = Charge(
                    kind=ChargeKind.SETTLED,
                    input_tokens=event.get("input_tokens", 0),
                    output_tokens=event.get("output_tokens", 0),
                    receipt_hash=event.get("receipt_hash", "rcpt"),
                )
                existing.transport = TransportState.RESPONSE_RECEIVED
            return SimpleNamespace(
                state=s,
                verdict=SimpleNamespace(kind=VerdictKind.ACCEPTED, intent=None, reason=None),
            )

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


def test_lifecycle_owner_duplicate_completion_with_next_pending():
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

    # Complete r1 with receipt_hash
    owner.record_completed("stage1", 60, 10, 160, receipt_hash="receipt-r1")
    assert bridge.events[-1]["req_id"] == "r1"

    # Replay completion with same receipt_hash: must match r1, NOT mistakenly complete pending r2!
    owner.record_completed("stage1", 60, 10, 160, receipt_hash="receipt-r1")
    assert bridge.events[-1]["req_id"] == "r1"

    # Subsequent completion for r2 routes to r2
    owner.record_completed("stage1", 60, 10, 160, receipt_hash="receipt-r2")
    assert bridge.events[-1]["req_id"] == "r2"


def test_lifecycle_owner_state_reconstruction_on_restart():
    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=5, max_tokens=500)},
        aggregate_max_calls=10,
        aggregate_max_tokens=1000,
    )
    store = DummyStore()
    bridge1 = FaultyBridge()
    owner1 = VerifiedLifecycleOwner(
        store=store,
        run_id="restart_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge1,
    )
    owner1.reserve("stage1", 160, req_id="r_persisted")

    # Reconstruct owner from existing store snapshot
    bridge2 = FaultyBridge()
    owner2 = VerifiedLifecycleOwner(
        store=store,
        run_id="restart_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge2,
    )
    # Anonymous completion must route to reconstructed r_persisted, not generate a new ID
    owner2.record_completed("stage1", 60, 10, 160)
    assert bridge2.events[-1]["req_id"] == "r_persisted"


def test_lifecycle_owner_quarantine_evidence_retention():
    class ConflictBridge(FaultyBridge):
        def apply(self, state, event):
            from types import SimpleNamespace
            s = state.model_copy(deep=True)
            s.fault = "CONFLICTING_USAGE_AFTER_RELEASE"
            return SimpleNamespace(
                state=s,
                verdict=SimpleNamespace(kind=VerdictKind.CONFLICT_FAULT, intent=None, reason=s.fault),
            )

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=5, max_tokens=500)},
        aggregate_max_calls=10,
        aggregate_max_tokens=1000,
    )
    store = DummyStore()
    owner = VerifiedLifecycleOwner(
        store=store,
        run_id="quarantine_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=ConflictBridge(),
    )
    verdict = owner.record_completed(
        "stage1", 60, 10, 160, req_id="r1", receipt_hash="QUARANTINE_RECEIPT_999"
    )
    assert verdict == VerdictKind.CONFLICT_FAULT

    # Verify quarantine evidence is stored
    found_receipt = any("QUARANTINE_RECEIPT_999" in str(rec) for rec in store.records)
    assert found_receipt, "Quarantine receipt evidence must be persisted in store records!"


def test_lifecycle_owner_quarantine_crash_gap_closed():
    """Verify that failing to write quarantine evidence does NOT persist a latched fault,

    allowing subsequent retry to successfully persist evidence and latched fault together.
    """
    class CrashBeforeEvidenceStore(DummyStore):
        def __init__(self):
            super().__init__()
            self.fail_evidence = True

        def set(self, table: str, key: str, payload: Any, kind: str):
            if table == "verified_quarantine_records" and self.fail_evidence:
                raise RuntimeError("SIMULATED_CRASH_BEFORE_EVIDENCE_COMMIT")
            super().set(table, key, payload, kind)

    class ConflictBridge(FaultyBridge):
        def apply(self, state, event):
            from types import SimpleNamespace
            if state.fault:
                return SimpleNamespace(
                    state=state,
                    verdict=SimpleNamespace(kind=VerdictKind.REJECTED, intent=None, reason="STATE_FAULT_LATCHED"),
                )
            s = state.model_copy(deep=True)
            s.fault = "CONFLICTING_USAGE_AFTER_RELEASE"
            return SimpleNamespace(
                state=s,
                verdict=SimpleNamespace(kind=VerdictKind.CONFLICT_FAULT, intent=None, reason=s.fault),
            )

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=5, max_tokens=500)},
        aggregate_max_calls=10,
        aggregate_max_tokens=1000,
    )
    store = CrashBeforeEvidenceStore()
    bridge = ConflictBridge()
    owner = VerifiedLifecycleOwner(
        store=store,
        run_id="crash_gap_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge,
    )

    # First attempt: simulated crash when writing quarantine records
    with pytest.raises(RuntimeError) as exc1:
        owner.record_completed("stage1", 60, 10, 160, req_id="r1", receipt_hash="CRASH_RECEIPT")
    assert "SIMULATED_CRASH_BEFORE_EVIDENCE_COMMIT" in str(exc1.value)

    # Ledger state in store must NOT have been latched with fault during failed evidence write!
    stored_ledger = store.get("verified_lifecycle_ledger", "crash_gap_run")
    assert stored_ledger.get("fault") is None, "Fault must NOT be latched if quarantine evidence write fails!"

    # Second attempt (after store recovery): retry successfully persists evidence and latches fault
    store.fail_evidence = False
    verdict = owner.record_completed(
        "stage1", 60, 10, 160, req_id="r1", receipt_hash="CRASH_RECEIPT"
    )
    assert verdict == VerdictKind.CONFLICT_FAULT

    # Verify evidence is now cleanly stored
    quarantine_entry = store.get("verified_quarantine_records", "crash_gap_run:r1")
    assert quarantine_entry is not None
    assert quarantine_entry["receipt_hash"] == "CRASH_RECEIPT"


def test_find_bend_app_discovers_version_trees(tmp_path, monkeypatch):
    """Verify that find_bend_app discovers versioned app trees (e.g. 2.0.16, 2.0.7, 2.0.5) in reverse semver order."""
    from protocollab.verified.bridge import find_bend_app

    monkeypatch.delenv("BEND_APP", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    app_2016 = tmp_path / ".bend" / "app" / "2.0.16" / "pkgA" / "bend2" / "main.ts"
    app_2016.parent.mkdir(parents=True, exist_ok=True)
    app_2016.write_text("// 2.0.16 fixture")

    app_207 = tmp_path / ".bend" / "app" / "2.0.7" / "pkgB" / "bend2" / "main.ts"
    app_207.parent.mkdir(parents=True, exist_ok=True)
    app_207.write_text("// 2.0.7 fixture")

    app_205 = tmp_path / ".bend" / "app" / "2.0.5" / "pkgC" / "bend2" / "main.ts"
    app_205.parent.mkdir(parents=True, exist_ok=True)
    app_205.write_text("// 2.0.5 fixture")

    found = find_bend_app()
    assert found == app_2016, f"Expected newest 2.0.16 version, got {found}"


def test_decision_adapter_verified_lifecycle_integration():
    """Verify that DecisionAdapter passes authentic cryptographic req_id, basis_ref,

    caps, and receipts to VerifiedLifecycleOwner rather than fabricated defaults.
    """
    from protocollab.actor import ModelPortConfig
    from protocollab.actor.diagnostic_config import DiagnosticConfig
    from protocollab.actor.modes import DecisionAdapter

    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    diag_cfg = DiagnosticConfig(
        model_port_config=port_cfg,
        decision_mode="free_json",
        renderer="demarcated",
    )
    adapter = DecisionAdapter(diag_cfg)

    class DummyPort:
        def __init__(self):
            self.config = port_cfg
            self.phase = "suffix"
            self.store = None
            self.input_tokens = 0
            self.output_tokens = 0

        def generate(self, packet, seed, prompt_override=None):
            self.input_tokens += 60
            self.output_tokens += 30
            return '{"kind": "WAIT"}'

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=5, max_tokens=2000)},
        aggregate_max_calls=10,
        aggregate_max_tokens=4000,
    )
    bridge = FaultyBridge()
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="adapter_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge,
    )

    packet = {
        "schema_version": "0.1",
        "public_alphabet": ["TICK"],
        "task": {},
        "decision_basis_ref": "basis-claim-777",
    }
    outcome = adapter.decide(DummyPort(), packet, seed=42, ledger=owner, stage="stage1")
    assert outcome.proposal.kind == "WAIT"

    # Inspect events emitted to bridge
    reserve_ev = next(e for e in bridge.events if e["kind"] == "Reserve")
    disp_ev = next(e for e in bridge.events if e["kind"] == "DispatchIntent")
    settle_ev = next(e for e in bridge.events if e["kind"] == "SettleUsage")

    # Verify authentic properties:
    assert reserve_ev["req_id"].startswith("req_")
    assert reserve_ev["basis_ref"] == "basis-claim-777"
    assert reserve_ev["max_input"] == 1000
    assert reserve_ev["max_output"] == 500

    assert disp_ev["req_id"] == reserve_ev["req_id"]

    assert settle_ev["req_id"] == reserve_ev["req_id"]
    assert settle_ev["input_tokens"] == 60
    assert settle_ev["output_tokens"] == 30
    assert settle_ev["receipt_hash"] != "receipt_default"
    assert len(settle_ev["receipt_hash"]) == 64  # SHA-256 hex digest


def test_find_bend_app_prefers_direct_layout(tmp_path, monkeypatch):
    """Verify that find_bend_app prefers direct layout (~/.bend/bend2/main.ts) over legacy version trees."""
    from protocollab.verified.bridge import find_bend_app

    monkeypatch.delenv("BEND_APP", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    direct = tmp_path / ".bend" / "bend2" / "main.ts"
    direct.parent.mkdir(parents=True, exist_ok=True)
    direct.write_text("// direct layout fixture")

    app_old = tmp_path / ".bend" / "app" / "2.0.7" / "fixture" / "bend2" / "main.ts"
    app_old.parent.mkdir(parents=True, exist_ok=True)
    app_old.write_text("// legacy version fixture")

    found = find_bend_app()
    assert found == direct, f"Expected direct layout {direct}, got {found}"


def test_repeated_prompt_distinct_request_ids():
    """Verify that multiple decide calls on the same packet generate distinct request IDs

    so that repeated port calls are not falsely coalesced or suppressed as duplicate noops.
    """
    from protocollab.actor import ModelPortConfig
    from protocollab.actor.diagnostic_config import DiagnosticConfig
    from protocollab.actor.modes import DecisionAdapter

    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test-repeat",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    diag_cfg = DiagnosticConfig(
        model_port_config=port_cfg,
        decision_mode="free_json",
        renderer="demarcated",
    )
    adapter = DecisionAdapter(diag_cfg)

    class CountingPort:
        def __init__(self):
            self.config = port_cfg
            self.phase = "suffix"
            self.store = None
            self.calls = 0
            self.input_tokens = 0
            self.output_tokens = 0

        def generate(self, packet, seed, prompt_override=None):
            self.calls += 1
            self.input_tokens += 20
            self.output_tokens += 10
            return '{"kind": "WAIT"}' if self.calls % 2 else '{"kind": "FINISH"}'

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=10, max_tokens=2000)},
        aggregate_max_calls=10,
        aggregate_max_tokens=2000,
    )
    bridge = FaultyBridge()
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="repeat_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge,
    )

    packet = {
        "schema_version": "0.1",
        "public_alphabet": ["TICK"],
        "decision_basis_ref": "basis",
        "task": {"id": 1},
    }
    port = CountingPort()
    outcomes = [adapter.decide(port, packet, seed=7, ledger=owner, stage="stage1") for _ in range(5)]

    reserve_events = [e for e in bridge.events if e["kind"] == "Reserve"]
    settle_events = [e for e in bridge.events if e["kind"] == "SettleUsage"]

    req_ids = [e["req_id"] for e in reserve_events]
    assert len(set(req_ids)) == 5, f"Expected 5 distinct req_ids for 5 calls, got {set(req_ids)}"
    assert len(settle_events) == 5
    assert len(outcomes) == 5


def test_receipt_hash_binds_model_output():
    """Verify that different raw model responses with identical token counts produce different receipt hashes."""
    from protocollab.actor import ModelPortConfig
    from protocollab.actor.diagnostic_config import DiagnosticConfig
    from protocollab.actor.modes import DecisionAdapter

    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test-receipt",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    diag_cfg = DiagnosticConfig(
        model_port_config=port_cfg,
        decision_mode="free_json",
        renderer="demarcated",
    )
    adapter = DecisionAdapter(diag_cfg)

    class AlternatingPort:
        def __init__(self):
            self.config = port_cfg
            self.phase = "suffix"
            self.store = None
            self.calls = 0
            self.input_tokens = 0
            self.output_tokens = 0

        def generate(self, packet, seed, prompt_override=None):
            self.calls += 1
            self.input_tokens += 20
            self.output_tokens += 10
            return '{"kind": "WAIT"}' if self.calls == 1 else '{"kind": "FINISH"}'

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=10, max_tokens=2000)},
        aggregate_max_calls=10,
        aggregate_max_tokens=2000,
    )
    bridge = FaultyBridge()
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="receipt_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge,
    )

    packet = {
        "schema_version": "0.1",
        "public_alphabet": ["TICK"],
        "decision_basis_ref": "basis",
        "task": {},
    }
    port = AlternatingPort()
    adapter.decide(port, packet, seed=7, ledger=owner, stage="stage1")
    adapter.decide(port, packet, seed=7, ledger=owner, stage="stage1")

    settle_events = [e for e in bridge.events if e["kind"] == "SettleUsage"]
    assert len(settle_events) == 2
    assert settle_events[0]["receipt_hash"] != settle_events[1]["receipt_hash"], (
        "Receipt hashes must differ when raw model responses differ!"
    )


def test_timeout_triggers_record_timeout_preserves_reservation():
    """Verify that TimeoutError triggers record_timeout, preserving the pending reservation."""
    from protocollab.actor import ModelPortConfig
    from protocollab.actor.diagnostic_config import DiagnosticConfig
    from protocollab.actor.modes import DecisionAdapter

    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test-timeout",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    diag_cfg = DiagnosticConfig(
        model_port_config=port_cfg,
        decision_mode="free_json",
        renderer="demarcated",
    )
    adapter = DecisionAdapter(diag_cfg)

    class TimeoutPort:
        def __init__(self):
            self.config = port_cfg
            self.phase = "suffix"
            self.store = None
            self.input_tokens = 0
            self.output_tokens = 0

        def generate(self, packet, seed, prompt_override=None):
            raise TimeoutError("TIMED_OUT_AFTER_PROVIDER_RECEIVED_REQUEST")

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=10, max_tokens=2000)},
        aggregate_max_calls=10,
        aggregate_max_tokens=2000,
    )
    bridge = FaultyBridge()
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="timeout_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge,
    )

    packet = {
        "schema_version": "0.1",
        "public_alphabet": ["TICK"],
        "decision_basis_ref": "basis",
        "task": {},
    }
    outcome = adapter.decide(TimeoutPort(), packet, seed=7, ledger=owner, stage="stage1")
    assert outcome.validation_outcome in ("TIMEOUT", "INFERENCE_FAILED")
    assert outcome.is_fallback

    # Must emit TimeoutUnknown to bridge, NOT FailureConclusive!
    timeout_ev = next((e for e in bridge.events if e["kind"] == "TimeoutUnknown"), None)
    failure_ev = next((e for e in bridge.events if e["kind"] == "FailureConclusive"), None)
    assert timeout_ev is not None, "TimeoutError must emit TimeoutUnknown event!"
    assert failure_ev is None, "TimeoutError must NOT emit FailureConclusive!"
    assert "TIMED_OUT_AFTER_PROVIDER_RECEIVED_REQUEST" in timeout_ev["reason"]


def test_candidate_score_settles_tokens_before_candidate_validation():
    """Verify that when score_candidates consumes tokens but evaluations are invalid,

    the consumed tokens are settled on the ledger rather than released as unspent.
    """
    from protocollab.actor import ModelPortConfig
    from protocollab.actor.diagnostic_config import DiagnosticConfig
    from protocollab.actor.modes import DecisionAdapter

    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test-cand",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    diag_cfg = DiagnosticConfig(
        model_port_config=port_cfg,
        decision_mode="candidate_score",
        candidate_registry=['{"kind":"WAIT"}', '{"kind":"FINISH"}'],
    )
    adapter = DecisionAdapter(diag_cfg)

    class MalformedScorePort:
        def __init__(self):
            self.config = port_cfg
            self.phase = "suffix"
            self.store = None
            self.input_tokens = 0
            self.output_tokens = 0

        def score_candidates(self, prompt, candidates, seed=0, claims=None):
            # Consumes 120 input and 25 output tokens, but returns empty evaluations
            return {
                "model_id": "test-cand",
                "input_tokens": 120,
                "output_tokens": 25,
                "evaluations": [],  # Mismatch: registry has 2 candidates
            }

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=10, max_tokens=2000)},
        aggregate_max_calls=10,
        aggregate_max_tokens=2000,
    )
    bridge = FaultyBridge()
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="cand_score_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge,
    )

    packet = {
        "schema_version": "0.1",
        "public_alphabet": ["TICK"],
        "decision_basis_ref": "basis",
        "task": {},
    }
    outcome = adapter.decide(MalformedScorePort(), packet, seed=7, ledger=owner, stage="stage1")
    assert outcome.validation_outcome == "INFERENCE_FAILED"
    assert outcome.is_fallback

    # Verify that usage was SETTLED on the bridge, not released!
    settle_ev = next((e for e in bridge.events if e["kind"] == "SettleUsage"), None)
    failure_ev = next((e for e in bridge.events if e["kind"] == "FailureConclusive"), None)
    assert settle_ev is not None, "Inference tokens must be settled on ledger even if candidate vector is malformed!"
    assert settle_ev["input_tokens"] == 120
    assert settle_ev["output_tokens"] == 25
    assert failure_ev is None, "Executed model call must not be released as FailureConclusive!"


def test_explicit_same_id_redispatch_prevented():
    """Verify that explicitly reusing an attempt ID does not permit a second physical backend call."""
    from protocollab.actor import ModelPortConfig
    from protocollab.actor.diagnostic_config import DiagnosticConfig
    from protocollab.actor.modes import DecisionAdapter

    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test-redispatch",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    diag_cfg = DiagnosticConfig(
        model_port_config=port_cfg,
        decision_mode="free_json",
        renderer="demarcated",
    )
    adapter = DecisionAdapter(diag_cfg)

    class CountingPort:
        def __init__(self):
            self.config = port_cfg
            self.phase = "suffix"
            self.store = None
            self.calls = 0
            self.input_tokens = 0
            self.output_tokens = 0

        def generate(self, packet, seed, prompt_override=None):
            self.calls += 1
            self.input_tokens += 100
            self.output_tokens += 10
            return '{"kind": "WAIT"}'

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=10, max_tokens=2000)},
        aggregate_max_calls=10,
        aggregate_max_tokens=2000,
    )
    bridge = FaultyBridge()
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="redispatch_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge,
    )

    packet = {
        "schema_version": "0.1",
        "public_alphabet": ["TICK"],
        "decision_basis_ref": "basis",
        "task": {},
    }
    port = CountingPort()

    # Call 1: Normal dispatch
    out1 = adapter.decide(port, packet, seed=7, ledger=owner, stage="stage1", req_id="attempt-reused")
    assert port.calls == 1
    assert out1.validation_outcome != "DUPLICATE_REQUEST"

    # Call 2: Reusing the same req_id
    out2 = adapter.decide(port, packet, seed=7, ledger=owner, stage="stage1", req_id="attempt-reused")
    assert port.calls == 1, "Physical model must NOT be called a second time when attempt ID is reused!"
    assert out2.validation_outcome == "DUPLICATE_REQUEST"
    assert out2.is_fallback
    assert out2.total_tokens_evaluated == 0


def test_transport_failures_route_to_timeout_unknown():
    """Verify that subprocess.TimeoutExpired, urllib.error.URLError, and ConnectionResetError

    emit TimeoutUnknown and preserve the reservation hold.
    """
    import subprocess
    import urllib.error

    from protocollab.actor import ModelPortConfig
    from protocollab.actor.diagnostic_config import DiagnosticConfig
    from protocollab.actor.modes import DecisionAdapter

    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test-transport-fail",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    diag_cfg = DiagnosticConfig(
        model_port_config=port_cfg,
        decision_mode="free_json",
        renderer="demarcated",
    )
    adapter = DecisionAdapter(diag_cfg)

    class ThrowingPort:
        def __init__(self, exc):
            self.config = port_cfg
            self.phase = "suffix"
            self.store = None
            self.input_tokens = 0
            self.output_tokens = 0
            self.exc = exc

        def generate(self, packet, seed, prompt_override=None):
            raise self.exc

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=10, max_tokens=2000)},
        aggregate_max_calls=10,
        aggregate_max_tokens=2000,
    )

    exceptions = [
        subprocess.TimeoutExpired(["worker"], 30),
        urllib.error.URLError(TimeoutError("connection timed out")),
        ConnectionResetError("peer reset connection"),
    ]

    for exc in exceptions:
        bridge = FaultyBridge()
        owner = VerifiedLifecycleOwner(
            store=DummyStore(),
            run_id=f"run_{type(exc).__name__}",
            allocation=alloc,
            config_hash="cfg",
            mode=LifecycleMode.AUTHORITATIVE,
            bridge=bridge,
        )
        packet = {
            "schema_version": "0.1",
            "public_alphabet": ["TICK"],
            "decision_basis_ref": "basis",
            "task": {},
        }
        outcome = adapter.decide(ThrowingPort(exc), packet, seed=7, ledger=owner, stage="stage1")
        assert outcome.is_fallback

        timeout_ev = next((e for e in bridge.events if e["kind"] == "TimeoutUnknown"), None)
        failure_ev = next((e for e in bridge.events if e["kind"] == "FailureConclusive"), None)
        assert timeout_ev is not None, f"{type(exc).__name__} must emit TimeoutUnknown!"
        assert failure_ev is None, f"{type(exc).__name__} must NOT emit FailureConclusive!"


def test_shadow_timeout_no_divergence():
    """Verify that in SHADOW mode, timeout preserves reservation on both active legacy ledger and shadow kernel."""
    from protocollab.actor import ModelPortConfig
    from protocollab.actor.diagnostic_config import DiagnosticConfig
    from protocollab.actor.modes import DecisionAdapter

    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test-shadow-timeout",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    diag_cfg = DiagnosticConfig(
        model_port_config=port_cfg,
        decision_mode="free_json",
        renderer="demarcated",
    )
    adapter = DecisionAdapter(diag_cfg)

    class TimeoutPort:
        def __init__(self):
            self.config = port_cfg
            self.phase = "suffix"
            self.store = None
            self.input_tokens = 0
            self.output_tokens = 0

        def generate(self, packet, seed, prompt_override=None):
            raise TimeoutError("timeout after possible send")

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=10, max_tokens=5000)},
        aggregate_max_calls=10,
        aggregate_max_tokens=5000,
    )
    bridge = FaultyBridge()
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="shadow_timeout_run",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.SHADOW,
        bridge=bridge,
    )
    packet = {
        "schema_version": "0.1",
        "public_alphabet": ["TICK"],
        "decision_basis_ref": "basis",
        "task": {},
    }
    adapter.decide(TimeoutPort(), packet, seed=7, ledger=owner, stage="stage1")

    # In SHADOW mode, active legacy ledger and projected shadow kernel state must match!
    active_usage = owner.state.aggregate
    shadow_usage = owner.project_diagnostic_state().aggregate

    assert active_usage.reserved_tokens == 1500, f"Legacy ledger must hold 1500 reserved tokens, got {active_usage.reserved_tokens}"
    assert shadow_usage.reserved_tokens == 1500, f"Shadow kernel must hold 1500 reserved tokens, got {shadow_usage.reserved_tokens}"
    assert active_usage.failures == 0, f"Legacy ledger must not mark timeout as conclusive failure, got {active_usage.failures}"
    assert shadow_usage.failures == 0, f"Shadow kernel must not mark timeout as conclusive failure, got {shadow_usage.failures}"


def test_find_bend_app_discovery_flexibility_and_vendored_fallback():
    """Verify that find_bend_app() dynamically resolves latest versions and falls back to vendored toolchain."""
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from protocollab.verified.bridge import find_bend_app

    # Scenario 1: multiple versions coexist -> picks latest semver version (e.g. 2.0.16 over 2.0.7)
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        for p in ["app/2.0.7/pkg/bend2/main.ts", "app/2.0.16/pkg/bend2/main.ts"]:
            f = home / ".bend" / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("// placeholder")
        with patch.dict("os.environ", {}, clear=True), patch.object(Path, "home", return_value=home):
            res = find_bend_app()
            assert res is not None
            assert "2.0.16" in str(res), f"Expected latest version 2.0.16, got {res}"

    # Scenario 2: empty home directory -> falls back to repository vendored toolchain
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        with patch.dict("os.environ", {}, clear=True), patch.object(Path, "home", return_value=home):
            res = find_bend_app()
            assert res is not None
            assert "verified/lifecycle/toolchain/bend2/main.ts" in str(res), f"Expected vendored fallback, got {res}"

    # Scenario 3: BEND_APP env var override takes highest precedence
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        custom_runner = home / "custom" / "main.ts"
        custom_runner.parent.mkdir(parents=True, exist_ok=True)
        custom_runner.write_text("// custom runner")
        with patch.dict("os.environ", {"BEND_APP": str(custom_runner)}), patch.object(Path, "home", return_value=home):
            res = find_bend_app()
            assert res == custom_runner, f"Expected BEND_APP override, got {res}"


def test_transport_timeout_or_drop_cyclic_exception_chain():
    """Verify is_transport_timeout_or_drop terminates cleanly on cyclic exception chains."""
    from protocollab.actor.modes import is_transport_timeout_or_drop

    # Cycle without timeout
    x = ValueError("outer")
    y = RuntimeError("inner")
    x.__cause__ = y
    y.__context__ = x
    assert is_transport_timeout_or_drop(x) is False

    # Cycle containing timeout
    t = TimeoutError("gateway timed out")
    z = ValueError("wrapped")
    z.__cause__ = t
    t.__context__ = z
    assert is_transport_timeout_or_drop(z) is True


def test_decision_outcome_fallback_preserves_measured_tokens():
    """Verify that fallback DecisionOutcome preserves measured per-request token usage."""
    from protocollab.actor import ModelPortConfig
    from protocollab.actor.diagnostic_config import DiagnosticConfig
    from protocollab.actor.modes import DecisionAdapter

    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    diag_cfg = DiagnosticConfig(
        model_port_config=port_cfg,
        decision_mode="free_json",
        renderer="demarcated",
    )
    adapter = DecisionAdapter(diag_cfg)

    class FailingInferencePort:
        def __init__(self):
            self.config = port_cfg
            self.phase = "suffix"
            self.store = None
            self.input_tokens = 0
            self.output_tokens = 0

        def generate(self, packet, seed, prompt_override=None):
            self.input_tokens += 120
            self.output_tokens += 35
            raise ValueError("SIMULATED_BACKEND_TRANSPORT_ERROR")

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=5, max_tokens=2000)},
        aggregate_max_calls=10,
        aggregate_max_tokens=4000,
    )
    bridge = FaultyBridge()
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="run_fallback_tokens",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge,
    )
    packet = {"schema_version": "0.1", "public_alphabet": ["TICK"], "task": {}}
    outcome = adapter.decide(FailingInferencePort(), packet, seed=42, ledger=owner, stage="stage1")

    assert outcome.is_fallback is True
    assert outcome.validation_outcome == "INFERENCE_FAILED"
    assert outcome.request_input_tokens == 120
    assert outcome.request_output_tokens == 35
    assert outcome.total_tokens_evaluated == 155


def test_prepared_reservation_duplicate_does_not_advance_dispatch():
    """Verify that deciding on an already-prepared reservation rejects as duplicate without advancing transport to DispatchedIntent."""
    from protocollab.actor import ModelPortConfig
    from protocollab.actor.diagnostic_config import DiagnosticConfig
    from protocollab.actor.modes import DecisionAdapter
    from protocollab.verified.protocol import TransportState

    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    diag_cfg = DiagnosticConfig(
        model_port_config=port_cfg,
        decision_mode="free_json",
        renderer="demarcated",
    )
    adapter = DecisionAdapter(diag_cfg)

    class DummyPort:
        def __init__(self):
            self.config = port_cfg
            self.phase = "suffix"
            self.store = None
            self.input_tokens = 0
            self.output_tokens = 0
            self.calls = 0

        def generate(self, packet, seed, prompt_override=None):
            self.calls += 1
            return '{"kind": "WAIT"}'

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=5, max_tokens=2000)},
        aggregate_max_calls=10,
        aggregate_max_tokens=4000,
    )
    bridge = FaultyBridge()
    owner = VerifiedLifecycleOwner(
        store=DummyStore(),
        run_id="run_prepared_dup",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.AUTHORITATIVE,
        bridge=bridge,
    )
    # Reserve initial request in Prepared state
    owner.reserve("stage1", 1500, req_id="req_prep_1", basis_ref="basis", max_input=1000, max_output=500)
    assert owner.bend_state.requests[0].transport == TransportState.PREPARED

    # Decide with the same request ID
    port = DummyPort()
    packet = {"schema_version": "0.1", "public_alphabet": ["TICK"], "task": {}}
    outcome = adapter.decide(port, packet, seed=42, ledger=owner, stage="stage1", req_id="req_prep_1")

    assert port.calls == 0
    assert outcome.validation_outcome == "DUPLICATE_REQUEST"
    # Verify request transport remained PREPARED and was NOT advanced to DISPATCHED_INTENT
    assert owner.bend_state.requests[0].transport == TransportState.PREPARED


def test_shadow_mode_duplicate_suppression_resilient_to_bridge_failure():
    """Verify that in SHADOW mode, legacy duplicate suppression holds even if the shadow bridge fails."""
    from protocollab.actor import ModelPortConfig
    from protocollab.actor.diagnostic_config import DiagnosticConfig
    from protocollab.actor.modes import DecisionAdapter

    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    diag_cfg = DiagnosticConfig(
        model_port_config=port_cfg,
        decision_mode="free_json",
        renderer="demarcated",
    )
    adapter = DecisionAdapter(diag_cfg)

    class CountingPort:
        def __init__(self):
            self.config = port_cfg
            self.phase = "suffix"
            self.store = None
            self.input_tokens = 0
            self.output_tokens = 0
            self.calls = 0

        def generate(self, packet, seed, prompt_override=None):
            self.calls += 1
            self.input_tokens += 10
            self.output_tokens += 5
            return '{"kind": "WAIT"}'

    class FailingBridge(FaultyBridge):
        def __init__(self):
            super().__init__()
            self.should_fail = False

        def apply(self, state, ev):
            if self.should_fail:
                raise RuntimeError("SHADOW_BRIDGE_DOWN")
            return super().apply(state, ev)

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=5, max_tokens=2000)},
        aggregate_max_calls=10,
        aggregate_max_tokens=4000,
    )
    bridge = FailingBridge()
    store = DummyStore()
    owner = VerifiedLifecycleOwner(
        store=store,
        run_id="run_shadow_resilient",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.SHADOW,
        bridge=bridge,
    )
    port = CountingPort()
    packet = {"schema_version": "0.1", "public_alphabet": ["TICK"], "task": {}}

    # First attempt succeeds
    out1 = adapter.decide(port, packet, seed=1, ledger=owner, stage="stage1", req_id="req_shadow_dup")
    assert port.calls == 1
    assert out1.validation_outcome == "VALID"

    # Simulate bridge failure on duplicate attempt
    bridge.should_fail = True
    out2 = adapter.decide(port, packet, seed=1, ledger=owner, stage="stage1", req_id="req_shadow_dup")
    assert port.calls == 1
    assert out2.validation_outcome == "DUPLICATE_REQUEST"


def test_shadow_mode_reopened_owner_prevents_duplicate_reservation_leak():
    """Verify that reopening a SHADOW owner on an existing run seeds seen_req_ids, preventing reservation leaks."""
    from protocollab.actor import ModelPortConfig
    from protocollab.actor.diagnostic_config import DiagnosticConfig
    from protocollab.actor.modes import DecisionAdapter

    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        max_output_tokens=500,
    )
    diag_cfg = DiagnosticConfig(
        model_port_config=port_cfg,
        decision_mode="free_json",
        renderer="demarcated",
    )
    adapter = DecisionAdapter(diag_cfg)

    class SingleCallPort:
        def __init__(self):
            self.config = port_cfg
            self.phase = "suffix"
            self.store = None
            self.input_tokens = 0
            self.output_tokens = 0
            self.calls = 0

        def generate(self, packet, seed, prompt_override=None):
            self.calls += 1
            self.input_tokens += 50
            self.output_tokens += 10
            return '{"kind": "WAIT"}'

    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=5, max_tokens=2000)},
        aggregate_max_calls=10,
        aggregate_max_tokens=4000,
    )
    store = DummyStore()
    bridge = FaultyBridge()
    owner1 = VerifiedLifecycleOwner(
        store=store,
        run_id="run_restart_leak_check",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.SHADOW,
        bridge=bridge,
    )
    port = SingleCallPort()
    packet = {"schema_version": "0.1", "public_alphabet": ["TICK"], "task": {}}

    # Initial decide completes
    out1 = adapter.decide(port, packet, seed=1, ledger=owner1, stage="stage1", req_id="req_restart_1")
    assert port.calls == 1
    assert out1.validation_outcome == "VALID"
    assert owner1.state.aggregate.reserved_tokens == 0

    # Reopen owner on the same run_id
    owner2 = VerifiedLifecycleOwner(
        store=store,
        run_id="run_restart_leak_check",
        allocation=alloc,
        config_hash="cfg",
        mode=LifecycleMode.SHADOW,
        bridge=bridge,
    )
    out2 = adapter.decide(port, packet, seed=1, ledger=owner2, stage="stage1", req_id="req_restart_1")
    assert port.calls == 1
    assert out2.validation_outcome == "DUPLICATE_REQUEST"
    # Ensure legacy ledger reserved_tokens is NOT leaked
    assert owner2.state.aggregate.reserved_tokens == 0
    assert owner2.project_diagnostic_state().aggregate.reserved_tokens == 0
