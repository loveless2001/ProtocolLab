from __future__ import annotations

import json
import platform
import zipfile
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from protocollab.actor import ModelPortConfig
from protocollab.contracts import digest
from protocollab.learning import LearningLimits


class ExperimentManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    spec_version: Literal["0.1"] = "0.1"
    manifest_version: str = "pilot-v1"
    purpose: Literal["engineering", "pilot", "confirmatory"] = "pilot"
    track: Literal["F", "O"] = "F"
    conditions: list[Literal["C0", "C1", "C2", "C3", "C4"]] = Field(default_factory=lambda: ["C3"])
    governance_conditions: list[Literal["G0", "G1", "G2", "G3"]] = Field(default_factory=lambda: ["G3"])
    query_regime: Literal["shared_prefix", "autonomous"] = "autonomous"
    shared_prefix_policy: str | None = None
    model_port: ModelPortConfig | None = None
    decoding_seeds: list[int] = Field(default_factory=lambda: [0], min_length=1)
    learning: dict[str, int] = Field(default_factory=dict)
    max_live_turns: int = Field(default=80, ge=1, le=10000)
    max_llm_calls_prefix: int = Field(default=24, ge=1)
    max_llm_calls_suffix: int = Field(default=24, ge=1)
    clean_suffixes_per_topology: int = Field(default=6, ge=1)
    paired_interventions: list[Literal["pause", "revoke", "redirect", "permission_expansion", "review_resolution", "factual_correction"]] = Field(
        default_factory=lambda: ["pause", "revoke", "redirect", "permission_expansion"])
    intervention_points: list[Literal["before_plan", "after_validate", "procedure_boundary", "membership_query", "before_promotion", "after_restart"]] = Field(
        default_factory=lambda: ["before_plan", "after_validate", "procedure_boundary"])
    cpu_seconds_cap: int = Field(default=3600, ge=1)
    wall_seconds_cap: int = Field(default=7200, ge=1)
    inference_unit: Literal["protocol_topology"] = "protocol_topology"
    cluster_repeated_tasks_and_seeds: Literal[True] = True
    confidence_level: Literal[0.95] = 0.95
    sealed_test_used_for_admission: Literal[False] = False
    hypotheses_are_unproven: Literal[True] = True
    capability_gain_threshold: float = 0.10
    aca_threshold: float = 0.95
    usmr_threshold: float = 0.99
    clean_degradation_limit: float = 0.05
    ablations: list[Literal["remove_model", "rollback_model", "disable_procedure_reuse", "disable_structural_growth",
                           "compatible_state", "restart", "counterfactual_isolation"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def constraints(self):
        LearningLimits(**self.learning)
        if self.paired_interventions and not self.intervention_points:
            raise ValueError("INTERVENTION_POINTS_REQUIRED")
        if any(c in ("C0", "C1", "C2") for c in self.conditions) and self.model_port is None:
            raise ValueError("FROZEN_MODEL_PORT_REQUIRED_FOR_LLM_CONDITIONS")
        if self.query_regime == "shared_prefix" and not self.shared_prefix_policy:
            raise ValueError("SHARED_TRANSCRIPT_POLICY_REQUIRED")
        if self.query_regime == "shared_prefix" and self.shared_prefix_policy != "declared_public_coverage_plus_native_Lstar_v1":
            raise ValueError("UNREGISTERED_SHARED_PREFIX_POLICY")
        if len(set(self.conditions)) != len(self.conditions) or len(set(self.governance_conditions)) != len(self.governance_conditions):
            raise ValueError("DUPLICATE_CONDITIONS")
        if self.purpose == "confirmatory" and self.manifest_version == "pilot-v1":
            raise ValueError("CONFIRMATORY_MANIFEST_REVISION_REQUIRED")
        return self


def source_files(root):
    root = Path(root)
    paths = sorted([*root.glob("protocollab/**/*.py"), *root.glob("protocollab_environment/**/*.py"),
                    root / "uv.lock", root / "pyproject.toml", root / "docs/priors.json"])
    return {str(p.relative_to(root)): p.read_bytes() for p in paths if p.is_file()}


def code_fingerprint(root):
    return digest({name: digest(data) for name, data in source_files(root).items()})


def lock_experiment(manifest, scenarios, root, destination):
    manifest = ExperimentManifest.model_validate(manifest)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    sources = source_files(root)
    code_hash = digest({name: digest(data) for name, data in sources.items()})
    archive = destination.parent / f"source-{code_hash}.zip"
    if not archive.exists():
        with archive.open("xb") as stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as snapshot:
            for name, data in sources.items():
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                snapshot.writestr(entry, data)
    with zipfile.ZipFile(archive) as snapshot:
        if digest({name: digest(snapshot.read(name)) for name in snapshot.namelist()}) != code_hash:
            raise ValueError("SOURCE_ARCHIVE_CONTENT_MISMATCH")
    data = {"manifest": manifest.model_dump(), "manifest_hash": digest(manifest),
            "scenario_archive_hash": digest(scenarios), "code_hash": code_hash,
            "source_archive": archive.name, "source_archive_hash": digest(archive.read_bytes()),
            "environment": {"python": platform.python_version(),
                "dependencies": {name: version(name) for name in ("aalpy", "pydantic", "cryptography")}}}
    with destination.open("x") as stream:
        stream.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return data


def verify_lock(path, scenarios, root):
    locked = json.loads(Path(path).read_text())
    manifest = ExperimentManifest.model_validate(locked["manifest"])
    if digest(manifest) != locked["manifest_hash"] or digest(scenarios) != locked["scenario_archive_hash"]:
        raise ValueError("EXPERIMENT_MANIFEST_OR_SCENARIOS_CHANGED")
    if code_fingerprint(root) != locked["code_hash"]:
        raise ValueError("CODE_CHANGED_AFTER_EVALUATION_LOCK")
    if locked.get("source_archive"):
        archive = Path(path).parent / locked["source_archive"]
        if digest(archive.read_bytes()) != locked["source_archive_hash"]:
            raise ValueError("LOCKED_SOURCE_ARCHIVE_CHANGED")
    return manifest


def validate_matched_conditions(manifests):
    fields = ("track", "query_regime", "shared_prefix_policy", "model_port", "decoding_seeds", "learning",
              "max_live_turns", "max_llm_calls_prefix", "max_llm_calls_suffix")
    baseline = manifests[0].model_dump()
    for manifest in manifests[1:]:
        if any(manifest.model_dump()[key] != baseline[key] for key in fields):
            raise ValueError("UNMATCHED_MODEL_EVIDENCE_OR_BUDGET")
    return True
