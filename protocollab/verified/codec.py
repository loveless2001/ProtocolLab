"""Codec for serialization/deserialization between Python models and Bend JSON wire format."""

from __future__ import annotations

from typing import Any

from protocollab.verified.protocol import (
    AccountingSummary,
    LedgerState,
    TransitionResult,
    TransitionVerdict,
)


def encode_state(state: LedgerState) -> dict[str, Any]:
    return state.model_dump()


def decode_state(data: dict[str, Any]) -> LedgerState:
    return LedgerState.model_validate(data)


def decode_verdict(data: dict[str, Any]) -> TransitionVerdict:
    return TransitionVerdict.model_validate(data)


def decode_result(data: dict[str, Any]) -> TransitionResult:
    return TransitionResult.model_validate(data)


def decode_summary(data: dict[str, Any]) -> AccountingSummary:
    return AccountingSummary.model_validate(data)
