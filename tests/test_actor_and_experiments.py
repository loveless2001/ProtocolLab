import json
from pathlib import Path

import pytest
from conftest import control
from pydantic import ValidationError

from protocollab.actor import FrozenModelPort, ModelPortConfig, build_packet, parse_proposal
from protocollab.evaluation.manifest import ExperimentManifest, lock_experiment, verify_lock
from protocollab.evaluation.shims import install_log_only
from protocollab.gateway import InjectedCrash
from protocollab.learning import BudgetExhausted
from protocollab.operator import generate_keys, load_authorities, submit_control
from protocollab_environment.generator import ProtocolConfig, solve
from protocollab_environment.generator.splits import make_archive, validate_archive


def test_actor_proposal_language_rejects_execution_and_spoofing():
    assert parse_proposal('{"kind":"ACT","operation":"INSPECT"}').operation == "INSPECT"
    for text in ('{"kind":"ACT","operation":"TICK"}', '{"kind":"GRANT"}',
                 '{"kind":"WAIT","source":"admin"}', '{"kind":"WAIT","kind":"ACT"}'):
        with pytest.raises((ValueError, ValidationError)):
            parse_proposal(text)


def test_api_port_usage_fingerprint_and_caps(runtime, monkeypatch):
    config = ModelPortConfig(backend="api", model_id="frozen-snapshot-2026-01", endpoint="https://model.invalid/generate",
                             expected_fingerprint="fixed-fingerprint", max_input_tokens=16384)
    packet = {"task": "verify"}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            return json.dumps({"model_id": config.model_id, "fingerprint": "fixed-fingerprint", "input_tokens": 8,
                               "output_tokens": 4, "text": '{"kind":"WAIT"}'}).encode()
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    port = FrozenModelPort(config, runtime.store, max_calls=1)
    assert parse_proposal(port.generate(packet)).kind == "WAIT"
    assert (port.calls, port.input_tokens, port.output_tokens) == (1, 8, 4)
    with pytest.raises(BudgetExhausted):
        port.generate(packet)
    restarted = FrozenModelPort(config, runtime.store, max_calls=1)
    with pytest.raises(BudgetExhausted):
        restarted.generate(packet)
    completed = [e for e in runtime.store.events() if e["kind"] == "llm.completed"]
    assert completed[0]["payload"]["fingerprint"] == "fixed-fingerprint"
    with pytest.raises(ValidationError):
        ModelPortConfig(backend="api", model_id="latest", endpoint="https://model.invalid")


def test_packet_conditions_and_raw_history(runtime):
    runtime.turn("INSPECT")
    c0 = build_packet(runtime, "C0")
    c1 = build_packet(runtime, "C1")
    c2 = build_packet(runtime, "C2")
    assert "model" not in c0 and "belief" not in c0
    assert "fixed_predictor" in c1 and "model" not in c1
    assert "model" in c2 and "belief" in c2
    observation = next(e for e in c0["history"]["events"] if e["kind"] == "epistemic.observation")
    assert "raw_packet" in observation["payload"]
    assert c0["history"]["complete_history_available"]


def test_experiment_lock_and_topology_split(tmp_path):
    archive = make_archive(seed=12, development=2, validation=2, sealed_per_stratum=2)
    assert validate_archive(archive)
    assert len({s["topology_hash"] for group in archive["splits"].values() for s in group}) == 10
    root = tmp_path / "code"
    root.mkdir()
    (root / "protocollab").mkdir()
    code = root / "protocollab" / "module.py"
    code.write_text("version = 1\n")
    lock = tmp_path / "lock.json"
    manifest = ExperimentManifest()
    lock_experiment(manifest, archive, root, lock)
    assert verify_lock(lock, archive, root) == manifest
    import zipfile
    locked = json.loads(lock.read_text())
    with zipfile.ZipFile(lock.parent / locked["source_archive"]) as snapshot:
        assert snapshot.read("protocollab/module.py") == b"version = 1\n"
    code.write_text("version = 2\n")
    with pytest.raises(ValueError, match="CODE_CHANGED"):
        verify_lock(lock, archive, root)
    with pytest.raises(FileExistsError):
        lock_experiment(manifest, archive, root, lock)


def test_log_only_shim_is_explicit_and_materially_differs(runtime):
    with pytest.raises(PermissionError):
        install_log_only(runtime, "G0")
    control(runtime, "PAUSE_DISPATCH")
    assert runtime.turn("SUBMIT_A")["action"]["status"] == "DENIED"
    install_log_only(runtime, "G0", disposable_benchmark=True)
    assert runtime.turn("SUBMIT_A")["action"]["status"] == "ACKNOWLEDGED"
    assert any(e["kind"] == "governance.would_deny" for e in runtime.store.events())


def test_operator_inbox_verified_at_sequencer(runtime, tmp_path):
    keys = tmp_path / "keys"
    generate_keys(keys)
    runtime.governance.keys = load_authorities(keys / "root-manifest.json")
    # Operator tool expects a run root containing the owner directory.
    owner_link = tmp_path / "operator-run" / "owner"
    owner_link.parent.mkdir()
    owner_link.symlink_to(runtime.directory)
    result = submit_control(owner_link.parent, keys / "operator.key", "operator-key", "operator", "PAUSE_DISPATCH")
    assert result["status"] == "QUEUED"
    assert not runtime.governance.is_paused("R")
    runtime.process_controls()
    assert runtime.governance.is_paused("R")
    assert json.loads(Path(result["receipt_path"]).read_text())["status"] == "ACCEPTED"


def test_clock_crash_reconciles_without_double_apply(runtime):
    runtime.turn("SUBMIT_A")
    runtime.turn("SIGNAL_X")
    checkpoint = runtime.checkpoint()
    with pytest.raises(InjectedCrash):
        runtime.broker.tick(crash_at="after_apply")
    pending = runtime.store.db.execute("SELECT command_id FROM clock_actions WHERE status='DISPATCHED'").fetchone()[0]
    original = runtime.backend.reconcile(pending)
    assert runtime.recover(checkpoint) == "RECOVERED"
    assert runtime.broker.reconcile_clock(pending)["domain_output"] == "TICKED"
    assert runtime.backend.apply("R", "TICK", pending) == original
    assert runtime.belief.snapshot.observation_refs


def test_unobservable_impossible_track_has_no_fake_success():
    assert solve(ProtocolConfig(impossible=True), max_turns=10) is None
