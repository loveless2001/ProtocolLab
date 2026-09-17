"""P1 Regression tests: Separate reasoning channels without rewriting proposals."""

import pytest
from pydantic import ValidationError

from protocollab.actor import (
    IsolatedActor,
    ModelPortConfig,
    extract_channels,
    parse_proposal,
)


def test_valid_reasoning_prefix_extracts_clean_payload():
    raw = "<think>Paused so WAIT is necessary.</think>\n{\"kind\":\"WAIT\"}"
    orig, reasoning, payload, policy = extract_channels(raw)
    assert orig == raw
    assert reasoning == "Paused so WAIT is necessary."
    assert payload == '{"kind":"WAIT"}'
    assert policy == "reasoning_envelope/v1"
    prop = parse_proposal(raw)
    assert prop.kind == "WAIT"


def test_incomplete_reasoning_prefix_raises_error():
    incomplete = "<think>Still reasoning about whether to WAIT or ACT"
    with pytest.raises(ValueError, match="THINKING_INCOMPLETE"):
        extract_channels(incomplete)
    with pytest.raises(ValueError, match="THINKING_INCOMPLETE"):
        parse_proposal(incomplete)


def test_duplicate_envelope_preserved_in_payload_and_fails_json_parsing():
    duplicate = "<think>first thought</think><think>second thought</think>{\"kind\":\"WAIT\"}"
    orig, reasoning, payload, policy = extract_channels(duplicate)
    assert reasoning == "first thought"
    assert payload == '<think>second thought</think>{"kind":"WAIT"}'
    # Final payload contains unparsed second envelope; strict json parsing fails without rewriting
    with pytest.raises(ValueError):
        parse_proposal(duplicate)


def test_malformed_envelope_stray_closing_tag_rejected():
    malformed = "</think>{\"kind\":\"WAIT\"}"
    with pytest.raises(ValueError, match="MALFORMED_REASONING_ENVELOPE"):
        extract_channels(malformed)


def test_literal_think_tags_in_json_values_are_not_stripped_and_remain_invalid():
    raw = '{"kind":"ACT","operation":"SIGNAL_<think>junk</think>X"}'
    orig, reasoning, payload, policy = extract_channels(raw)
    assert reasoning is None
    assert payload == raw
    # Must NOT become SIGNAL_X! It must remain SIGNAL_<think>junk</think>X and fail validation!
    with pytest.raises(ValidationError):
        parse_proposal(raw)


def test_literal_think_tags_in_json_keys_are_not_stripped():
    raw = '{"kind":"WAIT","<think>extra</think>":"value"}'
    orig, reasoning, payload, policy = extract_channels(raw)
    assert payload == raw
    # Extra key was not stripped, so extra="forbid" raises ValidationError
    with pytest.raises(ValidationError):
        parse_proposal(raw)


def test_literal_think_tag_inside_valid_string_field_is_preserved():
    raw = '<think>envelope thought</think>{"kind":"WAIT","reason":"saw <think> tag in input"}'
    orig, reasoning, payload, policy = extract_channels(raw)
    assert reasoning == "envelope thought"
    assert payload == '{"kind":"WAIT","reason":"saw <think> tag in input"}'
    prop = parse_proposal(raw)
    assert prop.kind == "WAIT"
    assert prop.reason == "saw <think> tag in input"


def test_isolated_actor_retains_channels_separately(runtime, monkeypatch):
    config = ModelPortConfig(
        backend="api", model_id="channel-test-port", endpoint="https://model.invalid"
    )

    class MockPort:
        phase = "suffix"
        last_request_seq = 101

        def __init__(self, store):
            self.store = store
            self.config = config

        def generate(self, packet, seed=0):
            return "<think>reasoning trace</think>\n{\"kind\":\"ACT\",\"operation\":\"INSPECT\"}"

    port = MockPort(runtime.store)
    actor = IsolatedActor(port)
    proposal = actor.propose({"task": "test"}, seed=7)
    assert proposal.kind == "ACT"
    assert proposal.operation == "INSPECT"

    raw_event = next(e for e in runtime.store.events() if e["kind"] == "actor.raw_proposal")
    assert raw_event["payload"]["extraction_policy"] == "reasoning_envelope/v1"
    raw_text = runtime.store.blob(raw_event["payload"]["text_ref"], raw=True).decode("utf-8")
    assert "<think>reasoning trace</think>" in raw_text

    payload_text = runtime.store.blob(raw_event["payload"]["final_payload_ref"], raw=True).decode("utf-8")
    assert payload_text == '{"kind":"ACT","operation":"INSPECT"}'

    reasoning_text = runtime.store.blob(raw_event["payload"]["reasoning_ref"], raw=True).decode("utf-8")
    assert reasoning_text == "reasoning trace"

    returned_event = next(e for e in runtime.store.events() if e["kind"] == "actor.proposal_returned")
    assert returned_event["payload"]["validation_outcome"] == "VALID"
    actor.close()
