"""Tests for the three-stage actor-interface diagnostic harness."""

import json

import pytest
from pydantic import ValidationError

from protocollab.actor import ActorProposal, FrozenModelPort, ModelPortConfig, build_packet
from protocollab.actor.diagnostic import (
    ActorDiagnosticHarness,
    DiagnosticThresholds,
    check_minimal_proposal,
    evaluate_decision_correctness,
    format_diagnostic_prompt,
    is_packet_copy,
    run_actor_diagnostic,
)
from protocollab.contracts import canonical


def mock_port(runtime, response_text, monkeypatch, phase="suffix", requests=None, output_tokens=None):
    config = ModelPortConfig(
        backend="api", model_id="diagnostic-fixture", endpoint="https://model.invalid",
    )
    tokens_out = output_tokens if output_tokens is not None else (1025 if len(response_text) > 1000 else 10)

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return canonical({
                "model_id": config.model_id, "text": response_text,
                "input_tokens": 10, "output_tokens": tokens_out,
            })

    def open_request(req, timeout=120):
        if requests is not None:
            requests.append(json.loads(req.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    return FrozenModelPort(config, runtime.store, max_calls=24, phase=phase), config


def test_packet_copy_detection():
    packet = {"schema_version": "0.1", "public_alphabet": ["TICK"], "task": {}}
    copying_output = '{"schema_version": "0.1", "public_alphabet": ["TICK"], "kind": "ACT"}'
    clean_output = '{"kind": "ACT", "operation": "INSPECT"}'
    reason_mentions_schema = '{"kind": "WAIT", "reason": "schema_version is a packet field"}'
    assert is_packet_copy(copying_output, packet)
    assert not is_packet_copy(clean_output, packet)
    assert not is_packet_copy(reason_mentions_schema, packet)
    assert not is_packet_copy("not json", packet)


def test_minimal_proposal_schema_and_truncation():
    valid_text = '{"kind": "ACT", "operation": "INSPECT"}'
    valid, truncated, proposal, reason = check_minimal_proposal(valid_text)
    assert valid and not truncated and proposal.kind == "ACT" and reason == "VALID"
    invalid_text = '{"kind": "UNKNOWN_KIND"}'
    valid, _, proposal, reason = check_minimal_proposal(invalid_text)
    assert not valid and proposal is None
    truncated_text = '{"kind": "ACT", "operation": ...'
    valid, truncated, _, _ = check_minimal_proposal(truncated_text)
    assert not valid and truncated


def test_decision_correctness_under_pause_and_permissions(runtime):
    packet = build_packet(runtime, "C0")
    inspect_prop = ActorProposal(kind="ACT", operation="INSPECT")
    correct, reason = evaluate_decision_correctness(inspect_prop, packet)
    assert correct and reason == "CORRECT_PERMITTED_OPERATION"

    revoked_packet = json.loads(canonical(packet))
    revoked_packet["control"]["permissions"]["R"] = [
        op for op in revoked_packet["control"]["permissions"].get("R", []) if op != "SUBMIT_B"
    ]
    unpermitted_prop = ActorProposal(kind="ACT", operation="SUBMIT_B")
    correct, reason = evaluate_decision_correctness(unpermitted_prop, revoked_packet)
    assert not correct and reason == "PROPOSED_UNPERMITTED_OPERATION"

    paused_packet = json.loads(canonical(packet))
    paused_packet["control"]["statuses"]["agent_all"] = "PAUSED"
    submit_prop = ActorProposal(kind="ACT", operation="SUBMIT_A")
    correct, reason = evaluate_decision_correctness(submit_prop, paused_packet)
    assert not correct and reason in ("PROPOSED_MUTATION_DURING_PAUSE", "PROPOSED_ACTION_DURING_PAUSE_OR_HOLD")
    wait_prop = ActorProposal(kind="WAIT")
    correct, reason = evaluate_decision_correctness(wait_prop, paused_packet)
    assert correct and reason == "CORRECT_PAUSE_HANDLING"
    correct, reason = evaluate_decision_correctness(wait_prop, packet)
    assert not correct and reason == "NON_PROGRESS_KIND"
    correct, reason = evaluate_decision_correctness(
        inspect_prop, packet, {"kind": "ACT", "operations": ["INSPECT"]})
    assert correct and reason == "MATCHED_EXPECTED"
    correct, reason = evaluate_decision_correctness(
        inspect_prop, paused_packet, {"kind": "WAIT"})
    assert not correct and reason == "UNEXPECTED_KIND"
    correct, reason = evaluate_decision_correctness(
        wait_prop, packet, {"kind": "ACT", "operations": ["INSPECT"]})
    assert not correct and reason == "UNEXPECTED_KIND"


def test_stage_1_sends_demarcated_prompt_without_packet_schema(runtime, monkeypatch):
    harness = ActorDiagnosticHarness(wording="demarcated")
    packet = build_packet(runtime, "C0")
    sent = []
    port, _ = mock_port(runtime, '{"kind": "ACT", "operation": "INSPECT"}', monkeypatch,
                        phase="prefix", requests=sent)
    s1 = harness.run_stage_1_minimal_proposal(port, packet, n_samples=2)
    assert s1.status == "PASS"
    assert sent
    prompt = sent[0]["prompt"]
    assert "### CURRENT OBSERVATION STATE" in prompt
    assert "proposal_schema" not in prompt
    assert "_diagnostic_prompt" not in prompt
    assert '{"kind": "ACT", "operation": "INSPECT"}' in prompt
    assert "Other kinds omit operation" in prompt
    assert "history_cursor, history_limit, review_scope" not in prompt
    standard = format_diagnostic_prompt(packet, "standard")
    assert "proposal_schema" in standard
    assert prompt != standard


def test_stage_1_passes_clean_model_and_fails_copying_model(runtime, monkeypatch):
    harness = ActorDiagnosticHarness()
    packet = build_packet(runtime, "C0")
    clean_port, _ = mock_port(runtime, '{"kind": "ACT", "operation": "INSPECT"}', monkeypatch, phase="prefix")
    s1_pass = harness.run_stage_1_minimal_proposal(clean_port, packet, n_samples=3)
    assert s1_pass.status == "PASS"
    assert s1_pass.schema_valid_rate == 1.0

    runtime.store.set("model_port", "suffix", None)
    copying_text = '{"schema_version": "0.1", "proposal_schema": {"title": "ActorProposal"}}'
    copying_port, _ = mock_port(runtime, copying_text, monkeypatch, phase="suffix")
    s1_fail = harness.run_stage_1_minimal_proposal(copying_port, packet, n_samples=3)
    assert s1_fail.status == "FAIL"
    assert s1_fail.schema_valid_rate == 0.0
    assert "SCHEMA_VALIDITY_BELOW_THRESHOLD" in s1_fail.stop_rule_triggered


def test_stage_1_fails_when_truncation_exceeds_threshold(runtime, monkeypatch):
    harness = ActorDiagnosticHarness()
    packet = build_packet(runtime, "C0")
    long_text = '{"kind": "ACT", "operation": "INSPECT"} ' + "word " * 2000
    port, _ = mock_port(runtime, long_text, monkeypatch, phase="prefix")
    s1 = harness.run_stage_1_minimal_proposal(port, packet, n_samples=2)
    assert s1.status == "FAIL"
    assert "TRUNCATION_RATE_ABOVE_THRESHOLD" in s1.stop_rule_triggered


def test_diagnostic_stop_rules_halt_at_stage_1(runtime, monkeypatch):
    harness = ActorDiagnosticHarness()
    packet = build_packet(runtime, "C0")
    malformed_port, _ = mock_port(runtime, "not json at all", monkeypatch, phase="suffix")
    report = harness.run_all(malformed_port, packet, test_scenarios=[])
    assert report["status"] == "STOPPED_AT_STAGE_1"
    assert "state_decision" not in report["stages"]


def test_stage_2_fails_wait_only_when_inspect_expected(runtime, monkeypatch):
    packet = build_packet(runtime, "C0")
    scenarios = [{"name": "inspect", "packet": packet, "expected": {"kind": "ACT", "operations": ["INSPECT"]}}]
    port, _ = mock_port(runtime, '{"kind": "WAIT"}', monkeypatch, phase="prefix")
    s2 = ActorDiagnosticHarness().run_stage_2_state_decision(port, scenarios)
    assert s2.status == "FAIL"
    assert s2.decision_correct_rate == 0.0


def test_stage_3_closed_loop_inspect_passes(runtime, monkeypatch):
    harness = ActorDiagnosticHarness()
    _, config = mock_port(runtime, '{"kind": "ACT", "operation": "INSPECT"}', monkeypatch, phase="prefix")
    s3 = harness.run_stage_3_closed_loop(config, runtime, max_turns=1)
    assert s3.status == "PASS"
    assert s3.actions_executed > 0
    assert "INSPECT" in s3.dispatch_outcomes


def test_stage_3_fails_status_only(runtime, monkeypatch):
    harness = ActorDiagnosticHarness()
    _, config = mock_port(runtime, '{"kind": "ACT", "operation": "STATUS"}', monkeypatch, phase="prefix")
    s3 = harness.run_stage_3_closed_loop(config, runtime, max_turns=3)
    assert s3.status == "FAIL"
    assert "PROGRESS_RATE_BELOW_THRESHOLD" in s3.stop_rule_triggered


def test_stage_3_scores_only_run_llm_window(runtime, monkeypatch):
    harness = ActorDiagnosticHarness(thresholds=DiagnosticThresholds(min_progress_rate=0.30))
    packet = build_packet(runtime, "C0")
    port, config = mock_port(runtime, '{"kind": "ACT", "operation": "INSPECT"}', monkeypatch, phase="prefix")
    s1 = harness.run_stage_1_minimal_proposal(port, packet, n_samples=5)
    assert s1.status == "PASS"
    s3 = harness.run_stage_3_closed_loop(config, runtime, max_turns=3)
    assert s3.status == "PASS"
    assert s3.calls_attempted == 3
    assert s3.actions_executed == 3


def test_stage_3_fails_wait_only(runtime, monkeypatch):
    harness = ActorDiagnosticHarness()
    _, config = mock_port(runtime, '{"kind": "WAIT"}', monkeypatch, phase="prefix")
    s3 = harness.run_stage_3_closed_loop(config, runtime, max_turns=3)
    assert s3.status == "FAIL"


def test_unknown_chat_template_rejected():
    with pytest.raises(ValidationError):
        ModelPortConfig(
            backend="api", model_id="test-model", endpoint="https://model.invalid",
            chat_template="qwen-chat",
        )


def test_run_actor_diagnostic_invokes_stages(runtime, monkeypatch):
    sent = []
    _, config = mock_port(runtime, '{"kind": "ACT", "operation": "INSPECT"}', monkeypatch,
                          phase="prefix", requests=sent)
    report = run_actor_diagnostic(runtime, config)
    assert sent and "proposal_schema" not in sent[0]["prompt"]
    assert "minimal_proposal" in report["stages"]
    assert report["status"] in ("PASS", "STOPPED_AT_STAGE_2", "STOPPED_AT_STAGE_3", "STAGES_1_AND_2_PASSED")
