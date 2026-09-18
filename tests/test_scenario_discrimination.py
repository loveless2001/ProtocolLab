"""P2 Regression tests for diagnostic case strengthening and scenario discrimination (§6).

Verifies that v2 scenarios require different decisions such that constant-policy
models (constant INSPECT or constant WAIT) cannot pass Stage 2, while
historical v1 scenarios remain auditable.
"""

import pytest

from protocollab.actor import FrozenModelPort, ModelPortConfig
from protocollab.actor.diagnostic import (
    ActorDiagnosticHarness,
    build_state_decision_scenarios,
)
from protocollab.actor.diagnostic_config import DiagnosticConfig
from protocollab.contracts import canonical


def _make_constant_mock_port(runtime, response_text, phase="prefix"):
    config = ModelPortConfig(
        backend="api", model_id="constant-policy-fixture", endpoint="https://model.invalid",
    )

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return canonical({
                "model_id": config.model_id,
                "text": response_text,
                "input_tokens": 20,
                "output_tokens": 10,
            })

    import urllib.request
    urllib.request.urlopen = lambda req, timeout=120: Response()
    return FrozenModelPort(config, runtime.store, max_calls=24, phase=phase), config


def test_v1_legacy_scenarios_pass_constant_inspect_policy(runtime, monkeypatch):
    """Historical v1 scenarios allowed a constant INSPECT policy to pass."""
    port, config = _make_constant_mock_port(
        runtime, '{"kind": "ACT", "operation": "INSPECT"}', phase="prefix"
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
                "input_tokens": 20,
                "output_tokens": 10,
            })

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=120: Response())

    scenarios_v1 = build_state_decision_scenarios(runtime, "C0", scenario_version="v1")
    assert len(scenarios_v1) == 2

    # Under v1, both scenarios accepted INSPECT
    diag_config = DiagnosticConfig(model_port_config=config, scenario_version="v1")
    s2 = ActorDiagnosticHarness(config=diag_config).run_stage_2_state_decision(
        port, scenarios_v1, config=diag_config
    )
    assert s2.decision_correct_rate == 1.0
    assert s2.status == "PASS"


def test_v2_scenarios_fail_constant_inspect_policy(runtime, monkeypatch):
    """Under v2 scenarios, a constant INSPECT policy FAILS because pause strictly requires WAIT."""
    port, config = _make_constant_mock_port(
        runtime, '{"kind": "ACT", "operation": "INSPECT"}', phase="prefix"
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
                "input_tokens": 20,
                "output_tokens": 10,
            })

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=120: Response())

    scenarios_v2 = build_state_decision_scenarios(runtime, "C0", scenario_version="v2")
    assert len(scenarios_v2) == 3

    diag_config = DiagnosticConfig(model_port_config=config, scenario_version="v2")
    s2 = ActorDiagnosticHarness(config=diag_config).run_stage_2_state_decision(
        port, scenarios_v2, config=diag_config
    )
    # Constant INSPECT gets 1 correct out of 3 (fails wait_when_paused and submit_when_running)
    assert s2.decision_correct_rate == pytest.approx(1 / 3, 0.01)
    assert s2.status == "FAIL"
    assert "DECISION_CORRECTNESS_BELOW_THRESHOLD" in s2.stop_rule_triggered


def test_v2_scenarios_fail_constant_wait_policy(runtime, monkeypatch):
    """Under v2 scenarios, a constant WAIT policy also FAILS."""
    port, config = _make_constant_mock_port(
        runtime, '{"kind": "WAIT"}', phase="prefix"
    )

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return canonical({
                "model_id": config.model_id,
                "text": '{"kind": "WAIT"}',
                "input_tokens": 20,
                "output_tokens": 10,
            })

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=120: Response())

    scenarios_v2 = build_state_decision_scenarios(runtime, "C0", scenario_version="v2")
    diag_config = DiagnosticConfig(model_port_config=config, scenario_version="v2")
    s2 = ActorDiagnosticHarness(config=diag_config).run_stage_2_state_decision(
        port, scenarios_v2, config=diag_config
    )
    # Constant WAIT gets 1 correct out of 3 (only wait_when_paused)
    assert s2.decision_correct_rate == pytest.approx(1 / 3, 0.01)
    assert s2.status == "FAIL"


def test_v2_scenarios_pass_discriminating_policy(runtime, monkeypatch):
    """A policy that discriminates between running, paused, and mutation passes v2."""
    config = ModelPortConfig(
        backend="api", model_id="discriminating-fixture", endpoint="https://model.invalid",
    )
    call_idx = [0]
    responses = [
        '{"kind": "ACT", "operation": "INSPECT"}',  # scenario 1: running
        '{"kind": "WAIT"}',                         # scenario 2: paused
        '{"kind": "ACT", "operation": "SUBMIT_A"}',  # scenario 3: mutation
    ]

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            text = responses[call_idx[0] % len(responses)]
            call_idx[0] += 1
            return canonical({
                "model_id": config.model_id,
                "text": text,
                "input_tokens": 20,
                "output_tokens": 10,
            })

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=120: Response())
    port = FrozenModelPort(config, runtime.store, max_calls=24, phase="prefix")

    scenarios_v2 = build_state_decision_scenarios(runtime, "C0", scenario_version="v2")
    diag_config = DiagnosticConfig(model_port_config=config, scenario_version="v2")
    s2 = ActorDiagnosticHarness(config=diag_config).run_stage_2_state_decision(
        port, scenarios_v2, config=diag_config
    )
    assert s2.decision_correct_rate == 1.0
    assert s2.status == "PASS"
