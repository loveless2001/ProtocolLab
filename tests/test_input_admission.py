"""Tests for unified final-input budget accounting and admission boundaries."""

import json
import urllib.request

import pytest

from protocollab.actor import (
    FrozenModelPort,
    ModelPortConfig,
    build_packet,
    render_formatted_input,
)
from protocollab.contracts import canonical
from protocollab.learning import BudgetExhausted


def mock_response():
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return canonical({
                "model_id": "test-model",
                "text": '{"kind":"WAIT"}',
                "input_tokens": 10,
                "output_tokens": 10,
            })
    return Response()


def test_packet_fits_raw_but_exceeds_formatted_limit_is_rejected_without_inference(runtime, monkeypatch):
    """A packet that fits raw bytes but exceeds budget after chat formatting must fail admission."""
    sent_requests = []

    def mock_urlopen(req, timeout=120):
        sent_requests.append(req)
        return mock_response()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    # Inject an incoming claim
    runtime.store.append("environment", "epistemic.claim", {"text": "unverified claim"}, "admin")
    packet = build_packet(runtime, "C0")
    raw_bytes = len(canonical(packet))

    # Calculate formatted bytes with qwen_chat template
    config_probe = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        chat_template="qwen_chat",
    )
    formatted = render_formatted_input(canonical(packet).decode("utf-8"), config_probe)
    formatted_bytes = len(formatted.encode("utf-8"))
    assert formatted_bytes > raw_bytes

    # Set budget so raw packet fits, but formatted input does not
    limit = formatted_bytes - 10
    assert raw_bytes < limit < formatted_bytes

    config = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        chat_template="qwen_chat",
        max_input_tokens=limit,
    )
    port = FrozenModelPort(config, runtime.store, max_calls=4)

    with pytest.raises(BudgetExhausted, match="formatted_input_byte_bound"):
        port.generate(packet)

    # Verify rejected input never reached inference
    assert len(sent_requests) == 0
    assert port.calls == 0

    # Verify admission failure was recorded separately in store
    events = runtime.store.events()
    rejected_events = [e for e in events if e["kind"] == "llm.input_admission_rejected"]
    assert len(rejected_events) == 1
    rejection = rejected_events[0]["payload"]
    assert rejection["reason"] == "FORMATTED_INPUT_BYTE_BOUND"
    assert rejection["formatted_bytes"] > limit
    assert rejection["packet_bytes"] < rejection["formatted_bytes"]
    assert rejection["limit_bytes"] == limit
    assert len(rejection["claims"]) == 1


def test_exact_limit_boundary_admitted_and_exact_plus_one_rejected(runtime, monkeypatch):
    """Exact limit must be admitted; limit + 1 byte must be rejected."""
    sent_requests = []

    def mock_urlopen(req, timeout=120):
        sent_requests.append(req)
        return mock_response()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    packet = build_packet(runtime, "C0")
    config_probe = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        chat_template="qwen_chat",
    )
    formatted = render_formatted_input(canonical(packet).decode("utf-8"), config_probe)
    exact_bytes = len(formatted.encode("utf-8"))

    # Case 1: Exact limit fits
    config_exact = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        chat_template="qwen_chat",
        max_input_tokens=exact_bytes,
    )
    port_exact = FrozenModelPort(config_exact, runtime.store, max_calls=4)
    result = port_exact.generate(packet)
    assert result == '{"kind":"WAIT"}'
    assert len(sent_requests) == 1
    assert port_exact.calls == 1

    events = runtime.store.events()
    delivered_events = [e for e in events if e["kind"] == "llm.input_delivered"]
    assert len(delivered_events) == 1
    evidence = runtime.store.blob(delivered_events[0]["payload"]["input_evidence_hash"])
    wire_prompt = json.loads(sent_requests[0].data)["prompt"]
    assert evidence["prompt"] == wire_prompt
    assert wire_prompt == canonical(packet).decode("utf-8")
    assert formatted != wire_prompt

    # Case 2: Exact limit - 1 byte rejects
    config_under = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        chat_template="qwen_chat",
        max_input_tokens=exact_bytes - 1,
    )
    # Create new store phase to isolate budget
    runtime.store.set("model_port", "prefix", None)
    port_under = FrozenModelPort(config_under, runtime.store, max_calls=4, phase="prefix")
    sent_requests.clear()

    with pytest.raises(BudgetExhausted, match="formatted_input_byte_bound"):
        port_under.generate(packet)

    assert len(sent_requests) == 0
    assert port_under.calls == 0


def test_non_ascii_multibyte_utf8_budget_accounting(runtime, monkeypatch):
    """Non-ASCII characters must be budgeted by UTF-8 bytes, not character count."""
    sent_requests = []

    def mock_urlopen(req, timeout=120):
        sent_requests.append(req)
        return mock_response()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    # Vietnamese multi-byte claim: "Cảnh báo khẩn cấp: dừng hoạt động ⚠️"
    # In UTF-8, each accented character takes 2-3 bytes and emoji takes 4 bytes.
    unicode_claim = "Cảnh báo khẩn cấp: dừng hoạt động ⚠️"
    assert len(unicode_claim.encode("utf-8")) > len(unicode_claim)

    runtime.store.append("environment", "epistemic.claim", {"text": unicode_claim}, "admin")
    packet = build_packet(runtime, "C0")

    config_probe = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        chat_template="qwen_chat",
    )
    formatted = render_formatted_input(canonical(packet).decode("utf-8"), config_probe)
    char_count = len(formatted)
    byte_count = len(formatted.encode("utf-8"))
    assert byte_count > char_count

    # Set max_input_tokens between char_count and byte_count:
    # If the harness incorrectly counted characters, it would admit;
    # correctly counting UTF-8 bytes will reject it!
    boundary_limit = char_count + (byte_count - char_count) // 2

    config_utf8 = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        chat_template="qwen_chat",
        max_input_tokens=boundary_limit,
    )
    port = FrozenModelPort(config_utf8, runtime.store, max_calls=4)

    with pytest.raises(BudgetExhausted, match="formatted_input_byte_bound"):
        port.generate(packet)

    assert len(sent_requests) == 0
    rejections = [e for e in runtime.store.events() if e["kind"] == "llm.input_admission_rejected"]
    assert len(rejections) == 1
    assert rejections[0]["payload"]["formatted_bytes"] > boundary_limit
    assert rejections[0]["payload"]["limit_bytes"] == boundary_limit


def test_chatml_omits_qwen_think_tags_and_unknown_template_is_rejected():
    from pydantic import ValidationError
    packet_text = '{"kind":"WAIT"}'
    qwen = ModelPortConfig(
        backend="api", model_id="test-model", endpoint="https://model.invalid", chat_template="qwen_chat")
    chatml = ModelPortConfig(
        backend="api", model_id="test-model", endpoint="https://model.invalid", chat_template="chatml")
    qwen_text = render_formatted_input(packet_text, qwen)
    chatml_text = render_formatted_input(packet_text, chatml)
    assert "<think>" in qwen_text
    assert "<think>" not in chatml_text
    assert chatml_text.endswith("<|im_start|>assistant\n")
    with pytest.raises(ValidationError):
        ModelPortConfig(
            backend="api", model_id="test-model", endpoint="https://model.invalid", chat_template="qwen-chat")


def test_history_is_trimmed_to_fit_formatted_limit_without_losing_claims_or_feedback(runtime, monkeypatch):
    """History events are trimmed to satisfy formatted limit, but claims and live feedback are retained."""
    sent_requests = []

    def mock_urlopen(req, timeout=120):
        sent_requests.append(req)
        return mock_response()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    # Create multiple history observations
    for _ in range(15):
        runtime.turn("STATUS")

    claim_text = "important pending claim that must not be trimmed"
    runtime.store.append("environment", "epistemic.claim", {"text": claim_text}, "admin")

    packet = build_packet(runtime, "C0")
    initial_history_len = len(packet["history"]["events"])
    assert initial_history_len > 5

    config_probe = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        chat_template="qwen_chat",
    )
    formatted_full = render_formatted_input(canonical(packet).decode("utf-8"), config_probe)
    full_bytes = len(formatted_full.encode("utf-8"))

    # Set budget smaller than full packet, but large enough for trimmed history + required fields
    target_budget = full_bytes - 1000

    config = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        chat_template="qwen_chat",
        max_input_tokens=target_budget,
    )
    port = FrozenModelPort(config, runtime.store, max_calls=4)
    result = port.generate(packet)

    assert result == '{"kind":"WAIT"}'
    assert len(sent_requests) == 1
    sent_data = json.loads(sent_requests[0].data)
    sent_packet = json.loads(sent_data["prompt"])

    # History was trimmed
    assert len(sent_packet["history"]["events"]) < initial_history_len
    # But incoming claims and live feedback were strictly retained!
    assert len(sent_packet["incoming_claims"]) == 1
    assert sent_packet["incoming_claims"][0]["text"] == claim_text
    assert sent_packet["live_feedback"]["latest_turn"] is not None
    delivered = [e for e in runtime.store.events() if e["kind"] == "llm.input_delivered"]
    evidence = runtime.store.blob(delivered[-1]["payload"]["input_evidence_hash"])
    assert evidence["prompt"] == sent_data["prompt"]
