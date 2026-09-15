"""Exercise the fixed interpreter and real broker at each reachable primitive boundary.

These are explicitly hypothetical governance probes. Public procedure validation
uses real replica queries separately; nothing here is added to observational data.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from protocollab.contracts import BeliefSnapshot, Task, uid


class ModelProbeBackend:
    def __init__(self, model, initial):
        self.model, self.initial = model, initial
        self.states, self.receipts = {}, {}
        self.applied = 0

    def create(self, resource, disposable, command_id):
        self.states[resource] = [self.initial, 0, 0]
        return {"kind": "HYPOTHETICAL_GOVERNANCE_PROBE"}

    def apply(self, resource, symbol, command_id):
        if command_id in self.receipts:
            return self.receipts[command_id]
        state, tick, sequence = self.states[resource]
        state, output = self.model.step(state, symbol)
        tick += symbol == "TICK"
        sequence += 1
        self.states[resource] = [state, tick, sequence]
        self.applied += 1
        domain = {"result_code": output}
        if symbol == "INSPECT":
            _, artifact, health = output.split(":")
            domain = {"result_code": "OK", "artifact": artifact, "health": health}
        receipt = {"event_id": uid(), "resource_id": resource, "causal_command_id": command_id,
                   "sequence": sequence, "logical_tick": tick, "transport_status": "ACKNOWLEDGED",
                   "domain": domain, "note": "hypothetical governance probe"}
        self.receipts[command_id] = receipt
        return receipt


def audit_boundaries(procedure, model, start_states, boundaries):
    from protocollab.evaluation.interventions import signed_control
    from protocollab.evaluation.runner import demo_authorities
    from protocollab.procedures import ProcedureRunner
    from protocollab.runtime import Runtime

    reports = []
    for initial in start_states:
        for boundary in sorted(boundaries):
            for change in ("pause", "goal", "model"):
                with tempfile.TemporaryDirectory(prefix="protocollab-fence-probe-") as path:
                    backend = ModelProbeBackend(model, initial)
                    keys, credentials = demo_authorities()
                    runtime = Runtime(Path(path), backend, keys, namespace="governance-probe",
                                      task=Task(artifact=procedure.goal_artifact, revision=procedure.goal_revision,
                                                minimum_tick_gap=procedure.minimum_tick_gap))
                    try:
                        model_hash = runtime.store.put_blob(model.artifact)
                        runtime.store.set("model", "active", {"hash": model_hash, "revision": model.artifact.revision})
                        belief = BeliefSnapshot(model_rev=model.artifact.revision, possible_model_states=[initial])
                        runtime.store.set("belief", "R", belief.model_dump())
                        runner = ProcedureRunner(runtime, procedure)
                        reachable = True
                        for _ in range(boundary):
                            if runner.step()["status"] != "RUNNING":
                                reachable = False
                                break
                        if not reachable or runner.nodes[runner.node_id].node_type not in ("ACT", "WAIT"):
                            continue
                        if change in ("pause", "goal"):
                            key, key_id = credentials["operator"]
                            extra = {} if change == "pause" else {"artifact": "B" if procedure.goal_artifact == "A" else "A"}
                            result = signed_control(runtime, key, key_id, "operator",
                                "PAUSE_DISPATCH" if change == "pause" else "REDIRECT", **extra)
                            if result["status"] != "ACCEPTED":
                                raise AssertionError("GOVERNANCE_PROBE_CONTROL_NOT_ACCEPTED")
                        else:
                            replacement = model.artifact.model_copy(update={"revision": model.artifact.revision + 1})
                            key = runtime.store.put_blob(replacement)
                            runtime.store.set("model", "active", {"hash": key, "revision": replacement.revision})
                        before = backend.applied
                        result = runner.step()
                        if result["status"] != "STALE" or backend.applied != before:
                            raise AssertionError("PROCEDURE_INTERRUPTION_GATE_FAILED")
                        reports.append({"kind": "HYPOTHETICAL_GOVERNANCE_PROBE", "boundary": boundary,
                                        "start_state": initial, "change": change, "result": "STALE",
                                        "new_backend_applications": backend.applied - before})
                    finally:
                        runtime.close()
    checked = {r["boundary"] for r in reports}
    if checked != set(boundaries):
        raise AssertionError("PROCEDURE_INTERRUPTION_COVERAGE_INCOMPLETE")
    return reports
