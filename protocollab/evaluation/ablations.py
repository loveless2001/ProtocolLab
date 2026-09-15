"""Evaluator-only fork diagnostics. Original observations and authority are unchanged."""

from __future__ import annotations

from protocollab.contracts import ALPHABET, BeliefSnapshot, ModelArtifact, Transition, digest
from protocollab.modeling import MealyModel
from protocollab.planning import plan


def compatible_state_intervention(model, left_history, right_history, suffix, task):
    left, _ = model.replay(left_history)
    right, _ = model.replay(right_history)
    if any(model.step(left, a)[1] != model.step(right, a)[1] for a in ("STATUS", "INSPECT")):
        raise ValueError("CAUSAL_PAIR_MUST_HAVE_SAME_PUBLIC_SURFACE")
    if left == right:
        return {"status": "MODEL_DOES_NOT_DISTINGUISH_PAIR", "prediction_changed": False,
                "model_hash": model.hash}
    predictions = [model.replay(suffix, state)[1] for state in (left, right)]
    proposals = []
    for state in (left, right):
        belief = BeliefSnapshot(model_rev=model.artifact.revision, possible_model_states=[state])
        result = plan(model, belief, task)
        if result["status"] == "PLANNED":
            procedure = result["procedure"]
            node = next(n for n in procedure.nodes if n.node_id == procedure.entry_node)
            proposals.append(node.operation or node.node_type)
        else:
            proposals.append(result["status"])
    return {"status": "SCORED_BEFORE_REGROUND", "model_hash": model.hash, "left_state": left, "right_state": right,
            "predictions": predictions, "next_proposals": proposals,
            "prediction_changed": predictions[0] != predictions[1], "proposal_changed": proposals[0] != proposals[1],
            "raw_observation_and_authority_mutated": False}


def fixed_schema_predictor(namespace, outputs, revision=0):
    """Explicit supplied one-state diagnostic; no learned state distinctions."""
    from protocollab.contracts import protected_dependencies
    defaults = {a: "INSPECT:BASE:HEALTHY" if a == "INSPECT" else
                "ACCEPTED" if a.startswith("SUBMIT") else "READY" if a == "STATUS" else
                "TICKED" if a == "TICK" else "CANCELLED" if a == "CANCEL" else "OK" for a in ALPHABET}
    defaults.update(outputs)
    return ModelArtifact(model_id="supplied-fixed-schema", namespace=namespace, revision=revision,
        initial_state="fixed", states=["fixed"], alphabet=list(ALPHABET),
        transitions=[Transition(from_state="fixed", input=a, to_state="fixed", output=defaults[a]) for a in ALPHABET],
        protected_dependencies=protected_dependencies())


def compare_models(current, incumbent, words):
    rows = [{"word": list(word), "current": current.replay(word)[1], "incumbent": incumbent.replay(word)[1]}
            for word in words]
    return {"kind": "EVALUATOR_FORK", "current_hash": current.hash, "incumbent_hash": incumbent.hash,
            "changed_predictions": sum(r["current"] != r["incumbent"] for r in rows), "rows": rows}


def isolation_fingerprint(runtime):
    return digest({"belief": runtime.belief.snapshot.model_dump(), "control": runtime.governance.snapshot,
                   "observations": runtime.belief.snapshot.observation_refs, "journal": runtime.store.tail,
                   "model": runtime.models.current_snapshot().hash if runtime.models.current_snapshot() else None})


def rollback_in_evaluator_fork(runtime, artifact, *, evaluator_fork=False):
    if not evaluator_fork:
        raise PermissionError("MODEL_ROLLBACK_DIAGNOSTIC_REQUIRES_EVALUATOR_FORK")
    artifact = ModelArtifact.model_validate(artifact.model_dump())
    from protocollab.contracts import protected_dependencies
    if artifact.protected_dependencies != protected_dependencies():
        raise PermissionError("PROTECTED_DEPENDENCY_CHANGE")
    previous = runtime.models.current_snapshot()
    observations = [runtime.capture.read_observation(ref) for ref in runtime.belief.snapshot.observation_refs]
    with runtime.store.transaction():
        key = runtime.store.put_blob(artifact)
        runtime.store.set("model", "active", {"hash": key, "revision": artifact.revision}, "model.promoted", "evaluator_diagnostic")
        runtime.belief.rebase(MealyModel(artifact), observations, runtime.belief.snapshot.complete_history)
        runtime.store.set("procedure", "active", None, "diagnostic.procedure_invalidated")
        report = {"kind": "EVALUATOR_FORK", "old_hash": previous.hash if previous else None, "new_hash": key,
                  "observations_retained": len(observations), "world_reset_performed": False}
        runtime.store.append("evaluation", "diagnostic.model_rollback", report)
        return report
