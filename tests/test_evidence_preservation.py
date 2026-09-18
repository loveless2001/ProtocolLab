"""P1 Regression tests for exposure and interaction evidence preservation (§5).

Verifies claim references are carried through in all decision modes,
delivered-input evidence is recorded for scoring, and DecisionOutcome
retains claim references.
"""


from protocollab.actor import FrozenModelPort, ModelPortConfig, build_packet
from protocollab.actor.diagnostic_config import DiagnosticConfig
from protocollab.actor.modes import DecisionAdapter
from protocollab.contracts import canonical


def test_claim_refs_preserved_in_decision_outcome_and_scoring(runtime, monkeypatch):
    """Claim references from incoming_claims are passed to score_candidates and DecisionOutcome."""
    monkeypatch.setattr("protocollab.actor.checkpoint_hashes", lambda d: {
        "weights_sha256": "0" * 64,
        "tokenizer_sha256": "0" * 64,
        "config_sha256": "0" * 64,
    })
    config = ModelPortConfig(
        backend="local_frozen_checkpoint",
        model_id="evidence-preservation-model",
        checkpoint="mock-checkpoint-dir",
        weights_sha256="0" * 64,
        tokenizer_sha256="0" * 64,
        config_sha256="0" * 64,
    )
    candidates = ['{"kind":"ACT","operation":"INSPECT"}', '{"kind":"WAIT"}']
    diag_config = DiagnosticConfig(
        model_port_config=config,
        decision_mode="candidate_score",
        candidate_registry=candidates,
    )
    adapter = DecisionAdapter(diag_config)
    port = FrozenModelPort(config, runtime.store, max_calls=10, phase="suffix")

    # Packet with incoming claims
    packet = build_packet(runtime, "C0")
    packet["incoming_claims"] = [
        {"seq": 42, "payload_hash": "a" * 64, "claim_text": "sample claim"},
    ]

    received_claims = []

    def mock_score(prompt, cands, seed=0, claims=None):
        if claims:
            received_claims.extend(claims)
        return {
            "model_id": config.model_id,
            "evaluations": [
                {"index": 0, "candidate": candidates[0], "score": -0.1, "tokens": 5},
                {"index": 1, "candidate": candidates[1], "score": -2.0, "tokens": 3},
            ],
            "input_tokens": 50,
            "output_tokens": 8,
        }

    monkeypatch.setattr(port, "score_candidates", mock_score)
    outcome = adapter.decide(port, packet, seed=7)

    # Claim refs carried into score_candidates call
    assert len(received_claims) == 1
    assert received_claims[0]["seq"] == 42
    assert received_claims[0]["payload_hash"] == "a" * 64

    # Claim refs attached to DecisionOutcome
    assert len(outcome.claim_refs) == 1
    assert outcome.claim_refs[0]["seq"] == 42


def test_scoring_records_input_delivered_event(runtime, monkeypatch):
    """score_candidates records llm.input_delivered in the store with claim refs."""
    config = ModelPortConfig(
        backend="api",
        model_id="delivery-test-model",
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
                "evaluations": [
                    {"index": 0, "candidate": '{"kind":"WAIT"}', "score": -0.5, "tokens": 2}
                ],
                "input_tokens": 30,
                "output_tokens": 5,
            })

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=120: Response())
    port = FrozenModelPort(config, runtime.store, max_calls=10, phase="suffix")

    claims = [{"seq": 10, "payload_hash": "b" * 64}]
    port.score_candidates("test prompt", ['{"kind":"WAIT"}'], seed=0, claims=claims)

    # Check store for llm.input_delivered event
    delivered_events = [
        e for e in runtime.store.events()
        if e["kind"] == "llm.input_delivered"
    ]
    assert len(delivered_events) >= 1
    last_delivered = delivered_events[-1]["payload"]
    assert last_delivered["boundary"] == "candidate_score_input"
    assert last_delivered["claims"] == claims
