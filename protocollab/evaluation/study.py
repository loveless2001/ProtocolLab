"""Topology-clustered study orchestration and report generation."""

from __future__ import annotations

import json
from pathlib import Path

from protocollab.contracts import Task, digest
from protocollab.evaluation.interventions import schedule_pair
from protocollab.evaluation.metrics import paired_topology_bootstrap, summarize_episode
from protocollab.evaluation.runner import run_episode


def run_study(manifest, archive, split, output):
    # Scorer/generator imports live only in this privileged evaluator, absent from actor mount.
    from protocollab_environment.generator.splits import validate_archive
    from protocollab_environment.scorer import score_aliases, score_episode, true_model_artifact
    validate_archive(archive)
    if split not in archive["splits"]:
        raise ValueError("UNKNOWN_SPLIT")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    rows, failures, constrained_cases = [], [], []
    for scenario in archive["splits"][split]:
        for seed in manifest.decoding_seeds:
            prefixes = {}
            collectors = ["C3"] if manifest.query_regime == "shared_prefix" else manifest.conditions
            for collector in collectors:
                prefix = output / "prefixes" / f"{scenario['scenario_id']}-{seed}-{collector}"
                report, _, _ = run_episode(prefix, scenario["config"], manifest, collector, "G3", seed,
                    true_model=true_model_artifact(scenario["config"]) if collector == "C4" else None, prefix_only=True)
                prefixes[collector] = prefix
                if report["adaptation"]["status"] not in ("ACTIVE", "NOT_REQUESTED", "PREFIX_COMPLETED"):
                    failures.append({"prefix": str(prefix), "reason": report["adaptation"]["status"]})
            for condition in manifest.conditions:
                for governance in manifest.governance_conditions:
                    cases = [(f"clean-{i}", None, None, "before_plan") for i in range(manifest.clean_suffixes_per_topology)]
                    for i, pair in enumerate(manifest.paired_interventions):
                        point = manifest.intervention_points[i % len(manifest.intervention_points)]
                        cases += [(f"{pair}-{'valid' if valid else 'invalid'}", pair, valid, point) for valid in (False, True)]
                    for case_index, (case_name, pair, valid, point) in enumerate(cases):
                        path = output / f"{scenario['scenario_id']}-{seed}-{condition}-{governance}-{case_name}"
                        prefix = prefixes["C3" if manifest.query_regime == "shared_prefix" else condition]
                        def intervention(runtime, credentials):
                            schedule_pair(runtime, pair, valid, credentials, point)
                        try:
                            scenario_class = "clean" if pair is None else "valid_correction" if valid else "invalid_intervention"
                            # Membership/promotion timing needs a fresh ongoing prefix, not a frozen cloned one.
                            template = None if point in ("membership_query", "before_promotion") else prefix
                            histories = ((), ("STATUS",), ("WAIT",), ("INSPECT",), ("SUBMIT_B", "CANCEL"), ("SIGNAL_Y", "STATUS"))
                            matched_index = manifest.paired_interventions.index(pair) if pair else case_index
                            task_id = "task-" + digest([scenario["scenario_id"], seed, matched_index, pair is not None])[:16]
                            report, events, task = run_episode(path, scenario["config"], manifest, condition, governance, seed,
                                task=Task(task_id=task_id, artifact="A" if matched_index % 2 == 0 else "B"),
                                true_model=true_model_artifact(scenario["config"]) if condition == "C4" else None,
                                prefix_template=template, scenario_class=scenario_class,
                                intervention=intervention if pair else None, intervention_point=point,
                                public_prehistory=histories[case_index % len(histories)] if pair is None else ())
                            actual = score_episode(path / "private" / "world.sqlite", events, task)
                            if scenario["alias_pairs"]:
                                from protocollab.contracts import ModelArtifact
                                from protocollab.evaluation.ablations import (
                                    compatible_state_intervention,
                                )
                                from protocollab.modeling import MealyModel
                                from protocollab.storage import Store
                                owner = Store(path / "owner" / "owner.sqlite", "episode", readonly=True)
                                try:
                                    active = owner.get("model", "active")
                                    if active:
                                        model = MealyModel(ModelArtifact.model_validate(owner.blob(active["hash"])))
                                        actual.update(score_aliases(model, scenario["config"], scenario["alias_pairs"]))
                                        diagnostics = ([compatible_state_intervention(model, p["left"], p["right"], p["suffix"], task)
                                                        for p in scenario["alias_pairs"]]
                                                       if "compatible_state" in manifest.ablations else [])
                                    else:
                                        actual.update(alias_pair_discrimination=None, alias_prediction_coverage=0.0)
                                        diagnostics = [{"status": "NO_MODEL", "prediction_changed": False}]
                                    if "compatible_state" in manifest.ablations:
                                        (path / "compatible-state-diagnostic.json").write_text(json.dumps(diagnostics, indent=2))
                                finally:
                                    owner.close()
                            row = summarize_episode(events, actual, condition, manifest.track, scenario["topology_hash"], seed, scenario_class)
                            row.update(governance_condition=governance, stratum=split, family=scenario["config"]["family"],
                                       query_regime=manifest.query_regime, adaptation_status=report["adaptation"]["status"],
                                       query_usage=report["query_usage"], resource_usage=report["resource_usage"],
                                       case=case_name, intervention_point=point)
                            rows.append(row)
                            if not actual["capability_eligible"]:
                                constrained_cases.append({"episode": str(path), "status": report["execution"]["status"],
                                    "pause_to_horizon": actual["pause_to_horizon"], "permission_blocked_goal": actual["permission_blocked_goal"]})
                            elif not actual["compliant_task_success"]:
                                failures.append({"episode": str(path), "reason": report["execution"]["status"],
                                                 "obeyed_pause_to_horizon": actual["pause_to_horizon"]})
                        except Exception as exc:
                            failures.append({"episode": str(path), "reason": type(exc).__name__, "message": str(exc)})
    summary = {"track": manifest.track, "split": split, "query_regime": manifest.query_regime,
               "inference_unit": "protocol_topology", "episodes": len(rows), "failed_cases": failures,
               "constrained_partial_cases": constrained_cases,
               "capability_differences": {governance: {
                   category: paired_topology_bootstrap([r for r in rows if r["governance_condition"] == governance and r["scenario_class"] == category], "C2", "C0")
                   for category in ("clean", "valid_correction", "invalid_intervention")}
                   for governance in manifest.governance_conditions},
               "research_verdict": "NOT_ESTABLISHED"}
    (output / "metrics.jsonl").write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    (output / "report.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (output / "scenario_archive.json").write_text(json.dumps(archive, indent=2, sort_keys=True) + "\n")
    return summary
