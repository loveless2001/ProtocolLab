import json
import zipfile
from pathlib import Path

import pytest

from protocollab.contracts import digest
from protocollab.evaluation.bundle import export_bundle, verify_bundle
from protocollab.evaluation.manifest import ExperimentManifest, lock_experiment
from protocollab.evaluation.metrics import summarize_episode
from protocollab_environment.scorer import score_episode


def test_native_bundle_excludes_signing_keys_replays_and_rescores(modeled_runtime, tmp_path):
    runtime = modeled_runtime
    planned = runtime.synthesize()
    assert runtime.run_procedure(planned["procedure"])["status"] == "SUCCESS"
    runtime.checkpoint()
    source = Path(__file__).resolve().parents[1]
    run = tmp_path / "run"
    run.mkdir()
    lock_experiment(ExperimentManifest(purpose="engineering"), {"fixture": True}, source, run / "experiment.lock.json")
    (run / "owner").mkdir()
    (run / "private").mkdir()
    (run / "keys").mkdir()
    (run / "keys/operator.key").write_text("DO-NOT-PUBLISH-PRIVATE-KEY")
    (run / "owner/control-inbox").mkdir()
    (run / "owner/control-inbox/unprocessed.json").write_text("DO-NOT-PUBLISH-INBOX")
    import sqlite3
    for connection, destination in ((runtime.store.db, run / "owner/owner.sqlite"),
                                    (runtime.backend.db, run / "private/world.sqlite")):
        target = sqlite3.connect(destination)
        connection.backup(target)
        target.close()
    runtime.store.export(run / "public")
    events = runtime.store.events()
    score = score_episode(run / "private/world.sqlite", events, runtime.governance.task)
    metrics = summarize_episode(events, score, "C3", "F", "test", 7)
    (run / "metrics.json").write_text(json.dumps(metrics))
    (run / "episode.json").write_text(json.dumps({"condition": "C3", "model_port_used": False}))
    archive = tmp_path / "evidence.zip"
    exported = export_bundle(run, archive)
    assert exported["sha256"] == digest(archive.read_bytes())
    with zipfile.ZipFile(archive) as bundle:
        assert "keys/operator.key" not in bundle.namelist()
        assert "owner/control-inbox/unprocessed.json" not in bundle.namelist()
        assert b"DO-NOT-PUBLISH" not in b"".join(bundle.read(name) for name in bundle.namelist())
    verified = verify_bundle(archive)
    assert verified["world_actions_performed"] == 0
    assert verified["episodes"][0]["replay"]["model_matches_retained_live_trace"]
    assert verified["episodes"][0]["scoring"]["status"] == "MATCH"
    archive.write_bytes(archive.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="BUNDLE_HASH_MISMATCH"):
        verify_bundle(archive)
