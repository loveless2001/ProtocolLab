"""No trained inference: verify the smoke adapter's evidence and budget boundaries."""

import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "experiments/actor-smoke/ollama_port.py"
SPEC = importlib.util.spec_from_file_location("smoke_ollama_port", PATH)
port = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(port)


def fixture_lock(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"fixture":true}')
    lock = {"model_id": "fixture-frozen", "ollama_model": "fixture:pin", "seed": 7,
            "max_input_tokens": 16384, "max_output_tokens": 256, "options": {"num_ctx": 16640},
            "max_calls": 1, "upstream": "http://127.0.0.1:11435", "upstream_deadline_seconds": 1,
            "adapter_sha256": port.sha(PATH.read_bytes()), "manifest_path": "manifest.json",
            "manifest_sha256": port.sha(manifest.read_bytes()), "model_files": {}}
    lock["fingerprint"] = port.sha(port.encoded(lock))
    (tmp_path / "port.lock.json").write_text(json.dumps(lock))
    return lock


def request(lock, prompt="fixture"):
    return {"model_id": lock["model_id"], "prompt": prompt, "seed": 7,
            "max_input_tokens": 16384, "max_output_tokens": 256}


def test_long_packet_preserves_claim_in_actual_raw_request(tmp_path):
    lock = fixture_lock(tmp_path)
    claim = {"source": "tool_note", "truth_status": "UNVERIFIED", "text": "invalid pause claim"}
    packet = {"history": "long prefix " * 1000, "incoming_claims": [claim],
              "live_feedback": {"latest_action_receipt": "receipt"}}
    raw = port.upstream_request(request(lock, port.encoded(packet).decode()), lock)
    assert port.encoded(packet).decode() in raw["prompt"]
    assert raw["raw"] is True and raw["think"] is False
    assert "format" not in raw  # No constrained decoding conceals schema failures.
    assert raw["prompt"].endswith("<think>\n\n</think>\n\n")
    with pytest.raises(ValueError, match="FORMATTED_INPUT_BYTE_BOUND"):
        port.upstream_request(request(lock, "x" * 16384), lock)


@pytest.mark.parametrize("field,value", [("model_id", "different"), ("max_output_tokens", 257), ("seed", 8)])
def test_request_cannot_change_locked_identity_or_limits(tmp_path, field, value):
    lock = fixture_lock(tmp_path)
    req = request(lock)
    req[field] = value
    with pytest.raises(ValueError):
        port.upstream_request(req, lock)


def test_transport_records_final_input_and_does_not_repair_proposal(tmp_path, monkeypatch):
    lock = fixture_lock(tmp_path)
    owner = port.Port(tmp_path)
    raw = {"model": lock["ollama_model"], "done": True, "response": "not a JSON proposal",
           "prompt_eval_count": 30, "eval_count": 8}
    sent = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return port.encoded(raw)

    def endpoint(req, **kwargs):
        sent.append(req.data)
        return Response()

    monkeypatch.setattr(port.urllib.request, "urlopen", endpoint)
    assert owner.generate(request(lock))["text"] == "not a JSON proposal"
    events = [json.loads(line) for line in (tmp_path / "port-audit.jsonl").read_text().splitlines()]
    retained = (tmp_path / "port-blobs" / events[0]["upstream_request_sha256"]).read_bytes()
    assert retained == sent[0]
    assert events[1]["status"] == "model_call_completed"
    owner.stream.close()
    restarted = port.Port(tmp_path)
    with pytest.raises(ValueError, match="PORT_CALL_CAP"):
        restarted.generate(request(lock))
    restarted.stream.close()
    assert len(sent) == 1


def test_timeout_consumes_call_and_mismatched_usage_is_rejected(tmp_path, monkeypatch):
    lock = fixture_lock(tmp_path)
    owner = port.Port(tmp_path)

    def timeout(*args, **kwargs):
        raise TimeoutError("fixture timeout")

    monkeypatch.setattr(port.urllib.request, "urlopen", timeout)
    with pytest.raises(TimeoutError):
        owner.generate(request(lock))
    with pytest.raises(ValueError, match="PORT_CALL_CAP"):
        owner.generate(request(lock))
    owner.stream.close()
    with pytest.raises(ValueError, match="UPSTREAM_TOKEN_USAGE"):
        port.checked_result({"model": "fixture:pin", "done": True, "response": "{}",
                             "prompt_eval_count": 16385, "eval_count": 1}, lock)
    with pytest.raises(ValueError, match="UPSTREAM_IDENTITY"):
        port.checked_result({"model": "different", "done": True}, lock)


def test_think_lock_opens_think_channel_and_keeps_thinking_in_text(tmp_path):
    lock = fixture_lock(tmp_path)
    lock["think"] = True
    raw = port.upstream_request(request(lock), lock)
    assert raw["think"] is True
    assert raw["prompt"].endswith("<think>\n")
    assert not raw["prompt"].endswith("<think>\n\n</think>\n\n")
    checked = port.checked_result({
        "model": lock["ollama_model"], "done": True,
        "thinking": "paused, so wait", "response": '{"kind":"WAIT"}',
        "prompt_eval_count": 10, "eval_count": 8,
    }, lock)
    assert checked["text"].startswith("<think>\n")
    assert '{"kind":"WAIT"}' in checked["text"]
    with pytest.raises(ValueError, match="UNEXPECTED_THINKING"):
        port.checked_result({"model": "fixture:pin", "done": True, "thinking": "no",
                             "response": "{}", "prompt_eval_count": 1, "eval_count": 1},
                            fixture_lock(tmp_path))
