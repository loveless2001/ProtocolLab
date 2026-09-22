"""Incomplete execution must not become a positive paired capability estimate."""

import json

import pytest

from protocollab.actor import ModelPortConfig
from protocollab.contracts import digest
from protocollab.evaluation.manifest import ExperimentManifest
from protocollab.evaluation.metrics import paired_topology_bootstrap
from protocollab.evaluation.study import run_study
from protocollab_environment.generator import ProtocolConfig


def row(topology, condition, value, seed=0, case="clean-0", **extra):
    return {"topology_id": topology, "condition": condition, "seed": seed,
            "case": case, "raw_goal_success": value, **extra}


def complete_rows():
    return [row(topology, condition, condition == "C2")
            for topology in ("t1", "t2") for condition in ("C2", "C0")]


@pytest.mark.parametrize("field,value", [("seed", 1), ("case", "clean-1")])
def test_different_tasks_or_seeds_are_not_paired(field, value):
    rows = complete_rows()
    for observation in rows:
        if observation["condition"] == "C0":
            observation[field] = value
    report = paired_topology_bootstrap(rows, "C2", "C0")
    assert report["status"] == "INCOMPLETE_PAIRS"
    assert report["matched_pair_n"] == 0
    assert report["incomplete_pair_n"] == report["missing_observation_n"] == 4
    assert report["interval"] is None
    assert "paired_mean_difference" not in report


def test_missing_pair_on_both_sides_remains_visible_from_plan():
    rows = complete_rows()
    planned = rows + [row("t1", c, None, case="clean-1") for c in ("C2", "C0")]
    report = paired_topology_bootstrap(rows, "C2", "C0", expected_rows=planned)
    assert report["status"] == "INCOMPLETE_PAIRS"
    assert report["matched_pair_n"] == report["topology_n"] == 2
    assert report["incomplete_pair_n"] == 1
    assert report["missing_observation_n"] == 2
    assert report["interval"] is None


def test_missing_score_blocks_inference_instead_of_dropping_observation():
    rows = complete_rows()
    rows[0]["raw_goal_success"] = None
    report = paired_topology_bootstrap(rows, "C2", "C0")
    assert report["status"] == "INCOMPLETE_PAIRS"
    assert report["missing_metric_n"] == report["incomplete_pair_n"] == 1
    assert report["missing_observation_n"] == 0


def test_ineligible_capability_case_excludes_both_condition_observations():
    rows = complete_rows() + [row("t1", "C2", False, case="pause-valid",
                                  capability_eligible=False),
                              row("t1", "C0", True, case="pause-valid")]
    report = paired_topology_bootstrap(rows, "C2", "C0")
    assert report["status"] == "ESTIMATED"
    assert report["paired_mean_difference"] == 1
    assert report["matched_pair_n"] == 2
    assert report["excluded_ineligible_pair_n"] == 1


def test_repeated_tasks_and_seeds_do_not_reweight_topology_clusters():
    rows = [row("t1", c, c == "C2", seed=seed)
            for seed in range(10) for c in ("C2", "C0")]
    rows += [row("t2", c, c == "C0") for c in ("C2", "C0")]
    report = paired_topology_bootstrap(rows, "C2", "C0")
    assert report["topology_n"] == 2
    assert report["matched_pair_n"] == 11
    assert report["paired_mean_difference"] == 0
    assert report["interval"] == [-1, 1]
    assert paired_topology_bootstrap(list(reversed(rows)), "C2", "C0") == report


def test_duplicate_or_unplanned_observation_cannot_reweight_pairs():
    rows = complete_rows()
    with pytest.raises(ValueError, match="DUPLICATE_PAIRED_OBSERVATION"):
        paired_topology_bootstrap(rows + [rows[0]], "C2", "C0")
    with pytest.raises(ValueError, match="DUPLICATE_PLANNED_OBSERVATION"):
        paired_topology_bootstrap(rows, "C2", "C0", expected_rows=rows + [rows[0]])
    with pytest.raises(ValueError, match="UNPLANNED_PAIRED_OBSERVATION"):
        paired_topology_bootstrap(rows, "C2", "C0", expected_rows=rows[1:])


@pytest.mark.parametrize("field", ["track", "governance_condition", "query_regime", "scenario_class"])
def test_incompatible_study_contexts_cannot_be_pooled(field):
    rows = complete_rows()
    rows[0][field] = "different"
    with pytest.raises(ValueError, match="UNMATCHED_COMPARISON_CONTEXT"):
        paired_topology_bootstrap(rows, "C2", "C0")


def test_nonfinite_metric_cannot_produce_confidence_interval():
    rows = complete_rows()
    rows[0]["raw_goal_success"] = float("nan")
    with pytest.raises(ValueError, match="NONFINITE_PAIRED_METRIC"):
        paired_topology_bootstrap(rows, "C2", "C0")


def test_study_keeps_double_execution_failure_in_planned_comparison(tmp_path, monkeypatch):
    # Real study scheduling/reporting; synthetic episode/scorer fixtures do no inference.
    manifest = ExperimentManifest(purpose="engineering", conditions=["C0", "C2"],
        model_port=ModelPortConfig(backend="api", model_id="fixture", endpoint="https://model.invalid"),
        clean_suffixes_per_topology=2, paired_interventions=[])
    scenarios = [{"scenario_id": topology, "topology_hash": topology,
                  "split": "development", "config": ProtocolConfig().to_dict(), "alias_pairs": []}
                 for topology in ("t1", "t2")]
    splits = {"development": scenarios}
    archive = {"splits": splits, "archive_hash": digest(splits)}

    def episode(path, config, manifest, condition, governance, seed, **kwargs):
        if not kwargs.get("prefix_only") and kwargs["task"].artifact == "B":
            raise RuntimeError("injected episode failure for both conditions")
        report = {"adaptation": {"status": "ACTIVE"}, "execution": {"status": "FIXTURE"},
                  "query_usage": {}, "resource_usage": {}}
        events = [{"seq": 1, "kind": "fixture", "payload": {"success": condition == "C2"}}]
        return report, events, kwargs.get("task")

    def score(path, events, task):
        return {"capability_eligible": True, "raw_goal_success": events[0]["payload"]["success"],
                "compliant_task_success": events[0]["payload"]["success"], "pause_to_horizon": False}

    monkeypatch.setattr("protocollab.evaluation.study.run_episode", episode)
    monkeypatch.setattr("protocollab_environment.scorer.score_episode", score)
    report = run_study(manifest, archive, "development", tmp_path)
    assert report["planned_episodes"] == 8
    assert report["episodes"] == report["missing_episodes"] == 4
    comparison = report["capability_differences"]["G3"]["clean"]
    assert comparison["status"] == "INCOMPLETE_PAIRS"
    assert comparison["matched_pair_n"] == comparison["incomplete_pair_n"] == 2
    assert comparison["missing_observation_n"] == 4
    assert comparison["interval"] is None
    plan = json.loads((tmp_path / "study-plan.json").read_text())
    assert len(plan["episodes"]) == 8
    assert plan.pop("plan_hash") == report["plan_hash"] == digest(plan)
