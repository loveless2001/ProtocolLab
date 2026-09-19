"""PL-ATTEMPT-1.0-draft Attempt Lifecycle Primitives and Gateway.

Governing contract: ATTEMPT_CONTRACT_V1.md
Only the active authority grants a new execution. Transport supplies evidence
of what happened. Accounting follows attributable evidence. Decision validation
cannot rewrite execution history or its cost.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from protocollab.contracts import digest
from protocollab.verified.lifecycle_owner import VerifiedLifecycleOwner
from protocollab.verified.protocol import (
    TransportState,
    VerdictKind,
)


class ExecutionState(str, Enum):
    READY = "READY"
    STARTED = "STARTED"
    FINISHED = "FINISHED"
    NOT_SENT = "NOT_SENT"


class AccountingStatus(str, Enum):
    PENDING = "PENDING"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


class ValidationOutcome(str, Enum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class UsageReport:
    kind: str  # "VerifiedFinal" | "PartialOrUnverified" | "Unknown"
    input_tokens: int = 0
    output_tokens: int = 0
    receipt_ref: str | None = None
    raw_ref: str | None = None


@dataclass(frozen=True)
class AttemptSpec:
    run_id: str
    attempt_id: str
    logical_decision_id: str
    stage: str
    original_decision_basis_ref: str
    input_ref: str
    backend_and_configuration_ref: str
    decision_mode: str
    candidate_registry_ref: str | None = None
    max_input: int = 0
    max_output: int = 0
    declared_reservation_charge: int = 0

    def __post_init__(self) -> None:
        if not self.attempt_id:
            raise ValueError("ATTEMPT_SPEC_EMPTY_ID: attempt_id must not be empty")
        if not self.stage:
            raise ValueError("ATTEMPT_SPEC_EMPTY_STAGE: stage must not be empty")
        expected_charge = self.max_input + self.max_output
        if self.declared_reservation_charge != expected_charge:
            raise ValueError(
                f"RESERVATION_CHARGE_MISMATCH: declared {self.declared_reservation_charge} "
                f"!= max_input ({self.max_input}) + max_output ({self.max_output})"
            )

    @property
    def binding_hash(self) -> str:
        """Deterministic fingerprint of this immutable binding."""
        payload = (
            f"{self.run_id}:{self.attempt_id}:{self.logical_decision_id}:{self.stage}:"
            f"{self.original_decision_basis_ref}:{self.input_ref}:"
            f"{self.backend_and_configuration_ref}:{self.decision_mode}:"
            f"{self.candidate_registry_ref}:{self.max_input}:{self.max_output}"
        )
        return digest(payload)


class OneShotPermit:
    """Unforgeable one-shot execution permit private to the execution gateway."""

    def __init__(self, permit_id: str, attempt_id: str):
        self.permit_id = permit_id
        self.attempt_id = attempt_id
        self._consumed = False
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Atomically consume the permit once."""
        with self._lock:
            if self._consumed:
                return False
            self._consumed = True
            return True

    @property
    def is_consumed(self) -> bool:
        with self._lock:
            return self._consumed


@dataclass
class TransportReport:
    attempt_id: str
    observed_boundary_and_evidence_refs: list[str] = field(default_factory=list)
    completion: bool = False
    usage: UsageReport = field(default_factory=lambda: UsageReport(kind="Unknown"))
    raw_response_ref: str | None = None
    raw_text: str | None = None
    stop_reason: str | None = None
    error_ref: str | None = None
    not_sent_proof_ref: str | None = None


@dataclass
class ValidationReport:
    outcome: ValidationOutcome
    reason: str | None = None
    parsed_payload: Any = None
    winning_json: str | None = None
    compute_meta: dict[str, Any] | None = None
    evaluations: list[dict[str, Any]] | None = None


@dataclass
class AttemptOutcome:
    attempt_id: str
    execution_state: ExecutionState
    accounting_status: AccountingStatus
    validation_outcome: str
    confirmed_input: int = 0
    confirmed_output: int = 0
    held_tokens: int = 0
    is_no_new_execution: bool = False
    raw_response: str | None = None
    parsed_payload: Any = None
    winning_json: str | None = None
    compute_meta: dict[str, Any] | None = None
    evaluations: list[dict[str, Any]] | None = None
    error: Exception | None = None
    conflict: str | None = None


# Gateway Verdicts (§3)
@dataclass
class PrepareReady:
    handle: str
    spec: AttemptSpec


@dataclass
class PrepareExisting:
    handle: str
    spec: AttemptSpec
    execution_state: ExecutionState
    outcome: AttemptOutcome | None = None


@dataclass
class PrepareRejected:
    reason: str


@dataclass
class PrepareConflict:
    reason: str


PrepareVerdict = PrepareReady | PrepareExisting | PrepareRejected | PrepareConflict


@dataclass
class ClaimGranted:
    permit: OneShotPermit


@dataclass
class ClaimNoGrant:
    reason: str
    retained_ref: str | None = None


ClaimStartVerdict = ClaimGranted | ClaimNoGrant


@dataclass
class RecordEvidenceRecorded:
    execution_state: ExecutionState
    accounting_status: AccountingStatus


@dataclass
class RecordEvidenceDuplicate:
    retained_ref: str


@dataclass
class RecordEvidenceConflict:
    reason: str


RecordEvidenceVerdict = (
    RecordEvidenceRecorded | RecordEvidenceDuplicate | RecordEvidenceConflict
)


@dataclass
class RecordValidationRecorded:
    validation_outcome: str


@dataclass
class RecordValidationDuplicate:
    retained_ref: str | None = None


@dataclass
class RecordValidationConflict:
    reason: str


RecordValidationVerdict = (
    RecordValidationRecorded | RecordValidationDuplicate | RecordValidationConflict
)


class AttemptGateway:
    """Atomic Gateway orchestrating the lifecycle of one physical inference attempt.

    Encapsulates: prepare -> claim_start -> transport -> record_evidence -> record_validation.
    Guarantees:
      - Only active authority grants execution.
      - At-most-once execution via OneShotPermit.
      - No inferred identities from queues, stages, or timings.
      - Exact repeat of binding is idempotent; reusing ID with different binding is Conflict.
      - Accounting follows attributable evidence; validation cannot change cost.
    """

    def __init__(self, owner: VerifiedLifecycleOwner):
        self.owner = owner
        self._specs: dict[str, AttemptSpec] = {}
        self._execution_states: dict[str, ExecutionState] = {}
        self._outcomes: dict[str, AttemptOutcome] = {}
        self._permits: dict[str, OneShotPermit] = {}
        self._lock = threading.Lock()

        # Seed existing persisted requests from owner into gateway records
        self._sync_from_owner()

    def _sync_from_owner(self) -> None:
        """Hydrate Gateway known attempts from the underlying store / owner."""
        bend_state = getattr(self.owner, "bend_state", None)
        if bend_state and hasattr(bend_state, "requests"):
            for req in bend_state.requests:
                req_id = req.req_id
                if req_id not in self._execution_states:
                    if req.transport == TransportState.PREPARED:
                        self._execution_states[req_id] = ExecutionState.READY
                    elif req.transport in (
                        TransportState.DISPATCHED_INTENT,
                        TransportState.SENT,
                        TransportState.OUTCOME_UNKNOWN,
                    ):
                        self._execution_states[req_id] = ExecutionState.STARTED
                    elif req.transport == TransportState.PROVEN_NOT_SENT:
                        self._execution_states[req_id] = ExecutionState.NOT_SENT
                    elif req.transport == TransportState.RESPONSE_RECEIVED:
                        self._execution_states[req_id] = ExecutionState.FINISHED

    def prepare(self, spec: AttemptSpec) -> PrepareVerdict:
        """Atomically persist binding and reservation; become READY (§3)."""
        with self._lock:
            # Check for existing binding under this attempt_id
            existing_spec = self._specs.get(spec.attempt_id)
            if existing_spec is not None:
                if existing_spec.binding_hash != spec.binding_hash:
                    return PrepareConflict(
                        f"IDENTITY_CONFLICT: attempt {spec.attempt_id} already bound "
                        f"with different parameters"
                    )
                exec_state = self._execution_states.get(
                    spec.attempt_id, ExecutionState.READY
                )
                outcome = self._outcomes.get(spec.attempt_id)
                return PrepareExisting(
                    handle=spec.attempt_id,
                    spec=spec,
                    execution_state=exec_state,
                    outcome=outcome,
                )

            # Check if underlying owner already recorded this req_id with different basis
            existing_req = self.owner.get_request_record(spec.attempt_id)
            if existing_req is not None:
                if existing_req.basis_ref != spec.original_decision_basis_ref:
                    return PrepareConflict(
                        f"IDENTITY_CONFLICT: request {spec.attempt_id} in ledger "
                        f"has basis {existing_req.basis_ref!r} != {spec.original_decision_basis_ref!r}"
                    )
                self._specs[spec.attempt_id] = spec
                exec_state = self._execution_states.get(
                    spec.attempt_id, ExecutionState.READY
                )
                outcome = self._outcomes.get(spec.attempt_id)
                return PrepareExisting(
                    handle=spec.attempt_id,
                    spec=spec,
                    execution_state=exec_state,
                    outcome=outcome,
                )

            # Check if active authority has latched a fault
            if getattr(self.owner, "bend_state", None) and self.owner.bend_state.fault:
                return PrepareRejected(
                    f"STATE_FAULT_LATCHED: {self.owner.bend_state.fault}"
                )

            # Atomic reservation with active authority
            v_res = self.owner.reserve(
                stage=spec.stage,
                reservation_tokens=spec.declared_reservation_charge,
                req_id=spec.attempt_id,
                basis_ref=spec.original_decision_basis_ref,
                max_input=spec.max_input,
                max_output=spec.max_output,
            )


            res_kind = getattr(v_res, "kind", getattr(v_res, "value", v_res))
            if res_kind in (VerdictKind.REJECTED, "Rejected", "REJECTED"):
                reason = getattr(v_res, "reason", "RESERVATION_REJECTED")
                return PrepareRejected(str(reason))
            if res_kind in (VerdictKind.CONFLICT_FAULT, "ConflictFault", "CONFLICT_FAULT"):
                reason = getattr(v_res, "reason", "RESERVATION_CONFLICT")
                return PrepareConflict(str(reason))
            if res_kind in (VerdictKind.DUPLICATE_NOOP, "DuplicateNoop", "DUPLICATE_NOOP"):
                self._specs[spec.attempt_id] = spec
                exec_state = self._execution_states.get(
                    spec.attempt_id, ExecutionState.READY
                )
                return PrepareExisting(
                    handle=spec.attempt_id,
                    spec=spec,
                    execution_state=exec_state,
                    outcome=self._outcomes.get(spec.attempt_id),
                )

            # Binding succeeded and is within limits -> READY
            self._specs[spec.attempt_id] = spec
            self._execution_states[spec.attempt_id] = ExecutionState.READY
            return PrepareReady(handle=spec.attempt_id, spec=spec)

    def claim_start(self, handle: str) -> ClaimStartVerdict:
        """Atomically become STARTED; return one live, one-shot permit (§3)."""
        with self._lock:
            exec_state = self._execution_states.get(handle)
            if exec_state is None:
                # Check underlying owner
                rec = self.owner.get_request_record(handle)
                if rec is None:
                    return ClaimNoGrant(reason="ATTEMPT_NOT_PREPARED")
                if rec.transport == TransportState.PREPARED:
                    exec_state = ExecutionState.READY
                elif rec.transport in (
                    TransportState.DISPATCHED_INTENT,
                    TransportState.SENT,
                    TransportState.OUTCOME_UNKNOWN,
                ):
                    exec_state = ExecutionState.STARTED
                else:
                    exec_state = ExecutionState.FINISHED
                self._execution_states[handle] = exec_state

            if exec_state == ExecutionState.STARTED:
                return ClaimNoGrant(
                    reason="ALREADY_STARTED_IN_PROGRESS",
                    retained_ref=handle,
                )
            if exec_state in (ExecutionState.FINISHED, ExecutionState.NOT_SENT):
                return ClaimNoGrant(
                    reason="ATTEMPT_ALREADY_TERMINATED",
                    retained_ref=handle,
                )
            if exec_state != ExecutionState.READY:
                return ClaimNoGrant(reason=f"INVALID_STATE_FOR_START: {exec_state}")

            spec = self._specs.get(handle)
            stage = spec.stage if spec else None
            if stage is None:
                rec = self.owner.get_request_record(handle)
                if rec is not None:
                    stage = rec.stage
            if stage is None:
                stage = "stage1"

            # Transition active authority to DispatchedIntent
            v_disp = self.owner.record_dispatched(stage=stage, req_id=handle)
            disp_kind = getattr(v_disp, "kind", getattr(v_disp, "value", v_disp))
            if disp_kind in (VerdictKind.DUPLICATE_NOOP, "DuplicateNoop", "DUPLICATE_NOOP"):
                return ClaimNoGrant(
                    reason="DUPLICATE_DISPATCH_NO_GRANT",
                    retained_ref=handle,
                )
            if disp_kind in (VerdictKind.REJECTED, "Rejected"):
                return ClaimNoGrant(
                    reason=str(getattr(v_disp, "reason", "DISPATCH_REJECTED"))
                )

            # Issue the one-shot permit
            permit = OneShotPermit(
                permit_id=f"permit_{uuid.uuid4().hex[:12]}",
                attempt_id=handle,
            )
            self._permits[handle] = permit
            self._execution_states[handle] = ExecutionState.STARTED
            return ClaimGranted(permit=permit)

    def record_evidence(
        self, handle: str, report: TransportReport
    ) -> RecordEvidenceVerdict:
        """Record transport evidence and settle or hold pending cost (§3, §4, §5)."""
        with self._lock:
            spec = self._specs.get(handle)
            stage = spec.stage if spec else None
            if stage is None:
                rec = self.owner.get_request_record(handle)
                if rec is not None:
                    stage = rec.stage
            if stage is None:
                stage = "stage1"

            # 1. Conclusively proven not-sent: release held reservation
            if report.not_sent_proof_ref is not None and not report.completion:
                self.owner.record_failed(
                    stage=stage,
                    req_id=handle,
                    evidence_hash=report.not_sent_proof_ref,
                )
                self._execution_states[handle] = ExecutionState.NOT_SENT
                return RecordEvidenceRecorded(
                    execution_state=ExecutionState.NOT_SENT,
                    accounting_status=AccountingStatus.RELEASED,
                )

            # 2. Verified final usage: settle attributable cost
            if report.usage.kind == "VerifiedFinal":
                receipt_ref = report.usage.receipt_ref or digest(
                    f"{handle}:{report.usage.input_tokens}:{report.usage.output_tokens}"
                )
                try:
                    self.owner.record_completed(
                        stage=stage,
                        input_tokens=report.usage.input_tokens,
                        output_tokens=report.usage.output_tokens,
                        reservation_tokens=spec.declared_reservation_charge if spec else 0,
                        req_id=handle,
                        receipt_hash=receipt_ref,
                    )
                except RuntimeError as err:
                    if "BOUND_VIOLATION_FAULT" in str(err) or "CONFLICT" in str(err):
                        return RecordEvidenceConflict(reason=str(err))
                    raise
                self._execution_states[handle] = ExecutionState.FINISHED
                return RecordEvidenceRecorded(
                    execution_state=ExecutionState.FINISHED,
                    accounting_status=AccountingStatus.SETTLED,
                )

            # 3. Partial or unknown usage after start: keep charge PENDING (§4, §5)
            # A timeout or dropped connection is NOT zero cost without positive proof.
            self.owner.record_timeout(
                stage=stage,
                req_id=handle,
                reason=report.error_ref or "OUTCOME_UNKNOWN_HELD_PENDING",
            )

            self._execution_states[handle] = ExecutionState.STARTED
            return RecordEvidenceRecorded(
                execution_state=ExecutionState.STARTED,
                accounting_status=AccountingStatus.PENDING,
            )

    def record_validation(
        self, handle: str, report: ValidationReport
    ) -> RecordValidationVerdict:
        """Record decision validation. CANNOT rewrite execution history or its cost (§1, §9)."""
        with self._lock:
            # Send validation event to active lifecycle owner (which records to Bend / Python)
            if hasattr(self.owner, "record_validation"):
                self.owner.record_validation(handle, report.outcome.value)
            return RecordValidationRecorded(validation_outcome=report.outcome.value)

    def execute_attempt(
        self,
        spec: AttemptSpec,
        transport_invoker: Callable[[OneShotPermit], TransportReport],
        validator: Callable[[TransportReport], ValidationReport],
    ) -> AttemptOutcome:
        """Execute one physical inference attempt through the atomic gateway (§3)."""
        # Step 1: Prepare
        prep = self.prepare(spec)
        if isinstance(prep, PrepareConflict):
            return AttemptOutcome(
                attempt_id=spec.attempt_id,
                execution_state=ExecutionState.READY,
                accounting_status=AccountingStatus.PENDING,
                validation_outcome="CONFLICT",
                is_no_new_execution=True,
                conflict=prep.reason,
            )
        if isinstance(prep, PrepareRejected):
            return AttemptOutcome(
                attempt_id=spec.attempt_id,
                execution_state=ExecutionState.READY,
                accounting_status=AccountingStatus.PENDING,
                validation_outcome="REJECTED",
                is_no_new_execution=True,
                error=RuntimeError(prep.reason),
            )
        if isinstance(prep, PrepareExisting):
            # Attempt already prepared or completed.
            if prep.execution_state in (
                ExecutionState.FINISHED,
                ExecutionState.NOT_SENT,
            ):
                # Return retained result without new call or charge (§3, §8 A02)
                if prep.outcome is not None:
                    cached = prep.outcome
                    cached.is_no_new_execution = True
                    return cached
                return AttemptOutcome(
                    attempt_id=spec.attempt_id,
                    execution_state=prep.execution_state,
                    accounting_status=AccountingStatus.SETTLED,
                    validation_outcome="NO_NEW_EXECUTION",
                    is_no_new_execution=True,
                )
            if prep.execution_state == ExecutionState.STARTED:
                return AttemptOutcome(
                    attempt_id=spec.attempt_id,
                    execution_state=ExecutionState.STARTED,
                    accounting_status=AccountingStatus.PENDING,
                    validation_outcome="ALREADY_IN_PROGRESS",
                    is_no_new_execution=True,
                )

        # Step 2: Claim Start
        claim = self.claim_start(spec.attempt_id)
        if isinstance(claim, ClaimNoGrant):
            return AttemptOutcome(
                attempt_id=spec.attempt_id,
                execution_state=self._execution_states.get(
                    spec.attempt_id, ExecutionState.READY
                ),
                accounting_status=AccountingStatus.PENDING,
                validation_outcome="NO_GRANT",
                is_no_new_execution=True,
                error=RuntimeError(claim.reason),
            )

        permit = claim.permit

        # Step 3: Invoke Transport with the OneShotPermit
        # The permit can only be consumed once by the transport invoker
        try:
            transport_report = transport_invoker(permit)
        except Exception as exc:
            # Physical transport invocation crashed unexpectedly
            transport_report = TransportReport(
                attempt_id=spec.attempt_id,
                completion=False,
                usage=UsageReport(kind="Unknown"),
                error_ref=f"UNCAUGHT_TRANSPORT_EXCEPTION:{type(exc).__name__}:{exc}",
            )

        # Step 4: Record Evidence & Settle Accounting
        ev_result = self.record_evidence(spec.attempt_id, transport_report)
        acct_status = getattr(
            ev_result, "accounting_status", AccountingStatus.PENDING
        )
        exec_state = getattr(
            ev_result, "execution_state", ExecutionState.STARTED
        )

        # Compute confirmed usage
        conf_in = 0
        conf_out = 0
        held = 0
        if acct_status == AccountingStatus.SETTLED:
            conf_in = transport_report.usage.input_tokens
            conf_out = transport_report.usage.output_tokens
        elif acct_status == AccountingStatus.PENDING:
            held = spec.declared_reservation_charge

        # Step 5: Record Validation (Decoupled from Accounting)
        val_report = validator(transport_report)
        self.record_validation(spec.attempt_id, val_report)

        outcome = AttemptOutcome(
            attempt_id=spec.attempt_id,
            execution_state=exec_state,
            accounting_status=acct_status,
            validation_outcome=val_report.outcome.value
            if val_report.outcome
            else "UNKNOWN",
            confirmed_input=conf_in,
            confirmed_output=conf_out,
            held_tokens=held,
            raw_response=transport_report.raw_text,
            parsed_payload=val_report.parsed_payload,
            winning_json=val_report.winning_json,
            compute_meta=val_report.compute_meta,
            evaluations=val_report.evaluations,
            error=RuntimeError(val_report.reason) if val_report.reason else None,
        )
        with self._lock:
            self._outcomes[spec.attempt_id] = outcome

        return outcome
