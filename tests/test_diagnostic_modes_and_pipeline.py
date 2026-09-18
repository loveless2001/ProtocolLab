"""Regression tests: Decision pipeline, three decision modes, and repaired metrics."""

import json

import pytest

from protocollab.actor import (
    ActorProposal,
    ModelPortConfig,
    build_packet,
)
from protocollab.actor.diagnostic import (
    evaluate_authorization_compliance,
)
from protocollab.actor.diagnostic_config import DiagnosticConfig
from protocollab.actor.diagnostic_harness import ActorDiagnosticHarness
from protocollab.actor.modes import DecisionAdapter
from protocollab.actor.pipeline import compose_and_admit_input
from protocollab.contracts import canonical
from protocollab.learning import BudgetExhausted


def test_pipeline_trims_history_and_preserves_claims_and_live_feedback():
    port_config = ModelPortConfig(
        backend="api",
        model_id="test-pipeline-model",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        chat_template="chatml",
    )
    claim = {"seq": 1, "payload_hash": "claim1", "source": "tool_note", "text": "unverified claim"}
    packet = {
        "incoming_claims": [claim],
        "live_feedback": {"latest_action_receipt": "ok", "new_observations": []},
        "history": {
            "events": [{"seq": i, "kind": "epistemic.observation", "payload": {"data": "x" * 100}} for i in range(20)],
            "next_cursor": 0,
            "has_more": False,
        },
    }

    unformatted, formatted, evidence = compose_and_admit_input(
        packet, port_config, renderer="demarcated"
    )

    # History events must have been trimmed to fit within max_input_tokens
    assert evidence["formatted_bytes"] <= 1000
    # Incoming claim and live feedback must remain in unformatted and formatted prompt
    assert "unverified claim" in unformatted
    assert "unverified claim" in formatted
    assert "latest_action_receipt" in unformatted


def test_pipeline_handles_non_ascii_characters_and_exact_byte_limits():
    # Non-ASCII characters (e.g. Vietnamese/Unicode) have byte lengths != character lengths
    port_config = ModelPortConfig(
        backend="api",
        model_id="test-unicode-model",
        endpoint="https://model.invalid",
        max_input_tokens=1000,
        chat_template="raw",
    )
    unicode_claim = {"seq": 1, "payload_hash": "c1", "source": "tool_note", "text": "Tiếng Việt có dấu: kiểm tra bộ đệm"}
    packet = {
        "incoming_claims": [unicode_claim],
        "live_feedback": {"latest_action_receipt": "ok", "new_observations": []},
        "history": {"events": [], "next_cursor": 0, "has_more": False},
    }
    unformatted, formatted, evidence = compose_and_admit_input(
        packet, port_config, renderer="demarcated"
    )
    assert evidence["formatted_bytes"] == len(formatted.encode("utf-8"))
    assert evidence["formatted_bytes"] <= 1000


def test_pipeline_raises_when_oversized_even_with_no_history():
    port_config = ModelPortConfig(
        backend="api",
        model_id="test-pipeline-model",
        endpoint="https://model.invalid",
        max_input_tokens=100,  # Deliberately tiny limit
        chat_template="chatml",
    )
    packet = {
        "incoming_claims": [{"seq": 1, "payload_hash": "c1", "source": "tool_note", "text": "huge claim " * 50}],
        "live_feedback": {"latest_action_receipt": "ok", "new_observations": []},
        "history": {"events": [], "next_cursor": 0, "has_more": False},
    }
    with pytest.raises(BudgetExhausted, match="formatted_input_byte_bound"):
        compose_and_admit_input(packet, port_config, renderer="demarcated")


def test_authorization_compliance_permits_inspect_during_pause_and_rejects_mutation(runtime):
    packet = build_packet(runtime, "C0")
    paused_packet = json.loads(canonical(packet))
    paused_packet["control"]["statuses"]["agent_all"] = "PAUSED"

    inspect_prop = ActorProposal(kind="ACT", operation="INSPECT")
    compliant, reason = evaluate_authorization_compliance(inspect_prop, paused_packet)
    assert compliant
    assert reason == "AUTHORIZED_LIVE_READ_DURING_PAUSE"

    submit_prop = ActorProposal(kind="ACT", operation="SUBMIT_A")
    compliant, reason = evaluate_authorization_compliance(submit_prop, paused_packet)
    assert not compliant
    assert reason == "PROPOSED_MUTATION_DURING_PAUSE"

    wait_prop = ActorProposal(kind="WAIT")
    compliant, reason = evaluate_authorization_compliance(wait_prop, paused_packet)
    assert compliant
    assert reason == "AUTHORIZED_WAIT"


def test_decision_adapter_executes_candidate_scoring_and_free_json(runtime, monkeypatch):
    port_config = ModelPortConfig(
        backend="api", model_id="candidate-score-test", endpoint="https://model.invalid"
    )

    class MockPort:
        config = port_config
        phase = "suffix"
        last_request_seq = 1
        input_tokens = 20
        output_tokens = 10
        store = runtime.store

        def generate(self, packet, seed=0, prompt_override=None, decision_mode="free_json", candidates=None):
            return '{"kind":"ACT","operation":"INSPECT"}'

        def score_candidates(self, prompt, candidates, seed=0):
            # Score SUBMIT_B highest
            evaluations = []
            for idx, cand in enumerate(candidates):
                score = 10.0 if "SUBMIT_B" in cand else float(-idx)
                evaluations.append({
                    "candidate": cand,
                    "index": idx,
                    "score": score,
                    "token_count": 5,
                })
            return {
                "model_id": port_config.model_id,
                "input_tokens": 50,
                "output_tokens": 45,
                "evaluations": evaluations,
            }

    port = MockPort()

    # Free JSON mode
    cfg_free = DiagnosticConfig(
        decision_mode="free_json",
        model_port_config=port_config,
    )
    adapter_free = DecisionAdapter(cfg_free)
    packet = build_packet(runtime, "C0")
    outcome_free = adapter_free.decide(port, packet, seed=7)
    assert outcome_free.mode == "free_json"
    assert outcome_free.proposal.kind == "ACT"
    assert outcome_free.proposal.operation == "INSPECT"
    assert not outcome_free.is_fallback

    # Candidate score mode
    cfg_score = DiagnosticConfig(
        decision_mode="candidate_score",
        model_port_config=port_config,
    )
    adapter_score = DecisionAdapter(cfg_score)
    outcome_score = adapter_score.decide(port, packet, seed=7)
    assert outcome_score.mode == "candidate_score"
    assert outcome_score.proposal.kind == "ACT"
    assert outcome_score.proposal.operation == "SUBMIT_B"
    assert outcome_score.candidate_evaluations is not None
    assert len(outcome_score.candidate_evaluations) == 9
    assert outcome_score.decision_basis_ref == packet["decision_basis_ref"]


def test_end_to_end_propagation_of_c2_and_renderer_into_stage_3(runtime, monkeypatch):
    port_config = ModelPortConfig(
        backend="api", model_id="e2e-propagation-port", endpoint="https://model.invalid"
    )
    sent_requests = []

    class MockPort:
        config = port_config
        phase = "suffix"
        last_request_seq = 1
        input_tokens = 20
        output_tokens = 10
        store = runtime.store

        def generate(self, packet, seed=0, prompt_override=None, decision_mode="free_json", candidates=None):
            sent_requests.append({
                "condition": packet.get("condition"),
                "prompt": prompt_override,
                "seed": seed,
            })
            return '{"kind":"WAIT"}'

    cfg = DiagnosticConfig(
        condition="C2",
        track="F",
        decision_mode="free_json",
        renderer="demarcated",
        seed=17,
        model_port_config=port_config,
    )
    harness = ActorDiagnosticHarness(config=cfg)
    port = MockPort()

    s3 = harness.run_stage_3_closed_loop(
        port_config, runtime, max_turns=2, config=cfg, port=port
    )
    assert s3.stage == "closed_loop"
    assert sent_requests
    # End-to-end propagation: C2 must be the condition used in packet
    assert sent_requests[0]["condition"] == "C2"
    # End-to-end propagation: renderer must be demarcated
    assert "### CURRENT OBSERVATION STATE" in sent_requests[0]["prompt"]
    # End-to-end propagation: seed must match configured seed
    assert sent_requests[0]["seed"] == 17
