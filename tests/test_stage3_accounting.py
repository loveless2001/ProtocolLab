"""P1 Regression tests for Stage 3 execution and progress accounting (§1).

Verifies distinct lifecycle tracking: proposed, denied/stale, dispatched,
acknowledged, and effective actions. Dispatches alone do not constitute progress.
"""


import pytest

from protocollab.actor import FrozenModelPort, ModelPortConfig
from protocollab.actor.diagnostic_harness import ActorDiagnosticHarness
from protocollab.contracts import canonical


def _make_mock_port(runtime, responses, monkeypatch, phase="suffix"):
    """Fixture returning a mock port that returns items from responses sequence."""
    config = ModelPortConfig(
        backend="api", model_id="stage3-accounting-fixture", endpoint="https://model.invalid",
    )
    call_idx = [0]

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            resp_text = responses[min(call_idx[0], len(responses) - 1)]
            call_idx[0] += 1
            return canonical({
                "model_id": config.model_id,
                "text": resp_text,
                "input_tokens": 50,
                "output_tokens": 15,
            })

    def open_request(req, timeout=120):
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    port = FrozenModelPort(config, runtime.store, max_calls=24, phase=phase)
    return port, config


def test_stage_3_wait_only_yields_zero_dispatches(runtime, monkeypatch):
    """Case 1: WAIT-only model yields 0 executed dispatches, not max_turns."""
    port, config = _make_mock_port(
        runtime, ['{"kind": "WAIT"}'], monkeypatch
    )
    harness = ActorDiagnosticHarness()
    s3 = harness.run_stage_3_closed_loop(config, runtime, max_turns=4, port=port)

    assert s3.proposed_actions == 4
    assert s3.dispatched_actions == 0
    assert s3.actions_executed == 0
    assert s3.acknowledged_actions == 0
    assert s3.effective_actions == 0
    assert s3.task_progress_count == 0
    assert s3.status == "FAIL"
    assert "PROGRESS_RATE_BELOW_THRESHOLD" in s3.stop_rule_triggered


def test_stage_3_mutation_denied_by_governance(runtime, monkeypatch):
    """Case 2: Operation denied by governance counts as proposed and denied/stale, not executed or effective."""
    # Revoke permissions for SUBMIT_A
    state = runtime.governance.snapshot
    state["permissions"]["R"] = [op for op in state["permissions"]["R"] if op != "SUBMIT_A"]
    runtime.store.set("governance", "control", state, "test.permission_revoked")

    port, config = _make_mock_port(
        runtime, ['{"kind": "ACT", "operation": "SUBMIT_A"}'], monkeypatch
    )
    harness = ActorDiagnosticHarness()
    s3 = harness.run_stage_3_closed_loop(config, runtime, max_turns=3, port=port)

    assert s3.proposed_actions == 3
    assert s3.denied_or_stale_actions == 3
    assert s3.dispatched_actions == 0
    assert s3.actions_executed == 0
    assert s3.acknowledged_actions == 0
    assert s3.effective_actions == 0
    assert s3.status == "FAIL"


def test_stage_3_repeated_identical_inspect_fails_progress(runtime, monkeypatch):
    """Case 3: Repeated identical INSPECT yields only 1 effective observation, failing progress gate."""
    # 3 turns of identical INSPECT on unchanged state
    port, config = _make_mock_port(
        runtime, ['{"kind": "ACT", "operation": "INSPECT"}'], monkeypatch
    )
    harness = ActorDiagnosticHarness()
    s3 = harness.run_stage_3_closed_loop(config, runtime, max_turns=3, port=port)

    # All 3 were proposed and dispatched
    assert s3.proposed_actions == 3
    assert s3.dispatched_actions == 3
    assert s3.acknowledged_actions == 3
    # But only the first was novel; identical subsequent inspects are not effective
    assert s3.effective_actions == 1
    assert s3.task_progress_count == 1
    assert s3.task_progress_rate == pytest.approx(1 / 3, 0.01)
    # Threshold is 0.50, so 0.33 fails
    assert s3.status == "FAIL"
    assert "PROGRESS_RATE_BELOW_THRESHOLD" in s3.stop_rule_triggered


def test_stage_3_novel_inspect_counts_effective(runtime, monkeypatch):
    """Case 4: INSPECT yields novel observation correlated with command ID."""
    # Single-turn inspect: 1 inspect out of 1 call = 1.0 progress rate
    port, config = _make_mock_port(
        runtime, ['{"kind": "ACT", "operation": "INSPECT"}'], monkeypatch
    )
    harness = ActorDiagnosticHarness()
    s3 = harness.run_stage_3_closed_loop(config, runtime, max_turns=1, port=port)

    assert s3.proposed_actions == 1
    assert s3.dispatched_actions == 1
    assert s3.acknowledged_actions == 1
    assert s3.effective_actions == 1
    assert s3.task_progress_rate == 1.0
    assert s3.status == "PASS"


def test_stage_3_task_completing_act_sequence(runtime, monkeypatch):
    """Case 5: Task-completing sequence satisfies contract and passes."""
    responses = [
        '{"kind": "ACT", "operation": "INSPECT"}',
        '{"kind": "ACT", "operation": "SUBMIT_A"}',
        '{"kind": "FINISH"}',
    ]
    port, config = _make_mock_port(runtime, responses, monkeypatch)
    harness = ActorDiagnosticHarness()
    s3 = harness.run_stage_3_closed_loop(config, runtime, max_turns=5, port=port)

    assert s3.proposed_actions >= 2
    assert s3.dispatched_actions >= 1
    assert s3.acknowledged_actions >= 1
    assert s3.effective_actions >= 1
