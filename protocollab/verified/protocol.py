"""ProtocolLab verified lifecycle kernel protocol and data models."""

from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class TransportState(str, Enum):
    PREPARED = "Prepared"
    DISPATCHED_INTENT = "DispatchedIntent"
    SENT = "Sent"
    RESPONSE_RECEIVED = "ResponseReceived"
    PROVEN_NOT_SENT = "ProvenNotSent"
    OUTCOME_UNKNOWN = "OutcomeUnknown"


class ChargeKind(str, Enum):
    PENDING = "Pending"
    SETTLED = "Settled"
    RELEASED = "Released"


class Charge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ChargeKind
    tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    receipt_hash: str | None = None
    evidence_hash: str | None = None


class StageLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str
    max_calls: int = Field(ge=0)
    max_tokens: int = Field(ge=0)


class RequestRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    req_id: str
    stage: str
    basis_ref: str
    config_hash: str
    max_input: int = Field(ge=0)
    max_output: int = Field(ge=0)
    transport: TransportState
    charge: Charge


class LedgerState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    config_hash: str
    agg_max_calls: int = Field(ge=0)
    agg_max_tokens: int = Field(ge=0)
    stage_limits: list[StageLimit] = Field(default_factory=list)
    requests: list[RequestRecord] = Field(default_factory=list)
    fault: str | None = None


class InferenceDispatchIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    req_id: str
    stage: str
    basis_ref: str
    config_hash: str
    max_input: int
    max_output: int


class VerdictKind(str, Enum):
    ACCEPTED = "Accepted"
    DUPLICATE_NOOP = "DuplicateNoop"
    REJECTED = "Rejected"
    CONFLICT_FAULT = "ConflictFault"


class TransitionVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: VerdictKind
    intent: InferenceDispatchIntent | None = None
    reason: str | None = None


class TransitionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: LedgerState
    verdict: TransitionVerdict


class StageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str
    calls_admitted: int
    spent_tokens: int
    held_tokens: int
    committed_tokens: int


class AccountingSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_spent: int
    total_held: int
    total_committed: int
    stage_summaries: list[StageSummary] = Field(default_factory=list)
    fault: str | None = None


class LifecycleMode(str, Enum):
    SHADOW = "SHADOW"
    AUTHORITATIVE = "AUTHORITATIVE"
