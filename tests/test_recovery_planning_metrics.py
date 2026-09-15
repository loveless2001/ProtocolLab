import json

import pytest
from conftest import control

from protocollab.contracts import BeliefSnapshot, ModelArtifact
from protocollab.evaluation.ablations import compatible_state_intervention, fixed_schema_predictor
from protocollab.evaluation.manifest import ExperimentManifest, validate_matched_conditions
from protocollab.evaluation.metrics import paired_topology_bootstrap, summarize_episode
from protocollab.gateway import InjectedCrash
from protocollab.modeling import MealyModel
from protocollab.planning import SearchLimits, plan
from protocollab.runtime import Runtime


def test_R01_crash_before_apply_records_no_effect(runtime):
    checkpoint = runtime.checkpoint()
    proposal = runtime.proposal("SUBMIT_A")
    runtime.broker.propose(proposal)
    with pytest.raises(InjectedCrash):
        runtime.broker.dispatch(proposal.command_id, "before_apply")
    assert runtime.backend.reconcile(proposal.command_id) is None
    assert runtime.recover(checkpoint) == "RECOVERED"
    assert runtime.broker.reconcile(proposal.command_id)["status"] == "FAILED"


def test_R03_missing_segment_never_synthesizes_history(runtime):
    checkpoint = runtime.checkpoint()
    runtime.store.db.execute("DROP TRIGGER immutable_events_delete")
    runtime.store.db.execute("PRAGMA foreign_keys=OFF")
    runtime.store.db.execute("DELETE FROM events WHERE seq=1")
    assert runtime.recover(checkpoint) == "RECOVERY_REQUIRED"
    assert runtime.governance.snapshot["statuses"]["agent_all"] == "RECOVERY_REQUIRED"
    assert runtime.turn("SUBMIT_A")["action"]["status"] == "DENIED"


def test_R03_mutated_owner_state_detected(runtime):
    checkpoint = runtime.checkpoint()
    runtime.store.db.execute("UPDATE state SET payload='{}' WHERE owner='belief'")
    assert runtime.recover(checkpoint) == "RECOVERY_REQUIRED"


def test_R04_restart_stales_ready_preserves_predictions(modeled_runtime):
    runtime = modeled_runtime
    runtime.turn("SUBMIT_A")
    model = runtime.models.current_snapshot()
    before = model.predict(runtime.belief.snapshot.possible_model_states, "SIGNAL_X")
    ready = runtime.proposal("SIGNAL_X")
    runtime.broker.propose(ready)
    checkpoint = runtime.checkpoint()
    runtime.close()
    reloaded = Runtime(runtime.directory, runtime.backend, runtime.governance.keys)
    try:
        assert reloaded.recover(checkpoint) == "RECOVERED"
        assert reloaded.broker.dispatch(ready.command_id)["status"] == "STALE"
        assert reloaded.models.current_snapshot().predict(reloaded.belief.snapshot.possible_model_states, "SIGNAL_X") == before
        assert reloaded.models.current_snapshot().hash == model.hash
    finally:
        reloaded.close()


def test_M01_model_migration_replays_history_not_names(modeled_runtime):
    runtime = modeled_runtime
    runtime.turn("SUBMIT_A")
    original = runtime.models.current_snapshot().artifact
    names = {q: f"new_{i}" for i, q in enumerate(reversed(original.states))}
    candidate = original.model_dump()
    candidate.update(revision=2, model_id="new-ids", initial_state=names[original.initial_state], states=list(names.values()))
    for transition in candidate["transitions"]:
        transition["from_state"] = names[transition["from_state"]]
        transition["to_state"] = names[transition["to_state"]]
    candidate = ModelArtifact.model_validate(candidate)
    old_state = runtime.belief.snapshot.possible_model_states[0]
    report = runtime.models.validate(candidate, runtime.query_adapter(), seed=31)
    assert report.status == "PASS"
    world_before = runtime.backend.db.execute("SELECT world FROM instances WHERE resource='R'").fetchone()[0]
    runtime.models.promote(candidate, report, runtime.governance.snapshot["epoch"])
    assert runtime.belief.snapshot.possible_model_states == [names[old_state]]
    assert runtime.backend.db.execute("SELECT world FROM instances WHERE resource='R'").fetchone()[0] == world_before
    assert runtime.store.get("procedure", "active") is None


def test_M03_E01_compatible_state_changes_behavior(modeled_runtime):
    runtime = modeled_runtime
    model = runtime.models.current_snapshot()
    left = ["SUBMIT_A", "TICK", "STATUS"]
    right = ["SUBMIT_A", "TICK", "SIGNAL_X", "TICK", "STATUS"]
    before = (runtime.belief.snapshot, runtime.governance.snapshot, runtime.store.tail)
    diagnostic = compatible_state_intervention(model, left, right, ["TICK", "INSPECT"], runtime.governance.task)
    assert diagnostic["prediction_changed"]
    assert diagnostic["proposal_changed"]
    assert before == (runtime.belief.snapshot, runtime.governance.snapshot, runtime.store.tail)
    fixed = MealyModel(fixed_schema_predictor("run", {}))
    assert fixed.replay(left)[0] == fixed.replay(right)[0]


def test_M02_model_rollback_preserves_world_and_evidence(modeled_runtime):
    from protocollab.evaluation.ablations import rollback_in_evaluator_fork
    runtime = modeled_runtime
    for operation in ("SUBMIT_A", "SIGNAL_X", "WAIT", "INSPECT"):
        runtime.turn(operation)
    world = runtime.backend.db.execute("SELECT world FROM instances WHERE resource='R'").fetchone()[0]
    refs = list(runtime.belief.snapshot.observation_refs)
    fixed = fixed_schema_predictor("run", {})
    with pytest.raises(PermissionError):
        rollback_in_evaluator_fork(runtime, fixed)
    report = rollback_in_evaluator_fork(runtime, fixed, evaluator_fork=True)
    assert not report["world_reset_performed"]
    assert runtime.backend.db.execute("SELECT world FROM instances WHERE resource='R'").fetchone()[0] == world
    assert runtime.belief.snapshot.observation_refs == refs
    assert runtime.belief.snapshot.facts[-1]["artifact"] == "A"
    assert "MODEL_MISMATCH" in runtime.belief.snapshot.flags


def test_P04_live_tick_grammar_and_uncertain_states(modeled_runtime):
    runtime = modeled_runtime
    result = runtime.synthesize(admit=False)
    assert result["status"] == "PLANNED"
    for node in result["procedure"].nodes:
        if node.node_type in ("ACT", "WAIT"):
            assert all(branch.tick_output == "TICKED" for branch in node.branches)
    exhausted = plan(runtime.models.current_snapshot(), runtime.belief.snapshot, runtime.governance.task,
                     limits=SearchLimits(depth=1, nodes=20))
    assert exhausted["status"] == "NO_PLAN_WITHIN_BOUND"
    assert "impossible" not in json.dumps(exhausted).lower()
    model = runtime.models.current_snapshot()
    initial = model.artifact.initial_state
    alternate = model.replay(["SUBMIT_B"])[0]
    ambiguous = BeliefSnapshot(model_rev=1, possible_model_states=[initial, alternate])
    result = plan(model, ambiguous, runtime.governance.task)
    assert result["status"] == "PLANNED"


def test_E02_unknown_accuracy_requires_coverage(runtime):
    runtime.turn("STATUS")
    result = summarize_episode(runtime.store.events(), {"raw_goal_success": False}, "C0", "F", "t1", 0)
    assert result["prediction_accuracy"] is None
    assert result["prediction_coverage"] == 0
    assert result["scored_transitions"] == 2


def test_E03_corrections_and_utility_reported_separately(runtime):
    from protocollab.evaluation.interventions import apply_pair
    credentials = {"operator": (runtime.test_key, "operator-key")}
    apply_pair(runtime, "pause", True, credentials)
    apply_pair(runtime, "pause", False, credentials)
    runtime.turn("SUBMIT_A")
    report = summarize_episode(runtime.store.events(), {"raw_goal_success": False, "compliant_task_success": False},
                               "C2", "F", "t1", 0)
    assert report["ACA"] == report["USMR"] == 1
    assert report["raw_goal_success"] is False
    assert "alignment_score" not in report


def test_E04_E05_matched_budgets_clustered_statistics():
    baseline = ExperimentManifest()
    changed = baseline.model_copy(update={"max_live_turns": 100})
    with pytest.raises(ValueError, match="UNMATCHED"):
        validate_matched_conditions([baseline, changed])
    rows = []
    for topology in ("t1", "t2", "t3"):
        for seed in range(8):
            for condition, score in (("C2", True), ("C0", False)):
                rows.append({"topology_id": topology, "seed": seed, "condition": condition, "raw_goal_success": score})
    report = paired_topology_bootstrap(rows, "C2", "C0")
    assert report["topology_n"] == 3
    assert report["interval"] == [1.0, 1.0]
    rows[0]["inference_unit"] = "transition"
    with pytest.raises(ValueError, match="PSEUDOREPLICATION"):
        paired_topology_bootstrap(rows, "C2", "C0")


def test_admission_cannot_self_approve_or_reclassify(modeled_runtime):
    runtime = modeled_runtime
    candidate = runtime.models.current_snapshot().artifact.model_dump()
    candidate["revision"] = 2
    candidate["protected_dependencies"]["operation_registry_hash"] = "a" * 64
    candidate = ModelArtifact.model_validate(candidate)
    report = runtime.models.validate(candidate, runtime.query_adapter())
    assert report.status == "FAIL"
    assert not report.protected_dependencies_unchanged
    fake = report.model_copy(update={"status": "PASS"})
    with pytest.raises(PermissionError):
        runtime.models.promote(candidate, fake, runtime.governance.snapshot["epoch"])


def test_pause_before_promotion_keeps_incumbent(modeled_runtime):
    runtime = modeled_runtime
    incumbent = runtime.models.current_snapshot()
    candidate = incumbent.artifact.model_copy(update={"revision": 2})
    epoch = runtime.governance.snapshot["epoch"]
    report = runtime.models.validate(candidate, runtime.query_adapter())
    control(runtime, "PAUSE_DISPATCH", scope="agent_all")
    with pytest.raises(Exception, match="STALE"):
        runtime.models.promote(candidate, report, epoch)
    assert runtime.models.current_snapshot().hash == incumbent.hash
