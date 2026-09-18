"""P1 Regression tests for executing declared decision mode in every stage (§3).

Verifies Stages 1-2 route through DecisionAdapter, preflight checks enforce
backend capabilities, and candidate registry is enforced.
"""

import json

import pytest

from protocollab.actor import FrozenModelPort, ModelPortConfig, build_packet
from protocollab.actor.diagnostic import build_state_decision_scenarios
from protocollab.actor.diagnostic_config import DiagnosticConfig
from protocollab.actor.diagnostic_harness import ActorDiagnosticHarness
from protocollab.actor.modes import DecisionAdapter, UnsupportedConfiguration
from protocollab.contracts import canonical


def test_stage_1_routes_through_adapter_with_constrained_json(runtime, monkeypatch):
    """Stage 1 uses DecisionAdapter and passes decision_mode="constrained_json" to the port."""
    config = ModelPortConfig(
        backend="api",
        model_id="routing-test-model",
        endpoint="https://model.invalid",
    )
    sent_requests = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return canonical({
                "model_id": config.model_id,
                "text": '{"kind": "ACT", "operation": "INSPECT"}',
                "input_tokens": 20,
                "output_tokens": 10,
            })

    def open_request(req, timeout=120):
        sent_requests.append(json.loads(req.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    port = FrozenModelPort(config, runtime.store, max_calls=10, phase="prefix")
    packet = build_packet(runtime, "C0")

    diag_config = DiagnosticConfig(
        model_port_config=config,
        decision_mode="constrained_json",
    )
    harness = ActorDiagnosticHarness(config=diag_config)
    s1 = harness.run_stage_1_minimal_proposal(port, packet, n_samples=2, config=diag_config)

    assert s1.status == "PASS"
    assert len(sent_requests) == 2
    # Verify the request received by the mock has decision_mode set
    assert sent_requests[0].get("decision_mode") == "constrained_json"
    assert "candidates" in sent_requests[0]


def test_stage_2_routes_through_adapter(runtime, monkeypatch):
    """Stage 2 routes through DecisionAdapter and records usage per request."""
    config = ModelPortConfig(
        backend="api",
        model_id="routing-stage2-model",
        endpoint="https://model.invalid",
    )

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return canonical({
                "model_id": config.model_id,
                "text": '{"kind": "ACT", "operation": "INSPECT"}',
                "input_tokens": 25,
                "output_tokens": 10,
            })

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=120: Response())
    port = FrozenModelPort(config, runtime.store, max_calls=10, phase="prefix")

    diag_config = DiagnosticConfig(
        model_port_config=config,
        decision_mode="free_json",
    )
    harness = ActorDiagnosticHarness(config=diag_config)
    scenarios = build_state_decision_scenarios(runtime, "C0", scenario_version="v1")
    s2 = harness.run_stage_2_state_decision(port, scenarios, config=diag_config)

    assert s2.status == "PASS"
    assert s2.completed_calls == len(scenarios)


def test_empty_candidate_registry_rejected_in_constrained_mode(runtime):
    """Empty candidate registry raises UnsupportedConfiguration in adapter and records UNSUPPORTED in harness."""
    config = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
    )
    diag_config = DiagnosticConfig(
        model_port_config=config,
        decision_mode="constrained_json",
        candidate_registry=[],  # empty
    )
    port = FrozenModelPort(config, runtime.store, max_calls=10, phase="prefix")
    packet = build_packet(runtime, "C0")

    # Direct adapter call raises UnsupportedConfiguration upfront
    adapter = DecisionAdapter(diag_config)
    with pytest.raises(UnsupportedConfiguration, match="requires a non-empty candidate_registry"):
        adapter.decide(port, packet, seed=7)

    # Harness records UNSUPPORTED status without crashing (§9)
    harness = ActorDiagnosticHarness(config=diag_config)
    s1 = harness.run_stage_1_minimal_proposal(port, packet, n_samples=1, config=diag_config)
    assert s1.status == "UNSUPPORTED"
    assert "UNSUPPORTED_CONFIGURATION" in s1.stop_rule_triggered
