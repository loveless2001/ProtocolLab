"""Replay and fresh adaptive validation, with atomic active-pointer swaps."""

from __future__ import annotations

import random

from protocollab.contracts import (
    ALPHABET,
    AdmissionReport,
    ModelArtifact,
    digest,
    protected_dependencies,
    uid,
)
from protocollab.learning import BudgetExhausted, DeterminismViolation, QueryInterrupted
from protocollab.modeling import MealyModel


class ModelRegistry:
    def __init__(self, store, governance, belief, capture):
        self.store, self.governance, self.belief, self.capture = store, governance, belief, capture
        self.frozen = False

    def current_snapshot(self, namespace=None):
        if namespace is not None and namespace != self.store.namespace:
            raise KeyError("UNKNOWN_MODEL_NAMESPACE")
        active = self.store.get("model", "active")
        return MealyModel(ModelArtifact.model_validate(self.store.blob(active["hash"]))) if active else None

    def store_candidate(self, artifact):
        artifact = ModelArtifact.model_validate(artifact.model_dump())
        with self.store.transaction():
            key = self.store.put_blob(artifact)
            event = self.store.set("model", key, {"status": "DRAFT", "hash": key}, "model.locked")
        return key, event["seq"]

    def validate(self, artifact, adapter, seed=None, regression_traces=()):
        if self.frozen:
            raise QueryInterrupted("FROZEN_SUFFIX")
        artifact = ModelArtifact.model_validate(artifact.model_dump())
        key, locked_seq = self.store_candidate(artifact)
        # Sample only after candidate lock. Retain the seed afterward for replay;
        # the learner never receives it before submitting the immutable artifact.
        if seed is None:
            import secrets
            seed = secrets.randbits(64)
        model = MealyModel(artifact)
        dependency_ok = artifact.protected_dependencies == protected_dependencies()
        traces = [t for t in adapter.traces() if t["status"] in ("COMPLETE", "PARTIAL")]
        declared = set(artifact.training_trace_refs)
        available = {t["trace_id"] for t in traces}
        counterexamples, probes = [], []
        started = adapter.usage
        status = "PASS"

        def mismatch(word, actual, expected, source):
            ref = self.store.put_blob({"word": list(word), "actual": actual, "expected": expected,
                                      "source_trace": source, "candidate_hash": key, "namespace": adapter.namespace})
            self.store.append("admission", "counterexample.recorded", {"ref": ref})
            counterexamples.append(ref)

        if not dependency_ok or not declared.issubset(available):
            status = "FAIL"
            self.store.append("admission", "candidate.rejected", {"hash": key,
                "reason": "PROTECTED_DEPENDENCY_CHANGE" if not dependency_ok else "UNATTRIBUTABLE_TRAINING_SET"})
        if status == "PASS":
            # Includes the declared set, incumbent regression, and all accepted past counterexamples.
            for trace in [*traces, *regression_traces]:
                _, predicted = model.replay(trace["word"])
                if predicted != trace["outputs"]:
                    mismatch(trace["word"], trace["outputs"], predicted, trace["trace_id"])
            if counterexamples:
                status = "FAIL"
        self.store.set("model", key, {"status": "REPLAY_VALIDATED" if status == "PASS" else "DRAFT", "hash": key})
        probe_start = self.store.append("admission", "probes.started", {"candidate_hash": key})["seq"]
        previous_phase = adapter.phase
        adapter.phase = "admission"
        try:
            if status == "PASS":
                rng = random.Random(seed)
                limits = adapter.limits
                while len(probes) < limits.min_probe_queries or adapter.usage["admission_steps"] - started["admission_steps"] < limits.min_probe_steps:
                    word = rng.choices(ALPHABET, k=min(limits.query_length, rng.randint(8, 16)))
                    actual = adapter.query(word, fresh=True)
                    trace = adapter.traces()[-1]
                    probes.append(trace["trace_id"])
                    _, expected = model.replay(word)
                    if actual != expected:
                        mismatch(word, actual, expected, trace["trace_id"])
                        status = "FAIL"
                        break
        except BudgetExhausted:
            status = "BUDGET_EXHAUSTED"
        except QueryInterrupted:
            status = "INTERRUPTED"
        except DeterminismViolation:
            status = "ASSUMPTION_VIOLATION"
        finally:
            adapter.phase = previous_phase
        probe_manifest = {"seed": seed, "trace_refs": probes, "candidate_hash": key,
                          "retired": True, "role": "adaptive_prefix_validation", "sealed": False}
        report = AdmissionReport(report_id=uid("admission"), candidate_hash=key, namespace=artifact.namespace,
            gate_version="fresh-probe/v1", training_manifest_hash=self.store.put_blob({"traces": [t["trace_id"] for t in traces]}),
            probe_manifest_hash=self.store.put_blob(probe_manifest), candidate_locked_seq=locked_seq,
            probe_started_seq=probe_start,
            real_probe_steps=adapter.usage["admission_steps"] - started["admission_steps"],
            probe_resets=adapter.usage["resets"] - started["resets"], counterexample_refs=counterexamples,
            status=status, protected_dependencies_unchanged=dependency_ok)
        report_hash = self.store.put_blob(report)
        self.store.set("model", key, {"status": "PROBE_VALIDATED" if status == "PASS" else "QUARANTINED" if status == "ASSUMPTION_VIOLATION" else "DRAFT",
                                      "hash": key, "report": report_hash}, "admission.completed")
        return report

    def promote(self, artifact, report, expected_epoch, replica_resource="replica"):
        with self.store.transaction():
            if self.frozen:
                raise QueryInterrupted("FROZEN_SUFFIX")
            key = digest(artifact)
            registered = self.store.get("model", key)
            if report.status != "PASS" or report.candidate_hash != key or not registered or registered["status"] != "PROBE_VALIDATED" or self.store.blob(registered["report"]) != report.model_dump():
                raise PermissionError("VALID_ADMISSION_REQUIRED")
            reason = self.governance.authorize(replica_resource, "RESET_REPLICA", expected_epoch, query=True)
            if reason:
                raise QueryInterrupted(reason)
            previous = self.current_snapshot()
            if previous and artifact.revision <= previous.artifact.revision:
                raise ValueError("MODEL_REVISION_NOT_MONOTONIC")
            if previous:
                self.store.set("model", previous.hash, {"status": "SUPERSEDED", "hash": previous.hash})
            self.store.set("model", key, {**registered, "status": "ACTIVE", "evidence_status": "CONSISTENT_WITH_TESTED_TRACES"})
            self.store.set("model", "active", {"hash": key, "revision": artifact.revision}, "model.promoted")
            history = [self.capture.read_observation(ref) for ref in self.belief.snapshot.observation_refs]
            self.belief.rebase(MealyModel(artifact), history, self.belief.snapshot.complete_history)
            self.store.set("procedure", "active", None, "procedure.invalidated")
            return key
