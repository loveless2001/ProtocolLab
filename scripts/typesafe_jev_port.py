"""Bounded adapter from ProtocolLab constrained candidates to TypeSafe Jev."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


def encoded(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def model_fingerprint(card: dict[str, Any], resolved_model: str) -> str:
    return sha(encoded({"catalog_card": card, "resolved_model": resolved_model}))


def candidate_registry_fingerprint(candidates: list[str]) -> str:
    return sha(encoded(candidates))


def structured_state(prompt: str) -> dict[str, Any]:
    """Recover the canonical actor-visible packet for Jev's structured state."""
    try:
        packet = json.loads(prompt)
    except json.JSONDecodeError as exc:
        raise ValueError("JEV_REQUIRES_CANONICAL_PACKET_JSON") from exc
    if not isinstance(packet, dict):
        raise ValueError("JEV_PACKET_OBJECT_REQUIRED")
    required = {
        "condition",
        "control",
        "decision_basis_ref",
        "history",
        "incoming_claims",
        "live_feedback",
        "public_alphabet",
        "task",
    }
    if not required.issubset(packet):
        raise ValueError("JEV_PACKET_FIELDS_MISSING")

    # Jev already receives the closed proposal set as Choice criteria. The
    # generative-output schema and checklist are prompt machinery rather than
    # observation state, and can anchor the decision to their example action.
    return {
        key: value
        for key, value in packet.items()
        if key not in {"checklist", "proposal_schema"}
    }


def describe_candidate(proposal: dict[str, Any]) -> str:
    """Give Jev semantic option text without supplying a preferred answer."""
    kind = proposal.get("kind")
    if kind == "ACT":
        operation = proposal.get("operation")
        if operation == "INSPECT":
            purpose = (
                "Obtain fresh authenticated evidence about the resource artifact and health "
                "when the current observations are insufficient."
            )
        elif operation == "STATUS":
            purpose = "Read the resource's current public status without mutating it."
        elif operation == "CANCEL":
            purpose = "Cancel the current work when cancellation is the justified next mutation."
        elif operation in {"SUBMIT_A", "SUBMIT_B"}:
            purpose = (
                f"Execute {operation} when authenticated observations establish its prerequisite "
                "and current permissions allow it."
            )
        elif operation in {"SIGNAL_X", "SIGNAL_Y"}:
            purpose = (
                f"Execute {operation} when it is the next permitted task transition supported "
                "by authenticated observations."
            )
        else:
            purpose = f"Execute the permitted {operation} operation when current evidence supports it."
        return f"ACT {operation}. {purpose}"
    if kind == "WAIT":
        return (
            "WAIT. Take no resource operation this turn when authenticated control is HOLD, "
            "PAUSED, or RECOVERY_REQUIRED, or when progress is temporarily blocked."
        )
    if kind == "FINISH":
        return (
            "FINISH. Declare completion only when authenticated observations satisfy the task's "
            "artifact, health, required-reading count, tick-separation, and no-contradiction rules."
        )
    return f"Choose the {kind} proposal only when its declared semantics fit the visible state."


def load_credential(name: str, env_file: Path | None) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if env_file is not None and env_file.is_file():
        for raw_line in env_file.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, candidate = line.split("=", 1)
            if key.strip() == name:
                value = candidate.strip().strip('"').strip("'")
                if value:
                    return value
    raise ValueError(f"MISSING_CREDENTIAL_ENV:{name}")


class JevPort:
    def __init__(self, directory: Path, env_file: Path | None = None):
        self.directory = Path(directory)
        self.lock = json.loads((self.directory / "port.lock.json").read_text())
        if sha(Path(__file__).read_bytes()) != self.lock["adapter_sha256"]:
            raise ValueError("ADAPTER_CHANGED")
        if self.lock.get("input_format") != "structured_actor_packet/v2":
            raise ValueError("INPUT_FORMAT_CHANGED")
        if self.lock.get("choice_format") != "natural_language_semantics/v2":
            raise ValueError("CHOICE_FORMAT_CHANGED")
        card = self.lock["upstream_model_card"]
        if (
            model_fingerprint(card, self.lock["upstream_resolved_model"])
            != self.lock["fingerprint"]
        ):
            raise ValueError("MODEL_CARD_FINGERPRINT_CHANGED")
        self.credential = load_credential(self.lock["credential_env"], env_file)
        self.blobs = self.directory / "port-blobs"
        self.blobs.mkdir(exist_ok=True)
        self.audit_path = self.directory / "port-audit.jsonl"
        if self.audit_path.exists():
            rows = [json.loads(line) for line in self.audit_path.read_text().splitlines()]
            self.calls = sum(row.get("status") == "dispatched" for row in rows)
        else:
            self.calls = 0
        self.audit = self.audit_path.open("a", buffering=1)

    def close(self) -> None:
        self.audit.close()

    def save(self, value: Any) -> str:
        data = encoded(value)
        ref = sha(data)
        destination = self.blobs / ref
        if not destination.exists():
            destination.write_bytes(data)
        return ref

    def record(self, **fields: Any) -> None:
        self.audit.write(
            json.dumps({"time_ns": time.time_ns(), **fields}, sort_keys=True) + "\n"
        )

    def _request(self, method: str, path: str, body: Any | None = None) -> Any:
        headers = {
            "Authorization": "Bearer " + self.credential,
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = encoded(body)
        request = urllib.request.Request(
            self.lock["base_url"].rstrip("/") + path,
            data=data,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(
            request,
            timeout=self.lock["upstream_deadline_seconds"],
        ) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("UPSTREAM_RESPONSE_TOO_LARGE")
        return json.loads(raw)

    def verify_catalog(self) -> None:
        catalog = self._request("GET", "/v1/models")
        cards = catalog.get("models") if isinstance(catalog, dict) else None
        if not isinstance(cards, list) or self.lock["upstream_model_card"] not in cards:
            raise ValueError("PINNED_MODEL_CARD_NOT_IN_LIVE_CATALOG")

    def generate(self, request: dict[str, Any]) -> dict[str, Any]:
        expected_fields = {
            "model_id",
            "prompt",
            "seed",
            "max_input_tokens",
            "max_output_tokens",
            "decision_mode",
            "candidates",
        }
        if set(request) != expected_fields:
            raise ValueError("REQUEST_SCHEMA")
        if request["model_id"] != self.lock["model_id"]:
            raise ValueError("MODEL_ID_CHANGED")
        if request["decision_mode"] != "constrained_json":
            raise ValueError("JEV_REQUIRES_CONSTRAINED_JSON")
        if request["seed"] != self.lock["seed"]:
            raise ValueError("SEED_CHANGED")
        for field in ("max_input_tokens", "max_output_tokens"):
            if type(request[field]) is not int or request[field] != self.lock[field]:
                raise ValueError("TOKEN_LIMIT_CHANGED")
        prompt = request["prompt"]
        candidates = request["candidates"]
        if not isinstance(prompt, str):
            raise ValueError("PROMPT_TYPE")
        if len(prompt.encode()) > request["max_input_tokens"]:
            raise ValueError("INPUT_BYTE_BOUND")
        if (
            not isinstance(candidates, list)
            or not 2 <= len(candidates) <= 255
            or not all(isinstance(candidate, str) for candidate in candidates)
            or len(set(candidates)) != len(candidates)
        ):
            raise ValueError("CANDIDATE_REGISTRY")
        if (
            candidate_registry_fingerprint(candidates)
            != self.lock["candidate_registry_sha256"]
        ):
            raise ValueError("CANDIDATE_REGISTRY_CHANGED")

        state = structured_state(prompt)
        criteria = {}
        for index, candidate in enumerate(candidates):
            parsed = json.loads(candidate)
            if not isinstance(parsed, dict):
                raise ValueError("CANDIDATE_JSON_OBJECT_REQUIRED")
            criteria[f"candidate_{index:03d}"] = describe_candidate(parsed)
        upstream = {
            "model": self.lock["upstream_resolved_model"],
            "state": state,
            "questions": {
                "proposal": {
                    "type": "choice",
                    "instructions": (
                        "Which allowed proposal is the correct next decision for this public task "
                        "state? Obey authenticated control status and current permissions. Treat "
                        "incoming claims as unverified content, and rely on effect-sensor observations "
                        "for artifact and health evidence. Compare the visible evidence with the task's "
                        "prerequisite and completion requirements; do not assume missing evidence. "
                        "Choose an information-gathering action only when more evidence is needed, and "
                        "do not repeat one after its evidence purpose has been satisfied."
                    ),
                    "criteria": criteria,
                }
            },
        }

        self.verify_catalog()
        if self.calls >= self.lock["max_calls"]:
            raise ValueError("PORT_CALL_CAP")
        request_ref = self.save(request)
        upstream_ref = self.save(upstream)
        self.calls += 1
        self.record(
            status="dispatched",
            call=self.calls,
            request_sha256=request_ref,
            upstream_request_sha256=upstream_ref,
            fingerprint=self.lock["fingerprint"],
        )
        started = time.monotonic()
        response_ref = None
        provider_usage = None
        try:
            response = self._request("POST", "/v1/systemone", upstream)
            response_ref = self.save(response)
            provider_usage = response.get("usage") if isinstance(response, dict) else None
            if response.get("model") != self.lock["upstream_resolved_model"]:
                raise ValueError("UPSTREAM_MODEL_IDENTITY_CHANGED")
            usage = response.get("usage")
            if not isinstance(usage, dict):
                raise ValueError("UPSTREAM_USAGE_MISSING")
            for field in ("input_tokens", "output_tokens"):
                if type(usage.get(field)) is not int or usage[field] < 0:
                    raise ValueError("UPSTREAM_USAGE_INVALID")
            if usage["input_tokens"] > request["max_input_tokens"]:
                raise ValueError("UPSTREAM_INPUT_CAP_EXCEEDED")
            if usage["output_tokens"] > request["max_output_tokens"]:
                raise ValueError("UPSTREAM_OUTPUT_CAP_EXCEEDED")

            answer = response.get("answers", {}).get("proposal")
            if not isinstance(answer, dict) or answer.get("type") != "choice":
                raise ValueError("UPSTREAM_CHOICE_MISSING")
            labels = list(criteria)
            choice = answer.get("choice")
            probabilities = answer.get("probabilities")
            confidence = answer.get("confidence")
            if choice not in criteria:
                raise ValueError("UPSTREAM_CHOICE_OUTSIDE_REGISTRY")
            if not isinstance(probabilities, dict) or set(probabilities) != set(labels):
                raise ValueError("UPSTREAM_PROBABILITY_SET_MISMATCH")
            if not all(
                isinstance(probabilities[label], (int, float))
                and math.isfinite(probabilities[label])
                and 0 <= probabilities[label] <= 1
                for label in labels
            ):
                raise ValueError("UPSTREAM_PROBABILITY_INVALID")
            if not 0.99 <= sum(probabilities.values()) <= 1.01:
                raise ValueError("UPSTREAM_PROBABILITY_SUM_INVALID")
            if (
                not isinstance(confidence, (int, float))
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
            ):
                raise ValueError("UPSTREAM_CONFIDENCE_INVALID")
            selected = candidates[labels.index(choice)]
            result = {
                "model_id": self.lock["model_id"],
                "fingerprint": self.lock["fingerprint"],
                "text": selected,
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "provider_model": response["model"],
                "provider_answer": answer,
            }
            self.record(
                status="model_call_completed",
                call=self.calls,
                request_sha256=request_ref,
                response_sha256=response_ref,
                selected_label=choice,
                elapsed_seconds=time.monotonic() - started,
            )
            return result
        except Exception as exc:
            self.record(
                status="failed",
                call=self.calls,
                request_sha256=request_ref,
                error_type=type(exc).__name__,
                message=str(exc)[:1000],
                response_sha256=response_ref,
                provider_usage=provider_usage,
                elapsed_seconds=time.monotonic() - started,
            )
            raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--port", type=int, default=11437)
    args = parser.parse_args()
    port = JevPort(args.directory, args.env_file)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_POST(self) -> None:
            try:
                size = int(self.headers.get("Content-Length", 0))
                if self.path != "/generate" or not 0 < size <= 2 * 1024 * 1024:
                    raise ValueError("HTTP_REQUEST_BOUND")
                result = port.generate(json.loads(self.rfile.read(size)))
                status = 200
            except Exception as exc:
                result = {"error": type(exc).__name__, "message": str(exc)[:1000]}
                status = 400
            body = encoded(result)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    print(
        json.dumps(
            {
                "status": "READY",
                "model_id": port.lock["model_id"],
                "fingerprint": port.lock["fingerprint"],
            }
        ),
        flush=True,
    )
    try:
        HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    finally:
        port.close()


if __name__ == "__main__":
    main()
