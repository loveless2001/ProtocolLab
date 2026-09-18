"""Immutable diagnostic configuration."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from protocollab.actor import ModelPortConfig
from protocollab.actor.budget import DiagnosticBudgetAllocation, StageBudgetLimits
from protocollab.contracts import uid

DEFAULT_CANDIDATES = [
    '{"kind":"ACT","operation":"SUBMIT_A"}',
    '{"kind":"ACT","operation":"SUBMIT_B"}',
    '{"kind":"ACT","operation":"SIGNAL_X"}',
    '{"kind":"ACT","operation":"SIGNAL_Y"}',
    '{"kind":"ACT","operation":"CANCEL"}',
    '{"kind":"ACT","operation":"STATUS"}',
    '{"kind":"ACT","operation":"INSPECT"}',
    '{"kind":"WAIT"}',
    '{"kind":"FINISH"}',
]


class DiagnosticThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False, frozen=True)
    min_schema_valid_rate: float = 0.80
    min_decision_correct_rate: float = 0.70
    min_progress_rate: float = 0.50
    max_truncation_rate: float = 0.10


def default_budget_allocation() -> DiagnosticBudgetAllocation:
    return DiagnosticBudgetAllocation(
        aggregate_max_calls=48,
        aggregate_max_tokens=48 * (16384 + 1024),
        stages={
            "minimal_proposal": StageBudgetLimits(max_calls=10, max_tokens=10 * (16384 + 1024)),
            "state_decision": StageBudgetLimits(max_calls=10, max_tokens=10 * (16384 + 1024)),
            "closed_loop": StageBudgetLimits(max_calls=28, max_tokens=28 * (16384 + 1024)),
        },
    )


class DiagnosticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    condition: Literal["C0", "C1", "C2", "C3", "C4"] = "C0"
    track: Literal["F", "O"] = "F"
    decision_mode: Literal["free_json", "constrained_json", "candidate_score"] = "free_json"
    model_port_config: ModelPortConfig
    renderer: Literal["standard", "demarcated"] = "demarcated"
    chat_template_revision: str = "v1"
    reasoning_mode: Literal["envelope_v1", "disabled"] = "envelope_v1"
    decoding_settings: dict[str, Any] = Field(default_factory=dict)
    candidate_registry: list[str] = Field(default_factory=lambda: list(DEFAULT_CANDIDATES))
    seed: int = 7
    thresholds: DiagnosticThresholds = Field(default_factory=DiagnosticThresholds)
    budget_allocation: DiagnosticBudgetAllocation = Field(default_factory=default_budget_allocation)
    run_id: str = Field(default_factory=uid)
    scenario_version: Literal["v1", "v2", "v3"] = "v3"
