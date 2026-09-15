"""Allowlisted native engineering evidence, with immutable replay and fresh scoring.

Signing keys, control inboxes, model credentials and temporary files are excluded.
The retained simulator database is explicitly evaluator-only scoring evidence.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from protocollab.contracts import Task, canonical, digest
from protocollab.evaluation.metrics import summarize_episode
from protocollab.evaluation.replay import replay_run
from protocollab.storage import Store

FILES = ("episode.json", "metrics.json", "report.md", "report.json", "metrics.jsonl",
         "study-plan.json",
         "experiment.lock.json", "scenario_archive.json", "keys/root-manifest.json",
         "owner/owner.sqlite", "private/world.sqlite", "monitor/monitor.sqlite",
         "public/journal.jsonl")


def rescore_run(run):
    from protocollab_environment.scorer import score_aliases, score_episode
    run = Path(run)
    store = Store(run / "owner/owner.sqlite", "episode", readonly=True)
    try:
        store.verify()
        events = store.events()
        task = Task.model_validate(store.get("governance", "control")["task"])
        scenario = None
        if (run / "metrics.json").is_file():
            recorded = json.loads((run / "metrics.json").read_bytes())
        else:
            # The reviewed study writer retained rows only at the study root.
            # Match its exact directory convention and topology, never row order.
            archive = json.loads((run.parent / "scenario_archive.json").read_bytes())
            candidates = []
            rows = [json.loads(line) for line in (run.parent / "metrics.jsonl").read_bytes().splitlines()]
            for scenarios in archive["splits"].values():
                for item in scenarios:
                    for row in rows:
                        expected = f"{item['scenario_id']}-{row['seed']}-{row['condition']}-{row['governance_condition']}-{row['case']}"
                        if expected == run.name and row["topology_id"] == item["topology_hash"]:
                            candidates.append((row, item))
            if len(candidates) != 1:
                raise ValueError("AMBIGUOUS_OR_MISSING_EPISODE_METRICS")
            recorded, scenario = candidates[0]
        score = score_episode(run / "private/world.sqlite", events, task)
        if scenario and scenario.get("alias_pairs"):
            from protocollab.contracts import ModelArtifact
            from protocollab.modeling import MealyModel
            active = store.get("model", "active")
            if active:
                model = MealyModel(ModelArtifact.model_validate(store.blob(active["hash"])))
                score.update(score_aliases(model, scenario["config"], scenario["alias_pairs"]))
            else:
                score.update(alias_pair_discrimination=None, alias_prediction_coverage=0.0)
        metrics = summarize_episode(events, score, recorded["condition"], recorded["track"],
            recorded["topology_id"], recorded["seed"], recorded.get("scenario_class", "clean"))
        # Study metrics can also contain evaluator-only diagnostics outside the
        # episode scorer; compare all metrics that this scorer recomputes.
        changed = {key: {"recorded": recorded.get(key), "recomputed": value}
                   for key, value in metrics.items() if recorded.get(key) != value}
        return {"status": "MATCH" if not changed else "DIFFERENT", "metrics": metrics,
                "differences": changed, "world_actions_performed": 0}
    finally:
        store.close()


def export_bundle(run, output):
    run, output = Path(run), Path(output)
    if output.exists():
        raise FileExistsError(output)
    roots = [run] if (run / "episode.json").is_file() else sorted(p.parent for p in run.glob("*/episode.json") if p.parent.name != "prefixes")
    if not roots:
        raise ValueError("NO_EPISODES_TO_EXPORT")
    for root in roots:
        episode = json.loads((root / "episode.json").read_bytes())
        if episode.get("condition") not in ("C3", "C4") or episode.get("model_port_used"):
            raise ValueError("BUNDLE_REQUIRES_NATIVE_ENGINEERING_RUN")
        store = Store(root / "owner/owner.sqlite", "episode", readonly=True)
        try:
            store.verify()
            if any(e["kind"].startswith("llm.") for e in store.events()):
                raise ValueError("MODEL_PORT_CONTENT_REQUIRES_SEPARATE_SANITIZATION")
        finally:
            store.close()
    payloads = {}
    with tempfile.TemporaryDirectory(prefix="protocollab-bundle-") as temporary:
        for root in sorted(set([run, *roots])):
            for name in FILES:
                path = root / name
                if not path.is_file():
                    continue
                relative = path.relative_to(run).as_posix()
                if path.suffix == ".sqlite":
                    # A consistent SQLite snapshot excludes WAL/SHM and deleted pages.
                    source = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
                    snapshot = Path(temporary) / "snapshot.sqlite"
                    target = sqlite3.connect(snapshot)
                    try:
                        source.backup(target)
                        target.execute("VACUUM")
                    finally:
                        target.close()
                        source.close()
                    payloads[relative] = snapshot.read_bytes()
                    snapshot.unlink()
                else:
                    payloads[relative] = path.read_bytes()
            lock = root / "experiment.lock.json"
            if lock.is_file():
                retained = json.loads(lock.read_bytes())
                archive = root / retained["source_archive"]
                if archive.parent != root or digest(archive.read_bytes()) != retained["source_archive_hash"]:
                    raise ValueError("SOURCE_ARCHIVE_HASH_MISMATCH")
                payloads[archive.relative_to(run).as_posix()] = archive.read_bytes()
    manifest = {"schema_version": "1", "kind": "native-engineering-evidence",
        "episodes": [p.relative_to(run).as_posix() for p in roots],
        "files": {name: digest(data) for name, data in sorted(payloads.items())},
        "sanitization": {"policy": "explicit file allowlist; native conditions only; SQLite backup and VACUUM",
            "excluded": ["private signing keys", "control inboxes", "process leases", "WAL/SHM", "model-port requests"],
            "privileged_evidence": "private/world.sqlite is evaluator-only replay/scoring data, never actor input"},
        "research_claims": "NOT_ESTABLISHED"}
    payloads["BUNDLE.json"] = canonical(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payloads.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
    pin = digest(output.read_bytes())
    output.with_suffix(output.suffix + ".sha256").write_text(f"{pin}  {output.name}\n")
    return {"status": "EXPORTED", "sha256": pin, "files": len(payloads), "episodes": manifest["episodes"]}


def verify_bundle(archive_path):
    archive_path = Path(archive_path)
    pin = archive_path.with_suffix(archive_path.suffix + ".sha256").read_text().split()[0]
    if digest(archive_path.read_bytes()) != pin:
        raise ValueError("BUNDLE_HASH_MISMATCH")
    with tempfile.TemporaryDirectory(prefix="protocollab-replay-") as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("DUPLICATE_BUNDLE_MEMBER")
            manifest = json.loads(archive.read("BUNDLE.json"))
            if set(names) != {*manifest["files"], "BUNDLE.json"}:
                raise ValueError("BUNDLE_CONTENT_MISMATCH")
            if sum(info.file_size for info in archive.infolist()) > 1024 ** 3:
                raise ValueError("BUNDLE_SIZE_LIMIT")
            for name, expected in manifest["files"].items():
                path = Path(name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("INVALID_BUNDLE_PATH")
                data = archive.read(name)
                if digest(data) != expected:
                    raise ValueError("BUNDLE_MEMBER_HASH_MISMATCH")
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        results = []
        for name in manifest["episodes"]:
            episode = root / name
            if not episode.resolve().is_relative_to(root):
                raise ValueError("INVALID_EPISODE_PATH")
            store = Store(episode / "owner/owner.sqlite", "episode", readonly=True)
            try:
                anchor = store.verify()
                for row in store.db.execute("SELECT hash FROM blobs"):
                    store.blob(row[0], raw=True)
                exported = [json.loads(line) for line in (episode / "public/journal.jsonl").read_bytes().splitlines()]
                if exported != store.events():
                    raise ValueError("PUBLIC_JOURNAL_MISMATCH")
            finally:
                store.close()
            results.append({"episode": name, "journal_anchor": anchor,
                            "replay": replay_run(episode), "scoring": rescore_run(episode)})
        # Historical bundles may rescore differently under corrected definitions.
        return {"status": "VERIFIED", "sha256": pin, "episodes": results,
                "world_actions_performed": 0, "research_claims": "NOT_ESTABLISHED"}
