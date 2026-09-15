"""Fixed bounded graph interpreter. Every primitive traverses the broker again."""

from __future__ import annotations

from protocollab.contracts import ALPHABET, ProcedureArtifact, digest, protected_dependencies
from protocollab.learning import QueryInterrupted
from protocollab.planning import evidence_step, turn_branches


def model_replay(procedure, model, start_states):
    nodes = {n.node_id: n for n in procedure.nodes}
    queue = [(procedure.entry_node, tuple(start_states), -1, 0, procedure.max_steps)]
    visited = set()
    boundaries = set()
    while queue:
        node_id, states, age, count, fuel = queue.pop()
        key = (node_id, states, age, count, fuel)
        if key in visited:
            continue
        visited.add(key)
        node = nodes[node_id]
        if node.node_type in ("ABORT", "ASK"):
            return False, "MODEL_REPLAY_ABORT", boundaries
        if node.node_type == "SUCCESS":
            if count < 2:
                return False, "UNSUPPORTED_SUCCESS", boundaries
            continue
        if fuel == 0:
            return False, "FUEL_EXHAUSTED", boundaries
        boundaries.add(procedure.max_steps - fuel)
        command = node.operation if node.node_type == "ACT" else "WAIT"
        for pair, targets in turn_branches(model, states, command).items():
            next_node = next((b.next_node for b in node.branches if (b.domain_output, b.tick_output) == pair), node.default_next)
            next_age, next_count = evidence_step(age, count, command, pair[0], procedure.goal_artifact, procedure.minimum_tick_gap)
            queue.append((next_node, targets, next_age, next_count, fuel - 1))
    return True, None, boundaries


def access_paths(model):
    access = {model.artifact.initial_state: ()}
    queue = [model.artifact.initial_state]
    for state in queue:
        for symbol in ALPHABET:
            target, _ = model.step(state, symbol)
            if target not in access:
                access[target] = access[state] + (symbol,)
                queue.append(target)
    return access


class ProcedureRegistry:
    def __init__(self, store, governance, model_registry):
        self.store, self.governance, self.models = store, governance, model_registry
        self.frozen = False

    def admit(self, procedure, start_states, adapter):
        if self.frozen:
            raise QueryInterrupted("FROZEN_SUFFIX")
        procedure = ProcedureArtifact.model_validate(procedure.model_dump())
        model = self.models.current_snapshot()
        if not model or procedure.model_hash != model.hash:
            raise ValueError("STALE_MODEL")
        if procedure.protected_dependencies != protected_dependencies():
            raise PermissionError("PROTECTED_DEPENDENCY_CHANGE")
        task = self.governance.task
        if (procedure.goal_artifact, procedure.goal_revision, procedure.minimum_tick_gap) != (
                task.artifact, task.revision, task.minimum_tick_gap):
            raise ValueError("STALE_EVIDENCE_CONTRACT")
        good, reason, boundaries = model_replay(procedure, model, start_states)
        if not good:
            raise ValueError(reason)
        epoch = self.governance.snapshot["epoch"]
        goal_rev = self.governance.task.revision
        paths = access_paths(model)
        nodes = {n.node_id: n for n in procedure.nodes}
        probe_refs = []
        previous_phase, adapter.phase = adapter.phase, "admission"
        try:
            for initial in start_states:
                # Public probe from a learned access path, never a private snapshot or reset-live.
                word = list(paths[initial])
                current, node_id, fuel = initial, procedure.entry_node, procedure.max_steps
                while nodes[node_id].node_type in ("ACT", "WAIT") and fuel:
                    node = nodes[node_id]
                    command = node.operation if node.node_type == "ACT" else "WAIT"
                    output = None
                    if command != "WAIT":
                        word.append(command)
                        current, output = model.step(current, command)
                    word.append("TICK")
                    current, tick = model.step(current, "TICK")
                    node_id = next((b.next_node for b in node.branches if (b.domain_output, b.tick_output) == (output, tick)), node.default_next)
                    fuel -= 1
                expected = model.replay(word)[1]
                actual = adapter.query(word, fresh=True)
                trace = adapter.traces()[-1]
                probe_refs.append(trace["trace_id"])
                if actual != expected:
                    raise ValueError("PROCEDURE_PUBLIC_PROBE_FAILED")
        finally:
            adapter.phase = previous_phase
        from protocollab.procedures.interruptions import audit_boundaries
        checks = audit_boundaries(procedure, model, start_states, boundaries)
        with self.store.transaction():
            reason = self.governance.authorize(adapter.resource, "RESET_REPLICA", epoch, query=True)
            if reason or self.governance.task.revision != goal_rev or self.models.current_snapshot().hash != model.hash:
                raise QueryInterrupted(reason or "STALE")
            key = self.store.put_blob(procedure)
            report = self.store.put_blob({"model_replay": "PASS", "public_probe_refs": probe_refs,
                                          "interruption_checks": checks, "start_states": start_states})
            self.store.set("procedure", key, {"status": "ACTIVE", "hash": key, "report": report,
                                              "start_states": list(start_states), "model_hash": model.hash})
            self.store.set("procedure", "active", {"hash": key}, "procedure.promoted")
            return key


def binding_valid(binding, current):
    return all(binding[k] == current[k] for k in ("epoch", "goal_rev", "model_hash"))


class ProcedureRunner:
    def __init__(self, runtime, procedure, decision_basis_ref=None):
        self.runtime, self.procedure = runtime, procedure
        self.nodes = {n.node_id: n for n in procedure.nodes}
        self.node_id, self.fuel = procedure.entry_node, procedure.max_steps
        self.binding = self.current_binding()
        self.decision_basis_ref = decision_basis_ref or runtime.bind_decision()
        self.status = "READY"

    def current_binding(self):
        model = self.runtime.models.current_snapshot()
        return {"epoch": self.runtime.governance.snapshot["epoch"],
                "goal_rev": self.runtime.governance.task.revision,
                "model_hash": model.hash if model else None}

    def step(self):
        hook = self.runtime.hooks.get("procedure_boundary")
        if hook:
            hook()
        self.runtime.sync_monitor()
        if self.status not in ("READY", "RUNNING"):
            return {"status": self.status}
        task = self.runtime.governance.task
        if (not binding_valid(self.binding, self.current_binding())
                or not self.runtime.decision_is_current(self.decision_basis_ref)
                or (self.procedure.goal_artifact, self.procedure.goal_revision, self.procedure.minimum_tick_gap)
                != (task.artifact, task.revision, task.minimum_tick_gap)):
            self.status = "STALE"
            return {"status": self.status}
        if self.runtime.governance.is_paused(self.runtime.governance.task.resource_id):
            self.status = "INTERRUPTED"
            return {"status": self.status}
        node = self.nodes[self.node_id]
        if node.node_type in ("SUCCESS", "ABORT", "ASK"):
            self.status = node.node_type
            if self.status == "SUCCESS" and self.runtime.finish()["status"] != "PUBLIC_CONTRACT_SATISFIED":
                self.status = "ABORT"
            return {"status": self.status}
        if self.fuel <= 0:
            self.status = "FUEL_EXHAUSTED"
            return {"status": self.status}
        self.fuel -= 1  # WAIT consumes fuel as ACT does.
        self.status = "RUNNING"
        result = self.runtime.turn(node.operation if node.node_type == "ACT" else "WAIT",
                                   decision_basis_ref=self.decision_basis_ref)
        action = result.get("action")
        if action and action["status"] not in ("ACKNOWLEDGED", "OUTCOME_OBSERVED"):
            self.status = "INTERRUPTED"
            return {"status": self.status, "turn": result}
        if not binding_valid(self.binding, self.current_binding()):
            # A control may arrive while the completed step's receipt/tick is
            # being captured. It cannot authorize the next old-policy branch.
            self.status = "STALE"
            return {"status": self.status, "turn": result}
        pair = (action["receipt"]["domain_output"] if action else None, result["tick"].domain_output)
        self.node_id = next((b.next_node for b in node.branches if (b.domain_output, b.tick_output) == pair), node.default_next)
        self.runtime.store.append("procedure", "procedure.step", {"hash": digest(self.procedure),
            "remaining_fuel": self.fuel, "next_node": self.node_id,
            "decision_basis_ref": self.decision_basis_ref, **self.binding})
        # Explicitly advance the decision basis only after interpreting this step's observations.
        previous = self.decision_basis_ref
        self.decision_basis_ref = self.runtime.bind_decision()
        self.runtime.store.append("procedure", "decision.revalidated", {
            "previous_basis_ref": previous, "decision_basis_ref": self.decision_basis_ref,
            "procedure_hash": digest(self.procedure), "next_node": self.node_id})
        return {"status": self.status, "turn": result}
