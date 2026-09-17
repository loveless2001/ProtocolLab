"""A bounded, audited localhost adapter for the existing ProtocolLab API contract.

No policy decisions, response repairs, retries, or prompt truncation occur here.
The dedicated Ollama instance must use the copied, hash-verified model directory.
"""

import argparse
import hashlib
import json
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


SYSTEM = "Use the public ProtocolLab packet and return one JSON proposal matching its schema."


def render(prompt, think=False):
    prefix = ("<|im_start|>system\n" + SYSTEM + "<|im_end|>\n<|im_start|>user\n"
              + prompt.strip() + "<|im_end|>\n<|im_start|>assistant\n")
    if think:
        return prefix + "<think>\n"
    return prefix + "<think>\n\n</think>\n\n"


def upstream_request(request, lock):
    if set(request) != {"model_id", "prompt", "seed", "max_input_tokens", "max_output_tokens"}:
        raise ValueError("REQUEST_SCHEMA")
    if request["model_id"] != lock["model_id"] or request["seed"] != lock["seed"]:
        raise ValueError("MODEL_OR_SEED_CHANGED")
    for key in ("max_input_tokens", "max_output_tokens"):
        if type(request[key]) is not int or request[key] != lock[key]:
            raise ValueError("TOKEN_LIMIT_CHANGED")
    if not isinstance(request["prompt"], str):
        raise ValueError("PROMPT_TYPE")
    think = bool(lock.get("think"))
    prompt = render(request["prompt"], think)
    # Conservative for the pinned byte-level tokenizer, includes all formatting.
    # Reject instead of letting the server drop required feedback or claims.
    if len(prompt.encode()) > lock["max_input_tokens"]:
        raise ValueError("FORMATTED_INPUT_BYTE_BOUND")
    return {"model": lock["ollama_model"], "prompt": prompt, "raw": True,
            "think": think, "stream": False, "keep_alive": "60m",
            "options": {**lock["options"], "seed": request["seed"],
                        "num_predict": request["max_output_tokens"]}}


def checked_result(result, lock):
    if result.get("model") != lock["ollama_model"] or result.get("done") is not True:
        raise ValueError("UPSTREAM_IDENTITY_OR_COMPLETION")
    thinking = result.get("thinking")
    if thinking and not lock.get("think"):
        raise ValueError("UNEXPECTED_THINKING")
    for key, limit in (("prompt_eval_count", "max_input_tokens"), ("eval_count", "max_output_tokens")):
        if type(result.get(key)) is not int or not 0 <= result[key] <= lock[limit]:
            raise ValueError("UPSTREAM_TOKEN_USAGE")
    if not isinstance(result.get("response"), str):
        raise ValueError("UPSTREAM_RESPONSE_TYPE")
    text = result["response"]
    if lock.get("think"):
        if thinking:
            text = "<think>\n" + thinking + "\n</think>\n" + text
        elif not text.strip().startswith("<think>"):
            text = "<think>\n" + text
    return {"model_id": lock["model_id"], "fingerprint": lock["fingerprint"],
            "text": text, "input_tokens": result["prompt_eval_count"],
            "output_tokens": result["eval_count"]}


class Port:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = json.loads((self.directory / "port.lock.json").read_text())
        body = {k: v for k, v in self.lock.items() if k != "fingerprint"}
        if sha(encoded(body)) != self.lock["fingerprint"]:
            raise ValueError("PORT_LOCK_CHANGED")
        if sha(Path(__file__).read_bytes()) != self.lock["adapter_sha256"]:
            raise ValueError("ADAPTER_CHANGED")
        if self.lock.get("engine_binary"):
            with Path(self.lock["engine_binary"]).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != self.lock["engine_sha256"]:
                    raise ValueError("ENGINE_BINARY_CHANGED")
        self.manifest = self.directory / self.lock["manifest_path"]
        self.verify_manifest()
        for name, pin in self.lock["model_files"].items():
            with (self.directory / "ollama-models/blobs" / name).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != pin["sha256"]:
                    raise ValueError("MODEL_FILE_CHANGED")
        self.blobs = self.directory / "port-blobs"
        self.blobs.mkdir(exist_ok=True)
        audit = self.directory / "port-audit.jsonl"
        self.calls = sum(json.loads(line)["status"] == "dispatched" for line in audit.read_text().splitlines()) if audit.exists() else 0
        self.stream = audit.open("a", buffering=1)

    def verify_manifest(self):
        if sha(self.manifest.read_bytes()) != self.lock["manifest_sha256"]:
            raise ValueError("MODEL_MANIFEST_CHANGED")

    def save(self, value):
        data = encoded(value)
        ref = sha(data)
        path = self.blobs / ref
        if not path.exists():
            path.write_bytes(data)
        return ref

    def record(self, **fields):
        self.stream.write(json.dumps({"time_ns": time.time_ns(), **fields}, sort_keys=True) + "\n")

    def generate(self, request):
        self.verify_manifest()
        upstream = upstream_request(request, self.lock)
        if self.calls >= self.lock["max_calls"]:
            raise ValueError("PORT_CALL_CAP")
        request_ref, upstream_ref = self.save(request), self.save(upstream)
        self.calls += 1  # Reserve before transport; no retry or refund on failure.
        self.record(status="dispatched", call=self.calls, request_sha256=request_ref,
                    upstream_request_sha256=upstream_ref, fingerprint=self.lock["fingerprint"])
        started = time.monotonic()
        try:
            req = urllib.request.Request(self.lock["upstream"] + "/api/generate", data=encoded(upstream),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.lock["upstream_deadline_seconds"]) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("UPSTREAM_RESPONSE_TOO_LARGE")
            result = json.loads(raw)
            response_ref = self.save(result)
            checked = checked_result(result, self.lock)
            self.verify_manifest()
            self.record(status="model_call_completed", call=self.calls, request_sha256=request_ref,
                        response_sha256=response_ref, elapsed_seconds=time.monotonic() - started)
            return checked
        except Exception as exc:
            self.record(status="failed", call=self.calls, request_sha256=request_ref,
                        error_type=type(exc).__name__, message=str(exc),
                        elapsed_seconds=time.monotonic() - started)
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--port", type=int, default=11436)
    args = parser.parse_args()
    port = Port(args.directory)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", 0))
                if self.path != "/generate" or not 0 < size <= 65536:
                    raise ValueError("HTTP_REQUEST_BOUND")
                result = port.generate(json.loads(self.rfile.read(size)))
                status = 200
            except Exception as exc:
                result = {"error": type(exc).__name__, "message": str(exc)}
                status = 400
            body = encoded(result)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The call remains recorded and charged after client timeout.

    print(json.dumps({"status": "READY", "fingerprint": port.lock["fingerprint"]}), flush=True)
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
