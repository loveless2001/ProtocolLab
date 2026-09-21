"""PL-ATTEMPT-1.0-draft Attempt Lifecycle Primitives and Gateway.

Governing contract: ATTEMPT_CONTRACT_V1.md
Only the active authority grants a new execution. Transport supplies evidence
of what happened. Accounting follows attributable evidence. Decision validation
cannot rewrite execution history or its cost.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any, Callable

from protocollab.contracts import digest
from protocollab.learning import BudgetExhausted
from protocollab.verified.lifecycle_owner import VerifiedLifecycleOwner
from protocollab.verified.protocol import (
    MAX_SAFE_INT,
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

    def __post_init__(self) -> None:
        if self.kind not in {"VerifiedFinal", "PartialOrUnverified", "Unknown"}:
            raise ValueError(f"UNKNOWN_USAGE_REPORT_KIND:{self.kind}")
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0 or value > MAX_SAFE_INT:
                raise ValueError(f"{name} outside supported numeric bounds")


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
        for name, value in (
            ("max_input", self.max_input),
            ("max_output", self.max_output),
            ("declared_reservation_charge", self.declared_reservation_charge),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0 or value > MAX_SAFE_INT:
                raise ValueError(f"{name} outside supported numeric bounds")
        expected_charge = self.max_input + self.max_output
        if self.declared_reservation_charge != expected_charge:
            raise ValueError(
                f"RESERVATION_CHARGE_MISMATCH: declared {self.declared_reservation_charge} "
                f"!= max_input ({self.max_input}) + max_output ({self.max_output})"
            )

    @property
    def binding_hash(self) -> str:
        """Deterministic fingerprint of this immutable binding."""
        return digest(asdict(self))


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


_TRUSTED_EVIDENCE_SEAL = object()


@dataclass(frozen=True)
class TransportReport:
    attempt_id: str
    observed_boundary_and_evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    completion: bool = False
    usage: UsageReport = field(default_factory=lambda: UsageReport(kind="Unknown"))
    raw_response_ref: str | None = None
    raw_text: str | None = None
    stop_reason: str | None = None
    error_ref: str | None = None
    not_sent_proof_ref: str | None = None
    evidence_source_ref: str | None = None
    _authority_seal: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Accept list-shaped adapter input while keeping the attested evidence
        # envelope immutable after construction.
        object.__setattr__(
            self,
            "observed_boundary_and_evidence_refs",
            tuple(self.observed_boundary_and_evidence_refs),
        )


class TrustedEvidenceAdapter:
    """Attest evidence received from a configured transport or receipt boundary.

    Raw actor-controlled fields are deliberately insufficient to release or
    settle a charge. Callers that ingest asynchronous receipts must do so
    through an adapter owned by their transport integration.
    """

    def __init__(self, source_ref: str):
        if not source_ref:
            raise ValueError("EVIDENCE_SOURCE_REF_REQUIRED")
        self.source_ref = source_ref

    def attest(self, report: TransportReport) -> TransportReport:
        if report.usage.kind == "VerifiedFinal" and not report.usage.receipt_ref:
            raise ValueError("VERIFIED_FINAL_RECEIPT_REQUIRED")
        if report.not_sent_proof_ref and (
            report.completion or report.usage.kind == "VerifiedFinal"
        ):
            raise ValueError("CONTRADICTORY_NOT_SENT_AND_COMPLETION_EVIDENCE")
        return replace(
            report,
            evidence_source_ref=self.source_ref,
            _authority_seal=_TRUSTED_EVIDENCE_SEAL,
        )


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
    execution_state: ExecutionState = ExecutionState.STARTED
    accounting_status: AccountingStatus = AccountingStatus.PENDING


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

    def _save_spec(self, spec: AttemptSpec) -> None:
        """Persist attempt specification to memory and backing store."""
        if hasattr(self.owner, "store") and hasattr(self.owner.store, "set"):
            self.owner.store.set(
                "verified_attempt_specs",
                f"{self.owner.run_id}:{spec.attempt_id}",
                asdict(spec),
                "verified.attempt_spec_persisted",
            )
        self._specs[spec.attempt_id] = spec

    def _load_spec(self, attempt_id: str, *, refresh: bool = False) -> AttemptSpec | None:
        """Load attempt specification from memory cache or backing store."""
        if not refresh and attempt_id in self._specs:
            return self._specs[attempt_id]
        if hasattr(self.owner, "store") and hasattr(self.owner.store, "get"):
            stored = self.owner.store.get("verified_attempt_specs", f"{self.owner.run_id}:{attempt_id}")
            if stored is not None and isinstance(stored, dict):
                spec = AttemptSpec(**stored)
                self._specs[attempt_id] = spec
                return spec
        return None

    def _save_outcome(
        self,
        handle: str,
        outcome: AttemptOutcome,
    ) -> None:
        """Persist an attempt outcome. Evidence is written separately first."""
        if hasattr(self.owner, "store") and hasattr(self.owner.store, "set"):
            outcome_dict = {
                "attempt_id": outcome.attempt_id,
                "execution_state": outcome.execution_state.value if isinstance(outcome.execution_state, Enum) else str(outcome.execution_state),
                "accounting_status": outcome.accounting_status.value if isinstance(outcome.accounting_status, Enum) else str(outcome.accounting_status),
                "validation_outcome": outcome.validation_outcome,
                "confirmed_input": outcome.confirmed_input,
                "confirmed_output": outcome.confirmed_output,
                "held_tokens": outcome.held_tokens,
                "is_no_new_execution": outcome.is_no_new_execution,
                "raw_response": outcome.raw_response,
                "parsed_payload": outcome.parsed_payload,
                "winning_json": outcome.winning_json,
                "compute_meta": outcome.compute_meta,
                "evaluations": outcome.evaluations,
                "error": str(outcome.error) if outcome.error is not None else None,
                "conflict": outcome.conflict,
            }
            self.owner.store.set(
                "verified_attempt_outcomes",
                f"{self.owner.run_id}:{handle}",
                outcome_dict,
                "verified.attempt_outcome_persisted",
            )
        self._outcomes[handle] = outcome

    @staticmethod
    def _report_dict(report: TransportReport) -> dict[str, Any]:
        return {
            "attempt_id": report.attempt_id,
            "completion": report.completion,
            "raw_text": report.raw_text,
            "raw_response_ref": report.raw_response_ref,
            "observed_boundary_and_evidence_refs": report.observed_boundary_and_evidence_refs,
            "usage": asdict(report.usage),
            "not_sent_proof_ref": report.not_sent_proof_ref,
            "error_ref": report.error_ref,
            "stop_reason": report.stop_reason,
            "evidence_source_ref": report.evidence_source_ref,
        }

    def _persist_evidence(
        self,
        handle: str,
        report: TransportReport,
    ) -> str:
        """Append immutable raw evidence without changing recovery selection."""
        rep_dict = self._report_dict(report)
        evidence_ref = digest(rep_dict)
        if hasattr(self.owner, "store") and hasattr(self.owner.store, "set"):
            self.owner.store.set(
                "verified_attempt_evidence_items",
                f"{self.owner.run_id}:{handle}:{evidence_ref}",
                rep_dict,
                "verified.attempt_evidence_item_persisted",
            )
        return evidence_ref

    def _promote_evidence(self, handle: str, report: TransportReport) -> None:
        """Select evidence that may drive recovery validation.

        Raw evidence is always retained by ``_persist_evidence``.  This pointer
        advances only after the active lifecycle authority accepts the matching
        transition, so a duplicate, conflicting, or late status notice cannot
        displace the response that settled the attempt.
        """
        if hasattr(self.owner, "store") and hasattr(self.owner.store, "set"):
            self.owner.store.set(
                "verified_attempt_evidence",
                f"{self.owner.run_id}:{handle}",
                self._report_dict(report),
                "verified.attempt_evidence_persisted",
            )

    def _quarantine_evidence(
        self, handle: str, report: TransportReport, reason: str
    ) -> str:
        with self._store_transaction():
            evidence_ref = self._persist_evidence(handle, report)
            self.owner.store.set(
                "verified_untrusted_evidence",
                f"{self.owner.run_id}:{handle}:{evidence_ref}",
                {
                    "run_id": self.owner.run_id,
                    "handle": handle,
                    "reported_attempt_id": report.attempt_id,
                    "evidence_ref": evidence_ref,
                    "reason": reason,
                },
                "verified.untrusted_evidence_quarantined",
            )
            return evidence_ref

    def _load_evidence(self, handle: str) -> TransportReport | None:
        if not hasattr(self.owner, "store") or not hasattr(self.owner.store, "get"):
            return None
        stored = self.owner.store.get(
            "verified_attempt_evidence", f"{self.owner.run_id}:{handle}"
        )
        if not isinstance(stored, dict):
            return None
        return TransportReport(
            attempt_id=stored["attempt_id"],
            observed_boundary_and_evidence_refs=stored.get(
                "observed_boundary_and_evidence_refs", []
            ),
            completion=stored.get("completion", False),
            usage=UsageReport(**stored.get("usage", {"kind": "Unknown"})),
            raw_response_ref=stored.get("raw_response_ref"),
            raw_text=stored.get("raw_text"),
            stop_reason=stored.get("stop_reason"),
            error_ref=stored.get("error_ref"),
            not_sent_proof_ref=stored.get("not_sent_proof_ref"),
            evidence_source_ref=stored.get("evidence_source_ref"),
            _authority_seal=_TRUSTED_EVIDENCE_SEAL,
        )

    @staticmethod
    def _validation_dict(report: ValidationReport) -> dict[str, Any]:
        parsed = report.parsed_payload
        if hasattr(parsed, "model_dump"):
            parsed = parsed.model_dump(mode="json")
        return {
            "outcome": report.outcome.value,
            "reason": report.reason,
            "parsed_payload": parsed,
            "winning_json": report.winning_json,
            "compute_meta": report.compute_meta,
            "evaluations": report.evaluations,
        }

    def _load_validation(self, handle: str) -> ValidationReport | None:
        if not hasattr(self.owner, "store") or not hasattr(self.owner.store, "get"):
            return None
        stored = self.owner.store.get(
            "verified_attempt_validations", f"{self.owner.run_id}:{handle}"
        )
        if not isinstance(stored, dict):
            return None
        return ValidationReport(
            outcome=ValidationOutcome(stored["outcome"]),
            reason=stored.get("reason"),
            parsed_payload=stored.get("parsed_payload"),
            winning_json=stored.get("winning_json"),
            compute_meta=stored.get("compute_meta"),
            evaluations=stored.get("evaluations"),
        )

    @contextmanager
    def _store_transaction(self):
        if hasattr(self.owner, "_store_transaction"):
            with self.owner._store_transaction():
                yield
        else:
            yield

    def _recover_after_rollback(self) -> None:
        self._specs.clear()
        self._outcomes.clear()
        self._execution_states.clear()
        if hasattr(self.owner, "reload_from_store"):
            self.owner.reload_from_store()
        self._sync_from_owner()

    def _load_outcome(
        self, handle: str, *, allow_fallback: bool = True
    ) -> AttemptOutcome | None:
        """Load the durable outcome before consulting the process-local cache.

        Trusted evidence may be reconciled by another owner process. The store
        is therefore authoritative whenever it is available; an unversioned
        in-memory result must never hide a newer persisted settlement.
        """
        if hasattr(self.owner, "store") and hasattr(self.owner.store, "get"):
            stored = self.owner.store.get("verified_attempt_outcomes", f"{self.owner.run_id}:{handle}")
            if stored is not None and isinstance(stored, dict):
                outcome = AttemptOutcome(
                    attempt_id=stored["attempt_id"],
                    execution_state=ExecutionState(stored["execution_state"]),
                    accounting_status=AccountingStatus(stored["accounting_status"]),
                    validation_outcome=stored["validation_outcome"],
                    confirmed_input=stored.get("confirmed_input", 0),
                    confirmed_output=stored.get("confirmed_output", 0),
                    held_tokens=stored.get("held_tokens", 0),
                    is_no_new_execution=stored.get("is_no_new_execution", False),
                    raw_response=stored.get("raw_response"),
                    parsed_payload=stored.get("parsed_payload"),
                    winning_json=stored.get("winning_json"),
                    compute_meta=stored.get("compute_meta"),
                    evaluations=stored.get("evaluations"),
                    error=(
                        RuntimeError(stored["error"])
                        if stored.get("error") is not None
                        else None
                    ),
                    conflict=stored.get("conflict"),
                )
                self._outcomes[handle] = outcome
                return outcome

            if not allow_fallback:
                return None

            # Fallback to evidence and request record
            ev_stored = self.owner.store.get("verified_attempt_evidence", f"{self.owner.run_id}:{handle}")
            rec = self.owner.get_request_record(handle)
            if rec is not None:
                raw_resp = ev_stored.get("raw_text") if isinstance(ev_stored, dict) else None
                charge_kind = getattr(rec.charge, "kind", getattr(rec.charge, "value", str(rec.charge)))
                if charge_kind in ("Settled", "SETTLED"):
                    acct_st = AccountingStatus.SETTLED
                    exec_st = ExecutionState.FINISHED
                    conf_in = getattr(rec.charge, "input_tokens", 0) or 0
                    conf_out = getattr(rec.charge, "output_tokens", 0) or 0
                    held = 0
                elif charge_kind in ("Released", "RELEASED"):
                    acct_st = AccountingStatus.RELEASED
                    exec_st = ExecutionState.NOT_SENT
                    conf_in = 0
                    conf_out = 0
                    held = 0
                else:
                    acct_st = AccountingStatus.PENDING
                    exec_st = ExecutionState.STARTED
                    conf_in = 0
                    conf_out = 0
                    held = getattr(rec.charge, "tokens", 0) or 0

                outcome = AttemptOutcome(
                    attempt_id=handle,
                    execution_state=exec_st,
                    accounting_status=acct_st,
                    validation_outcome="NO_NEW_EXECUTION",
                    confirmed_input=conf_in,
                    confirmed_output=conf_out,
                    held_tokens=held,
                    is_no_new_execution=True,
                    raw_response=raw_resp,
                )
                self._outcomes[handle] = outcome
                return outcome

        return self._outcomes.get(handle)

    def _outcome_from_record(
        self,
        handle: str,
        record: Any,
        existing: AttemptOutcome | None,
        *,
        raw_response: str | None = None,
        conflict: str | None = None,
    ) -> AttemptOutcome:
        """Project the active authority's request record into a gateway outcome."""
        charge_kind = getattr(
            record.charge,
            "kind",
            getattr(record.charge, "value", str(record.charge)),
        )
        if isinstance(charge_kind, Enum):
            charge_kind = charge_kind.value

        if charge_kind in ("Settled", "SETTLED"):
            accounting_status = AccountingStatus.SETTLED
            confirmed_input = getattr(record.charge, "input_tokens", 0) or 0
            confirmed_output = getattr(record.charge, "output_tokens", 0) or 0
            held_tokens = 0
        elif charge_kind in ("Released", "RELEASED"):
            accounting_status = AccountingStatus.RELEASED
            confirmed_input = 0
            confirmed_output = 0
            held_tokens = 0
        else:
            accounting_status = AccountingStatus.PENDING
            confirmed_input = 0
            confirmed_output = 0
            held_tokens = getattr(record.charge, "tokens", 0) or 0

        if existing is None:
            existing = AttemptOutcome(
                attempt_id=handle,
                execution_state=self._execution_state_for_record(record),
                accounting_status=accounting_status,
                validation_outcome="UNKNOWN",
            )
        return replace(
            existing,
            execution_state=self._execution_state_for_record(record),
            accounting_status=accounting_status,
            confirmed_input=confirmed_input,
            confirmed_output=confirmed_output,
            held_tokens=held_tokens,
            raw_response=(
                raw_response if raw_response is not None else existing.raw_response
            ),
            conflict=conflict if conflict is not None else existing.conflict,
        )

    def _sync_from_owner(self) -> None:
        """Hydrate Gateway known attempts from the underlying store / owner."""
        if getattr(self.owner, "mode", None) == "SHADOW" or getattr(
            getattr(self.owner, "mode", None), "value", None
        ) == "SHADOW":
            self.owner.legacy_ledger.refresh()
            requests = list(self.owner.legacy_ledger.state.attempts.values())
        else:
            bend_state = getattr(self.owner, "bend_state", None)
            requests = list(getattr(bend_state, "requests", []))
        for req in requests:
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

    @staticmethod
    def _execution_state_for_record(rec: Any) -> ExecutionState:
        if rec.transport == TransportState.PREPARED:
            return ExecutionState.READY
        if rec.transport in (
            TransportState.DISPATCHED_INTENT,
            TransportState.SENT,
            TransportState.OUTCOME_UNKNOWN,
        ):
            return ExecutionState.STARTED
        if rec.transport == TransportState.PROVEN_NOT_SENT:
            return ExecutionState.NOT_SENT
        return ExecutionState.FINISHED

    def prepare(self, spec: AttemptSpec) -> PrepareVerdict:
        """Atomically persist binding and reservation; become READY (§3)."""
        if spec.run_id != self.owner.run_id:
            return PrepareConflict(
                f"RUN_IDENTITY_MISMATCH: spec run_id {spec.run_id!r} does not match "
                f"active run {self.owner.run_id!r}"
            )
        with self._lock:
            try:
                with self._store_transaction():
                    existing_spec = self._load_spec(spec.attempt_id, refresh=True)
                    existing_req = self.owner.get_request_record(spec.attempt_id)
                    if existing_spec is not None:
                        if existing_spec.binding_hash != spec.binding_hash:
                            return PrepareConflict(
                                f"IDENTITY_CONFLICT: attempt {spec.attempt_id} already "
                                "bound with different parameters"
                            )
                        exec_state = (
                            self._execution_state_for_record(existing_req)
                            if existing_req is not None
                            else self._execution_states.get(
                                spec.attempt_id, ExecutionState.READY
                            )
                        )
                        self._execution_states[spec.attempt_id] = exec_state
                        return PrepareExisting(
                            handle=spec.attempt_id,
                            spec=existing_spec,
                            execution_state=exec_state,
                            outcome=self._load_outcome(spec.attempt_id),
                        )

                    # A reservation without its full immutable binding is unsafe
                    # to replay after a crash. Never reconstruct omitted fields.
                    if existing_req is not None:
                        return PrepareConflict(
                            f"IDENTITY_BINDING_MISSING: request {spec.attempt_id} has "
                            "a reservation but no complete AttemptSpec"
                        )

                    if self.owner.active_fault:
                        return PrepareRejected(
                            f"STATE_FAULT_LATCHED: {self.owner.active_fault}"
                        )

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
                        return PrepareRejected(
                            str(getattr(v_res, "reason", "RESERVATION_REJECTED"))
                        )
                    if res_kind in (
                        VerdictKind.CONFLICT_FAULT,
                        "ConflictFault",
                        "CONFLICT_FAULT",
                    ):
                        return PrepareConflict(
                            str(getattr(v_res, "reason", "RESERVATION_CONFLICT"))
                        )
                    if res_kind in (
                        VerdictKind.DUPLICATE_NOOP,
                        "DuplicateNoop",
                        "DUPLICATE_NOOP",
                    ):
                        # This can only be a legacy incomplete write because the
                        # full spec was absent at the start of this transaction.
                        return PrepareConflict(
                            f"IDENTITY_BINDING_MISSING: request {spec.attempt_id} "
                            "already exists without its complete AttemptSpec"
                        )

                    self._save_spec(spec)
                    self._execution_states[spec.attempt_id] = ExecutionState.READY
                    return PrepareReady(handle=spec.attempt_id, spec=spec)
            except BudgetExhausted as exc:
                self._recover_after_rollback()
                return PrepareRejected(str(exc))
            except Exception:
                self._recover_after_rollback()
                raise

    def claim_start(self, handle: str) -> ClaimStartVerdict:
        """Atomically become STARTED; return one live, one-shot permit (§3)."""
        with self._lock:
            exec_state = self._execution_states.get(handle)
            if exec_state is None:
                # Check underlying owner
                rec = self.owner.get_request_record(handle)
                if rec is None:
                    return ClaimNoGrant(reason="ATTEMPT_NOT_PREPARED")
                exec_state = self._execution_state_for_record(rec)
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
            self.owner._defer_shadow_observation = True
            try:
                v_disp = self.owner.record_dispatched(stage=stage, req_id=handle)
            finally:
                self.owner._defer_shadow_observation = False
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

    def trusted_evidence_adapter(self, source_ref: str) -> TrustedEvidenceAdapter:
        """Construct the adapter owned by a configured evidence integration."""
        return TrustedEvidenceAdapter(source_ref)

    @staticmethod
    def _attest_transport_result(
        report: TransportReport, permit: OneShotPermit
    ) -> TransportReport:
        """Attest a report observed directly at the gateway transport boundary."""
        usage = report.usage
        if not permit.is_consumed and report.not_sent_proof_ref is None:
            report = replace(
                report,
                not_sent_proof_ref=digest(
                    {
                        "attempt_id": permit.attempt_id,
                        "permit_id": permit.permit_id,
                        "fact": "transport_returned_before_permit_consumption",
                    }
                ),
            )
        if usage.kind == "VerifiedFinal" and usage.receipt_ref is None:
            usage = replace(
                usage,
                receipt_ref=digest(
                    {
                        "attempt_id": report.attempt_id,
                        "permit_id": permit.permit_id,
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "raw_response_ref": report.raw_response_ref,
                        "observed_refs": report.observed_boundary_and_evidence_refs,
                    }
                ),
            )
        return replace(
            report,
            usage=usage,
            evidence_source_ref="gateway_transport_boundary",
            _authority_seal=_TRUSTED_EVIDENCE_SEAL,
        )

    def record_evidence(
        self, handle: str, report: TransportReport
    ) -> RecordEvidenceVerdict:
        """Record transport evidence and settle or hold pending cost (§3, §4, §5)."""
        with self._lock:
            if report.attempt_id != handle:
                self._quarantine_evidence(handle, report, "IDENTITY_MISMATCH")
                return RecordEvidenceConflict(
                    f"IDENTITY_MISMATCH: report attempt_id {report.attempt_id!r} does not match handle {handle!r}"
                )
            if report._authority_seal is not _TRUSTED_EVIDENCE_SEAL:
                self._quarantine_evidence(
                    handle, report, "UNTRUSTED_EVIDENCE_SOURCE"
                )
                return RecordEvidenceConflict("UNTRUSTED_EVIDENCE_SOURCE")
            if report.not_sent_proof_ref and (
                report.completion or report.usage.kind == "VerifiedFinal"
            ):
                self._quarantine_evidence(
                    handle,
                    report,
                    "CONTRADICTORY_NOT_SENT_AND_COMPLETION_EVIDENCE",
                )
                return RecordEvidenceConflict(
                    "CONTRADICTORY_NOT_SENT_AND_COMPLETION_EVIDENCE"
                )
            if report.usage.kind == "VerifiedFinal" and not report.usage.receipt_ref:
                self._quarantine_evidence(
                    handle, report, "VERIFIED_FINAL_RECEIPT_REQUIRED"
                )
                return RecordEvidenceConflict("VERIFIED_FINAL_RECEIPT_REQUIRED")

            try:
                with self._store_transaction():
                    spec = self._load_spec(handle, refresh=True)
                    rec = self.owner.get_request_record(handle)
                    stage = spec.stage if spec else (rec.stage if rec is not None else "stage1")
                    evidence_ref = self._persist_evidence(handle, report)

                    if report.not_sent_proof_ref is not None and not report.completion:
                        permit = self._permits.get(handle)
                        if permit is not None and permit.is_consumed:
                            return RecordEvidenceConflict(
                                "NOT_SENT_PROOF_CONTRADICTS_CONSUMED_PERMIT"
                            )
                        rel_tokens = spec.declared_reservation_charge if spec else 0
                        if rel_tokens == 0 and rec is not None:
                            rel_tokens = getattr(rec.charge, "tokens", 0) or 0
                        verdict = self.owner.record_failed(
                            stage=stage,
                            reservation_tokens=rel_tokens,
                            req_id=handle,
                            evidence_hash=report.not_sent_proof_ref,
                        )
                        kind = getattr(verdict, "value", verdict)
                        if kind in ("ConflictFault", "Rejected"):
                            return RecordEvidenceConflict(
                                "NOT_SENT_RELEASE_CONFLICT",
                                execution_state=self._execution_state_for_record(
                                    self.owner.get_request_record(handle)
                                ),
                            )
                        if kind != "DuplicateNoop":
                            self._promote_evidence(handle, report)
                        self._execution_states[handle] = ExecutionState.NOT_SENT
                        outcome = AttemptOutcome(
                            attempt_id=handle,
                            execution_state=ExecutionState.NOT_SENT,
                            accounting_status=AccountingStatus.RELEASED,
                            validation_outcome=ValidationOutcome.NOT_ATTEMPTED.value,
                        )
                        self._save_outcome(handle, outcome)
                        return RecordEvidenceRecorded(
                            ExecutionState.NOT_SENT, AccountingStatus.RELEASED
                        )

                    if report.usage.kind == "VerifiedFinal":
                        receipt_ref = report.usage.receipt_ref
                        assert receipt_ref is not None
                        res_tokens = spec.declared_reservation_charge if spec else 0
                        if res_tokens == 0 and rec is not None:
                            res_tokens = getattr(rec.charge, "tokens", 0) or 0
                        verdict = self.owner.record_completed(
                            stage=stage,
                            input_tokens=report.usage.input_tokens,
                            output_tokens=report.usage.output_tokens,
                            reservation_tokens=res_tokens,
                            req_id=handle,
                            receipt_hash=receipt_ref,
                        )
                        kind = getattr(verdict, "value", verdict)
                        existing_out = self._load_outcome(
                            handle, allow_fallback=False
                        )
                        settled_rec = self.owner.get_request_record(handle)
                        conflict = existing_out.conflict if existing_out else None
                        if kind == "ConflictFault":
                            conflict = self.owner.active_fault or "EVIDENCE_CONFLICT"
                        elif kind == "Rejected":
                            conflict = "EVIDENCE_REJECTED"
                        if settled_rec is None:
                            return RecordEvidenceConflict(
                                conflict or "ATTEMPT_NOT_FOUND"
                            )
                        if kind not in ("DuplicateNoop", "ConflictFault", "Rejected"):
                            self._promote_evidence(handle, report)
                        updated_out = self._outcome_from_record(
                            handle,
                            settled_rec,
                            existing_out,
                            raw_response=report.raw_text,
                            conflict=conflict,
                        )
                        self._execution_states[handle] = updated_out.execution_state
                        self._save_outcome(handle, updated_out)
                        if kind == "DuplicateNoop":
                            return RecordEvidenceDuplicate(retained_ref=receipt_ref)
                        if kind in ("ConflictFault", "Rejected"):
                            return RecordEvidenceConflict(
                                conflict or "EVIDENCE_CONFLICT",
                                execution_state=updated_out.execution_state,
                                accounting_status=updated_out.accounting_status,
                            )
                        return RecordEvidenceRecorded(
                            updated_out.execution_state,
                            updated_out.accounting_status,
                        )

                    verdict = self.owner.record_timeout(
                        stage=stage,
                        req_id=handle,
                        reason=report.error_ref or "OUTCOME_UNKNOWN_HELD_PENDING",
                    )
                    kind = getattr(verdict, "value", verdict)
                    existing_out = self._load_outcome(
                        handle, allow_fallback=False
                    )
                    current_rec = self.owner.get_request_record(handle)
                    if current_rec is None:
                        return RecordEvidenceConflict("ATTEMPT_NOT_FOUND")
                    conflict = existing_out.conflict if existing_out else None
                    if kind in ("ConflictFault", "Rejected"):
                        conflict = self.owner.active_fault or "EVIDENCE_CONFLICT"
                    if kind not in ("DuplicateNoop", "ConflictFault", "Rejected"):
                        self._promote_evidence(handle, report)
                    updated_out = self._outcome_from_record(
                        handle,
                        current_rec,
                        existing_out,
                        raw_response=report.raw_text,
                        conflict=conflict,
                    )
                    self._execution_states[handle] = updated_out.execution_state
                    self._save_outcome(handle, updated_out)
                    if kind == "DuplicateNoop":
                        return RecordEvidenceDuplicate(retained_ref=evidence_ref)
                    if kind in ("ConflictFault", "Rejected"):
                        return RecordEvidenceConflict(
                            conflict or "EVIDENCE_CONFLICT",
                            execution_state=updated_out.execution_state,
                            accounting_status=updated_out.accounting_status,
                        )
                    return RecordEvidenceRecorded(
                        updated_out.execution_state,
                        updated_out.accounting_status,
                    )
            except Exception:
                self._recover_after_rollback()
                raise
            finally:
                if getattr(getattr(self.owner, "mode", None), "value", None) == "SHADOW":
                    try:
                        self.owner._drain_shadow_observations()
                    except Exception:
                        # Observer persistence is separate from the committed
                        # active evidence/accounting transaction.
                        pass

    def record_validation(
        self, handle: str, report: ValidationReport
    ) -> RecordValidationVerdict:
        """Record decision validation. CANNOT rewrite execution history or its cost (§1, §9)."""
        with self._lock:
            if self.owner.get_request_record(handle) is None:
                return RecordValidationConflict("ATTEMPT_NOT_FOUND")
            payload = self._validation_dict(report)
            key = f"{self.owner.run_id}:{handle}"
            try:
                with self._store_transaction():
                    existing = self.owner.store.get(
                        "verified_attempt_validations", key
                    )
                    if isinstance(existing, dict):
                        if digest(existing) == digest(payload):
                            return RecordValidationDuplicate(retained_ref=key)
                        return RecordValidationConflict(
                            "CONFLICTING_VALIDATION_REPORT"
                        )
                    if hasattr(self.owner, "record_validation"):
                        self.owner.record_validation(handle, report.outcome.value)
                    self.owner.store.set(
                        "verified_attempt_validations",
                        key,
                        payload,
                        "verified.attempt_validation_persisted",
                    )
                    current = self._load_outcome(handle)
                    if current is not None:
                        self._save_outcome(
                            handle,
                            replace(
                                current,
                                validation_outcome=report.outcome.value,
                                parsed_payload=report.parsed_payload,
                                winning_json=report.winning_json,
                                compute_meta=report.compute_meta,
                                evaluations=report.evaluations,
                                error=(
                                    RuntimeError(report.reason)
                                    if report.reason
                                    else None
                                ),
                            ),
                        )
                    return RecordValidationRecorded(
                        validation_outcome=report.outcome.value
                    )
            except Exception:
                self._recover_after_rollback()
                raise
            finally:
                if getattr(getattr(self.owner, "mode", None), "value", None) == "SHADOW":
                    try:
                        self.owner._drain_shadow_observations()
                    except Exception:
                        pass

    def _finalize_validation_outcome(
        self,
        handle: str,
        proposed_validation: ValidationReport,
        fallback_outcome: AttemptOutcome,
        *,
        is_no_new_execution: bool | None = None,
    ) -> AttemptOutcome:
        """Project canonical validation onto the latest durable outcome.

        Validation runs outside the Store transaction. While it is running,
        another owner may commit newer accounting or conflict state, or win the
        immutable validation write. Re-read both records under one write
        transaction before merging so finalization cannot overwrite either.
        """
        with self._lock:
            with self._store_transaction():
                canonical_validation = self._load_validation(handle)
                validation = canonical_validation or proposed_validation
                current_outcome = self._load_outcome(
                    handle, allow_fallback=False
                ) or fallback_outcome
                outcome = replace(
                    current_outcome,
                    validation_outcome=validation.outcome.value,
                    parsed_payload=validation.parsed_payload,
                    winning_json=validation.winning_json,
                    compute_meta=validation.compute_meta,
                    evaluations=validation.evaluations,
                    error=(
                        RuntimeError(validation.reason)
                        if validation.reason
                        else None
                    ),
                    is_no_new_execution=(
                        current_outcome.is_no_new_execution
                        if is_no_new_execution is None
                        else is_no_new_execution
                    ),
                    conflict=current_outcome.conflict or fallback_outcome.conflict,
                )
                self._save_outcome(handle, outcome)
                return outcome

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
                if (
                    prep.execution_state == ExecutionState.FINISHED
                    and prep.outcome is not None
                    and prep.outcome.validation_outcome == "UNKNOWN"
                ):
                    val_report = self._load_validation(spec.attempt_id)
                    retained_report = self._load_evidence(spec.attempt_id)
                    if val_report is None and retained_report is not None:
                        try:
                            val_report = validator(retained_report)
                        except Exception as val_exc:
                            val_report = ValidationReport(
                                outcome=ValidationOutcome.REJECTED,
                                reason=(
                                    f"VALIDATOR_EXCEPTION:{type(val_exc).__name__}:"
                                    f"{val_exc}"
                                ),
                            )
                        self.record_validation(spec.attempt_id, val_report)
                    if val_report is not None:
                        return self._finalize_validation_outcome(
                            spec.attempt_id,
                            val_report,
                            prep.outcome,
                            is_no_new_execution=True,
                        )
                # Return retained result without new call or charge (§3, §8 A02)
                if prep.outcome is not None:
                    return replace(prep.outcome, is_no_new_execution=True)
                rec = self.owner.get_request_record(spec.attempt_id)
                charge_kind = getattr(rec.charge, "kind", getattr(rec.charge, "value", str(rec.charge))) if rec else ""
                conf_in = getattr(rec.charge, "input_tokens", 0) or 0 if rec and charge_kind in ("Settled", "SETTLED") else 0
                conf_out = getattr(rec.charge, "output_tokens", 0) or 0 if rec and charge_kind in ("Settled", "SETTLED") else 0
                acct_st = (
                    AccountingStatus.SETTLED
                    if prep.execution_state == ExecutionState.FINISHED
                    else AccountingStatus.RELEASED
                )
                return AttemptOutcome(
                    attempt_id=spec.attempt_id,
                    execution_state=prep.execution_state,
                    accounting_status=acct_st,
                    validation_outcome="NO_NEW_EXECUTION",
                    confirmed_input=conf_in,
                    confirmed_output=conf_out,
                    held_tokens=0,
                    is_no_new_execution=True,
                )
            if prep.execution_state == ExecutionState.STARTED:
                if prep.outcome is not None:
                    return replace(prep.outcome, is_no_new_execution=True)
                return AttemptOutcome(
                    attempt_id=spec.attempt_id,
                    execution_state=ExecutionState.STARTED,
                    accounting_status=AccountingStatus.PENDING,
                    validation_outcome="ALREADY_IN_PROGRESS",
                    held_tokens=spec.declared_reservation_charge,
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
        transport_report = self._attest_transport_result(transport_report, permit)

        # Step 4: Record Evidence & Settle Accounting
        ev_result = self.record_evidence(spec.attempt_id, transport_report)
        evidence_outcome = self._load_outcome(
            spec.attempt_id, allow_fallback=False
        )
        acct_status = (
            evidence_outcome.accounting_status
            if evidence_outcome is not None
            else getattr(ev_result, "accounting_status", AccountingStatus.PENDING)
        )
        exec_state = (
            evidence_outcome.execution_state
            if evidence_outcome is not None
            else getattr(ev_result, "execution_state", ExecutionState.STARTED)
        )
        evidence_conflict = (
            ev_result.reason if isinstance(ev_result, RecordEvidenceConflict) else None
        )

        # Compute confirmed usage
        conf_in = 0
        conf_out = 0
        held = 0
        if evidence_outcome is not None:
            conf_in = evidence_outcome.confirmed_input
            conf_out = evidence_outcome.confirmed_output
            held = evidence_outcome.held_tokens
        elif acct_status == AccountingStatus.SETTLED:
            conf_in = transport_report.usage.input_tokens
            conf_out = transport_report.usage.output_tokens
        elif acct_status == AccountingStatus.PENDING:
            held = spec.declared_reservation_charge

        # Step 5: Record Validation (Decoupled from Accounting)
        # Any validator exception preserves settled accounting and produces a standardized rejection outcome (§4, §7)
        try:
            val_report = validator(transport_report)
        except Exception as val_exc:
            val_report = ValidationReport(
                outcome=ValidationOutcome.REJECTED,
                reason=f"VALIDATOR_EXCEPTION:{type(val_exc).__name__}:{val_exc}",
            )

        self.record_validation(spec.attempt_id, val_report)
        fallback_outcome = AttemptOutcome(
            attempt_id=spec.attempt_id,
            execution_state=exec_state,
            accounting_status=acct_status,
            validation_outcome="UNKNOWN",
            confirmed_input=conf_in,
            confirmed_output=conf_out,
            held_tokens=held,
            raw_response=transport_report.raw_text,
            conflict=evidence_conflict,
        )
        return self._finalize_validation_outcome(
            spec.attempt_id,
            val_report,
            fallback_outcome,
        )
