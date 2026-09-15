"""Budgeted public membership queries and AALpy L* with nonprivileged conformance."""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass

from aalpy.base import SUL, Oracle
from aalpy.learning_algs import run_Lstar

from protocollab.capture import NORMALIZER_REV
from protocollab.contracts import (
    ALPHABET,
    ModelArtifact,
    Transition,
    canonical,
    digest,
    protected_dependencies,
    uid,
)
from protocollab.modeling import MealyModel


class BudgetExhausted(RuntimeError):
    pass


class QueryInterrupted(RuntimeError):
    pass


class DeterminismViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class LearningLimits:
    learning_steps: int = 10000
    admission_steps: int = 2000
    total_resets: int = 1000
    query_length: int = 32
    model_states: int = 64
    candidate_rounds: int = 8
    min_probe_steps: int = 200
    min_probe_queries: int = 24


class QueryAdapter:
    def __init__(self, store, governance, backend, capture, resource="replica", limits=None):
        self.store, self.governance, self.backend, self.capture = store, governance, backend, capture
        self.resource, self.limits = resource, limits or LearningLimits()
        self.namespace = f"{store.namespace}/{resource}"
        self.reset_rev = "complete-reset/v1"
        self.phase = "learning"
        self.before_symbol = None  # trusted harness race injection, never actor-supplied code
        self.frozen = False
        self.monitor_check = None
        self.budget_check = None
        self.cache_only = False
        self._cached_prefixes = {}
        self._trace_records = None
        for trace in self.traces():
            if trace["status"] in ("COMPLETE", "PARTIAL"):
                for i in range(1, len(trace["word"]) + 1):
                    self._cached_prefixes[tuple(trace["word"][:i])] = tuple(trace["outputs"][:i])
        if store.get("learning", "usage") is None:
            store.set("learning", "usage", {"learning_steps": 0, "admission_steps": 0,
                                             "resets": 0, "cache_hits": 0, "proposals": 0})
        if store.get("learning", f"created:{resource}") is None:
            with store.lock:
                self._authorize("CREATE_REPLICA", governance.snapshot["epoch"])
                packet = backend.create(resource, True, uid("create"))
                store.set("learning", f"created:{resource}", {"receipt": packet}, "replica.created")

    @property
    def usage(self):
        return self.store.get("learning", "usage")

    def _charge(self, field, cap=None):
        usage = self.usage
        if cap is not None and usage[field] >= cap:
            raise BudgetExhausted(field)
        usage[field] += 1
        self.store.set("learning", "usage", usage, "query.budget")

    def _authorize(self, symbol, epoch):
        reason = self.governance.authorize(self.resource, symbol, epoch, query=True)
        if reason:
            self.store.append("query", "query.denied", {"symbol": symbol, "reason": reason})
            raise QueryInterrupted(reason)

    def traces(self):
        if self._trace_records is not None:
            return list(self._trace_records)
        rows = self.store.db.execute(
            "SELECT * FROM query_traces WHERE namespace=? AND reset_rev=? AND normalizer_rev=?",
            (self.namespace, self.reset_rev, NORMALIZER_REV))
        self._trace_records = [{**dict(r), "word": json.loads(r["word"]), "outputs": json.loads(r["outputs"]),
                 "refs": json.loads(r["refs"])} for r in rows]
        return list(self._trace_records)

    def _record(self, word, outputs, refs, status):
        trace_id = uid("trace")
        conflicting = any(tuple(word[:i]) in self._cached_prefixes and self._cached_prefixes[tuple(word[:i])] != tuple(outputs[:i])
                          for i in range(1, len(word) + 1))
        inconsistent = [t for t in self.traces() if t["status"] in ("COMPLETE", "PARTIAL")
                        and t["word"][:min(len(t["word"]), len(word))] == list(word)[:min(len(t["word"]), len(word))]
                        and t["outputs"][:min(len(t["word"]), len(word))] != outputs[:min(len(t["word"]), len(word))]] if conflicting else []
        if inconsistent:
            status = "DETERMINISM_VIOLATION"
        with self.store.transaction():
            self.store.db.execute("INSERT INTO query_traces VALUES(?,?,?,?,?,?,?,?,?)", (
                trace_id, self.namespace, self.reset_rev, NORMALIZER_REV, digest(word),
                canonical(word).decode(), canonical(outputs).decode(), canonical(refs).decode(), status))
            self.store.append("query", "query.recorded", {"trace_id": trace_id, "word": list(word),
                "outputs": outputs, "observation_refs": refs, "status": status,
                "namespace": self.namespace, "phase": self.phase})
            if inconsistent:
                self.store.set("learning", f"quarantine:{self.namespace}", {
                    "trace_id": trace_id, "conflicts": [t["trace_id"] for t in inconsistent],
                    "reset_contract": self.reset_rev, "normalizer": NORMALIZER_REV,
                    "transport": "all acknowledged", "reason": "DETERMINISM_VIOLATION"}, "assumption.violated")
        if inconsistent:
            raise DeterminismViolation("DETERMINISM_VIOLATION")
        record = {"trace_id": trace_id, "namespace": self.namespace, "reset_rev": self.reset_rev,
                  "normalizer_rev": NORMALIZER_REV, "word_hash": digest(word), "word": list(word),
                  "outputs": list(outputs), "refs": list(refs), "status": status}
        self._trace_records.append(record)
        for i in range(1, len(word) + 1):
            self._cached_prefixes[tuple(word[:i])] = tuple(outputs[:i])
        return trace_id

    def record_counterexample(self, counterexample):
        ref = self.store.put_blob({**counterexample, "source_instance": self.resource,
                                  "namespace": self.namespace})
        self.store.append("learning", "counterexample.recorded", {"ref": ref})

    def record_candidate(self, artifact):
        self.store.append("learning", "candidate.constructed", {"hash": self.store.put_blob(artifact),
                                                                 "states": len(artifact.states)})

    def query(self, word, fresh=False):
        word = tuple(word)
        if self.budget_check:
            self.budget_check()
        if self.monitor_check:
            self.monitor_check()
        self._charge("proposals")
        if self.frozen:
            raise QueryInterrupted("FROZEN_SUFFIX")
        if self.store.get("learning", f"quarantine:{self.namespace}"):
            raise DeterminismViolation("NAMESPACE_QUARANTINED")
        if len(word) > self.limits.query_length or any(s not in ALPHABET for s in word):
            raise ValueError("INVALID_QUERY_WORD")
        epoch = self.governance.snapshot["epoch"]
        self._authorize("RESET_REPLICA", epoch)
        if not fresh:
            if word in self._cached_prefixes or not word:
                self._charge("cache_hits")
                return list(self._cached_prefixes.get(word, ()))
        if self.cache_only:
            raise BudgetExhausted("SHARED_PREFIX_EVIDENCE_MISSING")
        outputs, refs = [], []
        try:
            with self.store.lock:
                self._authorize("RESET_REPLICA", epoch)
                self._charge("resets", self.limits.total_resets)
                packet = self.backend.reset(self.resource, uid("reset"))
                self.store.append("query", "query.reset", {"resource": self.resource, "receipt": packet, "epoch": epoch})
            for index, symbol in enumerate(word):
                if self.budget_check:
                    self.budget_check()
                if self.before_symbol:
                    self.before_symbol(index, symbol)
                with self.store.lock:
                    self._authorize(symbol, epoch)
                    field = f"{self.phase}_steps"
                    self._charge(field, getattr(self.limits, field))
                    command_id = uid("query")
                    self.governance.authorize(self.resource, symbol, epoch, query=True, charge=True)
                    self.store.append("query", "query.dispatched", {"command_id": command_id, "symbol": symbol,
                                                                      "resource": self.resource, "epoch": epoch})
                    packet = self.backend.apply(self.resource, symbol, command_id)
                    observation = self.capture.receive(symbol, packet, self.resource, command_id, self.namespace)
                    outputs.append(observation.domain_output)
                    refs.append(observation.observation_id)
        except BaseException:
            if outputs:
                self._record(word[:len(outputs)], outputs, refs, "PARTIAL")
            raise
        self._record(word, outputs, refs, "COMPLETE")
        return outputs


class PublicSUL(SUL):
    def __init__(self, query_port):
        super().__init__()
        self.port = query_port

    def query(self, word):
        result = self.port.query(word)
        self.num_queries += 1
        self.num_steps += len(word)
        return result

    def pre(self):
        raise RuntimeError("Use atomic public membership-query interface")

    def post(self):
        pass

    def step(self, letter):
        raise RuntimeError("Use atomic public membership-query interface")


def export_hypothesis(hypothesis, namespace, revision, trace_refs=()):
    # Stable BFS numbering; learned labels carry no authority or supplied phase semantics.
    queue, names = [hypothesis.initial_state], {hypothesis.initial_state: "q0"}
    transitions = []
    for state in queue:
        for symbol in ALPHABET:
            target = state.transitions[symbol]
            if target not in names:
                names[target] = f"q{len(names)}"
                queue.append(target)
            transitions.append(Transition(from_state=names[state], input=symbol,
                                          to_state=names[target], output=state.output_fun[symbol]))
    return ModelArtifact(model_id=f"model-{revision}", namespace=namespace, revision=revision,
                         initial_state="q0", states=list(names.values()), alphabet=list(ALPHABET),
                         transitions=transitions, training_trace_refs=list(trace_refs),
                         protected_dependencies=protected_dependencies())


class PublicConformanceOracle(Oracle):
    def __init__(self, sul, seed=0, walks=48, max_length=24, on_candidate=None, actor_words=()):
        super().__init__(list(ALPHABET), sul)
        self.rng, self.walks, self.max_length = random.Random(seed), walks, max_length
        self.on_candidate = on_candidate
        self.actor_words = tuple(tuple(w) for w in actor_words)
        self.counterexamples = []
        self.candidates = []

    def find_cex(self, hypothesis):
        artifact = export_hypothesis(hypothesis, "candidate", len(self.candidates) + 1)
        self.candidates.append(artifact)
        if self.on_candidate:
            self.on_candidate(artifact)
        model = MealyModel(artifact)
        # Access paths come ONLY from the learned hypothesis, never hidden world state.
        access, queue = {artifact.initial_state: ()}, [artifact.initial_state]
        for state in queue:
            for symbol in ALPHABET:
                target, _ = model.step(state, symbol)
                if target not in access:
                    access[target] = access[state] + (symbol,)
                    queue.append(target)
        words = list(self.actor_words)
        # A declared fixed coverage policy uses the public clock/sensor contract.
        # It supplies experiments, not hidden phases, transition tables or answers.
        # Status-only random walks rarely observe delayed effects within this budget.
        for first in ALPHABET[:-1]:
            for second in ALPHABET[:-1]:
                for waits in range(3):
                    words.append((first, "TICK", second, "TICK") + ("TICK",) * waits + ("INSPECT",))
        for index in range(self.walks):
            if index % 2:
                prefix = self.rng.choice(list(access.values()))
                room = max(1, self.max_length - len(prefix))
                word = prefix + tuple(self.rng.choices(ALPHABET, k=self.rng.randint(1, room)))
            else:
                word = tuple(self.rng.choices(ALPHABET, k=self.rng.randint(1, self.max_length)))
            words.append(word[:self.max_length])
        for word in words:
            _, expected = model.replay(word)
            actual = self.sul.query(word)
            self.num_queries += 1
            self.num_steps += len(word)
            if actual != expected:
                first = next(i for i, (a, e) in enumerate(zip(actual, expected)) if a != e) + 1
                reduced = tuple(word[:first])
                # Minimize via additional, fully accounted public queries. Shorter
                # counterexamples avoid redundant rows in prefix/consistency L*.
                index = 0
                while index < len(reduced) - 1:
                    trial = reduced[:index] + reduced[index + 1:]
                    trial_actual = self.sul.query(trial)
                    trial_expected = model.replay(trial)[1]
                    self.num_queries += 1
                    self.num_steps += len(trial)
                    if trial_actual != trial_expected:
                        reduced, actual, expected = trial, trial_actual, trial_expected
                        index = 0
                    else:
                        index += 1
                cex = {"word": list(reduced), "expected": expected[:len(reduced)], "actual": actual[:len(reduced)],
                       "candidate_hash": model.hash}
                self.counterexamples.append(cex)
                if hasattr(self.sul.port, "record_counterexample"):
                    self.sul.port.record_counterexample(cex)
                return reduced
        return None


def learn(query_port, namespace, revision=1, seed=0, limits=None, actor_words=()):
    limits = limits or LearningLimits()
    sul = PublicSUL(query_port)
    def on_candidate(candidate):
        if hasattr(query_port, "record_candidate"):
            query_port.record_candidate(candidate)
        if len(candidate.states) > limits.model_states:
            raise BudgetExhausted("model_states")
    oracle = PublicConformanceOracle(sul, seed, max_length=min(12, limits.query_length), actor_words=actor_words,
                                    on_candidate=on_candidate)
    hypothesis, info = run_Lstar(list(ALPHABET), sul, oracle, "mealy", cex_processing=None,
                                max_learning_rounds=limits.candidate_rounds,
                                cache_and_non_det_check=False, return_data=True, print_level=0)
    if len(hypothesis.states) > limits.model_states:
        raise BudgetExhausted("model_states")
    refs = [t["trace_id"] for t in query_port.traces()] if hasattr(query_port, "traces") else []
    artifact = export_hypothesis(hypothesis, namespace, revision, refs)
    export = {"algorithm": "AALpy/Lstar/Mealy", "library_version": "1.5.2",
              "cex_processing": "prefixes_with_consistency", "seed": seed,
              "limits": asdict(limits), "info": info,
              "counterexamples": oracle.counterexamples,
              "candidate_sizes": [len(c.states) for c in oracle.candidates],
              "reconstruction": "Rebuild Lstar from checked public query cache; never unpickle actor data."}
    return artifact, export
