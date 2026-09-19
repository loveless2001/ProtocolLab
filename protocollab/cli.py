"""Operator/evaluator CLI. Never installed as an actor-side privileged endpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from protocollab.evaluation.manifest import ExperimentManifest, lock_experiment, verify_lock


def load(path):
    return yaml.safe_load(Path(path).read_text())


def owner_store_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_file():
        return path
    for candidate in (path / "owner" / "owner.sqlite", path / "owner.sqlite"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("MISSING_OWNER_STORE")


def main():
    parser = argparse.ArgumentParser(description="ProtocolLab: governed black-box protocol learning")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run the isolated native alias fixture and retain all evidence")
    demo.add_argument("--output", default="runs/demo")
    demo.add_argument("--seed", type=int, default=7)
    demo.add_argument("--reset-cap", type=int, default=2000, help="Explicit calibrated fixture override; design default is 1000")
    demo.add_argument("--track", choices=["F", "O"], default="F")
    schema = commands.add_parser("schema", help="Export versioned runtime Pydantic schemas")
    schema.add_argument("--output", default="contracts/runtime.schema.json")
    verify = commands.add_parser("verify", help="Verify retained owner journal and all artifacts")
    verify.add_argument("run")
    generate = commands.add_parser("generate", help="Generate a private topology-split archive")
    generate.add_argument("--output", required=True)
    generate.add_argument("--seed", type=int, default=0)
    generate.add_argument("--development", type=int, default=6)
    generate.add_argument("--validation", type=int, default=6)
    generate.add_argument("--sealed-per-stratum", type=int, default=16)
    lock = commands.add_parser("lock", help="Lock code, configuration, budgets, thresholds and scenario archive")
    lock.add_argument("manifest")
    lock.add_argument("archive")
    lock.add_argument("--output", required=True)
    study = commands.add_parser("run", help="Run conditions from a previously locked experiment")
    study.add_argument("lock")
    study.add_argument("archive")
    study.add_argument("--split", default="development")
    study.add_argument("--output", required=True)
    keys = commands.add_parser("keys", help="Generate separate operator, permission-owner, reviewer and monitor keys")
    keys.add_argument("directory")
    control = commands.add_parser("control", help="Sign an operator event and enqueue it for the running owner")
    control.add_argument("run")
    control.add_argument("--private-key", required=True)
    control.add_argument("--key-id", required=True)
    control.add_argument("--principal", required=True)
    control.add_argument("--verb", required=True)
    control.add_argument("--scope", default="R")
    control.add_argument("--operation")
    control.add_argument("--artifact", choices=["A", "B"])
    control.add_argument("--appeal-id")
    control.add_argument("--resolution", choices=["UPHOLD", "LIFT", "NARROW", "DENY"])
    replay = commands.add_parser("replay", help="Replay retained public traces against the active model without world actions")
    replay.add_argument("run")
    score = commands.add_parser("score", help="Recompute metrics from retained owner and simulator evidence")
    score.add_argument("run")
    bundle = commands.add_parser("bundle", help="Export a sanitized, hash-pinned native evidence ZIP")
    bundle.add_argument("run")
    bundle.add_argument("--output", required=True)
    bundle_verify = commands.add_parser("verify-bundle", help="Verify, replay and rescore an evidence ZIP")
    bundle_verify.add_argument("archive")
    recover = commands.add_parser("recover", help="Restart the owner, verify hashes, reconcile and stale outstanding plans")
    recover.add_argument("run")
    report = commands.add_parser("report", help="Render a Markdown report from retained episode metrics")
    report.add_argument("run")
    shadow_monitor = commands.add_parser(
        "shadow-monitor",
        help="Start or evaluate a durable verified-lifecycle shadow window",
    )
    shadow_commands = shadow_monitor.add_subparsers(dest="shadow_command", required=True)
    shadow_start = shadow_commands.add_parser(
        "start", help="Start a 14-day shadow monitoring window"
    )
    shadow_start.add_argument("store")
    shadow_start.add_argument("--monitor-id", required=True)
    shadow_report = shadow_commands.add_parser(
        "report", help="Evaluate a shadow monitoring window"
    )
    shadow_report.add_argument("store")
    shadow_report.add_argument("--monitor-id", required=True)
    shadow_report.add_argument("--output")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if args.command == "schema":
        from protocollab import contracts
        names = ("DecisionBasis", "ActionProposal", "ControlEvent", "ObservationRecord", "PredictionRecord", "ModelArtifact", "ProcedureArtifact", "AdmissionReport")
        definitions = {name: getattr(contracts, name).model_json_schema() for name in names}
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps({"spec_version": "0.1", "schemas": definitions}, indent=2) + "\n")
        print(destination)
    elif args.command == "verify":
        from protocollab.storage import Store
        if not (Path(args.run) / "owner" / "owner.sqlite").is_file():
            raise FileNotFoundError("MISSING_OWNER_STORE")
        store = Store(Path(args.run) / "owner" / "owner.sqlite", "episode", readonly=True)
        try:
            anchor = store.verify()
            count = 0
            for row in store.db.execute("SELECT hash FROM blobs"):
                store.blob(row[0], raw=True)
                count += 1
            print(json.dumps({"status": "VERIFIED", "journal_anchor": anchor, "artifacts": count}))
        finally:
            store.close()
    elif args.command == "shadow-monitor":
        from protocollab.storage import Store
        from protocollab.verified.shadow_audit import (
            analyze_shadow_monitor,
            start_shadow_monitor,
        )

        path = owner_store_path(args.store)
        if args.shadow_command == "start":
            store = Store(path, "shadow-monitor")
            try:
                result = start_shadow_monitor(store, args.monitor_id)
            finally:
                store.close()
        else:
            store = Store(path, "shadow-monitor", readonly=True)
            try:
                result = analyze_shadow_monitor(store, args.monitor_id)
            finally:
                store.close()
            if args.output:
                destination = Path(args.output)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
            if result["status"] != "PASS":
                print(json.dumps(result, indent=2, sort_keys=True))
                raise SystemExit(1)
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "demo":
        from protocollab.evaluation.metrics import summarize_episode
        from protocollab.evaluation.runner import run_episode
        from protocollab_environment.generator import ProtocolConfig
        from protocollab_environment.scorer import score_episode
        manifest = ExperimentManifest(purpose="engineering", track=args.track,
            manifest_version="alias-fixture-reset-calibration-v1", learning={"total_resets": args.reset_cap})
        config = ProtocolConfig().to_dict()
        destination = Path(args.output)
        destination.mkdir(parents=True, exist_ok=True)
        lock_experiment(manifest, {"fixture": "design-section-23", "config": config}, root, destination / "experiment.lock.json")
        report, events, task = run_episode(destination, config, manifest, seed=args.seed)
        actual = score_episode(destination / "private" / "world.sqlite", events, task)
        metrics = summarize_episode(events, actual, "C3", args.track, "design-alias-fixture", args.seed)
        (destination / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
        from protocollab.evaluation.report import render_episode_report
        render_episode_report(destination)
        print(json.dumps({"output": str(destination), "adaptation": report["adaptation"]["status"],
                          "execution": report["execution"]["status"], "metrics": metrics}, indent=2))
    elif args.command == "generate":
        from protocollab_environment.generator.splits import make_archive
        archive = make_archive(args.seed, args.development, args.validation, args.sealed_per_stratum)
        with Path(args.output).open("x") as stream:
            json.dump(archive, stream, indent=2)
        print(json.dumps({"archive_hash": archive["archive_hash"], "counts": {k: len(v) for k, v in archive["splits"].items()}}))
    elif args.command == "lock":
        result = lock_experiment(load(args.manifest), load(args.archive), root, args.output)
        print(json.dumps({"status": "LOCKED", "manifest_hash": result["manifest_hash"]}))
    elif args.command == "run":
        from protocollab.evaluation.study import run_study
        archive = load(args.archive)
        manifest = verify_lock(args.lock, archive, root)
        result = run_study(manifest, archive, args.split, args.output)
        print(json.dumps(result, indent=2))
        if result["intervention_coverage"]["status"] != "COMPLETE":
            raise SystemExit(1)
    elif args.command == "keys":
        from protocollab.operator import generate_keys
        result = generate_keys(args.directory)
        print(json.dumps({"status": "CREATED", "principals": [r["principal"] for r in result.values()]}))
    elif args.command == "control":
        from protocollab.operator import submit_control
        fields = {name: getattr(args, name) for name in ("operation", "artifact", "appeal_id", "resolution") if getattr(args, name) is not None}
        result = submit_control(args.run, args.private_key, args.key_id, args.principal, args.verb, args.scope, **fields)
        print(json.dumps(result))
    elif args.command == "replay":
        from protocollab.evaluation.replay import replay_run
        print(json.dumps(replay_run(args.run), indent=2))
    elif args.command == "score":
        from protocollab.evaluation.bundle import rescore_run
        result = rescore_run(args.run)
        print(json.dumps(result, indent=2))
        if result["status"] != "MATCH":
            raise SystemExit(1)
    elif args.command == "bundle":
        from protocollab.evaluation.bundle import export_bundle
        print(json.dumps(export_bundle(args.run, args.output), indent=2))
    elif args.command == "verify-bundle":
        from protocollab.evaluation.bundle import verify_bundle
        print(json.dumps(verify_bundle(args.archive), indent=2))
    elif args.command == "report":
        from protocollab.evaluation.report import render_episode_report
        print(render_episode_report(args.run))
    elif args.command == "recover":
        import sqlite3

        from protocollab.isolation import EnvironmentProcess, MonitorProcess
        from protocollab.operator import load_authorities
        from protocollab.runtime import Runtime
        directory = Path(args.run)
        connection = sqlite3.connect(f"file:{directory / 'private' / 'world.sqlite'}?mode=ro", uri=True)
        config = json.loads(connection.execute("SELECT payload FROM config").fetchone()[0])
        connection.close()
        backend = EnvironmentProcess(directory / "private", config)
        monitor = MonitorProcess(directory / "monitor")
        runtime = Runtime(directory / "owner", backend, load_authorities(directory / "keys" / "root-manifest.json"), namespace="episode")
        runtime.monitor = monitor
        try:
            checkpoint = runtime.store.get("checkpoint", "latest")
            if not checkpoint:
                raise ValueError("MISSING_CHECKPOINT")
            result = runtime.recover(checkpoint["hash"])
            runtime.sync_monitor()
            runtime.store.export(directory / "public")
            print(json.dumps({"status": result, "epoch": runtime.governance.snapshot["epoch"]}))
        finally:
            runtime.close()
            monitor.close()
            backend.close()


if __name__ == "__main__":
    main()
