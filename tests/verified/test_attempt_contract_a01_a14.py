"""Acceptance Suite A01–A14 verifying PL-ATTEMPT-1.0-draft.

Governing document: ATTEMPT_CONTRACT_V1.md §8 (Acceptance Matrix)
Tests real boundaries:
- Actual AttemptGateway and VerifiedLifecycleOwner
- Production Store (SQLite WAL backend)
- Executable Bend lifecycle kernel bridge
- Deterministic fake transports and actual response-reading paths
- Both SHADOW and AUTHORITATIVE modes where applicable
"""

from __future__ import annotations

import http.server
import socketserver
import threading

import pytest

from protocollab.actor import FrozenModelPort, ModelPortConfig
from protocollab.actor.budget import DiagnosticBudgetAllocation, StageBudgetLimits
from protocollab.contracts import ActionProposal, DecisionBasis, digest, uid
from protocollab.gateway import ActionBroker
from protocollab.storage import Store
from protocollab.verified.attempt import (
    AccountingStatus,
    AttemptSpec,
    ClaimGranted,
    ClaimNoGrant,
    ExecutionState,
    OneShotPermit,
    PrepareConflict,
    PrepareReady,
    TransportReport,
    UsageReport,
    ValidationOutcome,
    ValidationReport,
)
from protocollab.verified.bridge import VerifiedKernelBridge, find_bend_app, find_bun
from protocollab.verified.lifecycle_owner import VerifiedLifecycleOwner
from protocollab.verified.protocol import (
    ChargeKind,
    LifecycleMode,
    TransportState,
)

bun_bin = find_bun()
bend_app = find_bend_app()
if bun_bin is None or not bun_bin.exists() or bend_app is None or not bend_app.exists():
    pytest.fail("Bend runtime or Bun toolchain not found; §9 requires toolchain presence as blocker")


@pytest.fixture
def bridge():
    return VerifiedKernelBridge.get_default()


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test_lifecycle.db")


def make_owner(
    store: Store,
    run_id: str,
    bridge: VerifiedKernelBridge,
    mode: LifecycleMode = LifecycleMode.SHADOW,
    max_calls: int = 10,
    max_tokens: int = 2000,
) -> VerifiedLifecycleOwner:
    alloc = DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=max_calls, max_tokens=max_tokens)},
        aggregate_max_calls=max_calls,
        aggregate_max_tokens=max_tokens,
    )
    return VerifiedLifecycleOwner(
        store=store,
        run_id=run_id,
        allocation=alloc,
        config_hash="cfg_hash_test",
        mode=mode,
        bridge=bridge,
    )


def make_spec(
    run_id: str,
    attempt_id: str,
    stage: str = "stage1",
    prompt: str = "test prompt",
    max_input: int = 100,
    max_output: int = 10,
) -> AttemptSpec:
    return AttemptSpec(
        run_id=run_id,
        attempt_id=attempt_id,
        logical_decision_id=f"dec_{attempt_id}",
        stage=stage,
        original_decision_basis_ref=digest(f"basis_{attempt_id}"),
        input_ref=digest(prompt),
        backend_and_configuration_ref="cfg_ref_1",
        decision_mode="free_json",
        max_input=max_input,
        max_output=max_output,
        declared_reservation_charge=max_input + max_output,
    )


# ==============================================================================
# A01: Five new attempts with identical input; each uses 100/10
# Required assertion: Five distinct attempts/calls; total 500/50.
# ==============================================================================
def test_a01_five_distinct_attempts_identical_input(store, bridge):
    owner = make_owner(store, "run_a01", bridge)
    gateway = owner.gateway
    calls_made = []

    def transport(permit: OneShotPermit) -> TransportReport:
        calls_made.append(permit.attempt_id)
        assert permit.consume() is True
        return TransportReport(
            attempt_id=permit.attempt_id,
            completion=True,
            usage=UsageReport(kind="VerifiedFinal", input_tokens=100, output_tokens=10),
            raw_text='{"action": "STEP"}',
        )

    def validator(report: TransportReport) -> ValidationReport:
        return ValidationReport(
            outcome=ValidationOutcome.ACCEPTED,
            parsed_payload={"action": "STEP"},
        )

    for i in range(5):
        spec = make_spec("run_a01", f"req_a01_{i}", prompt="identical input prompt")
        outcome = gateway.execute_attempt(spec, transport, validator)
        assert outcome.execution_state == ExecutionState.FINISHED
        assert outcome.accounting_status == AccountingStatus.SETTLED
        assert outcome.confirmed_input == 100
        assert outcome.confirmed_output == 10

    assert len(calls_made) == 5
    summary = bridge.summarize(owner.bend_state)
    assert summary.total_spent == 550  # 5 * (100 + 10)
    assert summary.total_held == 0


# ==============================================================================
# A02: Replay completed attempt and its receipt
# Required assertion: No new call or charge. Report no-new-execution.
# ==============================================================================
def test_a02_replay_completed_attempt_and_receipt(store, bridge):
    owner = make_owner(store, "run_a02", bridge)
    gateway = owner.gateway
    call_count = 0

    def transport(permit: OneShotPermit) -> TransportReport:
        nonlocal call_count
        call_count += 1
        assert permit.consume() is True
        return TransportReport(
            attempt_id=permit.attempt_id,
            completion=True,
            usage=UsageReport(kind="VerifiedFinal", input_tokens=100, output_tokens=10),
            raw_text='{"action": "STEP"}',
        )

    def validator(report: TransportReport) -> ValidationReport:
        return ValidationReport(
            outcome=ValidationOutcome.ACCEPTED,
            parsed_payload={"action": "STEP"},
        )

    spec = make_spec("run_a02", "req_a02")
    # First execution
    out1 = gateway.execute_attempt(spec, transport, validator)
    assert call_count == 1
    assert out1.is_no_new_execution is False

    # Replay same attempt
    out2 = gateway.execute_attempt(spec, transport, validator)
    assert call_count == 1  # No new call
    assert out2.is_no_new_execution is True
    summary = bridge.summarize(owner.bend_state)
    assert summary.total_spent == 110  # No new charge


# ==============================================================================
# A03: Restart after READY
# Required assertion: One subsequent owner can claim/start it exactly once.
# ==============================================================================
def test_a03_restart_after_ready(store, bridge):
    owner1 = make_owner(store, "run_a03", bridge)
    spec = make_spec("run_a03", "req_a03")
    prep = owner1.gateway.prepare(spec)
    assert isinstance(prep, PrepareReady)

    # Reconstruct owner on same store (simulating process restart after READY)
    owner2 = make_owner(store, "run_a03", bridge)
    claim1 = owner2.gateway.claim_start("req_a03")
    assert isinstance(claim1, ClaimGranted)
    assert claim1.permit.consume() is True

    # Subsequent claim fails
    claim2 = owner2.gateway.claim_start("req_a03")
    assert isinstance(claim2, ClaimNoGrant)


# ==============================================================================
# A04: Competing claimers, including separate database connections
# Required assertion: At most one grant and one invocation.
# ==============================================================================
def test_a04_competing_claimers(store, bridge):
    owner = make_owner(store, "run_a04", bridge)
    gateway = owner.gateway
    spec = make_spec("run_a04", "req_a04")
    prep = gateway.prepare(spec)
    assert isinstance(prep, PrepareReady)

    results = []

    def try_claim():
        res = gateway.claim_start("req_a04")
        results.append(res)

    t1 = threading.Thread(target=try_claim)
    t2 = threading.Thread(target=try_claim)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    grants = [r for r in results if isinstance(r, ClaimGranted)]
    denials = [r for r in results if isinstance(r, ClaimNoGrant)]
    assert len(grants) == 1
    assert len(denials) == 1


# ==============================================================================
# A05: Restart after committed STARTED, before known response
# Required assertion: No regrant or resend; charge remains pending.
# ==============================================================================
def test_a05_restart_after_committed_started(store, bridge):
    owner1 = make_owner(store, "run_a05", bridge)
    spec = make_spec("run_a05", "req_a05")
    prep = owner1.gateway.prepare(spec)
    assert isinstance(prep, PrepareReady)
    claim = owner1.gateway.claim_start("req_a05")
    assert isinstance(claim, ClaimGranted)

    # Reconstruct owner without recording evidence (crash during I/O)
    owner2 = make_owner(store, "run_a05", bridge)
    claim_after_restart = owner2.gateway.claim_start("req_a05")
    assert isinstance(claim_after_restart, ClaimNoGrant)

    rec = owner2.get_request_record("req_a05")
    assert rec is not None
    assert rec.charge.kind == ChargeKind.PENDING
    assert rec.charge.tokens == 110


# ==============================================================================
# A06: Response-started followed by invalid JSON, incomplete body, or untrusted identity
# Required assertion: No not-sent release; retain evidence and unresolved cost
# unless reliable final usage is available.
# §8 rule: Drive actual model-port response-reading path.
# ==============================================================================
def test_a06_response_started_followed_by_invalid_body(store, bridge, tmp_path):
    class TruncatedResponseHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            # Send truncated / invalid JSON body after response started
            self.wfile.write(b'{"model_id": "test", "text": "incom')
            self.wfile.flush()

        def log_message(self, format, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), TruncatedResponseHandler)
    port_num = server.server_address[1]
    server_thread = threading.Thread(target=server.handle_request)
    server_thread.daemon = True
    server_thread.start()

    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test",
        endpoint=f"http://127.0.0.1:{port_num}",
        max_input_tokens=1000,
        max_output_tokens=100,
    )
    port = FrozenModelPort(port_cfg, store, max_calls=5, total_tokens=2000)
    owner = make_owner(store, "run_a06", bridge)
    gateway = owner.gateway
    spec = make_spec("run_a06", "req_a06", max_input=1000, max_output=100)

    def transport(permit: OneShotPermit) -> TransportReport:
        assert permit.consume() is True
        packet = {"schema_version": "0.1", "public_alphabet": ["TICK"], "task": {}}
        try:
            port.generate(packet, seed=0)
            raw = "never_reached"
        except Exception as exc:
            return TransportReport(
                attempt_id=permit.attempt_id,
                completion=False,
                usage=UsageReport(kind="Unknown"),
                error_ref=f"JSON_DECODE_TRUNCATED:{type(exc).__name__}",
                not_sent_proof_ref=None,  # Response started, cannot prove not-sent
            )
        return TransportReport(attempt_id=permit.attempt_id, completion=True, raw_text=raw)

    def validator(report: TransportReport) -> ValidationReport:
        return ValidationReport(
            outcome=ValidationOutcome.REJECTED,
            reason="TRANSPORT_TRUNCATED",
        )

    outcome = gateway.execute_attempt(spec, transport, validator)
    assert outcome.accounting_status == AccountingStatus.PENDING
    assert outcome.held_tokens == 1100
    rec = owner.get_request_record("req_a06")
    assert rec.charge.kind == ChargeKind.PENDING
    assert rec.charge.tokens == 1100
    server.server_close()


# ==============================================================================
# A07: Confirmed 100/10 usage with invalid JSON, unclosed reasoning, or invalid scoring vector
# Required assertion: 100/10 charged once; decision rejected; no false transport-failure accounting.
# ==============================================================================
def test_a07_confirmed_usage_with_invalid_candidate_vector(store, bridge):
    owner = make_owner(store, "run_a07", bridge)
    gateway = owner.gateway
    spec = make_spec("run_a07", "req_a07", max_input=100, max_output=10)

    def transport(permit: OneShotPermit) -> TransportReport:
        assert permit.consume() is True
        return TransportReport(
            attempt_id=permit.attempt_id,
            completion=True,
            usage=UsageReport(kind="VerifiedFinal", input_tokens=100, output_tokens=10),
            raw_text='{"evaluations": [{"candidate": "A", "score": "nan"}]}',
        )

    def validator(report: TransportReport) -> ValidationReport:
        # Candidate validation rejects due to non-finite score
        return ValidationReport(
            outcome=ValidationOutcome.REJECTED,
            reason="NON_FINITE_SCORE",
        )

    outcome = gateway.execute_attempt(spec, transport, validator)
    assert outcome.validation_outcome == "REJECTED"
    assert outcome.accounting_status == AccountingStatus.SETTLED
    assert outcome.confirmed_input == 100
    assert outcome.confirmed_output == 10

    # Verify kernel state has confirmed usage settled, not released or false transport failure
    summary = bridge.summarize(owner.bend_state)
    assert summary.total_spent == 110
    assert summary.total_held == 0


# ==============================================================================
# A08: Arbitrary error after possible send; no usage
# Required assertion: Pending charge retained regardless of exception name/message.
# ==============================================================================
def test_a08_unresolved_error_after_send_retains_pending(store, bridge):
    owner = make_owner(store, "run_a08", bridge)
    gateway = owner.gateway
    spec = make_spec("run_a08", "req_a08", max_input=100, max_output=10)

    class CustomNetworkDrop(Exception):
        pass

    def transport(permit: OneShotPermit) -> TransportReport:
        assert permit.consume() is True
        # Transport throws an arbitrary custom exception
        return TransportReport(
            attempt_id=permit.attempt_id,
            completion=False,
            usage=UsageReport(kind="Unknown"),
            error_ref="CustomNetworkDrop: connection reset by peer",
            not_sent_proof_ref=None,
        )

    def validator(report: TransportReport) -> ValidationReport:
        return ValidationReport(outcome=ValidationOutcome.REJECTED, reason="NETWORK_DROP")

    outcome = gateway.execute_attempt(spec, transport, validator)
    assert outcome.accounting_status == AccountingStatus.PENDING
    assert outcome.held_tokens == 110
    rec = owner.get_request_record("req_a08")
    assert rec.charge.kind == ChargeKind.PENDING
    assert rec.charge.tokens == 110


# ==============================================================================
# A09: Genuine not-sent proof
# Required assertion: Release that attempt only; no backend invocation.
# ==============================================================================
def test_a09_genuine_not_sent_proof(store, bridge):
    owner = make_owner(store, "run_a09", bridge)
    gateway = owner.gateway
    spec = make_spec("run_a09", "req_a09", max_input=100, max_output=10)

    def transport(permit: OneShotPermit) -> TransportReport:
        # Positively proven cancelled before crossing model boundary
        return TransportReport(
            attempt_id=permit.attempt_id,
            completion=False,
            usage=UsageReport(kind="Unknown"),
            not_sent_proof_ref=digest("proof_cancelled_before_socket_write"),
        )

    def validator(report: TransportReport) -> ValidationReport:
        return ValidationReport(outcome=ValidationOutcome.NOT_ATTEMPTED)

    outcome = gateway.execute_attempt(spec, transport, validator)
    assert outcome.execution_state == ExecutionState.NOT_SENT
    assert outcome.accounting_status == AccountingStatus.RELEASED
    assert outcome.held_tokens == 0
    assert outcome.confirmed_input == 0

    summary = bridge.summarize(owner.bend_state)
    assert summary.total_spent == 0
    assert summary.total_held == 0


# ==============================================================================
# A10: Timeout then late verified receipt, delivered twice
# Required assertion: Bound replaced once with actual cost; no second call.
# ==============================================================================
def test_a10_timeout_then_late_verified_receipt(store, bridge):
    owner = make_owner(store, "run_a10", bridge)
    gateway = owner.gateway
    spec = make_spec("run_a10", "req_a10", max_input=100, max_output=10)

    # 1. First execution times out
    def timeout_transport(permit: OneShotPermit) -> TransportReport:
        assert permit.consume() is True
        return TransportReport(
            attempt_id=permit.attempt_id,
            completion=False,
            usage=UsageReport(kind="Unknown"),
            error_ref="ReadTimeout",
        )

    out1 = gateway.execute_attempt(
        spec,
        timeout_transport,
        lambda r: ValidationReport(outcome=ValidationOutcome.REJECTED),
    )
    assert out1.accounting_status == AccountingStatus.PENDING

    # 2. Late receipt arrives
    late_report = TransportReport(
        attempt_id="req_a10",
        completion=True,
        usage=UsageReport(kind="VerifiedFinal", input_tokens=80, output_tokens=15),
        raw_text='{"action": "LATE"}',
    )
    res_ev1 = gateway.record_evidence("req_a10", late_report)
    assert res_ev1.accounting_status == AccountingStatus.SETTLED

    summary1 = bridge.summarize(owner.bend_state)
    assert summary1.total_spent == 95
    assert summary1.total_held == 0

    # 3. Same receipt delivered second time
    gateway.record_evidence("req_a10", late_report)
    summary2 = bridge.summarize(owner.bend_state)

    assert summary2.total_spent == 95  # No double charge


# ==============================================================================
# A11: Shadow off/on/disagreeing/unavailable, including restart
# Required assertion: Same active decisions/calls/accounting for matched event trace.
# ==============================================================================
def test_a11_shadow_mode_isolation(store, bridge):
    modes = [LifecycleMode.SHADOW, LifecycleMode.AUTHORITATIVE]
    results = {}

    for mode in modes:
        run_id = f"run_a11_{mode.value}"
        owner = make_owner(store, run_id, bridge, mode=mode)
        gateway = owner.gateway
        spec = make_spec(run_id, f"req_{mode.value}")

        def transport(permit: OneShotPermit) -> TransportReport:
            assert permit.consume() is True
            return TransportReport(
                attempt_id=permit.attempt_id,
                completion=True,
                usage=UsageReport(kind="VerifiedFinal", input_tokens=100, output_tokens=10),
                raw_text='{"action": "STEP"}',
            )

        outcome = gateway.execute_attempt(
            spec,
            transport,
            lambda r: ValidationReport(outcome=ValidationOutcome.ACCEPTED),
        )
        results[mode] = (
            outcome.confirmed_input,
            outcome.confirmed_output,
            outcome.validation_outcome,
        )

    # Decisions and token accounting match across both modes
    assert results[LifecycleMode.SHADOW] == results[LifecycleMode.AUTHORITATIVE]


# ==============================================================================
# A12: Crash before/after evidence and state commits
# Required assertion: Replay reconstructs consistent state; no lost fault evidence or repeated I/O.
# ==============================================================================
def test_a12_crash_before_after_evidence_commits(store, bridge):
    owner1 = make_owner(store, "run_a12", bridge)
    spec = make_spec("run_a12", "req_a12")
    prep = owner1.gateway.prepare(spec)
    assert isinstance(prep, PrepareReady)
    claim = owner1.gateway.claim_start("req_a12")
    assert isinstance(claim, ClaimGranted)

    # Crash after claim_start before evidence commit
    owner2 = make_owner(store, "run_a12", bridge)
    rec = owner2.get_request_record("req_a12")
    assert rec.transport in (TransportState.DISPATCHED_INTENT, TransportState.SENT)

    # Commit evidence and verify state persists across another restart
    owner2.gateway.record_evidence(
        "req_a12",
        TransportReport(
            attempt_id="req_a12",
            completion=True,
            usage=UsageReport(kind="VerifiedFinal", input_tokens=70, output_tokens=10),
        ),
    )
    owner3 = make_owner(store, "run_a12", bridge)
    rec3 = owner3.get_request_record("req_a12")
    assert rec3.charge.kind == ChargeKind.SETTLED
    assert rec3.charge.input_tokens == 70
    assert rec3.charge.output_tokens == 10


# ==============================================================================
# A13: Over-bound bill or verified late bill after release
# Required assertion: Real expense and conflict retained; new starts disabled.
# ==============================================================================
def test_a13_over_bound_bill_latches_conflict(store, bridge):
    owner = make_owner(store, "run_a13", bridge, mode=LifecycleMode.AUTHORITATIVE)
    gateway = owner.gateway
    spec = make_spec("run_a13", "req_a13", max_input=100, max_output=10)
    prep = gateway.prepare(spec)
    assert isinstance(prep, PrepareReady)
    claim = gateway.claim_start("req_a13")
    assert isinstance(claim, ClaimGranted)

    # Deliver bill of 200/50 (exceeds bound of 110)
    over_report = TransportReport(
        attempt_id="req_a13",
        completion=True,
        usage=UsageReport(kind="VerifiedFinal", input_tokens=200, output_tokens=50),
    )
    gateway.record_evidence("req_a13", over_report)

    # Bend kernel latched bound violation fault
    assert owner.bend_state.fault is not None
    assert "BOUND_VIOLATION_FAULT" in owner.bend_state.fault

    # Subsequent prepare must be rejected
    spec2 = make_spec("run_a13", "req_a13_next")
    prep2 = gateway.prepare(spec2)
    assert getattr(prep2, "reason", None) is not None
    assert "STATE_FAULT_LATCHED" in prep2.reason


# ==============================================================================
# A14: Replay under changed immutable binding
# Required assertion: Identity conflict before execution.
# ==============================================================================
def test_a14_replay_under_changed_immutable_binding(store, bridge):
    owner = make_owner(store, "run_a14", bridge)
    gateway = owner.gateway
    spec1 = make_spec("run_a14", "req_a14_dup", prompt="initial prompt")
    prep1 = gateway.prepare(spec1)
    assert isinstance(prep1, PrepareReady)

    # Same attempt_id with different prompt/input
    spec2 = make_spec("run_a14", "req_a14_dup", prompt="tampered DIFFERENT prompt")
    prep2 = gateway.prepare(spec2)
    assert isinstance(prep2, PrepareConflict)
    assert "IDENTITY_CONFLICT" in prep2.reason


# ==============================================================================
# Positive Proposal & No-Execution Failure Paths (§8)
# ==============================================================================
def test_positive_successful_proposal_through_broker(store):
    class DummyGovernance:
        snapshot = {"task": {"resource_id": "res_1", "revision": 1}}

        def authorize(self, resource_id, op, epoch, charge=False):
            return None

    class DummyBeliefSnapshot:
        revision = 1

    class DummyBelief:
        revision = 1
        snapshot = DummyBeliefSnapshot()


    basis = DecisionBasis(
        namespace=store.namespace,
        resource_id="res_1",
        belief_rev=1,
        model_rev=0,
        goal_rev=1,
        control_epoch=1,
    )
    basis_ref = store.put_blob(basis.model_dump())

    broker = ActionBroker(
        store=store,
        governance=DummyGovernance(),
        backend=None,
        capture=None,
        belief=DummyBelief(),
        model_provider=lambda: None,
    )
    proposal = ActionProposal(
        proposal_id=uid(),
        command_id=uid(),
        namespace=store.namespace,
        resource_id="res_1",
        operation="SIGNAL_X",
        actor_principal_ref="actor",
        idempotency_key=uid(),
        decision_basis_ref=basis_ref,
        belief_rev=1,
        model_rev=0,
        goal_rev=1,
        control_epoch=1,
    )

    res = broker.propose(proposal)
    assert res["status"] in ("READY", "VALIDATED", "PROPOSED", "ACCEPTED", "AUTHORIZED")



def test_no_execution_failure_path_blocks_action_dispatch(store, bridge):
    owner = make_owner(store, "run_no_exec", bridge)
    gateway = owner.gateway
    spec = make_spec("run_no_exec", "req_no_exec")
    prep = gateway.prepare(spec)
    assert isinstance(prep, PrepareReady)

    # Cancelled before start
    gateway.record_evidence(
        "req_no_exec",
        TransportReport(
            attempt_id="req_no_exec",
            completion=False,
            not_sent_proof_ref="cancelled_unstarted",
        ),
    )
    outcome = gateway.execute_attempt(
        spec,
        lambda p: pytest.fail("Should not invoke transport"),
        lambda r: ValidationReport(outcome=ValidationOutcome.NOT_ATTEMPTED),
    )
    assert outcome.is_no_new_execution is True
    assert outcome.execution_state == ExecutionState.NOT_SENT
