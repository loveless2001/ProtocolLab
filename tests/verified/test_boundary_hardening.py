"""Boundary and shell hardening tests based on independent review probes."""

from typing import Any

import pytest
from pydantic import ValidationError

from protocollab.actor.budget import DiagnosticBudgetAllocation, StageBudgetLimits
from protocollab.verified.lifecycle_owner import VerifiedLifecycleOwner
from protocollab.verified.protocol import (
    MAX_SAFE_INT,
    Charge,
    LedgerState,
    LifecycleMode,
    StageLimit,
    VerdictKind,
)


class DummyStore:
    def __init__(self):
        self.records = []
        self.data = {}

    def get(self, table: str, key: str):
        return self.data.get((table, key))

    def set(self, table: str, key: str, payload: Any, kind: str):
        self.data[(table, key)] = payload
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

        from protocollab.verified.protocol import Charge, ChargeKind, RequestRecord, TransportState

        if event.get("kind") == "Reserve":
            s = state.model_copy(deep=True)
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
    with pytest.raises(RuntimeError) as exc_info:
        owner.record_completed("stage1", 60, 10, 160, req_id="r1", receipt_hash="QUARANTINE_RECEIPT_999")
    assert "CONFLICTING_USAGE_AFTER_RELEASE" in str(exc_info.value)

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
    with pytest.raises(RuntimeError) as exc2:
        owner.record_completed("stage1", 60, 10, 160, req_id="r1", receipt_hash="CRASH_RECEIPT")
    assert "CONFLICTING_USAGE_AFTER_RELEASE" in str(exc2.value)

    # Verify evidence is now cleanly stored
    quarantine_entry = store.get("verified_quarantine_records", "crash_gap_run:r1")
    assert quarantine_entry is not None
    assert quarantine_entry["receipt_hash"] == "CRASH_RECEIPT"


def test_find_bend_app_discovers_version_trees(tmp_path, monkeypatch):
    """Verify that find_bend_app discovers versioned app trees (e.g. 2.0.7, 2.0.5) in reverse order."""
    from protocollab.verified.bridge import find_bend_app

    monkeypatch.delenv("BEND_APP", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    app_207 = tmp_path / ".bend" / "app" / "2.0.7" / "pkgA" / "bend2" / "main.ts"
    app_207.parent.mkdir(parents=True, exist_ok=True)
    app_207.write_text("// 2.0.7 fixture")

    app_205 = tmp_path / ".bend" / "app" / "2.0.5" / "pkgB" / "bend2" / "main.ts"
    app_205.parent.mkdir(parents=True, exist_ok=True)
    app_205.write_text("// 2.0.5 fixture")

    found = find_bend_app()
    assert found == app_207, f"Expected newest 2.0.7 version, got {found}"


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


def test_find_bend_app_enforces_toolchain_lock(tmp_path, monkeypatch):
    """Verify that find_bend_app selects the pinned toolchain lock version (2.0.7)

    even when an unpinned higher version (e.g. 2.0.8) is present in the app tree.
    """
    from protocollab.verified.bridge import find_bend_app

    monkeypatch.delenv("BEND_APP", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    app_208 = tmp_path / ".bend" / "app" / "2.0.8" / "fixture" / "bend2" / "main.ts"
    app_208.parent.mkdir(parents=True, exist_ok=True)
    app_208.write_text("// 2.0.8 unpinned fixture")

    app_207 = tmp_path / ".bend" / "app" / "2.0.7" / "fixture" / "bend2" / "main.ts"
    app_207.parent.mkdir(parents=True, exist_ok=True)
    app_207.write_text("// 2.0.7 pinned fixture")

    found = find_bend_app()
    assert found == app_207, f"Expected locked 2.0.7 version, got {found}"


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



