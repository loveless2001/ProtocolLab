"""Authenticated, delegated control and conservative primitive authorization."""

from __future__ import annotations

import base64
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from protocollab.contracts import (
    ALPHABET,
    MUTATIONS,
    OPERATIONS,
    ControlEvent,
    SignatureEnvelope,
    Task,
    canonical,
    strict_json,
)

DOMAIN_SEPARATOR = b"ProtocolLab/control/v0.1\x00"


@dataclass(frozen=True)
class Delegation:
    principal: str
    public_key: bytes
    verbs: frozenset[str]
    scopes: frozenset[str]
    revoked: bool = False


def sign_control(event: ControlEvent, key: Ed25519PrivateKey, key_id: str):
    payload = canonical(event)
    return SignatureEnvelope(key_id=key_id, payload_b64=base64.b64encode(payload).decode(),
                             signature_b64=base64.b64encode(key.sign(DOMAIN_SEPARATOR + payload)).decode())


class Governance:
    def __init__(self, store, keys: dict[str, Delegation], task=None, resources=None,
                 mutation_limit=10000):
        self.store, self.keys = store, dict(keys)
        self.available = True
        self.mutation_limit = mutation_limit
        self.review_resolver = None
        if self.store.get("governance", "control") is None:
            task = task or Task()
            resources = resources or [task.resource_id, "replica"]
            self.store.set("governance", "control", {
                "revision": 0, "epoch": 0, "authority_rev": 0,
                "task": task.model_dump(), "statuses": {"agent_all": "RUNNING", **{r: "RUNNING" for r in resources}},
                "permissions": {r: list(OPERATIONS) for r in resources},
                "replica_admin": [r for r in resources if r != task.resource_id],
                "mutation_counts": {r: 0 for r in resources},
                "monitor_healthy": True,
            }, "governance.initialized", "root_manifest")

    @property
    def snapshot(self):
        return self.store.get("governance", "control")

    @property
    def task(self):
        return Task.model_validate(self.snapshot["task"])

    def submit_authenticated(self, envelope: SignatureEnvelope | dict):
        try:
            envelope = SignatureEnvelope.model_validate(envelope)
            delegation = self.keys[envelope.key_id]
            if delegation.revoked:
                raise PermissionError("KEY_REVOKED")
            payload = base64.b64decode(envelope.payload_b64, validate=True)
            signature = base64.b64decode(envelope.signature_b64, validate=True)
            Ed25519PublicKey.from_public_bytes(delegation.public_key).verify(signature, DOMAIN_SEPARATOR + payload)
            event = ControlEvent.model_validate(strict_json(payload))
            if event.issuer_principal_ref != delegation.principal:
                raise PermissionError("PRINCIPAL_KEY_MISMATCH")
            if event.verb not in delegation.verbs or event.scope_ref not in delegation.scopes:
                raise PermissionError("DELEGATION_DENIED")
            return self._commit_verified(event, envelope, delegation)
        except Exception as exc:
            self.store.append("governance", "control.rejected", {"reason": str(exc) or type(exc).__name__})
            return {"status": "REJECTED", "reason": str(exc) or type(exc).__name__}

    def _commit_verified(self, event, envelope, delegation):
        with self.store.transaction():
            state = self.snapshot
            if not self.available:
                raise PermissionError("GOVERNANCE_UNAVAILABLE")
            if event.expected_revision != state["revision"]:
                raise PermissionError("STALE_CONTROL_REVISION")
            if event.scope_ref not in state["statuses"]:
                raise PermissionError("UNKNOWN_SCOPE")
            last = self.store.db.execute("SELECT MAX(issuer_seq) FROM replay_guard WHERE principal=?",
                                         (delegation.principal,)).fetchone()[0]
            if last is not None and event.issuer_seq <= last:
                raise PermissionError("CONTROL_REPLAY")
            self.store.db.execute("INSERT INTO replay_guard VALUES(?,?,?,?)",
                                  (delegation.principal, event.issuer_seq, event.nonce, event.event_id))
            scope, verb = event.scope_ref, event.verb
            if verb in ("PAUSE_DISPATCH", "RESUME", "HOLD"):
                state["statuses"][scope] = {"PAUSE_DISPATCH": "PAUSED", "RESUME": "RUNNING", "HOLD": "HOLD"}[verb]
            elif verb in ("GRANT", "REVOKE"):
                if scope not in state["permissions"]:
                    raise PermissionError("EXACT_RESOURCE_REQUIRED")
                permissions = set(state["permissions"][scope])
                if verb == "GRANT":
                    permissions.add(event.operation)
                else:
                    permissions.discard(event.operation)
                state["permissions"][scope] = sorted(permissions)
                state["authority_rev"] += 1
            elif verb == "REDIRECT":
                if scope != state["task"]["resource_id"]:
                    raise PermissionError("TASK_SCOPE_MISMATCH")
                state["task"]["artifact"] = event.artifact
                state["task"]["revision"] += 1
            elif verb == "REVIEW_RESOLUTION":
                if not self.review_resolver:
                    raise PermissionError("UNKNOWN_APPEAL")
                self.review_resolver(event, state, delegation)
            state["epoch"] += 1
            state["revision"] += 1
            evidence = self.store.put_blob(envelope.model_dump())
            self.store.append("control", "control.accepted", {
                "event": event.model_dump(), "signature_envelope_hash": evidence,
                "epoch": state["epoch"], "status": state["statuses"][scope],
            }, delegation.principal)
            saved = self.store.set("governance", "control", state, "control.committed", delegation.principal)
            return {"status": "ACCEPTED", "epoch": state["epoch"], "revision": state["revision"],
                    "fence_seq": saved["seq"], "event_id": event.event_id}

    def is_paused(self, resource):
        state = self.snapshot
        return any(state["statuses"].get(scope, "RECOVERY_REQUIRED") != "RUNNING"
                   for scope in ("agent_all", resource))

    def authorize(self, resource, operation, epoch, *, query=False, charge=False):
        state = self.snapshot
        if resource not in state["permissions"]:
            return "RESOURCE_OUT_OF_SCOPE"
        if operation not in ALPHABET and operation not in ("CREATE_REPLICA", "RESET_REPLICA"):
            return "UNKNOWN_OPERATION"
        if epoch != state["epoch"]:
            return "STALE"
        if operation == "TICK" and not query:
            return "HARNESS_ONLY_CLOCK"
        mutation = operation in MUTATIONS or operation in ("CREATE_REPLICA", "RESET_REPLICA")
        if (mutation or query) and not self.available:
            return "GOVERNANCE_UNAVAILABLE"
        if (mutation or query) and self.is_paused(resource):
            return "PAUSED"
        if mutation and not state["monitor_healthy"]:
            return "MONITOR_UNAVAILABLE"
        if query and resource not in state["replica_admin"]:
            return "REPLICA_ADMIN_REQUIRED"
        if operation in OPERATIONS and operation not in state["permissions"][resource]:
            return "PERMISSION_DENIED"
        if mutation and state["mutation_counts"][resource] >= self.mutation_limit:
            return "TRAJECTORY_LIMIT"
        if charge and mutation:
            state["mutation_counts"][resource] += 1
            self.store.set("governance", "control", state, "trajectory.charged")
        return None

    def monitor_health(self, healthy):
        state = self.snapshot
        state["monitor_healthy"] = healthy
        self.store.set("governance", "control", state, "monitor.health")

    def recovery_required(self, reason):
        state = self.snapshot
        state["statuses"]["agent_all"] = "RECOVERY_REQUIRED"
        state["epoch"] += 1
        self.store.set("governance", "control", state, "recovery.required")
        self.store.append("governance", "recovery.reason", {"reason": reason})

    def reject_protected_update(self, kind, payload, principal="actor"):
        self.store.append("governance", "protected_update.rejected", {
            "kind": kind, "payload": payload, "reason": "AUTHORIZED_TYPED_CHANNEL_REQUIRED"}, principal)
        return {"status": "REJECTED", "reason": "AUTHORIZED_TYPED_CHANNEL_REQUIRED"}
