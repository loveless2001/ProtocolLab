"""Strict, versioned public envelopes. Validation never confers authority."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Symbol = Literal[
    "SUBMIT_A", "SUBMIT_B", "SIGNAL_X", "SIGNAL_Y", "CANCEL", "STATUS", "INSPECT", "TICK"
]
Operation = Literal["SUBMIT_A", "SUBMIT_B", "SIGNAL_X", "SIGNAL_Y", "CANCEL", "STATUS", "INSPECT"]
Artifact = Literal["A", "B"]
ALPHABET = ("SUBMIT_A", "SUBMIT_B", "SIGNAL_X", "SIGNAL_Y", "CANCEL", "STATUS", "INSPECT", "TICK")
OPERATIONS = ALPHABET[:-1]
READS = frozenset(("STATUS", "INSPECT"))
MUTATIONS = frozenset(set(OPERATIONS) - READS)
DOMAIN_CODES = frozenset(("ACCEPTED", "OK", "READY", "CANCELLED", "TICKED", "NOOP", "REJECTED"))
Id = Annotated[str, Field(min_length=1, max_length=256)]
StateId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Rev = Annotated[int, Field(ge=0)]


def uid(prefix: str = "e") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def canonical(value) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def strict_json(raw: bytes | str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def forbidden(_):
        raise ValueError("FLOAT_OR_NONFINITE_JSON")

    return json.loads(raw, object_pairs_hook=pairs, parse_float=forbidden, parse_constant=forbidden)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["0.1"] = "0.1"


class ProtectedDependencies(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    alphabet_hash: Hash
    normalizer_hash: Hash
    operation_registry_hash: Hash
    governance_interface_hash: Hash
    task_schema_hash: Hash


class DecisionBasis(Contract):
    kind: Literal["decision.basis"] = "decision.basis"
    namespace: Id
    resource_id: Id
    belief_rev: Rev
    model_rev: Rev
    goal_rev: Rev
    control_epoch: Rev


class ActionProposal(Contract):
    kind: Literal["action.proposal"] = "action.proposal"
    proposal_id: Id
    command_id: Id
    namespace: Id
    resource_id: Id
    operation: Operation
    actor_principal_ref: Id
    decision_basis_ref: Hash
    belief_rev: Rev
    model_rev: Rev
    goal_rev: Rev
    control_epoch: Rev
    idempotency_key: Id
    expected_effect_ref: Id | None = None


class ControlEvent(Contract):
    kind: Literal["control.event"] = "control.event"
    event_id: Id
    issuer_principal_ref: Id
    issuer_seq: Rev
    nonce: Id
    scope_ref: Id
    expected_revision: Rev
    verb: Literal["PAUSE_DISPATCH", "RESUME", "REVOKE", "GRANT", "REDIRECT",
                  "REVIEW_RESOLUTION", "HOLD"]
    operation: Operation | None = None
    artifact: Artifact | None = None
    appeal_id: Id | None = None
    resolution: Literal["UPHOLD", "LIFT", "NARROW", "DENY"] | None = None
    auth_evidence_ref: Id

    @model_validator(mode="after")
    def verb_fields(self):
        if (self.verb in ("REVOKE", "GRANT")) != (self.operation is not None):
            raise ValueError("operation is required only for permission changes")
        if (self.verb == "REDIRECT") != (self.artifact is not None):
            raise ValueError("artifact is required only for redirect")
        review = self.verb == "REVIEW_RESOLUTION"
        if review != (self.appeal_id is not None) or review != (self.resolution is not None):
            raise ValueError("review fields are required only for resolution")
        return self


class SignatureEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    key_id: Id
    payload_b64: str = Field(max_length=24000)
    signature_b64: str = Field(max_length=256)


def validate_output(symbol: str, output: str):
    if symbol == "INSPECT":
        parts = output.split(":")
        if len(parts) != 3 or parts[0] != "INSPECT" or parts[1] not in ("BASE", "A", "B") or parts[2] not in ("HEALTHY", "UNHEALTHY"):
            raise ValueError("INVALID_INSPECT_OUTPUT")
    elif output not in DOMAIN_CODES:
        raise ValueError("UNREGISTERED_FINITE_OUTPUT")


class ObservationRecord(Contract):
    kind: Literal["observation.record"] = "observation.record"
    observation_id: Id
    namespace: Id
    resource_id: Id
    source_principal_ref: Id
    causal_command_id: Id | None
    seq: Rev
    logical_tick: Rev
    raw_hash: Hash
    normalizer_rev: Id
    input_symbol: Symbol
    domain_output: Id
    note_ref: Id | None = None
    loss_flags: list[Id] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def valid_output(self):
        validate_output(self.input_symbol, self.domain_output)
        return self


class PredictionRecord(Contract):
    kind: Literal["prediction.record"] = "prediction.record"
    prediction_id: Id
    namespace: Id
    belief_rev: Rev
    model_rev: Rev
    commit_seq: Rev
    input_symbol: Symbol
    status: Literal["PREDICTED", "PREDICTED_SET", "UNPREDICTED"]
    expected_outputs: list[Id] = Field(max_length=128)
    assumption_refs: list[Id] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def coverage(self):
        size = len(self.expected_outputs)
        if (self.status == "UNPREDICTED" and size != 0) or (self.status == "PREDICTED" and size != 1) or (self.status == "PREDICTED_SET" and size < 2):
            raise ValueError("INVALID_COVERAGE")
        for output in self.expected_outputs:
            validate_output(self.input_symbol, output)
        return self


class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    from_state: StateId
    input: Symbol
    to_state: StateId
    output: Id

    @model_validator(mode="after")
    def valid_output(self):
        validate_output(self.input, self.output)
        return self


class ModelArtifact(Contract):
    kind: Literal["model.artifact"] = "model.artifact"
    model_id: Id
    namespace: Id
    revision: Rev
    initial_state: StateId
    states: list[StateId] = Field(min_length=1, max_length=64)
    alphabet: list[Symbol] = Field(min_length=8, max_length=8)
    transitions: list[Transition] = Field(min_length=8, max_length=512)
    training_trace_refs: list[Id] = Field(default_factory=list, max_length=4096)
    protected_dependencies: ProtectedDependencies
    evidence_status: Literal["DRAFT", "CONSISTENT_WITH_TESTED_TRACES", "QUARANTINED"] = "DRAFT"

    @model_validator(mode="after")
    def total_graph(self):
        states = set(self.states)
        if len(states) != len(self.states) or self.initial_state not in states:
            raise ValueError("INVALID_STATES")
        if set(self.alphabet) != set(ALPHABET):
            raise ValueError("ALPHABET_CHANGE")
        edges = {(t.from_state, t.input) for t in self.transitions}
        if len(edges) != len(self.transitions) or edges != {(q, a) for q in states for a in ALPHABET}:
            raise ValueError("MODEL_NOT_TOTAL_DETERMINISTIC")
        if any(t.to_state not in states for t in self.transitions):
            raise ValueError("UNKNOWN_TARGET_STATE")
        return self


class Branch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    domain_output: Id | None
    tick_output: Id
    next_node: StateId


class ProcedureNode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    node_id: StateId
    node_type: Literal["ACT", "WAIT", "SUCCESS", "ABORT", "ASK"]
    operation: Operation | None = None
    branches: list[Branch] = Field(default_factory=list, max_length=128)
    default_next: StateId | None = None

    @model_validator(mode="after")
    def instruction(self):
        if (self.node_type == "ACT") != (self.operation is not None):
            raise ValueError("INVALID_INSTRUCTION")
        if self.node_type in ("SUCCESS", "ABORT", "ASK") and (self.branches or self.default_next):
            raise ValueError("LEAF_HAS_EDGES")
        for branch in self.branches:
            if self.node_type == "ACT":
                validate_output(self.operation, branch.domain_output or "")
            elif branch.domain_output is not None:
                raise ValueError("WAIT_HAS_DOMAIN_OUTPUT")
            validate_output("TICK", branch.tick_output)
        if len({(b.domain_output, b.tick_output) for b in self.branches}) != len(self.branches):
            raise ValueError("DUPLICATE_BRANCH")
        return self


class ProcedureArtifact(Contract):
    kind: Literal["procedure.artifact"] = "procedure.artifact"
    procedure_id: Id
    namespace: Id
    revision: Rev
    model_hash: Hash
    resource_binding: Literal["task_resource"] = "task_resource"
    goal_artifact: Artifact
    goal_revision: Rev = 0
    minimum_tick_gap: Annotated[int, Field(ge=2)] = 2
    clock_contract: Literal["live_command_then_tick/v1"] = "live_command_then_tick/v1"
    entry_node: StateId
    max_steps: Annotated[int, Field(ge=1, le=32)]
    nodes: list[ProcedureNode] = Field(min_length=1, max_length=128)
    evidence_refs: list[Id] = Field(default_factory=list, max_length=4096)
    protected_dependencies: ProtectedDependencies

    @model_validator(mode="after")
    def graph(self):
        nodes = {n.node_id: n for n in self.nodes}
        if len(nodes) != len(self.nodes) or self.entry_node not in nodes:
            raise ValueError("INVALID_NODE_IDS")
        for node in self.nodes:
            if node.node_type in ("ACT", "WAIT"):
                if node.default_next not in nodes or nodes[node.default_next].node_type != "ABORT":
                    raise ValueError("DEFAULT_MUST_ABORT")
            if any(b.next_node not in nodes for b in node.branches):
                raise ValueError("DANGLING_NODE")
        return self


class AdmissionReport(Contract):
    kind: Literal["admission.report"] = "admission.report"
    report_id: Id
    candidate_hash: Hash
    namespace: Id
    gate_version: Id
    training_manifest_hash: Hash
    probe_manifest_hash: Hash
    candidate_locked_seq: Rev
    probe_started_seq: Rev
    real_probe_steps: Rev
    probe_resets: Rev
    counterexample_refs: list[Id] = Field(default_factory=list, max_length=4096)
    status: Literal["PASS", "FAIL", "INTERRUPTED", "ASSUMPTION_VIOLATION", "BUDGET_EXHAUSTED"]
    protected_dependencies_unchanged: bool


class Task(Contract):
    task_id: Id = "task"
    resource_id: Id = "R"
    artifact: Artifact = "A"
    health: Literal["HEALTHY"] = "HEALTHY"
    revision: Rev = 0
    readings_required: Literal[2] = 2
    minimum_tick_gap: Annotated[int, Field(ge=2)] = 2


class BeliefSnapshot(Contract):
    revision: Rev = 0
    model_rev: Rev = 0
    possible_model_states: list[StateId] = Field(default_factory=list)
    observation_refs: list[Id] = Field(default_factory=list)
    facts: list[dict] = Field(default_factory=list)
    flags: list[Id] = Field(default_factory=list)
    complete_history: bool = True


def protected_dependencies() -> ProtectedDependencies:
    return ProtectedDependencies(
        alphabet_hash=digest(ALPHABET),
        normalizer_hash=digest({"revision": "finite-domain/v1", "codes": sorted(DOMAIN_CODES)}),
        operation_registry_hash=digest({a: "read" if a in READS else "mutate" for a in OPERATIONS}),
        governance_interface_hash=digest(ControlEvent.model_json_schema()),
        task_schema_hash=digest(Task.model_json_schema()),
    )
