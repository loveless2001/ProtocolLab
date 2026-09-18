"""Reference trace test matrix verifying all 13 lifecycle and budget accounting cases."""

from typing import Any
import pytest
from protocollab.actor.budget import DiagnosticBudgetAllocation, StageBudgetLimits
from protocollab.learning import BudgetExhausted
from protocollab.verified.bridge import VerifiedKernelBridge
from protocollab.verified.lifecycle_owner import VerifiedLifecycleOwner
from protocollab.verified.protocol import (
    ChargeKind,
    LedgerState,
    LifecycleMode,
    StageLimit,
    TransportState,
    VerdictKind,
)


class InMemoryStore:
    def __init__(self):
        self.data: dict[tuple[str, str], Any] = {}
        self.journal: list[dict[str, Any]] = []

    def get(self, table: str, key: str):
        return self.data.get((table, key))

    def set(self, table: str, key: str, val: Any, event_kind: str):
        self.data[(table, key)] = val
        self.journal.append({"table": table, "key": key, "val": val, "kind": event_kind})


@pytest.fixture
def bridge():
    b = VerifiedKernelBridge.get_default()
    yield b


@pytest.fixture
def standard_state(bridge):
    limits = [
        StageLimit(stage="stage1", max_calls=5, max_tokens=500),
        StageLimit(stage="stage2", max_calls=5, max_tokens=500),
    ]
    return bridge.init(
        run_id="test_run",
        config_hash="cfg_hash_1",
        agg_max_calls=10,
        agg_max_tokens=1000,
        stage_limits=limits,
    )


# 1. Clean lifecycle: Reserve -> Dispatch -> Transport -> Settle
def test_trace_1_clean_lifecycle(bridge, standard_state):
    # Reserve
    ev_res = {
        "kind": "Reserve",
        "req_id": "r1",
        "stage": "stage1",
        "basis_ref": "b1",
        "config_hash": "cfg_hash_1",
        "max_input": 100,
        "max_output": 50,
    }
    res1 = bridge.apply(standard_state, ev_res)
    assert res1.verdict.kind == VerdictKind.ACCEPTED
    assert res1.verdict.intent is not None
    assert res1.verdict.intent.req_id == "r1"
    assert res1.state.requests[0].charge.kind == ChargeKind.PENDING
    assert res1.state.requests[0].charge.tokens == 150

    # Dispatch
    ev_disp = {"kind": "DispatchIntent", "req_id": "r1"}
    res2 = bridge.apply(res1.state, ev_disp)
    assert res2.verdict.kind == VerdictKind.ACCEPTED
    assert res2.state.requests[0].transport == TransportState.DISPATCHED_INTENT

    # Transport
    ev_trans = {"kind": "TransportObserved", "req_id": "r1", "boundary": "http://api"}
    res3 = bridge.apply(res2.state, ev_trans)
    assert res3.verdict.kind == VerdictKind.ACCEPTED
    assert res3.state.requests[0].transport == TransportState.SENT

    # Settle
    ev_settle = {
        "kind": "SettleUsage",
        "req_id": "r1",
        "input_tokens": 40,
        "output_tokens": 30,
        "receipt_hash": "rec1",
    }
    res4 = bridge.apply(res3.state, ev_settle)
    assert res4.verdict.kind == VerdictKind.ACCEPTED
    assert res4.state.requests[0].transport == TransportState.RESPONSE_RECEIVED
    assert res4.state.requests[0].charge.kind == ChargeKind.SETTLED
    assert res4.state.requests[0].charge.input_tokens == 40
    assert res4.state.requests[0].charge.output_tokens == 30

    sum_res = bridge.summarize(res4.state)
    assert sum_res.total_spent == 70
    assert sum_res.total_held == 0
    assert sum_res.total_committed == 70


# 2. Call limit enforcement
def test_trace_2_call_limit_enforcement(bridge):
    limits = [StageLimit(stage="stage1", max_calls=1, max_tokens=500)]
    s = bridge.init("run_calls", "cfg", 10, 1000, limits)

    # First call admitted
    ev1 = {
        "kind": "Reserve",
        "req_id": "r1",
        "stage": "stage1",
        "basis_ref": "b1",
        "config_hash": "cfg",
        "max_input": 50,
        "max_output": 25,
    }
    res1 = bridge.apply(s, ev1)
    assert res1.verdict.kind == VerdictKind.ACCEPTED

    # Second call rejected
    ev2 = {
        "kind": "Reserve",
        "req_id": "r2",
        "stage": "stage1",
        "basis_ref": "b1",
        "config_hash": "cfg",
        "max_input": 50,
        "max_output": 25,
    }
    res2 = bridge.apply(res1.state, ev2)
    assert res2.verdict.kind == VerdictKind.REJECTED
    assert res2.verdict.reason == "CALL_LIMIT_EXCEEDED"
    assert len(res2.state.requests) == 1


# 3. Token limit enforcement
def test_trace_3_token_limit_enforcement(bridge):
    limits = [StageLimit(stage="stage1", max_calls=10, max_tokens=100)]
    s = bridge.init("run_tokens", "cfg", 10, 1000, limits)

    # Attempt to reserve 150 tokens when stage limit is 100
    ev = {
        "kind": "Reserve",
        "req_id": "r1",
        "stage": "stage1",
        "basis_ref": "b1",
        "config_hash": "cfg",
        "max_input": 100,
        "max_output": 50,
    }
    res = bridge.apply(s, ev)
    assert res.verdict.kind == VerdictKind.REJECTED
    assert res.verdict.reason == "TOKEN_LIMIT_EXCEEDED"


# 4. Stage limit isolation
def test_trace_4_stage_limit_isolation(bridge):
    limits = [
        StageLimit(stage="stage1", max_calls=1, max_tokens=100),
        StageLimit(stage="stage2", max_calls=5, max_tokens=500),
    ]
    s = bridge.init("run_iso", "cfg", 10, 1000, limits)

    # Exhaust stage1
    ev1 = {"kind": "Reserve", "req_id": "r1", "stage": "stage1", "basis_ref": "b", "config_hash": "cfg", "max_input": 50, "max_output": 50}
    res1 = bridge.apply(s, ev1)
    assert res1.verdict.kind == VerdictKind.ACCEPTED

    # Stage1 second call fails
    ev2 = {"kind": "Reserve", "req_id": "r2", "stage": "stage1", "basis_ref": "b", "config_hash": "cfg", "max_input": 10, "max_output": 10}
    res2 = bridge.apply(res1.state, ev2)
    assert res2.verdict.kind == VerdictKind.REJECTED

    # Stage2 is unaffected and succeeds!
    ev3 = {"kind": "Reserve", "req_id": "r3", "stage": "stage2", "basis_ref": "b", "config_hash": "cfg", "max_input": 50, "max_output": 50}
    res3 = bridge.apply(res1.state, ev3)
    assert res3.verdict.kind == VerdictKind.ACCEPTED


# 5. Timeout preservation (Held tokens not zeroed)
def test_trace_5_timeout_preservation(bridge, standard_state):
    ev1 = {"kind": "Reserve", "req_id": "r1", "stage": "stage1", "basis_ref": "b", "config_hash": "cfg_hash_1", "max_input": 100, "max_output": 50}
    res1 = bridge.apply(standard_state, ev1)

    ev_disp = {"kind": "DispatchIntent", "req_id": "r1"}
    res2 = bridge.apply(res1.state, ev_disp)

    # Timeout
    ev_time = {"kind": "TimeoutUnknown", "req_id": "r1", "reason": "gateway_timeout"}
    res3 = bridge.apply(res2.state, ev_time)
    assert res3.verdict.kind == VerdictKind.ACCEPTED
    assert res3.state.requests[0].transport == TransportState.OUTCOME_UNKNOWN
    # Critical invariant: Charge is still PENDING with 150 tokens held!
    assert res3.state.requests[0].charge.kind == ChargeKind.PENDING
    assert res3.state.requests[0].charge.tokens == 150

    sum_res = bridge.summarize(res3.state)
    assert sum_res.total_held == 150
    assert sum_res.total_spent == 0


# 6. Conflict fault latching
def test_trace_6_conflict_fault_latching(bridge, standard_state):
    ev1 = {"kind": "Reserve", "req_id": "r1", "stage": "stage1", "basis_ref": "b1", "config_hash": "cfg_hash_1", "max_input": 100, "max_output": 50}
    res1 = bridge.apply(standard_state, ev1)

    # Conflicting reservation: different max_input
    ev_conflict = {"kind": "Reserve", "req_id": "r1", "stage": "stage1", "basis_ref": "b1", "config_hash": "cfg_hash_1", "max_input": 200, "max_output": 50}
    res2 = bridge.apply(res1.state, ev_conflict)
    assert res2.verdict.kind == VerdictKind.CONFLICT_FAULT
    assert res2.verdict.reason == "CONFLICTING_REQUEST_IDENTITY"
    assert res2.state.fault == "CONFLICTING_REQUEST_IDENTITY"


# 7. Bound overrun fault
def test_trace_7_bound_overrun_fault(bridge, standard_state):
    ev1 = {"kind": "Reserve", "req_id": "r1", "stage": "stage1", "basis_ref": "b1", "config_hash": "cfg_hash_1", "max_input": 100, "max_output": 50}
    res1 = bridge.apply(standard_state, ev1)

    # Settle with 120 + 50 = 170 > 150
    ev_settle = {"kind": "SettleUsage", "req_id": "r1", "input_tokens": 120, "output_tokens": 50, "receipt_hash": "over_receipt"}
    res2 = bridge.apply(res1.state, ev_settle)
    assert res2.verdict.kind == VerdictKind.CONFLICT_FAULT
    assert res2.verdict.reason == "BOUND_VIOLATION_FAULT"
    assert res2.state.fault == "BOUND_VIOLATION_FAULT"
    # Even though fault is latched, actual observed tokens are counted
    sum_res = bridge.summarize(res2.state)
    assert sum_res.total_spent == 170


# 8. Conclusive failure release
def test_trace_8_conclusive_failure_release(bridge, standard_state):
    ev1 = {"kind": "Reserve", "req_id": "r1", "stage": "stage1", "basis_ref": "b1", "config_hash": "cfg_hash_1", "max_input": 100, "max_output": 50}
    res1 = bridge.apply(standard_state, ev1)
    assert bridge.summarize(res1.state).total_held == 150

    # Failure with conclusive evidence
    ev_fail = {"kind": "FailureConclusive", "req_id": "r1", "evidence_hash": "cert_fail_proof"}
    res2 = bridge.apply(res1.state, ev_fail)
    assert res2.verdict.kind == VerdictKind.ACCEPTED
    assert res2.state.requests[0].transport == TransportState.PROVEN_NOT_SENT
    assert res2.state.requests[0].charge.kind == ChargeKind.RELEASED
    # Budget returned to available: total_held drops to 0, spent is 0
    sum_res = bridge.summarize(res2.state)
    assert sum_res.total_held == 0
    assert sum_res.total_spent == 0


# 9. Duplicate request idempotent no-op
def test_trace_9_duplicate_noop(bridge, standard_state):
    ev1 = {"kind": "Reserve", "req_id": "r1", "stage": "stage1", "basis_ref": "b1", "config_hash": "cfg_hash_1", "max_input": 100, "max_output": 50}
    res1 = bridge.apply(standard_state, ev1)

    # Identical reservation duplicate
    res2 = bridge.apply(res1.state, ev1)
    assert res2.verdict.kind == VerdictKind.DUPLICATE_NOOP
    assert len(res2.state.requests) == 1

    # Dispatch duplicate
    ev_disp = {"kind": "DispatchIntent", "req_id": "r1"}
    res3 = bridge.apply(res1.state, ev_disp)
    res4 = bridge.apply(res3.state, ev_disp)
    assert res4.verdict.kind == VerdictKind.DUPLICATE_NOOP


# 10. Multi-stage concurrent tracking
def test_trace_10_multi_stage_concurrent(bridge, standard_state):
    ev_r1 = {"kind": "Reserve", "req_id": "r1", "stage": "stage1", "basis_ref": "b1", "config_hash": "cfg_hash_1", "max_input": 100, "max_output": 50}
    ev_r2 = {"kind": "Reserve", "req_id": "r2", "stage": "stage2", "basis_ref": "b2", "config_hash": "cfg_hash_1", "max_input": 80, "max_output": 40}

    s1 = bridge.apply(standard_state, ev_r1).state
    s2 = bridge.apply(s1, ev_r2).state

    ev_settle_r1 = {"kind": "SettleUsage", "req_id": "r1", "input_tokens": 40, "output_tokens": 30, "receipt_hash": "rec1"}
    ev_settle_r2 = {"kind": "SettleUsage", "req_id": "r2", "input_tokens": 30, "output_tokens": 20, "receipt_hash": "rec2"}

    s3 = bridge.apply(s2, ev_settle_r1).state
    s4 = bridge.apply(s3, ev_settle_r2).state

    sum_res = bridge.summarize(s4)
    assert sum_res.total_spent == 120
    assert sum_res.total_held == 0
    stages = {ss.stage: ss for ss in sum_res.stage_summaries}
    assert stages["stage1"].spent_tokens == 70
    assert stages["stage2"].spent_tokens == 50


# 11. Terminal settled cannot release
def test_trace_11_terminal_settled_cannot_release(bridge, standard_state):
    ev_res = {"kind": "Reserve", "req_id": "r1", "stage": "stage1", "basis_ref": "b1", "config_hash": "cfg_hash_1", "max_input": 100, "max_output": 50}
    s1 = bridge.apply(standard_state, ev_res).state
    ev_settle = {"kind": "SettleUsage", "req_id": "r1", "input_tokens": 40, "output_tokens": 30, "receipt_hash": "rec1"}
    s2 = bridge.apply(s1, ev_settle).state

    # Trying to release an already settled request
    ev_fail = {"kind": "FailureConclusive", "req_id": "r1", "evidence_hash": "fail_ev"}
    res = bridge.apply(s2, ev_fail)
    assert res.verdict.kind == VerdictKind.REJECTED
    assert res.verdict.reason == "CANNOT_RELEASE_SETTLED_REQUEST"


# 12. Latched fault rejects transitions
def test_trace_12_latched_fault_rejects(bridge, standard_state):
    # Latch a fault via conflict
    ev_res = {"kind": "Reserve", "req_id": "r1", "stage": "stage1", "basis_ref": "b1", "config_hash": "cfg_hash_1", "max_input": 100, "max_output": 50}
    s1 = bridge.apply(standard_state, ev_res).state
    ev_conflict = {"kind": "Reserve", "req_id": "r1", "stage": "stage1", "basis_ref": "b1", "config_hash": "cfg_hash_1", "max_input": 200, "max_output": 50}
    s_fault = bridge.apply(s1, ev_conflict).state
    assert s_fault.fault is not None

    # Any new event on faulted state must be rejected
    ev_new = {"kind": "Reserve", "req_id": "r2", "stage": "stage1", "basis_ref": "b2", "config_hash": "cfg_hash_1", "max_input": 10, "max_output": 10}
    res = bridge.apply(s_fault, ev_new)
    assert res.verdict.kind == VerdictKind.REJECTED
    assert res.verdict.reason == "STATE_FAULT_LATCHED"


# 13. Deterministic replay from journal
def test_trace_13_deterministic_replay(bridge, standard_state):
    events = [
        {"kind": "Reserve", "req_id": "r1", "stage": "stage1", "basis_ref": "b1", "config_hash": "cfg_hash_1", "max_input": 100, "max_output": 50},
        {"kind": "DispatchIntent", "req_id": "r1"},
        {"kind": "TransportObserved", "req_id": "r1", "boundary": "http://api"},
        {"kind": "SettleUsage", "req_id": "r1", "input_tokens": 40, "output_tokens": 30, "receipt_hash": "rec1"},
        {"kind": "Reserve", "req_id": "r2", "stage": "stage2", "basis_ref": "b2", "config_hash": "cfg_hash_1", "max_input": 60, "max_output": 30},
        {"kind": "FailureConclusive", "req_id": "r2", "evidence_hash": "proof_fail"},
    ]

    # Run fold
    folded_state_1 = bridge.fold(standard_state, events)
    folded_state_2 = bridge.fold(standard_state, events)

    assert folded_state_1.model_dump() == folded_state_2.model_dump()
    sum1 = bridge.summarize(folded_state_1)
    sum2 = bridge.summarize(folded_state_2)
    assert sum1.model_dump() == sum2.model_dump()
    assert sum1.total_spent == 70
    assert sum1.total_held == 0
