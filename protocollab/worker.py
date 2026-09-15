"""JSON-only process entrypoints; no pickle and no arbitrary dispatch by attribute."""

from __future__ import annotations

import json
import sys

_request_id = None


def usage():
    import resource
    result = resource.getrusage(resource.RUSAGE_SELF)
    return {"cpu_seconds": result.ru_utime + result.ru_stime, "peak_rss_kib": result.ru_maxrss}


def send(value):
    if _request_id is not None:
        value = {**value, "_request_id": _request_id}
    print(json.dumps(value, allow_nan=False, separators=(",", ":")), flush=True)


def receive():
    global _request_id
    line = sys.stdin.readline(16 * 1024 * 1024)
    if not line:
        raise EOFError()
    packet = json.loads(line)
    _request_id = packet.pop("_request_id", None)
    return packet


class RemoteQuery:
    def _call(self, packet):
        send(packet)
        response = receive()
        if response["type"] == "error":
            from protocollab.learning import BudgetExhausted, DeterminismViolation, QueryInterrupted
            exception = {"BudgetExhausted": BudgetExhausted, "QueryInterrupted": QueryInterrupted,
                         "DeterminismViolation": DeterminismViolation}.get(response["error"], RuntimeError)
            raise exception(response["message"])
        return response["result"]

    def query(self, word):
        return self._call({"type": "query", "word": list(word)})

    def record_counterexample(self, value):
        self._call({"type": "counterexample", "value": value})

    def record_candidate(self, artifact):
        self._call({"type": "candidate", "value": artifact.model_dump()})


def learner():
    from protocollab.learning import LearningLimits, learn
    config = receive()
    artifact, export = learn(RemoteQuery(), config["namespace"], config["revision"], config["seed"],
                             LearningLimits(**config["limits"]))
    export["worker_usage"] = usage()
    send({"type": "result", "artifact": artifact.model_dump(), "export": export})


def environment():
    # This import is only available in the private environment image/mount namespace.
    from protocollab_environment.generator import ProtocolConfig
    from protocollab_environment.service import EnvironmentService
    config = receive()
    service = EnvironmentService("/state/world.sqlite", ProtocolConfig.from_dict(config["config"]))
    send({"type": "ready"})
    while True:
        request = receive()
        op = request.pop("op")
        try:
            if op == "create":
                result = service.create(**request)
            elif op == "apply":
                result = service.apply(**request)
            elif op == "reset":
                result = service.reset(**request)
            elif op == "reconcile":
                result = service.reconcile(**request)
            elif op == "usage":
                result = usage()
            else:
                raise PermissionError("UNKNOWN_ENVIRONMENT_ENDPOINT")
            send({"type": "result", "result": result})
        except Exception as exc:
            send({"type": "error", "error": type(exc).__name__, "message": str(exc)})


def monitor():
    from protocollab.monitor import Monitor
    receive()
    monitor = Monitor("/state/monitor.sqlite")
    send({"type": "ready", "anchor": monitor.anchor})
    while True:
        request = receive()
        if request["op"] == "usage":
            send({"type": "result", "result": usage()})
            continue
        if request["op"] != "consume":
            raise PermissionError("UNKNOWN_MONITOR_ENDPOINT")
        send({"type": "result", "result": monitor.consume(request["events"])})


def probe():
    import importlib.util
    import os
    import socket
    from pathlib import Path
    request = receive()
    startup = None
    if request.get("startup"):
        import encodings
        import sqlite3
        import ssl
        import sysconfig

        import aalpy
        import cryptography
        import pydantic
        startup = {"executable": sys.executable, "resolved_executable": str(Path(sys.executable).resolve()),
                   "base_prefix": sys.base_prefix, "stdlib": sysconfig.get_path("stdlib"),
                   "encodings": encodings.__file__, "sqlite": sqlite3.sqlite_version,
                   "ssl": ssl.OPENSSL_VERSION, "aalpy": aalpy.__file__,
                   "pydantic": pydantic.__version__, "cryptography": cryptography.__version__}
    paths = {}
    for path in request.get("paths", []):
        try:
            Path(path).read_bytes()
            paths[path] = "READABLE"
        except OSError:
            paths[path] = "DENIED"
    try:
        sock = socket.create_connection(("1.1.1.1", 443), timeout=0.1)
        sock.close()
        network = "CONNECTED"
    except OSError:
        network = "DENIED"
    send({"type": "result", "uid": os.getuid(), "pid": os.getpid(), "paths": paths,
          "private_import": importlib.util.find_spec("protocollab_environment") is not None,
          "startup": startup,
          "network": network, "environment_keys": sorted(os.environ)})


def actor():
    from protocollab.actor import parse_proposal
    while True:
        request = receive()
        if request["op"] == "parse":
            result = parse_proposal(request["text"]).model_dump()
        elif request["op"] == "plan":
            from protocollab.contracts import BeliefSnapshot, ModelArtifact, Task
            from protocollab.modeling import MealyModel
            from protocollab.planning import SearchLimits, plan
            result = plan(MealyModel(ModelArtifact.model_validate(request["model"])),
                          BeliefSnapshot.model_validate(request["belief"]), Task.model_validate(request["task"]),
                          request["allowed"], SearchLimits(**request["limits"]))
            if result["status"] == "PLANNED":
                result["procedure"] = result["procedure"].model_dump()
            result["worker_usage"] = usage()
        else:
            raise PermissionError("UNKNOWN_ACTOR_ENDPOINT")
        send({"type": "result", "result": result})


if __name__ == "__main__":
    try:
        {"learner": learner, "environment": environment, "monitor": monitor,
         "probe": probe, "actor": actor}[sys.argv[1]]()
    except EOFError:
        pass
    except Exception as exc:
        send({"type": "error", "error": type(exc).__name__, "message": str(exc)})
