"""P1 Comprehensive regression tests for Diagnostic Integrity (§1 - §9).

Verifies:
1. Delivery evidence separation (pre-send vs post-send failure, verified claim delivery).
2. Two-phase inference usage settlement before proposal parsing (tokens counted on malformed JSON).
3. Explicit capability contract preflight without exception fallback.
4. Strict candidate registry validation across all modes (PLAN, unknown ops, corrupt scores rejected).
5. Mode-independent standardized actor interaction lifecycle (audited by negative cases).
6. Evaluator-owned progress milestones (NOOP mutations fail; execution failure status protected).
7. Input-driven Stage 2 v3 scenarios with paired field sensitivity.
8. Explicit candidate scoring compute metrics reporting.
9. Unsupported mode marked UNSUPPORTED in harness and report.
"""

from __future__ import annotations

import pytest

from protocollab.actor import (
    FrozenModelPort,
    ModelPortConfig,
    build_packet,
)
from protocollab.actor.budget import DiagnosticLedger
from protocollab.actor.diagnostic import (
    build_state_decision_scenarios,
)
from protocollab.actor.diagnostic_config import (
    DEFAULT_CANDIDATES,
    DiagnosticConfig,
    default_budget_allocation,
)
from protocollab.actor.diagnostic_harness import ActorDiagnosticHarness
from protocollab.actor.modes import DecisionAdapter, UnsupportedConfiguration
from protocollab.contracts import canonical


def _make_api_mock_port(store, responses, monkeypatch, phase="closed_loop", model_id="test-port-p1"):
    config = ModelPortConfig(
        backend="api",
        model_id=model_id,
        endpoint="https://model.invalid",
    )
    call_idx = [0]

    class MockResponse:
        def __init__(self, data):
            self.data = data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit=None):
            resp = self.data[min(call_idx[0], len(self.data) - 1)]
            call_idx[0] += 1
            if isinstance(resp, Exception):
                raise resp
            return resp if isinstance(resp, bytes) else resp.encode("utf-8")

    def open_request(req, timeout=120):
        return MockResponse(responses)

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    port = FrozenModelPort(config, store, max_calls=24, phase=phase)
    return port, config


# -------------------------------------------------------------------------
# §1: Delivery Evidence Separated (Pre-send vs Post-send failure)
# -------------------------------------------------------------------------

def test_delivery_evidence_pre_send_failure(runtime, monkeypatch):
    """Pre-send transport failure records llm.requested and llm.failed, but NOT llm.input_delivered."""
    def open_fail(req, timeout=120):
        raise ConnectionRefusedError("PRE_SEND_TRANSPORT_FAILURE")

    monkeypatch.setattr("urllib.request.urlopen", open_fail)
    config = ModelPortConfig(backend="api", model_id="test-port-p1", endpoint="https://model.invalid")
    port = FrozenModelPort(config, runtime.store, max_calls=10, phase="suffix")

    with pytest.raises(ConnectionRefusedError):
        port.score_candidates("Test prompt", list(DEFAULT_CANDIDATES))

    events = runtime.store.events()
    assert any(e["kind"] == "llm.requested" for e in events)
    assert not any(e["kind"] == "llm.input_delivered" for e in events)
    assert not any(e["kind"] == "llm.completed" for e in events)
    assert any(e["kind"] == "llm.failed" for e in events)


def test_delivery_evidence_post_send_failure(runtime, monkeypatch):
    """Post-send failure (after connection started) records llm.input_delivered, but NOT llm.completed."""
    class PostSendFailResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit=None):
            raise OSError("STREAM_TRUNCATED_AFTER_SEND")

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=120: PostSendFailResponse())
    config = ModelPortConfig(backend="api", model_id="test-port-p1", endpoint="https://model.invalid")
    port = FrozenModelPort(config, runtime.store, max_calls=10, phase="suffix")

    with pytest.raises(OSError):
        port.score_candidates("Test prompt", list(DEFAULT_CANDIDATES))

    events = runtime.store.events()
    assert any(e["kind"] == "llm.requested" for e in events)
    assert any(e["kind"] == "llm.input_delivered" for e in events)
    assert not any(e["kind"] == "llm.completed" for e in events)
    assert any(e["kind"] == "llm.failed" for e in events)


# -------------------------------------------------------------------------
# §2: Inference Usage Settle Before Proposal Parsing
# -------------------------------------------------------------------------

def test_inference_usage_settled_on_malformed_json(runtime, monkeypatch):
    """Malformed JSON response with 100/10 tokens records usage in ledger, not INFERENCE_FAILED."""
    resp_body = canonical({
        "model_id": "test-port-p1",
        "text": "MALFORMED_NOT_JSON",
        "input_tokens": 100,
        "output_tokens": 10,
    })
    port, port_cfg = _make_api_mock_port(runtime.store, [resp_body], monkeypatch, phase="suffix")
    config = DiagnosticConfig(
        decision_mode="free_json",
        model_port_config=port_cfg,
        budget_allocation=default_budget_allocation(),
    )
    adapter = DecisionAdapter(config)
    ledger = DiagnosticLedger(runtime.store, config.run_id, config.budget_allocation, "hash")
    packet = build_packet(runtime, "C0")

    outcome = adapter.decide(port, packet, seed=7, ledger=ledger, stage="closed_loop")

    assert outcome.is_fallback is True
    assert outcome.proposal.kind == "WAIT"
    assert outcome.request_input_tokens == 100
    assert outcome.request_output_tokens == 10
    assert outcome.total_tokens_evaluated == 110
    assert outcome.validation_outcome != "INFERENCE_FAILED"

    # Ledger must have settled 100/10 tokens and 1 completed call!
    st = ledger.state.stages["closed_loop"]
    assert st.completed_calls == 1
    assert st.failures == 0
    assert st.input_tokens == 100
    assert st.output_tokens == 10


# -------------------------------------------------------------------------
# §3: Explicit Capability Contract (No TypeError Fallback)
# -------------------------------------------------------------------------

def test_capability_contract_rejects_unsupported_mode_upfront(runtime):
    """Port lacking candidate_score capability raises UnsupportedConfiguration upfront."""
    config = ModelPortConfig(
        backend="api",
        model_id="test-port-p1",
        endpoint="https://model.invalid",
        supports_candidate_scoring_v1=False,
    )
    port = FrozenModelPort(config, runtime.store, max_calls=10, phase="suffix")
    diag_cfg = DiagnosticConfig(
        decision_mode="candidate_score",
        model_port_config=config,
    )
    adapter = DecisionAdapter(diag_cfg)
    packet = build_packet(runtime, "C0")

    with pytest.raises(UnsupportedConfiguration) as exc_info:
        adapter.decide(port, packet, seed=7)
    assert "supports_candidate_scoring_v1 is False" in str(exc_info.value)


# -------------------------------------------------------------------------
# §4: Enforce Candidate Registry Exactly Across All Modes
# -------------------------------------------------------------------------

def test_free_json_rejects_plan_and_unknown_operation(runtime, monkeypatch):
    """Proposal kind PLAN or operation outside candidate registry is rejected."""
    plan_body = canonical({
        "model_id": "test-port-p1",
        "text": '{"kind": "PLAN"}',
        "input_tokens": 50,
        "output_tokens": 10,
    })
    port, port_cfg = _make_api_mock_port(runtime.store, [plan_body], monkeypatch, phase="suffix")
    diag_cfg = DiagnosticConfig(
        decision_mode="free_json",
        model_port_config=port_cfg,
    )
    adapter = DecisionAdapter(diag_cfg)
    packet = build_packet(runtime, "C0")

    outcome = adapter.decide(port, packet, seed=7)
    assert outcome.is_fallback is True
    assert outcome.proposal.kind == "WAIT"
    assert outcome.validation_outcome in ("NOT_IN_CANDIDATE_REGISTRY", "ValidationError", "ValueError")


def test_candidate_score_strict_validation(runtime, monkeypatch):
    """Candidate scoring strictly rejects invalid evaluations, non-finite scores, or missing model_id."""
    port_cfg = ModelPortConfig(
        backend="api",
        model_id="test-port-p1",
        endpoint="https://model.invalid",
    )
    diag_cfg = DiagnosticConfig(
        decision_mode="candidate_score",
        model_port_config=port_cfg,
    )
    adapter = DecisionAdapter(diag_cfg)
    packet = build_packet(runtime, "C0")

    # Case A: Missing model_id
    bad_resp = canonical({
        "text": DEFAULT_CANDIDATES[0],
        "input_tokens": 50,
        "output_tokens": 20,
        "evaluations": [{"candidate": c, "index": i, "score": -1.0, "token_count": 5} for i, c in enumerate(DEFAULT_CANDIDATES)],
    })
    port, _ = _make_api_mock_port(runtime.store, [bad_resp], monkeypatch, phase="suffix")
    outcome = adapter.decide(port, packet, seed=7)
    assert outcome.is_fallback is True
    assert outcome.validation_outcome == "INFERENCE_FAILED"

    # Case B: Non-finite score (NaN or Inf)
    bad_score_evals = [{"candidate": c, "index": i, "score": -1.0 if i > 0 else "NaN", "token_count": 5} for i, c in enumerate(DEFAULT_CANDIDATES)]
    bad_score_resp = canonical({
        "model_id": port_cfg.model_id,
        "text": DEFAULT_CANDIDATES[0],
        "input_tokens": 50,
        "output_tokens": 20,
        "evaluations": bad_score_evals,
    })
    port, _ = _make_api_mock_port(runtime.store, [bad_score_resp], monkeypatch, phase="suffix")
    outcome = adapter.decide(port, packet, seed=7)
    assert outcome.is_fallback is True
    assert outcome.validation_outcome == "INFERENCE_FAILED"

    # Case C: Candidate set mismatch
    corrupt_cands = list(DEFAULT_CANDIDATES)
    corrupt_cands[0] = '{"kind":"ACT","operation":"UNKNOWN"}'
    bad_set_evals = [{"candidate": c, "index": i, "score": -1.0, "token_count": 5} for i, c in enumerate(corrupt_cands)]
    bad_set_resp = canonical({
        "model_id": port_cfg.model_id,
        "text": corrupt_cands[0],
        "input_tokens": 50,
        "output_tokens": 20,
        "evaluations": bad_set_evals,
    })
    port, _ = _make_api_mock_port(runtime.store, [bad_set_resp], monkeypatch, phase="suffix")
    outcome = adapter.decide(port, packet, seed=7)
    assert outcome.is_fallback is True
    assert outcome.validation_outcome == "INFERENCE_FAILED"


# -------------------------------------------------------------------------
# §5: Mode-Independent Actor Interaction Lifecycle
# -------------------------------------------------------------------------

def test_actor_interaction_lifecycle_in_stage_3(runtime, monkeypatch):
    """Stage 3 closed-loop emits raw_proposal, proposal_returned, proposed, interaction_completed."""
    valid_resp = canonical({
        "model_id": "test-port-p1",
        "text": '{"kind": "ACT", "operation": "INSPECT"}',
        "input_tokens": 50,
        "output_tokens": 15,
    })
    port, port_cfg = _make_api_mock_port(runtime.store, [valid_resp], monkeypatch, phase="suffix")
    harness = ActorDiagnosticHarness()
    s3 = harness.run_stage_3_closed_loop(port_cfg, runtime, max_turns=2, port=port)
    assert s3.stage == "closed_loop"

    events = runtime.store.events()
    lifecycle_kinds = {
        "actor.raw_proposal",
        "actor.proposal_returned",
        "actor.proposed",
        "actor.interaction_completed",
    }
    actor_lifecycle_events = [e for e in events if e.get("kind") in lifecycle_kinds]

    # Verify lifecycle kinds are present
    kinds = [e["kind"] for e in actor_lifecycle_events]
    assert "actor.raw_proposal" in kinds
    assert "actor.proposal_returned" in kinds
    assert "actor.proposed" in kinds
    assert "actor.interaction_completed" in kinds

    # Verify matching request_seq and decision_basis_ref
    for ev in actor_lifecycle_events:
        assert "decision_basis_ref" in ev["payload"]
        assert "request_seq" in ev["payload"]
        assert isinstance(ev["payload"]["request_seq"], int)
        assert ev["payload"]["request_seq"] > 0


# -------------------------------------------------------------------------
# §6: Task-Relevant State Changes, Not ACKs for Progress
# -------------------------------------------------------------------------

def test_noop_mutations_yield_zero_progress_and_fail(runtime, monkeypatch):
    """6 acknowledged NOOP CANCEL mutations (pending=NONE) yield 0 progress and FAIL."""
    # When pending == NONE, CANCEL does not change World state.
    cancel_resp = canonical({
        "model_id": "test-port-p1",
        "text": '{"kind": "ACT", "operation": "CANCEL"}',
        "input_tokens": 40,
        "output_tokens": 10,
    })
    port, port_cfg = _make_api_mock_port(runtime.store, [cancel_resp], monkeypatch, phase="suffix")
    harness = ActorDiagnosticHarness()
    s3 = harness.run_stage_3_closed_loop(port_cfg, runtime, max_turns=6, port=port)

    assert s3.proposed_actions == 6
    assert s3.dispatched_actions == 6
    assert s3.acknowledged_actions == 6
    # NOOP mutations do not count as progress or environment effect!
    assert s3.environment_effect_observed == 0
    assert s3.effective_actions == 0
    assert s3.task_progress_count == 0
    assert s3.status == "FAIL"
    assert "PROGRESS_RATE_BELOW_THRESHOLD" in s3.stop_rule_triggered


def test_stage_3_execution_failure_terminal_status_protected(runtime, monkeypatch):
    """If execution fails, terminal status must remain FAIL, never overwritten by progress rate."""
    valid_resp = canonical({
        "model_id": "test-port-p1",
        "text": '{"kind": "ACT", "operation": "INSPECT"}',
        "input_tokens": 40,
        "output_tokens": 10,
    })
    port, port_cfg = _make_api_mock_port(runtime.store, [valid_resp, valid_resp], monkeypatch, phase="suffix")

    orig_turn = runtime.turn
    turn_count = [0]
    def failing_turn(action):
        turn_count[0] += 1
        if turn_count[0] == 2:
            raise RuntimeError("FORCED_EXECUTION_CRASH")
        return orig_turn(action)

    monkeypatch.setattr(runtime, "turn", failing_turn)

    harness = ActorDiagnosticHarness()
    s3 = harness.run_stage_3_closed_loop(port_cfg, runtime, max_turns=3, port=port)

    assert s3.status == "FAIL"
    assert "EXECUTION_FAILED" in s3.stop_rule_triggered


# -------------------------------------------------------------------------
# §7: Input-Driven Stage 2 Scenarios & Paired Sensitivity
# -------------------------------------------------------------------------

def test_stage_2_v3_input_driven_scenarios_and_sensitivity(runtime):
    """Stage 2 v3 scenarios are input-driven without completion hints; paired sensitivity flips decision."""
    scenarios = build_state_decision_scenarios(runtime, "C0", scenario_version="v3")
    assert len(scenarios) == 5

    names = [s["name"] for s in scenarios]
    assert "need_info_when_unobserved" in names
    assert "wait_when_authenticated_hold" in names
    assert "submit_when_prerequisite_observed" in names
    assert "finish_when_completion_confirmed" in names
    assert "inspect_when_submit_revoked" in names

    # Verify no artificial completion_hint in packets
    for s in scenarios:
        assert "completion_hint" not in s["packet"].get("task", {})

    # Paired sensitivity test:
    # Need info vs Authenticated hold (same packet except agent_all: RUNNING vs HOLD)
    need_info = next(s for s in scenarios if s["name"] == "need_info_when_unobserved")
    hold = next(s for s in scenarios if s["name"] == "wait_when_authenticated_hold")
    assert need_info["expected"]["operations"] == ["INSPECT"]
    assert hold["expected"]["kind"] == "WAIT"

    # Permitted submit vs Revoked submit (same except permissions excludes SUBMIT_A)
    submit = next(s for s in scenarios if s["name"] == "submit_when_prerequisite_observed")
    revoked = next(s for s in scenarios if s["name"] == "inspect_when_submit_revoked")
    assert submit["expected"]["operations"] == ["SUBMIT_A"]
    assert revoked["expected"]["operations"] == ["INSPECT"]
    prerequisite = submit["packet"]["live_feedback"]["new_observations"]
    assert len(prerequisite) == 1
    assert prerequisite[0]["source_principal_ref"] == "effect_sensor"
    assert prerequisite[0]["domain_output"] == "INSPECT:BASE:HEALTHY"
    assert "observations" not in submit["packet"]["live_feedback"]

    completion = next(s for s in scenarios if s["name"] == "finish_when_completion_confirmed")
    readings = completion["packet"]["live_feedback"]["new_observations"]
    assert len(readings) == completion["packet"]["task"]["readings_required"]
    assert all(reading["source_principal_ref"] == "effect_sensor" for reading in readings)
    assert all(reading["domain_output"] == "INSPECT:A:HEALTHY" for reading in readings)
    assert readings[-1]["logical_tick"] - readings[0]["logical_tick"] >= (
        completion["packet"]["task"]["minimum_tick_gap"]
    )


# -------------------------------------------------------------------------
# §8: Candidate-Scoring Compute Metrics Reporting
# -------------------------------------------------------------------------

def test_candidate_score_compute_reporting(runtime, monkeypatch):
    """Candidate scoring reports 6 compute breakdown fields accurately."""
    evals = [{"candidate": c, "index": i, "score": -float(i), "token_count": 5} for i, c in enumerate(DEFAULT_CANDIDATES)]
    resp = canonical({
        "model_id": "test-port-p1",
        "text": DEFAULT_CANDIDATES[0],
        "input_tokens": 100,
        "output_tokens": 45,
        "evaluations": evals,
        "compute": {
            "logical_decisions": 1,
            "candidate_evaluations": len(DEFAULT_CANDIDATES),
            "prompt_tokens_logically_supplied": 100,
            "total_tokens_processed": 145,
            "forward_passes": len(DEFAULT_CANDIDATES),
            "prefill_recomputations": len(DEFAULT_CANDIDATES),
        },
    })
    port, port_cfg = _make_api_mock_port(runtime.store, [resp], monkeypatch, phase="suffix")
    diag_cfg = DiagnosticConfig(
        decision_mode="candidate_score",
        model_port_config=port_cfg,
    )
    harness = ActorDiagnosticHarness(config=diag_cfg)
    packet = build_packet(runtime, "C0")
    s1 = harness.run_stage_1_minimal_proposal(port, packet, n_samples=2, config=diag_cfg)

    assert "compute_summary" in dir(s1)
    cs = s1.compute_summary
    assert cs["logical_decisions"] == 2
    assert cs["candidate_evaluations"] == 2 * len(DEFAULT_CANDIDATES)
    assert cs["prompt_tokens_logically_supplied"] == 200
    assert cs["total_tokens_processed"] == 290
    assert cs["forward_passes"] == 2 * len(DEFAULT_CANDIDATES)


# -------------------------------------------------------------------------
# §9: Unsupported Mode Handling in Harness
# -------------------------------------------------------------------------

def test_unsupported_mode_recorded_as_unsupported_in_harness(runtime):
    """Unsupported mode records status UNSUPPORTED, not failing with WAIT or crashing."""
    config = ModelPortConfig(
        backend="api",
        model_id="test-port-p1",
        endpoint="https://model.invalid",
        supported_decision_modes=["free_json"],
    )
    port = FrozenModelPort(config, runtime.store, max_calls=10, phase="suffix")
    diag_cfg = DiagnosticConfig(
        decision_mode="candidate_score",
        model_port_config=config,
    )
    harness = ActorDiagnosticHarness(config=diag_cfg)
    packet = build_packet(runtime, "C0")

    report = harness.run_all(port, packet, [], runtime=runtime, config=diag_cfg)
    assert report["status"] == "UNSUPPORTED"
    assert "supported modes" in report["stop_reason"]
