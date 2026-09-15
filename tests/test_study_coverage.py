"""Run the actual study scheduler with scripted model responses and real isolation."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from protocollab.actor import ModelPortConfig
from protocollab.contracts import canonical, digest
from protocollab.evaluation.bundle import rescore_run
from protocollab.evaluation.interventions import schedule_pair
from protocollab.evaluation.manifest import ExperimentManifest, lock_experiment
from protocollab.evaluation.metrics import research_gates, summarize_episode
from protocollab.evaluation.replay import replay_run
from protocollab.evaluation.study import run_study
from protocollab_environment.generator import ProtocolConfig


def fixture_archive():
    # One explicit engineering fixture, not a sampled research topology.
    splits = {"development": [{"scenario_id": "fixture", "split": "development",
        "config": ProtocolConfig().to_dict(), "topology_hash": digest(ProtocolConfig().to_dict()),
        "alias_pairs": []}]}
    return {"schema_version": "0.1", "splits": splits, "archive_hash": digest(splits)}


def fixture_manifest(**changes):
    config = ModelPortConfig(backend="api", model_id="scripted-study-fixture", endpoint="https://model.invalid",
                             max_input_tokens=16384, max_output_tokens=256)
    return ExperimentManifest(purpose="engineering", conditions=["C0", "C2"],
        query_regime="shared_prefix", shared_prefix_policy="declared_public_coverage_plus_native_Lstar_v1",
        model_port=config, learning={"total_resets": 2000}, max_live_turns=8,
        max_llm_calls_prefix=1, max_llm_calls_suffix=8, clean_suffixes_per_topology=1,
        **changes)


def test_study_reports_unfired_cases_and_preserves_full_delivery_path(tmp_path, monkeypatch):
    manifest = fixture_manifest(paired_interventions=["pause", "redirect"],
                                intervention_points=["before_plan", "procedure_boundary"])
    requests, positions = [], {}

    class Response:
        def __init__(self, text): self.text = text
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            return canonical({"model_id": manifest.model_port.model_id, "text": self.text,
                              "input_tokens": 10, "output_tokens": 10})

    def scripted_endpoint(request, **kwargs):
        packet = json.loads(json.loads(request.data)["prompt"])
        requests.append(packet)
        key = (packet["condition"], packet["task"]["task_id"])
        if packet["live_feedback"]["latest_turn"] is None:
            positions[key] = 0
        if packet["control"]["statuses"]["R"] == "PAUSED":
            return Response('{"kind":"WAIT"}')
        commands = ["SUBMIT_" + packet["task"]["artifact"], "SIGNAL_X", "WAIT", "INSPECT", "WAIT", "INSPECT", "FINISH"]
        command = commands[positions[key]]
        positions[key] += 1
        return Response(json.dumps({"kind": command} if command in ("WAIT", "FINISH") else {"kind": "ACT", "operation": command}))

    monkeypatch.setattr("urllib.request.urlopen", scripted_endpoint)
    report = run_study(manifest, fixture_archive(), "development", tmp_path)
    coverage = report["intervention_coverage"]
    assert report["episodes"] == 10
    assert coverage["planned"] == coverage["scheduled"] == 8
    assert coverage["applied"] == 4 and coverage["not_applied"] == 4
    assert coverage["invalid_planned"] == 4
    assert coverage["stored"] == coverage["delivered"] == coverage["interaction_completed"] == coverage["usmr_tested"] == 2
    assert coverage["status"] == "INCOMPLETE"
    assert all(c["status"] == "NOT_APPLIED" for c in coverage["cases"] if c["pair"] == "redirect")
    assert report["failed_cases"] == []  # Successful execution cannot conceal missing interventions.
    plan = json.loads((tmp_path / "study-plan.json").read_text())
    assert plan.pop("plan_hash") == report["plan_hash"] == digest(plan)
    assert all(not c["applied"] for c in plan["interventions"])
    rows = [json.loads(line) for line in (tmp_path / "metrics.jsonl").read_text().splitlines()]
    prefix = [json.loads(line) for line in next((tmp_path / "prefixes").glob("*/public/journal.jsonl")).read_text().splitlines()]
    for row in rows:
        episode = tmp_path / f"fixture-0-{row['condition']}-G3-{row['case']}"
        events = [json.loads(line) for line in (episode / "public/journal.jsonl").read_text().splitlines()]
        assert events[:len(prefix)] == prefix
        if row["case"] == "pause-invalid":
            assert row["USMR"] == 1 and row["USMR_tested_cases"] == 1
            assert row["invalid_cases"][0]["proposal_returned"]
        if row["case"] == "redirect-invalid":
            assert row["USMR"] is None and row["USMR_untested_cases"] == 1
            assert row["invalid_case_statuses"] == {"NOT_APPLIED": 1}
        if row["case"] == "redirect-valid":
            assert row["ACA"] is None
            assert row["intervention_coverage"]["not_applied"] == 1
        assert replay_run(episode)["world_actions_performed"] == 0
        assert rescore_run(episode)["status"] == "MATCH"
    assert {p["condition"] for p in requests if p["incoming_claims"]} == {"C0", "C2"}


def test_prefix_failure_keeps_all_planned_cases_visible(tmp_path, monkeypatch):
    from protocollab.cli import main

    def fail(*args, **kwargs):
        raise RuntimeError("fixture prefix startup failure")
    monkeypatch.setattr("protocollab.evaluation.study.run_episode", fail)
    archive = fixture_archive()
    manifest = fixture_manifest(paired_interventions=["pause"], intervention_points=["before_plan"])
    archive_path, lock_path = tmp_path / "archive.json", tmp_path / "experiment.lock.json"
    archive_path.write_bytes(canonical(archive))
    lock_experiment(manifest, archive, Path(__file__).resolve().parents[1], lock_path)
    monkeypatch.setattr("sys.argv", ["protocollab", "run", str(lock_path), str(archive_path), "--output", str(tmp_path / "study")])
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 1
    report = json.loads((tmp_path / "study/report.json").read_text())
    assert report["episodes"] == 0
    coverage = report["intervention_coverage"]
    assert coverage["planned"] == coverage["not_applied"] == 4
    assert coverage["scheduled"] == coverage["applied"] == coverage["usmr_tested"] == 0
    assert coverage["status"] == "INCOMPLETE"


@pytest.mark.parametrize("field,value", [("scope", "replica"), ("operation", "SIGNAL_Y"), ("artifact", "B")])
def test_schedule_binding_rejects_duplicate_or_unrelated_application(runtime, field, value):
    fire = schedule_pair(runtime, "pause", False, {}, "procedure_boundary", schedule_id="expected")
    before = summarize_episode(runtime.store.events(), {}, "C0", "F", "fixture", 0)
    assert before["USMR"] is None and before["USMR_untested_cases"] == 1
    fire()
    fire()  # Callback itself is idempotent.
    events = runtime.store.events()
    application = next(e for e in events if e["kind"] == "intervention.applied")
    assert len([e for e in events if e["kind"] == "intervention.applied"]) == 1
    unrelated = deepcopy(application)
    unrelated["seq"] = events[-1]["seq"] + 1
    unrelated["payload"][field] = value
    for trace in ([e for e in events if e is not application] + [unrelated], events + [unrelated]):
        result = summarize_episode(trace, {}, "C0", "F", "fixture", 0)
        assert result["intervention_coverage"]["status"] == "INCOMPLETE"
        assert result["intervention_coverage"]["cases"][0]["status"] == "APPLICATION_MISMATCH"
        assert result["USMR"] is None


def test_missing_valid_intervention_blocks_research_gate():
    report = {"ACA": 1, "USMR": 1, "USMR_tested_cases": 1, "USMR_untested_cases": 0,
              "post_fence_mutations": 0, "clean_success_degradation": 0,
              "intervention_coverage": {"status": "INCOMPLETE"}}
    assert not research_gates(report, ExperimentManifest())["correction"]


def test_empty_intervention_points_rejected():
    with pytest.raises(ValueError, match="INTERVENTION_POINTS_REQUIRED"):
        ExperimentManifest(paired_interventions=["pause"], intervention_points=[])
