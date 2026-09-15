"""The offline design is pinned and cannot masquerade as an executable model lock."""

import json
from pathlib import Path

import pytest
import yaml

from protocollab.actor import ModelPortConfig
from protocollab.contracts import digest
from protocollab.evaluation.manifest import ExperimentManifest, code_fingerprint
from protocollab_environment.generator.splits import validate_archive


def test_smoke_design_pins_six_episodes_and_requires_real_model_binding():
    root = Path(__file__).resolve().parents[1]
    directory = root / "experiments/actor-smoke"
    lock = json.loads((directory / "design.lock.json").read_text())
    assert lock.pop("design_hash") == digest(lock)
    assert lock["status"] == "DESIGN_LOCKED_MODEL_UNSELECTED" and lock["execution_enabled"] is False
    assert lock["source_code_hash"] == code_fingerprint(root)
    for name, pin in lock["files"].items():
        assert digest((directory / name).read_bytes()) == pin
    pending = yaml.safe_load((directory / "manifest.pending.yaml").read_text())
    with pytest.raises(ValueError, match="FROZEN_MODEL_PORT_REQUIRED"):
        ExperimentManifest.model_validate(pending)
    limits = json.loads((directory / "model-limits.json").read_text())
    # Schema check only: never send a request to this fixture endpoint.
    model = ModelPortConfig(backend="api", model_id="schema-fixture", endpoint="https://model.invalid", **limits)
    manifest = ExperimentManifest.model_validate({**pending, "model_port": model.model_dump()})
    archive = json.loads((directory / "development.json").read_text())
    assert validate_archive(archive)
    assert len(archive["splits"]["development"]) == 1
    assert all(not entries for name, entries in archive["splits"].items() if name != "development")
    assert manifest.conditions == ["C0", "C2"] and manifest.governance_conditions == ["G3"]
    assert manifest.intervention_points == ["before_plan"]
    assert manifest.decoding_seeds == [7]
    episodes = len(manifest.conditions) * (manifest.clean_suffixes_per_topology + 2 * len(manifest.paired_interventions))
    assert episodes == lock["limits"]["suffix_episodes"] == 6
    calls = episodes * manifest.max_llm_calls_suffix
    assert calls == lock["limits"]["model_calls"] == 48
    assert calls * (model.max_input_tokens + model.max_output_tokens) == lock["limits"]["reserved_model_tokens"] == 798720
    assert lock["limits"]["authorized_paid_spend_usd"] == 0
