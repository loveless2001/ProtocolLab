"""Bounded AND-OR search over command-or-WAIT, then mandatory TICK."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from protocollab.contracts import (
    ALPHABET,
    Branch,
    ProcedureArtifact,
    ProcedureNode,
    protected_dependencies,
    uid,
)


@dataclass(frozen=True)
class SearchLimits:
    depth: int = 16
    nodes: int = 10000
    procedure_steps: int = 32


class SearchExhausted(RuntimeError):
    pass


def turn_branches(model, states, command):
    groups = {}
    for state in states:
        middle, output = (state, None) if command == "WAIT" else model.step(state, command)
        target, tick = model.step(middle, "TICK")
        groups.setdefault((output, tick), set()).add(target)
    return {pair: tuple(sorted(states)) for pair, states in groups.items()}


def evidence_step(age, count, command, output, artifact, minimum_tick_gap=2):
    if command == "INSPECT":
        if output != f"INSPECT:{artifact}:HEALTHY":
            return -1, 0
        if count == 0:
            age, count = 0, 1
        elif age >= minimum_tick_gap:
            count = 2
    return min(minimum_tick_gap, age + 1) if age >= 0 else -1, count


def plan(model, belief, task, allowed=None, limits=None):
    limits = limits or SearchLimits()
    if not belief.possible_model_states:
        return {"status": "NO_PLAN_WITHIN_BOUND", "reason": "STATE_UNRESOLVED", "expanded_nodes": 0}
    allowed = set(ALPHABET[:-1] if allowed is None else allowed)
    commands = [a for a in ALPHABET[:-1] if a in allowed] + ["WAIT"]
    expanded = 0

    @lru_cache(maxsize=None)
    def search(states, age, count, fuel, depth):
        nonlocal expanded
        if count >= 2:
            return (0, {"leaf": "SUCCESS"})
        if fuel <= 0 or depth <= 0:
            return None
        expanded += 1
        if expanded > limits.nodes:
            raise SearchExhausted()
        best = None
        for command in commands:
            cost = 1 if command == "WAIT" else 2
            if cost > fuel:
                continue
            branches = turn_branches(model, states, command)
            children, worst = {}, 0
            for pair, targets in branches.items():
                next_age, next_count = evidence_step(age, count, command, pair[0], task.artifact, task.minimum_tick_gap)
                result = search(targets, next_age, next_count, fuel - cost, depth - 1)
                if result is None:
                    break
                worst = max(worst, result[0])
                children[pair] = result[1]
            else:
                total = cost + worst
                if best is None or total < best[0]:
                    best = (total, {"command": command, "branches": children})
        return best

    result = None
    try:
        # Iterative cost deepening minimizes worst-case primitive + tick count.
        for fuel in range(1, 2 * limits.depth + 1):
            result = search(tuple(sorted(belief.possible_model_states)), -1, 0, fuel, limits.depth)
            if result:
                break
    except SearchExhausted:
        return {"status": "NO_PLAN_WITHIN_BOUND", "reason": "NODE_BUDGET", "expanded_nodes": expanded}
    if not result:
        return {"status": "NO_PLAN_WITHIN_BOUND", "reason": "DEPTH_BUDGET", "expanded_nodes": expanded}
    nodes = [ProcedureNode(node_id="abort", node_type="ABORT")]

    def emit(policy):
        node_id = f"n{len(nodes)}"
        index = len(nodes)
        nodes.append(None)
        if "leaf" in policy:
            node = ProcedureNode(node_id=node_id, node_type=policy["leaf"])
        else:
            command = policy["command"]
            branches = [Branch(domain_output=pair[0], tick_output=pair[1], next_node=emit(child))
                        for pair, child in sorted(policy["branches"].items(), key=lambda item: str(item[0]))]
            node = ProcedureNode(node_id=node_id, node_type="WAIT" if command == "WAIT" else "ACT",
                                 operation=None if command == "WAIT" else command,
                                 branches=branches, default_next="abort")
        nodes[index] = node
        return node_id

    entry = emit(result[1])
    if len(nodes) > 128:
        return {"status": "NO_PLAN_WITHIN_BOUND", "reason": "PROCEDURE_SIZE", "expanded_nodes": expanded}
    procedure = ProcedureArtifact(procedure_id=uid("procedure"), namespace=model.artifact.namespace,
        revision=model.artifact.revision, model_hash=model.hash, goal_artifact=task.artifact,
        goal_revision=task.revision, minimum_tick_gap=task.minimum_tick_gap,
        entry_node=entry, max_steps=min(limits.depth, limits.procedure_steps), nodes=nodes,
        protected_dependencies=protected_dependencies())
    return {"status": "PLANNED", "procedure": procedure, "worst_case_cost": result[0], "expanded_nodes": expanded}
