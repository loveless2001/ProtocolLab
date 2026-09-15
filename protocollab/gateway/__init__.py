"""Final dispatch fence, durable intent ledger, and conservative reconciliation."""

from __future__ import annotations

import json
import time

from protocollab.contracts import MUTATIONS, ActionProposal, DecisionBasis, canonical, digest, uid
from protocollab.modeling import prediction
from protocollab.storage import RecoveryRequired


class InjectedCrash(BaseException):
    """Test-only process-crash boundary. Never interpreted as a domain output."""


class ActionBroker:
    def __init__(self, store, governance, backend, capture, belief, model_provider):
        self.store, self.governance, self.backend = store, governance, backend
        self.capture, self.belief, self.model_provider = capture, belief, model_provider
        self.dispatch_timeout_ms = 1000

    def _row(self, command_id):
        return self.store.db.execute("SELECT * FROM actions WHERE command_id=?", (command_id,)).fetchone()

    def _status(self, command_id, status, reason=None, receipt=None):
        with self.store.transaction():
            self.store.db.execute("UPDATE actions SET status=?,receipt=COALESCE(?,receipt) WHERE command_id=?",
                                  (status, canonical(receipt).decode() if receipt else None, command_id))
            self.store.append("gateway", f"action.{status.lower()}", {"command_id": command_id, "reason": reason})
        return {"status": status, "command_id": command_id, "reason": reason, "receipt": receipt}

    def _authorize(self, proposal):
        state, belief, model = self.governance.snapshot, self.belief.snapshot, self.model_provider()
        try:
            basis = DecisionBasis.model_validate(self.store.blob(proposal.decision_basis_ref))
        except (KeyError, ValueError, RecoveryRequired):
            return "INVALID_DECISION_BASIS"
        if any(getattr(proposal, field) != getattr(basis, field) for field in
               ("namespace", "resource_id", "belief_rev", "model_rev", "goal_rev", "control_epoch")):
            return "DECISION_BASIS_MISMATCH"
        if proposal.namespace != self.store.namespace or proposal.resource_id != state["task"]["resource_id"]:
            return "RESOURCE_OUT_OF_SCOPE"
        if proposal.operation in MUTATIONS and self.store.db.execute(
                "SELECT 1 FROM actions WHERE scope=? AND status IN ('DISPATCHED','DISPATCH_UNCERTAIN') LIMIT 1",
                (proposal.resource_id,)).fetchone():
            return "RECONCILIATION_REQUIRED"
        if proposal.operation in MUTATIONS and self.store.db.execute(
                "SELECT 1 FROM clock_actions WHERE status IN ('DISPATCHED','DISPATCH_UNCERTAIN') LIMIT 1").fetchone():
            return "RECONCILIATION_REQUIRED"
        if (proposal.goal_rev != state["task"]["revision"] or proposal.belief_rev != belief.revision
                or proposal.model_rev != (model.artifact.revision if model else 0)):
            return "STALE"
        return self.governance.authorize(proposal.resource_id, proposal.operation, proposal.control_epoch)

    def propose(self, payload, principal="actor"):
        with self.store.transaction():
            self.store.append("actor", "action.proposed", payload.model_dump() if isinstance(payload, ActionProposal) else payload, principal)
            try:
                proposal = ActionProposal.model_validate(payload)
            except Exception:
                self.store.append("broker", "action.denied", {"reason": "INVALID_SCHEMA"}, principal)
                return {"status": "DENIED", "reason": "INVALID_SCHEMA"}
            if proposal.actor_principal_ref != principal:
                return {"status": "DENIED", "reason": "TRANSPORT_IDENTITY_MISMATCH"}
            payload_hash = digest(proposal)
            existing = self.store.db.execute(
                "SELECT * FROM actions WHERE command_id=? OR proposal_id=? OR (namespace=? AND scope=? AND idempotency_key=?)",
                (proposal.command_id, proposal.proposal_id, proposal.namespace, proposal.resource_id, proposal.idempotency_key),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    self.store.append("broker", "action.denied", {"reason": "IDEMPOTENCY_PAYLOAD_MISMATCH"})
                    return {"status": "DENIED", "reason": "IDEMPOTENCY_PAYLOAD_MISMATCH"}
                return {"status": existing["status"], "command_id": existing["command_id"],
                        "receipt": json.loads(existing["receipt"]) if existing["receipt"] else None}
            self.store.db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?)", (
                proposal.command_id, proposal.proposal_id, proposal.namespace, proposal.resource_id,
                proposal.idempotency_key, payload_hash, canonical(proposal).decode(), "PROPOSED", None, None,
            ))
            reason = self._authorize(proposal)
            if reason:
                return self._status(proposal.command_id, "STALE" if reason == "STALE" else "DENIED", reason)
            self._status(proposal.command_id, "VALIDATED")
            return self._status(proposal.command_id, "READY")

    def dispatch(self, command_id, crash_at=None):
        # No actor callback inside the boundary. Control commit uses the same owner lock.
        with self.store.lock:
            row = self._row(command_id)
            if row is None:
                raise KeyError(command_id)
            if row["status"] != "READY":
                return {"status": row["status"], "receipt": json.loads(row["receipt"]) if row["receipt"] else None}
            proposal = ActionProposal.model_validate(json.loads(row["proposal"]))
            reason = self._authorize(proposal)
            if reason:
                return self._status(command_id, "STALE" if reason == "STALE" else "CANCELLED_BEFORE_DISPATCH", reason)
            model = self.model_provider()
            with self.store.transaction():
                predicted = prediction(self.store, self.belief.snapshot, model, proposal.operation)
                self.governance.authorize(proposal.resource_id, proposal.operation, proposal.control_epoch, charge=True)
                event = self.store.append("gateway", "action.dispatched", {
                    "command_id": command_id, "resource_id": proposal.resource_id,
                    "operation": proposal.operation, "epoch": proposal.control_epoch,
                    "goal_rev": proposal.goal_rev, "model_rev": proposal.model_rev,
                    "belief_rev": proposal.belief_rev, "decision_basis_ref": proposal.decision_basis_ref,
                    "prediction_id": predicted.prediction_id,
                })
                self.store.db.execute("UPDATE actions SET status='DISPATCHED',dispatch_seq=? WHERE command_id=?",
                                      (event["seq"], command_id))
            if crash_at == "before_apply":
                raise InjectedCrash("before_apply")
            started = time.monotonic()
            try:
                packet = self.backend.apply(proposal.resource_id, proposal.operation, command_id)
                if crash_at == "after_apply":
                    raise InjectedCrash("after_apply")
            except Exception as exc:
                return self._status(command_id, "DISPATCH_UNCERTAIN", type(exc).__name__)
            elapsed_ms = (time.monotonic() - started) * 1000
            self.store.append("gateway", "gateway.latency", {"command_id": command_id, "elapsed_us": int(elapsed_ms * 1000)})
            result = self._ack(proposal, packet, model)
            if elapsed_ms > self.dispatch_timeout_ms:
                self.governance.monitor_health(False)
                self.store.append("gateway", "dispatch.deadline_exceeded", {"command_id": command_id})
            return result

    def _ack(self, proposal, packet, model):
        with self.store.transaction():
            observation = self.capture.receive(proposal.operation, packet, proposal.resource_id, proposal.command_id)
            self.belief.update(observation, model)
            result = self._status(proposal.command_id, "ACKNOWLEDGED", receipt=observation.model_dump())
            if proposal.operation == "INSPECT":
                result = self._status(proposal.command_id, "OUTCOME_OBSERVED", receipt=observation.model_dump())
            return result

    def propose_and_dispatch(self, proposal, principal="actor", before_dispatch=None):
        ready = self.propose(proposal, principal)
        if ready["status"] == "READY":
            if before_dispatch:
                before_dispatch()
            return self.dispatch(ready["command_id"])
        return ready

    def tick(self, crash_at=None):
        with self.store.lock:
            resource, command_id = self.governance.task.resource_id, uid("clock")
            model = self.model_provider()
            with self.store.transaction():
                record = prediction(self.store, self.belief.snapshot, model, "TICK")
                event = self.store.append("scheduler", "clock.dispatched", {"command_id": command_id, "prediction_id": record.prediction_id})
                self.store.db.execute("INSERT INTO clock_actions VALUES(?,?,?,?,?)",
                                      (command_id, resource, event["seq"], "DISPATCHED", None))
            if crash_at == "before_apply":
                raise InjectedCrash("clock_before_apply")
            try:
                packet = self.backend.apply(resource, "TICK", command_id)
            except Exception:
                self.store.db.execute("UPDATE clock_actions SET status='DISPATCH_UNCERTAIN' WHERE command_id=?", (command_id,))
                self.governance.monitor_health(False)
                raise
            if crash_at == "after_apply":
                raise InjectedCrash("clock_after_apply")
            with self.store.transaction():
                observation = self.capture.receive("TICK", packet, resource, command_id)
                self.belief.update(observation, model)
                self.store.db.execute("UPDATE clock_actions SET status='ACKNOWLEDGED',receipt=? WHERE command_id=?",
                                      (canonical(observation).decode(), command_id))
            return observation

    def reconcile_clock(self, command_id):
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM clock_actions WHERE command_id=?", (command_id,)).fetchone()
            if row is None:
                raise KeyError(command_id)
            if row["receipt"]:
                return json.loads(row["receipt"])
            packet = self.backend.reconcile(command_id)
            if packet is None:
                self.store.db.execute("UPDATE clock_actions SET status='FAILED' WHERE command_id=?", (command_id,))
                return None
            with self.store.transaction():
                observation = self.capture.receive("TICK", packet, row["resource"], command_id)
                self.belief.update(observation, self.model_provider())
                self.store.db.execute("UPDATE clock_actions SET status='ACKNOWLEDGED',receipt=? WHERE command_id=?",
                                      (canonical(observation).decode(), command_id))
            return observation.model_dump()

    def reconcile(self, command_id):
        with self.store.lock:
            row = self._row(command_id)
            if row is None:
                raise KeyError(command_id)
            if row["receipt"]:
                return {"status": row["status"], "receipt": json.loads(row["receipt"])}
            if row["status"] not in ("DISPATCHED", "DISPATCH_UNCERTAIN"):
                return {"status": row["status"]}
            packet = self.backend.reconcile(command_id)
            if packet is None:
                # Supplied simulator supports authoritative command lookup. Do not invent an effect.
                return self._status(command_id, "FAILED", "AUTHORITATIVELY_NOT_APPLIED")
            proposal = ActionProposal.model_validate(json.loads(row["proposal"]))
            return self._ack(proposal, packet, self.model_provider())
