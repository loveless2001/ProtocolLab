"""P1 Regression tests for per-request budget settlement and lifecycle states (§2).

Acceptance criterion: 3 calls of 100 input + 10 output tokens must total
300 input and 30 output tokens in the ledger, NOT 600 input / 60 output.
"""


import pytest

from protocollab.actor import FrozenModelPort, ModelPortConfig, build_packet
from protocollab.actor.diagnostic_config import DiagnosticConfig
from protocollab.actor.diagnostic_harness import ActorDiagnosticHarness
from protocollab.actor.modes import DecisionAdapter, UnsupportedConfiguration
from protocollab.contracts import canonical


def _make_counting_mock_port(runtime, input_per_call=100, output_per_call=10, phase="prefix"):
    """Mock port returning fixed token counts per call."""
    config = ModelPortConfig(
        backend="api",
        model_id="budget-settlement-fixture",
        endpoint="https://model.invalid",
        max_input_tokens=4096,
        max_output_tokens=1024,
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
                "input_tokens": input_per_call,
                "output_tokens": output_per_call,
            })

    def open_request(req, timeout=120):
        return Response()

    import urllib.request
    urllib.request.urlopen = open_request
    port = FrozenModelPort(config, runtime.store, max_calls=24, phase=phase)
    return port, config


def test_budget_settles_per_request_usage_not_cumulative(runtime, monkeypatch):
    """Acceptance test: 3 calls of 100+10 tokens must total 300/30, not 600/60."""
    port, config = _make_counting_mock_port(runtime, input_per_call=100, output_per_call=10, phase="prefix")

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return canonical({
                "model_id": config.model_id,
                "text": '{"kind": "ACT", "operation": "INSPECT"}',
                "input_tokens": 100,
                "output_tokens": 10,
            })

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=120: Response())

    harness = ActorDiagnosticHarness()
    packet = build_packet(runtime, "C0")
    diag_config = DiagnosticConfig(
        condition="C0",
        model_port_config=config,
        decision_mode="free_json",
    )

    # Run Stage 1 with exactly 3 samples
    s1 = harness.run_stage_1_minimal_proposal(port, packet, n_samples=3, config=diag_config)
    assert s1.status == "PASS"
    assert s1.completed_calls == 3

    # Check the underlying ledger state
    ledger = harness._get_ledger(port.store, diag_config)
    stage_usage = ledger.state.stages["minimal_proposal"]

    # Verify per-request settlement: 3 * 100 = 300, 3 * 10 = 30
    assert stage_usage.input_tokens == 300, (
        f"Expected 300 input tokens, got {stage_usage.input_tokens} (double-counting bug if 600)"
    )
    assert stage_usage.output_tokens == 30, (
        f"Expected 30 output tokens, got {stage_usage.output_tokens} (double-counting bug if 60)"
    )
    assert ledger.state.aggregate.input_tokens == 300
    assert ledger.state.aggregate.output_tokens == 30


def test_lifecycle_state_unsupported_configuration(runtime):
    """UNSUPPORTED_CONFIGURATION lifecycle: preflight raises before admission."""
    # Port declaring only free_json does not support candidate_score
    config = ModelPortConfig(
        backend="api",
        model_id="test-api-model",
        endpoint="https://model.invalid",
        supported_decision_modes=["free_json"],
    )
    diag_config = DiagnosticConfig(
        model_port_config=config,
        decision_mode="candidate_score",
    )
    adapter = DecisionAdapter(diag_config)
    port = FrozenModelPort(config, runtime.store, max_calls=10, phase="prefix")
    packet = build_packet(runtime, "C0")

    with pytest.raises(UnsupportedConfiguration, match="does not support decision mode"):
        adapter.decide(port, packet, seed=7)


def test_lifecycle_state_admission_rejected(runtime, monkeypatch):
    """ADMISSION_REJECTED lifecycle: byte bound exceeded yields ADMISSION_REJECTED outcome."""
    config = ModelPortConfig(
        backend="api",
        model_id="test-model",
        endpoint="https://model.invalid",
        max_input_tokens=50,  # Deliberately tiny limit to trigger admission rejection
    )
    diag_config = DiagnosticConfig(
        model_port_config=config,
        decision_mode="free_json",
    )
    adapter = DecisionAdapter(diag_config)
    port = FrozenModelPort(config, runtime.store, max_calls=10, phase="prefix")
    packet = build_packet(runtime, "C0")

    outcome = adapter.decide(port, packet, seed=7)
    assert outcome.is_fallback is True
    assert outcome.validation_outcome == "ADMISSION_REJECTED"
    assert outcome.backend_meta.get("error_type") == "AdmissionRejected"
