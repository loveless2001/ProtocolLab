"""Persistent multi-stage diagnostic budget ledger.

Enforces stage-specific sub-budgets and aggregate diagnostic limits without
allowing stage transitions or process restarts to replenish allocations.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from protocollab.learning import BudgetExhausted


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


class DiagnosticLedgerState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    config_hash: str
    allocation: DiagnosticBudgetAllocation
    stages: dict[str, StageUsageRecord] = Field(default_factory=dict)
    aggregate: StageUsageRecord = Field(default_factory=StageUsageRecord)


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

    def _sync(self, event_kind: str = "diagnostic.budget_updated"):
        self.store.set("diagnostic_ledger", self.run_id, self.state.model_dump(), event_kind)

    def record_admission_attempt(self, stage: str):
        stage_limits = self.state.allocation.stages.get(stage)
        stage_usage = self.state.stages.get(stage)
        agg_usage = self.state.aggregate
        if stage_limits is None or stage_usage is None:
            raise ValueError(f"UNDECLARED_DIAGNOSTIC_STAGE: {stage}")

        stage_usage.calls_attempted += 1
        agg_usage.calls_attempted += 1
        self._sync("diagnostic.admission_attempted")

        if stage_usage.calls_attempted > stage_limits.max_calls:
            raise BudgetExhausted(f"stage_{stage}_max_calls_exceeded")
        if agg_usage.calls_attempted > self.state.allocation.aggregate_max_calls:
            raise BudgetExhausted("aggregate_diagnostic_max_calls_exceeded")

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
        stage_limits = self.state.allocation.stages.get(stage)
        stage_usage = self.state.stages.get(stage)
        agg_usage = self.state.aggregate
        if stage_limits is None or stage_usage is None:
            raise ValueError(f"UNDECLARED_DIAGNOSTIC_STAGE: {stage}")

        if (stage_usage.input_tokens + stage_usage.output_tokens + stage_usage.reserved_tokens + reservation_tokens > stage_limits.max_tokens):
            raise BudgetExhausted(f"stage_{stage}_token_reservation_exceeded")
        if (agg_usage.input_tokens + agg_usage.output_tokens + agg_usage.reserved_tokens + reservation_tokens > self.state.allocation.aggregate_max_tokens):
            raise BudgetExhausted("aggregate_diagnostic_token_reservation_exceeded")

        stage_usage.reservations += 1
        stage_usage.reserved_tokens += reservation_tokens
        agg_usage.reservations += 1
        agg_usage.reserved_tokens += reservation_tokens
        self._sync("diagnostic.call_reserved")

    def record_dispatched(self, stage: str, req_id: str | None = None, *args: Any, **kwargs: Any):
        stage_usage = self.state.stages.get(stage)
        agg_usage = self.state.aggregate
        if stage_usage is None:
            raise ValueError(f"UNDECLARED_DIAGNOSTIC_STAGE: {stage}")
        stage_usage.dispatched_inference += 1
        agg_usage.dispatched_inference += 1
        self._sync("diagnostic.call_dispatched")

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
        stage_limits = self.state.allocation.stages[stage]
        stage_usage = self.state.stages[stage]
        agg_usage = self.state.aggregate

        stage_usage.completed_calls += 1
        stage_usage.input_tokens += input_tokens
        stage_usage.output_tokens += output_tokens
        stage_usage.reserved_tokens = max(0, stage_usage.reserved_tokens - reservation_tokens)

        agg_usage.completed_calls += 1
        agg_usage.input_tokens += input_tokens
        agg_usage.output_tokens += output_tokens
        agg_usage.reserved_tokens = max(0, agg_usage.reserved_tokens - reservation_tokens)

        self._sync("diagnostic.call_completed")
        if stage_usage.input_tokens + stage_usage.output_tokens > stage_limits.max_tokens:
            raise BudgetExhausted(f"stage_{stage}_tokens_exceeded")
        if agg_usage.input_tokens + agg_usage.output_tokens > self.state.allocation.aggregate_max_tokens:
            raise BudgetExhausted("aggregate_diagnostic_tokens_exceeded")

    def record_failed(
        self,
        stage: str,
        reservation_tokens: int,
        req_id: str | None = None,
        evidence_hash: str | None = None,
        *args: Any,
        **kwargs: Any,
    ):
        stage_usage = self.state.stages[stage]
        agg_usage = self.state.aggregate
        stage_usage.failures += 1
        stage_usage.reserved_tokens = max(0, stage_usage.reserved_tokens - reservation_tokens)
        agg_usage.failures += 1
        agg_usage.reserved_tokens = max(0, agg_usage.reserved_tokens - reservation_tokens)
        self._sync("diagnostic.call_failed")

    def record_timeout(
        self,
        stage: str,
        reservation_tokens: int,
        req_id: str | None = None,
        reason: str = "timeout",
        *args: Any,
        **kwargs: Any,
    ):
        stage_usage = self.state.stages[stage]
        agg_usage = self.state.aggregate
        stage_usage.failures += 1
        stage_usage.reserved_tokens = max(0, stage_usage.reserved_tokens - reservation_tokens)
        agg_usage.failures += 1
        agg_usage.reserved_tokens = max(0, agg_usage.reserved_tokens - reservation_tokens)
        self._sync("diagnostic.call_timeout")


