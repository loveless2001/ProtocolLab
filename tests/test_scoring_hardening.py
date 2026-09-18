"""P1 Regression tests for candidate-scoring results hardening (§4).

Verifies identity/fingerprint validation, complete candidate set validation,
finite score validation, and explicit full-continuation scoring semantics.
"""



from protocollab.actor import FrozenModelPort, ModelPortConfig, build_packet
from protocollab.actor.diagnostic_config import DiagnosticConfig
from protocollab.actor.modes import DecisionAdapter


def _setup_scoring_adapter(runtime, monkeypatch, model_id="test-local-model", expected_fingerprint=None):
    monkeypatch.setattr("protocollab.actor.checkpoint_hashes", lambda d: {
        "weights_sha256": "0" * 64,
        "tokenizer_sha256": "0" * 64,
        "config_sha256": "0" * 64,
    })
    config = ModelPortConfig(
        backend="local_frozen_checkpoint",
        model_id=model_id,
        checkpoint="mock-checkpoint-dir",
        weights_sha256="0" * 64,
        tokenizer_sha256="0" * 64,
        config_sha256="0" * 64,
        expected_fingerprint=expected_fingerprint,
    )
    candidates = [
        '{"kind":"ACT","operation":"INSPECT"}',
        '{"kind":"WAIT"}',
    ]
    diag_config = DiagnosticConfig(
        model_port_config=config,
        decision_mode="candidate_score",
        candidate_registry=candidates,
    )
    adapter = DecisionAdapter(diag_config)
    port = FrozenModelPort(config, runtime.store, max_calls=10, phase="suffix")
    packet = build_packet(runtime, "C0")
    return adapter, port, packet, candidates


def test_scoring_validates_model_identifier(runtime, monkeypatch):
    """Model identifier mismatch in scoring results raises ValueError."""
    adapter, port, packet, candidates = _setup_scoring_adapter(
        runtime, monkeypatch, model_id="expected-model"
    )

    def mock_score(prompt, cands, seed=0, claims=None):
        return {
            "model_id": "wrong-model-id",
            "evaluations": [
                {"index": 0, "candidate": candidates[0], "score": -1.5, "tokens": 5},
                {"index": 1, "candidate": candidates[1], "score": -2.0, "tokens": 3},
            ],
            "input_tokens": 50,
            "output_tokens": 8,
        }

    monkeypatch.setattr(port, "score_candidates", mock_score)
    outcome = adapter.decide(port, packet, seed=7)
    assert outcome.is_fallback is True
    assert "MODEL_IDENTIFIER_MISMATCH" in outcome.raw_response


def test_scoring_validates_fingerprint(runtime, monkeypatch):
    """Fingerprint mismatch in scoring results raises ValueError."""
    adapter, port, packet, candidates = _setup_scoring_adapter(
        runtime, monkeypatch, expected_fingerprint="expected-fp-1234"
    )

    def mock_score(prompt, cands, seed=0, claims=None):
        return {
            "model_id": "test-local-model",
            "fingerprint": "different-fp-5678",
            "evaluations": [
                {"index": 0, "candidate": candidates[0], "score": -1.5, "tokens": 5},
                {"index": 1, "candidate": candidates[1], "score": -2.0, "tokens": 3},
            ],
            "input_tokens": 50,
            "output_tokens": 8,
        }

    monkeypatch.setattr(port, "score_candidates", mock_score)
    outcome = adapter.decide(port, packet, seed=7)
    assert outcome.is_fallback is True
    assert "MODEL_FINGERPRINT_CHANGED" in outcome.raw_response


def test_scoring_rejects_missing_candidate(runtime, monkeypatch):
    """Evaluation count mismatch (missing candidate) raises ValueError."""
    adapter, port, packet, candidates = _setup_scoring_adapter(runtime, monkeypatch)

    def mock_score(prompt, cands, seed=0, claims=None):
        # Return only 1 evaluation when 2 were expected
        return {
            "model_id": "test-local-model",
            "evaluations": [
                {"index": 0, "candidate": candidates[0], "score": -1.5, "tokens": 5},
            ],
            "input_tokens": 50,
            "output_tokens": 5,
        }

    monkeypatch.setattr(port, "score_candidates", mock_score)
    outcome = adapter.decide(port, packet, seed=7)
    assert outcome.is_fallback is True
    assert "CANDIDATE_COUNT_MISMATCH" in outcome.raw_response


def test_scoring_rejects_non_finite_scores(runtime, monkeypatch):
    """NaN or Inf score in evaluations raises ValueError."""
    adapter, port, packet, candidates = _setup_scoring_adapter(runtime, monkeypatch)

    def mock_score(prompt, cands, seed=0, claims=None):
        return {
            "model_id": "test-local-model",
            "evaluations": [
                {"index": 0, "candidate": candidates[0], "score": float("nan"), "tokens": 5},
                {"index": 1, "candidate": candidates[1], "score": -2.0, "tokens": 3},
            ],
            "input_tokens": 50,
            "output_tokens": 8,
        }

    monkeypatch.setattr(port, "score_candidates", mock_score)
    outcome = adapter.decide(port, packet, seed=7)
    assert outcome.is_fallback is True
    assert "NON_FINITE_SCORE" in outcome.raw_response


def test_scoring_semantics_explicitly_declared(runtime, monkeypatch):
    """Successful scoring outcome declares full_continuation_log_likelihood semantics."""
    adapter, port, packet, candidates = _setup_scoring_adapter(runtime, monkeypatch)

    def mock_score(prompt, cands, seed=0, claims=None):
        return {
            "model_id": "test-local-model",
            "evaluations": [
                {"index": 0, "candidate": candidates[0], "score": -1.5, "tokens": 5},
                {"index": 1, "candidate": candidates[1], "score": -0.5, "tokens": 3},
            ],
            "input_tokens": 50,
            "output_tokens": 8,
        }

    monkeypatch.setattr(port, "score_candidates", mock_score)
    outcome = adapter.decide(port, packet, seed=7)
    assert outcome.is_fallback is False
    assert outcome.scoring_semantics == "full_continuation_log_likelihood"
    assert outcome.proposal.kind == "WAIT"  # candidate 1 had higher score (-0.5 > -1.5)
