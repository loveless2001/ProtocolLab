"""P1 Regression tests: Preserve budget history across diagnostic stages."""

import pytest

from protocollab.actor.budget import (
    DiagnosticBudgetAllocation,
    DiagnosticLedger,
    StageBudgetLimits,
)
from protocollab.contracts import digest
from protocollab.learning import BudgetExhausted


def test_budget_ledger_enforces_stage_and_aggregate_limits(runtime):
    alloc = DiagnosticBudgetAllocation(
        aggregate_max_calls=4,
        aggregate_max_tokens=1000,
        stages={
            "stage_1": StageBudgetLimits(max_calls=2, max_tokens=400),
            "stage_2": StageBudgetLimits(max_calls=3, max_tokens=600),
        },
    )
    config_hash = digest({"model": "test-model"})
    run_id = "test-run-001"
    ledger = DiagnosticLedger(runtime.store, run_id, alloc, config_hash)

    # Stage 1: call 1
    ledger.record_admission_attempt("stage_1")
    ledger.reserve("stage_1", 150)
    ledger.record_dispatched("stage_1")
    ledger.record_completed("stage_1", input_tokens=80, output_tokens=20, reservation_tokens=150)

    assert ledger.state.stages["stage_1"].calls_attempted == 1
    assert ledger.state.stages["stage_1"].completed_calls == 1
    assert ledger.state.stages["stage_1"].input_tokens == 80
    assert ledger.state.stages["stage_1"].output_tokens == 20
    assert ledger.state.aggregate.calls_attempted == 1
    assert ledger.state.aggregate.completed_calls == 1

    # Stage 1: call 2
    ledger.record_admission_attempt("stage_1")
    ledger.reserve("stage_1", 150)
    ledger.record_dispatched("stage_1")
    ledger.record_completed("stage_1", input_tokens=90, output_tokens=30, reservation_tokens=150)

    # Stage 1: call 3 exceeds stage limit
    with pytest.raises(BudgetExhausted, match="stage_stage_1_max_calls_exceeded"):
        ledger.record_admission_attempt("stage_1")

    # Transition to Stage 2: does NOT replenish aggregate budget
    assert ledger.state.aggregate.calls_attempted == 3
    assert ledger.state.aggregate.completed_calls == 2
    ledger.record_admission_attempt("stage_2")
    ledger.reserve("stage_2", 200)
    ledger.record_dispatched("stage_2")
    ledger.record_completed("stage_2", input_tokens=100, output_tokens=50, reservation_tokens=200)

    assert ledger.state.aggregate.calls_attempted == 4
    assert ledger.state.aggregate.completed_calls == 3

    # Attempt #5 on stage 2 exceeds aggregate max calls (limit is 4)
    with pytest.raises(BudgetExhausted, match="aggregate_diagnostic_max_calls_exceeded"):
        ledger.record_admission_attempt("stage_2")


def test_budget_ledger_resumes_on_restart_and_rejects_incompatible_config(runtime):
    alloc = DiagnosticBudgetAllocation(
        aggregate_max_calls=10,
        aggregate_max_tokens=2000,
        stages={"s1": StageBudgetLimits(max_calls=5, max_tokens=1000)},
    )
    config_hash = digest({"model": "test-model"})
    run_id = "test-run-restart"

    # Initial run
    ledger1 = DiagnosticLedger(runtime.store, run_id, alloc, config_hash)
    ledger1.record_admission_attempt("s1")
    ledger1.reserve("s1", 100)
    ledger1.record_dispatched("s1")
    ledger1.record_completed("s1", 50, 20, 100)

    # Process restart: re-instantiate ledger with same run_id
    ledger2 = DiagnosticLedger(runtime.store, run_id, alloc, config_hash)
    assert ledger2.state.stages["s1"].calls_attempted == 1
    assert ledger2.state.stages["s1"].completed_calls == 1
    assert ledger2.state.aggregate.completed_calls == 1

    # Incompatible config change on same run_id is rejected
    diff_hash = digest({"model": "different-model"})
    with pytest.raises(ValueError, match="DIAGNOSTIC_CONFIGURATION_CHANGED"):
        DiagnosticLedger(runtime.store, run_id, alloc, diff_hash)

    # Incompatible budget change on same run_id is rejected
    diff_alloc = DiagnosticBudgetAllocation(
        aggregate_max_calls=20,
        aggregate_max_tokens=4000,
        stages={"s1": StageBudgetLimits(max_calls=10, max_tokens=2000)},
    )
    with pytest.raises(ValueError, match="DIAGNOSTIC_BUDGET_ALLOCATION_CHANGED"):
        DiagnosticLedger(runtime.store, run_id, diff_alloc, config_hash)


def test_failure_and_timeout_consumes_reservation_without_replenishing(runtime):
    alloc = DiagnosticBudgetAllocation(
        aggregate_max_calls=5,
        aggregate_max_tokens=500,
        stages={"s1": StageBudgetLimits(max_calls=3, max_tokens=300)},
    )
    config_hash = digest({"model": "test-model"})
    run_id = "test-run-failure"
    ledger = DiagnosticLedger(runtime.store, run_id, alloc, config_hash)

    ledger.record_admission_attempt("s1")
    ledger.reserve("s1", 150)
    ledger.record_dispatched("s1")
    # Call fails (e.g. timeout)
    ledger.record_failed("s1", 150)

    assert ledger.state.stages["s1"].failures == 1
    assert ledger.state.stages["s1"].calls_attempted == 1
    assert ledger.state.stages["s1"].completed_calls == 0
    assert ledger.state.stages["s1"].reserved_tokens == 0
