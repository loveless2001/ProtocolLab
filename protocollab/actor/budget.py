"""Persistent multi-stage diagnostic budget ledger.

Enforces stage-specific sub-budgets and aggregate diagnostic limits without
allowing stage transitions or process restarts to replenish allocations.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from protocollab.learning import BudgetExhausted

MAX_SAFE_INT = 9_007_199_254_740_991


class StageBudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    max_calls: int = Field(gt=0)
    max_tokens: int = Field(gt=0)


class DiagnosticBudgetAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    aggregate_max_calls: int = Field(gt=0)
    aggregate_max_tokens: int = Field(gt=0)
    stages: dict[str, StageBudgetLimits] = Field(default_factory=dict)


class StageUsageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    calls_attempted: int = 0
    reservations: int = 0
    dispatched_inference: int = 0
    completed_calls: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reserved_tokens: int = 0


class DiagnosticCharge(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: str
    tokens: int | None = Field(default=None, ge=0, le=MAX_SAFE_INT)
    input_tokens: int | None = Field(default=None, ge=0, le=MAX_SAFE_INT)
    output_tokens: int | None = Field(default=None, ge=0, le=MAX_SAFE_INT)
    receipt_hash: str | None = None
    evidence_hash: str | None = None


class DiagnosticAttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    req_id: str
    stage: str
    basis_ref: str
    config_hash: str
    max_input: int = Field(ge=0, le=MAX_SAFE_INT)
    max_output: int = Field(ge=0, le=MAX_SAFE_INT)
    transport: str
    charge: DiagnosticCharge


class DiagnosticLedgerState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    config_hash: str
    allocation: DiagnosticBudgetAllocation
    stages: dict[str, StageUsageRecord] = Field(default_factory=dict)
    aggregate: StageUsageRecord = Field(default_factory=StageUsageRecord)
    attempts: dict[str, DiagnosticAttemptRecord] = Field(default_factory=dict)
    fault: str | None = None


class DiagnosticLedger:
    """Persistent, audited ledger across diagnostic stages."""

    def __init__(self, store: Any, run_id: str, allocation: DiagnosticBudgetAllocation, config_hash: str):
        self.store = store
        self.run_id = run_id
        stored = store.get("diagnostic_ledger", run_id)
        if stored is None:
            state = DiagnosticLedgerState(
                run_id=run_id,
                config_hash=config_hash,
                allocation=allocation,
                stages={s: StageUsageRecord() for s in allocation.stages},
                aggregate=StageUsageRecord(),
            )
            store.set("diagnostic_ledger", run_id, state.model_dump(), "diagnostic.budget_initialized")
            self.state = state
        else:
            state = DiagnosticLedgerState.model_validate(stored)
            if state.config_hash != config_hash:
                raise ValueError("DIAGNOSTIC_CONFIGURATION_CHANGED")
            if state.allocation.model_dump() != allocation.model_dump():
                raise ValueError("DIAGNOSTIC_BUDGET_ALLOCATION_CHANGED")
            self.state = state
        self._seen_req_ids: set[str] = set(self.state.attempts)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if hasattr(self.store, "transaction"):
            with self.store.transaction():
                yield
        elif hasattr(self.store, "lock"):
            with self.store.lock:
                yield
        else:
            yield

    def refresh(self) -> None:
        stored = self.store.get("diagnostic_ledger", self.run_id)
        if stored is not None:
            self.state = DiagnosticLedgerState.model_validate(stored)
            self._seen_req_ids = set(self.state.attempts)

    def _sync(self, event_kind: str = "diagnostic.budget_updated"):
        self.store.set("diagnostic_ledger", self.run_id, self.state.model_dump(), event_kind)

    def _recompute_attempt_usage(self) -> None:
        """Project accounting counters from durable per-attempt records."""
        attempted_by_stage = {
            name: usage.calls_attempted for name, usage in self.state.stages.items()
        }
        aggregate_attempted = self.state.aggregate.calls_attempted
        self.state.stages = {
            name: StageUsageRecord(calls_attempted=attempted_by_stage.get(name, 0))
            for name in self.state.allocation.stages
        }
        self.state.aggregate = StageUsageRecord(calls_attempted=aggregate_attempted)
        for rec in self.state.attempts.values():
            usage = self.state.stages.setdefault(rec.stage, StageUsageRecord())
            aggregate = self.state.aggregate
            usage.reservations += 1
            aggregate.reservations += 1
            if rec.transport in (
                "DispatchedIntent",
                "Sent",
                "ResponseReceived",
                "OutcomeUnknown",
            ):
                usage.dispatched_inference += 1
                aggregate.dispatched_inference += 1
            if rec.charge.kind == "Pending":
                held = rec.charge.tokens or 0
                usage.reserved_tokens += held
                aggregate.reserved_tokens += held
            elif rec.charge.kind == "Settled":
                usage.completed_calls += 1
                aggregate.completed_calls += 1
                usage.input_tokens += rec.charge.input_tokens or 0
                usage.output_tokens += rec.charge.output_tokens or 0
                aggregate.input_tokens += rec.charge.input_tokens or 0
                aggregate.output_tokens += rec.charge.output_tokens or 0
            elif rec.charge.kind == "Released":
                usage.failures += 1
                aggregate.failures += 1

    def record_admission_attempt(self, stage: str):
        exhausted_reason: str | None = None
        with self._transaction():
            self.refresh()
            stage_limits = self.state.allocation.stages.get(stage)
            stage_usage = self.state.stages.get(stage)
            agg_usage = self.state.aggregate
            if stage_limits is None or stage_usage is None:
                raise ValueError(f"UNDECLARED_DIAGNOSTIC_STAGE: {stage}")

            stage_usage.calls_attempted += 1
            agg_usage.calls_attempted += 1
            self._sync("diagnostic.admission_attempted")

            if stage_usage.calls_attempted > stage_limits.max_calls:
                exhausted_reason = f"stage_{stage}_max_calls_exceeded"
            elif agg_usage.calls_attempted > self.state.allocation.aggregate_max_calls:
                exhausted_reason = "aggregate_diagnostic_max_calls_exceeded"
        if exhausted_reason:
            raise BudgetExhausted(exhausted_reason)

    def reserve(
        self,
        stage: str,
        reservation_tokens: int,
        req_id: str | None = None,
        basis_ref: str = "",
        max_input: int | None = None,
        max_output: int | None = None,
        *args: Any,
        **kwargs: Any,
    ):
        with self._transaction():
            self.refresh()
            if req_id is not None:
                self._recompute_attempt_usage()
            if self.state.fault:
                raise BudgetExhausted(f"STATE_FAULT_LATCHED:{self.state.fault}")

            stage_limits = self.state.allocation.stages.get(stage)
            stage_usage = self.state.stages.get(stage)
            agg_usage = self.state.aggregate
            if stage_limits is None or stage_usage is None:
                raise ValueError(f"UNDECLARED_DIAGNOSTIC_STAGE: {stage}")

            if req_id is not None and req_id in self.state.attempts:
                prior = self.state.attempts[req_id]
                if (
                    prior.stage == stage
                    and prior.basis_ref == basis_ref
                    and prior.config_hash == self.state.config_hash
                    and prior.max_input == (max_input if max_input is not None else reservation_tokens // 2)
                    and prior.max_output == (max_output if max_output is not None else reservation_tokens - reservation_tokens // 2)
                ):
                    return "DuplicateNoop"
                self.state.fault = "ATTEMPT_IDENTITY_CONFLICT"
                self._sync("diagnostic.attempt_identity_conflict")
                return "ConflictFault"

            stage_attempts = sum(1 for rec in self.state.attempts.values() if rec.stage == stage)
            if req_id is not None and stage_attempts >= stage_limits.max_calls:
                raise BudgetExhausted(f"stage_{stage}_max_calls_exceeded")
            if req_id is not None and len(self.state.attempts) >= self.state.allocation.aggregate_max_calls:
                raise BudgetExhausted("aggregate_diagnostic_max_calls_exceeded")
            if (
                stage_usage.input_tokens
                + stage_usage.output_tokens
                + stage_usage.reserved_tokens
                + reservation_tokens
                > stage_limits.max_tokens
            ):
                raise BudgetExhausted(f"stage_{stage}_token_reservation_exceeded")
            if (
                agg_usage.input_tokens
                + agg_usage.output_tokens
                + agg_usage.reserved_tokens
                + reservation_tokens
                > self.state.allocation.aggregate_max_tokens
            ):
                raise BudgetExhausted("aggregate_diagnostic_token_reservation_exceeded")

            if req_id is not None:
                mi = max_input if max_input is not None else reservation_tokens // 2
                mo = max_output if max_output is not None else reservation_tokens - mi
                self.state.attempts[req_id] = DiagnosticAttemptRecord(
                    req_id=req_id,
                    stage=stage,
                    basis_ref=basis_ref,
                    config_hash=self.state.config_hash,
                    max_input=mi,
                    max_output=mo,
                    transport="Prepared",
                    charge=DiagnosticCharge(kind="Pending", tokens=reservation_tokens),
                )
                self._seen_req_ids.add(req_id)
                self._recompute_attempt_usage()
            else:
                stage_usage.reservations += 1
                stage_usage.reserved_tokens += reservation_tokens
                agg_usage.reservations += 1
                agg_usage.reserved_tokens += reservation_tokens
            self._sync("diagnostic.call_reserved")
            return "Accepted"

    def record_dispatched(self, stage: str, req_id: str | None = None, *args: Any, **kwargs: Any):
        with self._transaction():
            self.refresh()
            if self.state.fault:
                raise BudgetExhausted(f"STATE_FAULT_LATCHED:{self.state.fault}")
            stage_usage = self.state.stages.get(stage)
            agg_usage = self.state.aggregate
            if stage_usage is None:
                raise ValueError(f"UNDECLARED_DIAGNOSTIC_STAGE: {stage}")
            if req_id is not None:
                rec = self.state.attempts.get(req_id)
                if rec is None:
                    return "Rejected"
                if rec.transport != "Prepared":
                    return "DuplicateNoop"
                self.state.attempts[req_id] = rec.model_copy(
                    update={"transport": "DispatchedIntent"}
                )
                self._recompute_attempt_usage()
            else:
                stage_usage.dispatched_inference += 1
                agg_usage.dispatched_inference += 1
            self._sync("diagnostic.call_dispatched")
            return "Accepted"

    def record_completed(
        self,
        stage: str,
        input_tokens: int,
        output_tokens: int,
        reservation_tokens: int,
        req_id: str | None = None,
        receipt_hash: str | None = None,
        *args: Any,
        **kwargs: Any,
    ):
        with self._transaction():
            self.refresh()
            stage_limits = self.state.allocation.stages[stage]
            stage_usage = self.state.stages[stage]
            agg_usage = self.state.aggregate
            prior_kind: str | None = None

            if req_id is not None:
                rec = self.state.attempts.get(req_id)
                if rec is None:
                    return "Rejected"
                prior_kind = rec.charge.kind
                if prior_kind == "Settled":
                    if (
                        rec.charge.input_tokens == input_tokens
                        and rec.charge.output_tokens == output_tokens
                        and rec.charge.receipt_hash == receipt_hash
                    ):
                        return "DuplicateNoop"
                    self.state.fault = self.state.fault or "CONFLICTING_USAGE_RECEIPT"
                    self._sync("diagnostic.usage_conflict")
                    return "ConflictFault"
                reservation_tokens = rec.charge.tokens or 0 if prior_kind == "Pending" else 0
                self.state.attempts[req_id] = rec.model_copy(
                    update={
                        "transport": "ResponseReceived",
                        "charge": DiagnosticCharge(
                            kind="Settled",
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            receipt_hash=receipt_hash or "receipt_missing",
                        ),
                    }
                )

            if req_id is not None:
                self._recompute_attempt_usage()
                stage_usage = self.state.stages[stage]
                agg_usage = self.state.aggregate
            else:
                stage_usage.completed_calls += 1
                stage_usage.input_tokens += input_tokens
                stage_usage.output_tokens += output_tokens
                stage_usage.reserved_tokens = max(
                    0, stage_usage.reserved_tokens - reservation_tokens
                )
                agg_usage.completed_calls += 1
                agg_usage.input_tokens += input_tokens
                agg_usage.output_tokens += output_tokens
                agg_usage.reserved_tokens = max(
                    0, agg_usage.reserved_tokens - reservation_tokens
                )

            conflict = prior_kind == "Released"
            if conflict:
                self.state.fault = self.state.fault or "CONFLICTING_USAGE_AFTER_RELEASE"
            elif req_id is not None:
                rec = self.state.attempts[req_id]
                if input_tokens > rec.max_input or output_tokens > rec.max_output:
                    conflict = True
                    self.state.fault = self.state.fault or "BOUND_VIOLATION_FAULT"
            if stage_usage.input_tokens + stage_usage.output_tokens > stage_limits.max_tokens:
                conflict = True
                self.state.fault = self.state.fault or f"stage_{stage}_tokens_exceeded"
            if agg_usage.input_tokens + agg_usage.output_tokens > self.state.allocation.aggregate_max_tokens:
                conflict = True
                self.state.fault = self.state.fault or "aggregate_diagnostic_tokens_exceeded"
            self._sync("diagnostic.call_completed")
            return "ConflictFault" if conflict else "Accepted"

    def record_failed(
        self,
        stage: str,
        reservation_tokens: int,
        req_id: str | None = None,
        evidence_hash: str | None = None,
        *args: Any,
        **kwargs: Any,
    ):
        with self._transaction():
            self.refresh()
            stage_usage = self.state.stages[stage]
            agg_usage = self.state.aggregate
            if req_id is not None:
                rec = self.state.attempts.get(req_id)
                if rec is None:
                    return "Rejected"
                if rec.charge.kind == "Released":
                    return "DuplicateNoop"
                if rec.charge.kind == "Settled":
                    self.state.fault = self.state.fault or "RELEASE_AFTER_SETTLEMENT"
                    self._sync("diagnostic.release_conflict")
                    return "ConflictFault"
                reservation_tokens = rec.charge.tokens or reservation_tokens
                self.state.attempts[req_id] = rec.model_copy(
                    update={
                        "transport": "ProvenNotSent",
                        "charge": DiagnosticCharge(
                            kind="Released",
                            evidence_hash=evidence_hash or "missing_evidence",
                        ),
                    }
                )
            if req_id is not None:
                self._recompute_attempt_usage()
            else:
                stage_usage.failures += 1
                stage_usage.reserved_tokens = max(
                    0, stage_usage.reserved_tokens - reservation_tokens
                )
                agg_usage.failures += 1
                agg_usage.reserved_tokens = max(
                    0, agg_usage.reserved_tokens - reservation_tokens
                )
            self._sync("diagnostic.call_failed")
            return "Accepted"

    def record_timeout(
        self,
        stage: str,
        reservation_tokens: int,
        req_id: str | None = None,
        reason: str = "timeout",
        *args: Any,
        **kwargs: Any,
    ):
        with self._transaction():
            self.refresh()
            if req_id is not None:
                rec = self.state.attempts.get(req_id)
                if rec is None:
                    return "Rejected"
                if rec.charge.kind != "Pending":
                    return "DuplicateNoop"
                self.state.attempts[req_id] = rec.model_copy(
                    update={"transport": "OutcomeUnknown"}
                )
                self._recompute_attempt_usage()
            # The reservation remains held against in-flight ambiguity.
            self._sync("diagnostic.call_timeout")
            return "Accepted"
