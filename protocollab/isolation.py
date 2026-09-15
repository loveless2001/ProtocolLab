"""Linux user/PID/network namespaces with minimal read-only filesystem mounts."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import sys
import time
from pathlib import Path

from protocollab.contracts import ModelArtifact
from protocollab.learning import BudgetExhausted, DeterminismViolation, QueryInterrupted


class IsolationUnavailable(RuntimeError):
    pass


def sandbox_command(role, state_directory=None):
    if sys.platform != "linux" or not shutil.which("bwrap"):
        raise IsolationUnavailable("Linux bubblewrap is required; no insecure runtime fallback")
    root = Path(__file__).resolve().parent.parent
    venv = Path(sys.prefix)
    if venv == Path(sys.base_prefix):
        raise IsolationUnavailable("Run using the pinned project virtual environment")
    command = ["bwrap", "--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-ipc",
               "--unshare-uts", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
               "--uid", str({"learner": 10001, "actor": 10001, "environment": 10002, "monitor": 10003,
                               "probe": 10001}[role]), "--gid", "10000", "--clearenv",
               "--setenv", "PATH", "/usr/bin", "--setenv", "PYTHONPATH", "/app",
               "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
               "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib",
               "--ro-bind", "/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev",
               "--tmpfs", "/tmp", "--dir", "/app", "--ro-bind", str(venv), "/venv",
               "--ro-bind", str(root / "protocollab"), "/app/protocollab", "--chdir", "/app"]
    if role == "environment":
        command += ["--ro-bind", str(root / "protocollab_environment"), "/app/protocollab_environment"]
    if state_directory is not None:
        if role not in ("environment", "monitor"):
            raise ValueError("Untrusted workers cannot mount persistent state")
        Path(state_directory).mkdir(parents=True, exist_ok=True)
        command += ["--bind", str(Path(state_directory).resolve()), "/state"]
    command += ["/venv/bin/python", "-m", "protocollab.worker", role]
    return command


class WorkerProcess:
    def __init__(self, role, state_directory=None):
        import resource
        self.role = role
        self._read_buffer = b""
        self._request_counter = 0
        def limits():
            resource.setrlimit(resource.RLIMIT_CPU, (3600, 3600))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        self.process = subprocess.Popen(sandbox_command(role, state_directory), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        bufsize=1, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, preexec_fn=limits)

    def send(self, packet):
        if self.process.poll() is not None:
            error = self.process.stderr.read(4000)
            raise IsolationUnavailable(f"{self.role} exited: {error}")
        self.process.stdin.write(json.dumps(packet, allow_nan=False, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def receive(self, timeout=120):
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while b"\n" not in self._read_buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError(f"{self.role} response deadline")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise IsolationUnavailable(f"{self.role} exited: {self.process.stderr.read(4000)}")
                self._read_buffer += chunk
                if len(self._read_buffer) > 16 * 1024 * 1024:
                    raise ValueError("WORKER_MESSAGE_SIZE_LIMIT")
            line, self._read_buffer = self._read_buffer.split(b"\n", 1)
        finally:
            selector.close()
        if not line:
            raise IsolationUnavailable(f"{self.role} exited: {self.process.stderr.read(4000)}")
        packet = json.loads(line)
        if packet.get("type") == "error":
            error = {"BudgetExhausted": BudgetExhausted, "QueryInterrupted": QueryInterrupted,
                     "DeterminismViolation": DeterminismViolation}.get(packet.get("error"), RuntimeError)
            raise error(packet["message"])
        return packet

    def request(self, packet, timeout=120):
        self._request_counter += 1
        request_id = self._request_counter
        deadline = time.monotonic() + timeout
        self.send({**packet, "_request_id": request_id})
        while True:
            response = self.receive(max(0, deadline - time.monotonic()))
            if response.get("_request_id") == request_id:
                return response

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


class EnvironmentProcess:
    """Gateway-only handle. Actor/learner never inherit this pipe or object."""
    def __init__(self, directory, config):
        self.worker = WorkerProcess("environment", directory)
        self.worker.request({"op": "init", "config": config}, timeout=10)

    def _call(self, operation, **arguments):
        return self.worker.request({"op": operation, **arguments}, timeout=1)["result"]

    def create(self, resource, disposable, command_id):
        return self._call("create", resource=resource, disposable=disposable, command_id=command_id)

    def reset(self, resource, command_id):
        return self._call("reset", resource=resource, command_id=command_id)

    def apply(self, resource, symbol, command_id):
        return self._call("apply", resource=resource, symbol=symbol, command_id=command_id)

    def reconcile(self, command_id):
        return self._call("reconcile", command_id=command_id)

    def usage(self):
        return self._call("usage")

    def close(self):
        self.worker.close()


class MonitorProcess:
    def __init__(self, directory):
        self.worker = WorkerProcess("monitor", directory)
        response = self.worker.request({"op": "init"}, timeout=10)
        self.anchor = response["anchor"]

    def consume(self, events):
        response = self.worker.request({"op": "consume", "events": events})["result"]
        self.anchor = response["anchor"]
        return response

    def usage(self):
        return self.worker.request({"op": "usage"})["result"]

    def close(self):
        self.worker.close()


def isolated_learn(adapter, namespace, revision, seed, limits):
    from dataclasses import asdict
    worker = WorkerProcess("learner")
    try:
        worker.send({"op": "learn", "namespace": namespace, "revision": revision,
                     "seed": seed, "limits": asdict(limits)})
        while True:
            packet = worker.receive()
            if packet["type"] == "result":
                artifact = ModelArtifact.model_validate(packet["artifact"])
                # Attribution is assigned from owner records, not trusted from worker references.
                artifact = artifact.model_copy(update={"training_trace_refs": [t["trace_id"] for t in adapter.traces()]})
                return artifact, packet["export"]
            try:
                if packet["type"] == "query":
                    response = adapter.query(packet["word"])
                elif packet["type"] == "counterexample":
                    adapter.record_counterexample(packet["value"])
                    response = None
                elif packet["type"] == "candidate":
                    adapter.record_candidate(ModelArtifact.model_validate(packet["value"]))
                    response = None
                else:
                    raise PermissionError("UNREGISTERED_LEARNER_RPC")
                worker.send({"type": "response", "result": response})
            except Exception as exc:
                worker.send({"type": "error", "error": type(exc).__name__, "message": str(exc)})
    finally:
        worker.close()


def isolated_plan(model, belief, task, allowed=None, limits=None):
    from dataclasses import asdict

    from protocollab.contracts import ProcedureArtifact
    from protocollab.planning import SearchLimits
    worker = WorkerProcess("actor")
    try:
        result = worker.request({"op": "plan", "model": model.artifact.model_dump(),
            "belief": belief.model_dump(), "task": task.model_dump(), "allowed": allowed,
            "limits": asdict(limits or SearchLimits())})["result"]
        if result["status"] == "PLANNED":
            result["procedure"] = ProcedureArtifact.model_validate(result["procedure"])
        return result
    finally:
        worker.close()
