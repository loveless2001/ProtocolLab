"""ProtocolLab verified lifecycle kernel protocol and data models."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_SAFE_INT = 9_007_199_254_740_991


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
    tokens: int | None = Field(default=None, ge=0, le=MAX_SAFE_INT)
    input_tokens: int | None = Field(default=None, ge=0, le=MAX_SAFE_INT)
    output_tokens: int | None = Field(default=None, ge=0, le=MAX_SAFE_INT)
    receipt_hash: str | None = None
    evidence_hash: str | None = None

    @model_validator(mode="after")
    def validate_charge_variants(self) -> Charge:
        if self.kind == ChargeKind.PENDING:
            if self.tokens is None:
                raise ValueError("Pending charge requires non-negative 'tokens'")
        elif self.kind == ChargeKind.SETTLED:
            if self.input_tokens is None or self.output_tokens is None or self.receipt_hash is None:
                raise ValueError(
                    "Settled charge requires 'input_tokens', 'output_tokens', and 'receipt_hash'"
                )
        elif self.kind == ChargeKind.RELEASED:
            if self.evidence_hash is None:
                raise ValueError("Released charge requires 'evidence_hash'")
        return self


class StageLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str
    max_calls: int = Field(ge=0, le=MAX_SAFE_INT)
    max_tokens: int = Field(ge=0, le=MAX_SAFE_INT)


class RequestRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    req_id: str
    stage: str
    basis_ref: str
    config_hash: str
    max_input: int = Field(ge=0, le=MAX_SAFE_INT)
    max_output: int = Field(ge=0, le=MAX_SAFE_INT)
    transport: TransportState
    charge: Charge


class LedgerState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    config_hash: str
    agg_max_calls: int = Field(ge=0, le=MAX_SAFE_INT)
    agg_max_tokens: int = Field(ge=0, le=MAX_SAFE_INT)
    stage_limits: list[StageLimit] = Field(default_factory=list)
    requests: list[RequestRecord] = Field(default_factory=list)
    fault: str | None = None


class InferenceDispatchIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    req_id: str
    stage: str
    basis_ref: str
    config_hash: str
    max_input: int = Field(ge=0, le=MAX_SAFE_INT)
    max_output: int = Field(ge=0, le=MAX_SAFE_INT)


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
