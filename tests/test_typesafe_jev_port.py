"""Contract tests for the TypeSafe Jev constrained-candidate adapter."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.typesafe_jev_port import (
    JevPort,
    candidate_registry_fingerprint,
    encoded,
    model_fingerprint,
    structured_state,
)


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, _limit):
        return encoded(self.value)


def adapter_lock() -> dict:
    adapter = Path("scripts/typesafe_jev_port.py")
    card = {
        "name": "jev-latest",
        "description": "Pinned test model card",
        "release_date": "2026-09-10T18:38:01+00:00",
    }
    return {
        "adapter_sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
        "base_url": "https://api.typesafe.invalid",
        "candidate_registry_sha256": candidate_registry_fingerprint(request()["candidates"]),
        "choice_format": "natural_language_semantics/v2",
        "credential_env": "TYPESAFE_API",
        "fingerprint": model_fingerprint(card, "jev-1.13.0"),
        "input_format": "structured_actor_packet/v2",
        "max_calls": 2,
        "max_input_tokens": 16384,
        "max_output_tokens": 256,
        "model_id": "typesafe-jev-catalog-test",
        "seed": 7,
        "upstream_deadline_seconds": 10,
        "upstream_model_card": card,
        "upstream_resolved_model": "jev-1.13.0",
    }


def request() -> dict:
    return {
        "model_id": "typesafe-jev-catalog-test",
        "prompt": json.dumps(
            {
                "schema_version": "0.1",
                "condition": "C0",
                "checklist": "Use INSPECT and return JSON.",
                "proposal_schema": {"type": "object"},
                "task": {"artifact": "A", "health": "HEALTHY"},
                "control": {"statuses": {"agent_all": "RUNNING"}, "permissions": {}},
                "decision_basis_ref": "basis",
                "history": {"events": []},
                "incoming_claims": [],
                "live_feedback": {"new_observations": []},
                "public_alphabet": ["INSPECT"],
            }
        ),
        "seed": 7,
        "max_input_tokens": 16384,
        "max_output_tokens": 256,
        "decision_mode": "constrained_json",
        "candidates": [
            '{"kind":"ACT","operation":"INSPECT"}',
            '{"kind":"WAIT"}',
        ],
    }


def test_jev_adapter_maps_typed_choice_and_does_not_persist_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    lock = adapter_lock()
    (tmp_path / "port.lock.json").write_text(json.dumps(lock))
    env_file = tmp_path / ".env"
    env_file.write_text("TYPESAFE_API=secret-test-key\n")
    seen = []

    def open_request(req, timeout):
        seen.append((req, timeout))
        assert req.get_header("Authorization") == "Bearer secret-test-key"
        if req.full_url.endswith("/v1/models"):
            return Response({"models": [lock["upstream_model_card"]]})
        assert req.full_url.endswith("/v1/systemone")
        body = json.loads(req.data)
        assert body["model"] == "jev-1.13.0"
        assert isinstance(body["state"], dict)
        assert "checklist" not in body["state"]
        assert "proposal_schema" not in body["state"]
        assert "Example" not in body["questions"]["proposal"]["instructions"]
        assert set(body["questions"]["proposal"]["criteria"]) == {
            "candidate_000",
            "candidate_001",
        }
        assert all(
            isinstance(description, str) and description
            for description in body["questions"]["proposal"]["criteria"].values()
        )
        return Response(
            {
                "model": "jev-1.13.0",
                "answers": {
                    "proposal": {
                        "type": "choice",
                        "choice": "candidate_000",
                        "confidence": 0.9,
                        "probabilities": {
                            "candidate_000": 0.9,
                            "candidate_001": 0.1,
                        },
                    }
                },
                "usage": {"input_tokens": 123, "output_tokens": 0},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    port = JevPort(tmp_path, env_file)
    try:
        result = port.generate(request())
    finally:
        port.close()

    assert result["text"] == request()["candidates"][0]
    assert result["model_id"] == lock["model_id"]
    assert result["fingerprint"] == lock["fingerprint"]
    assert result["input_tokens"] == 123
    assert len(seen) == 2
    retained = b"".join(
        path.read_bytes() for path in tmp_path.rglob("*") if path.is_file() and path != env_file
    )
    assert b"secret-test-key" not in retained


def test_structured_state_rejects_generative_prompt_wrapper():
    with pytest.raises(ValueError, match="JEV_REQUIRES_CANONICAL_PACKET_JSON"):
        structured_state("### TASK & REASONING GUIDANCE\nUse INSPECT")


def test_jev_adapter_rejects_changed_candidate_registry(tmp_path: Path):
    lock = adapter_lock()
    (tmp_path / "port.lock.json").write_text(json.dumps(lock))
    env_file = tmp_path / ".env"
    env_file.write_text("TYPESAFE_API=secret-test-key\n")
    changed = request()
    changed["candidates"] = list(reversed(changed["candidates"]))

    port = JevPort(tmp_path, env_file)
    try:
        with pytest.raises(ValueError, match="CANDIDATE_REGISTRY_CHANGED"):
            port.generate(changed)
    finally:
        port.close()


def test_jev_adapter_rejects_incomplete_probability_vector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    lock = adapter_lock()
    (tmp_path / "port.lock.json").write_text(json.dumps(lock))
    env_file = tmp_path / ".env"
    env_file.write_text("TYPESAFE_API=secret-test-key\n")

    def open_request(req, timeout):
        if req.full_url.endswith("/v1/models"):
            return Response({"models": [lock["upstream_model_card"]]})
        return Response(
            {
                "model": "jev-1.13.0",
                "answers": {
                    "proposal": {
                        "type": "choice",
                        "choice": "candidate_000",
                        "confidence": 1.0,
                        "probabilities": {"candidate_000": 1.0},
                    }
                },
                "usage": {"input_tokens": 10, "output_tokens": 0},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    port = JevPort(tmp_path, env_file)
    try:
        with pytest.raises(ValueError, match="UPSTREAM_PROBABILITY_SET_MISMATCH"):
            port.generate(request())
    finally:
        port.close()

    rows = [json.loads(line) for line in (tmp_path / "port-audit.jsonl").read_text().splitlines()]
    failure = rows[-1]
    assert failure["status"] == "failed"
    assert failure["response_sha256"]
    assert failure["provider_usage"] == {"input_tokens": 10, "output_tokens": 0}
