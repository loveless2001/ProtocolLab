"""Trusted sequencer; untrusted actors interact with a restricted request surface."""

from __future__ import annotations

from pathlib import Path

from protocollab.belief import BeliefService
from protocollab.capture import Capture
from protocollab.contracts import ActionProposal, DecisionBasis, Task, digest, uid
from protocollab.gateway import ActionBroker
from protocollab.governance import Governance
from protocollab.learning import LearningLimits, QueryAdapter, learn
from protocollab.modeling import simulate
from protocollab.modeling.admission import ModelRegistry
from protocollab.planning import plan
from protocollab.procedures import ProcedureRegistry, ProcedureRunner
from protocollab.storage import Store


class Runtime:
    def __init__(self, directory, backend, keys, namespace="run", task=None, limits=None):
        import fcntl
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lease = (self.directory / "runtime.lock").open("a+")
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lease.close()
            raise RuntimeError("OWNER_PROCESS_ALREADY_RUNNING") from None
        self.store = Store(self.directory / "owner.sqlite", namespace)
        self.backend = backend
        task = task or Task()
        self.governance = Governance(self.store, keys, task)
        self.capture = Capture(self.store)
        self.belief = BeliefService(self.store, self.governance.task.resource_id)
        self.models = ModelRegistry(self.store, self.governance, self.belief, self.capture)
        self.procedures = ProcedureRegistry(self.store, self.governance, self.models)
        self.broker = ActionBroker(self.store, self.governance, backend, self.capture, self.belief, self.models.current_snapshot)
        self.limits = limits or LearningLimits()
        self.adapter = None
        self.max_turns = 80
        self.monitor = None
        self.planner = plan
        self.budget_check = None
        self.hooks = {}
        self.control_inbox = self.directory / "control-inbox"
        self.control_inbox.mkdir(exist_ok=True)
        from protocollab.review import ReviewService
        self.review = ReviewService(self.store, self.governance)
        if self.store.get("runtime", "initialized") is None:
            packet = backend.create(task.resource_id, False, "create-live-" + namespace)
            self.store.set("runtime", "initialized", {"receipt": packet, "live_turns": 0})

    def query_adapter(self):
        if self.adapter is None:
            self.adapter = QueryAdapter(self.store, self.governance, self.backend, self.capture, limits=self.limits)
            self.adapter.monitor_check = self.sync_monitor
            self.adapter.budget_check = self.budget_check
            self.adapter.frozen = self.store.get("runtime", "phase") == "F_SUFFIX"
        return self.adapter

    def current_decision_basis(self):
        control, belief, model = self.governance.snapshot, self.belief.snapshot, self.models.current_snapshot()
        return DecisionBasis(namespace=self.store.namespace, resource_id=control["task"]["resource_id"],
            belief_rev=belief.revision, model_rev=model.artifact.revision if model else 0,
            goal_rev=control["task"]["revision"], control_epoch=control["epoch"])

    def bind_decision(self):
        with self.store.lock:
            return self.store.put_blob(self.current_decision_basis())

    def decision_is_current(self, decision_basis_ref):
        basis = DecisionBasis.model_validate(self.store.blob(decision_basis_ref))
        return basis == self.current_decision_basis()

    def proposal(self, operation, principal="actor", decision_basis_ref=None):
        decision_basis_ref = decision_basis_ref or self.bind_decision()
        basis = DecisionBasis.model_validate(self.store.blob(decision_basis_ref))
        command_id = uid("command")
        return ActionProposal(proposal_id=uid("proposal"), command_id=command_id,
            **basis.model_dump(exclude={"schema_version", "kind"}), operation=operation,
            actor_principal_ref=principal, decision_basis_ref=decision_basis_ref,
            idempotency_key=command_id)

    def turn(self, command="WAIT", before_dispatch=None, decision_basis_ref=None):
        # Bind before any pending controls are processed, never after selection.
        decision_basis_ref = decision_basis_ref or self.bind_decision()
        self.process_controls()
        if self.budget_check:
            self.budget_check()
        self.sync_monitor()
        initialized = self.store.get("runtime", "initialized")
        if initialized["live_turns"] >= self.max_turns:
            raise RuntimeError("LIVE_TURN_BUDGET_EXHAUSTED")
        result = None
        if command != "WAIT":
            def dispatch_boundary():
                hook = before_dispatch or self.hooks.get("after_validate")
                if hook:
                    hook()
                self.process_controls()
            result = self.broker.propose_and_dispatch(
                self.proposal(command, decision_basis_ref=decision_basis_ref),
                before_dispatch=dispatch_boundary)
        tick = self.broker.tick()  # also advances during pause, denial or actor timeout
        initialized["live_turns"] += 1
        self.store.set("runtime", "initialized", initialized, "turn.completed")
        self.store.set("runtime", "latest_turn", {"action": result, "tick": tick.model_dump(),
                                                   "decision_basis_ref": decision_basis_ref})
        if result is not None:
            self.store.set("runtime", "latest_action", result)
        self.review.expire(tick.logical_tick)
        self.sync_monitor()
        return {"action": result, "tick": tick}

    def sync_monitor(self):
        self.process_controls()
        if self.monitor is None:
            return
        try:
            hold = False
            while True:
                events = self.store.events(after=self.monitor.anchor[0], limit=256)
                if not events:
                    break
                response = self.monitor.consume(events)
                hold = hold or response["hold"]
                for alert in response["new_alerts"]:
                    self.store.append("monitor", "monitor.alert", alert, "monitor")
            if hold:
                if self.governance.snapshot["monitor_healthy"]:
                    self.governance.monitor_health(False)
                # Emergency hold is limited to the delegated actor scope.
                state = self.governance.snapshot
                if state["statuses"]["agent_all"] != "HOLD":
                    state["statuses"]["agent_all"] = "HOLD"
                    state["epoch"] += 1
                    self.store.set("governance", "control", state, "monitor.emergency_hold", "monitor")
        except Exception:
            if self.governance.snapshot["monitor_healthy"]:
                self.governance.monitor_health(False)

    def process_controls(self):
        import json
        for path in sorted(self.control_inbox.glob("*.json")):
            try:
                envelope = json.loads(path.read_bytes())
                response = self.governance.submit_authenticated(envelope)
            except Exception as exc:
                response = {"status": "REJECTED", "reason": type(exc).__name__}
            response_path = path.with_suffix(".receipt")
            response_path.write_text(json.dumps(response, sort_keys=True))
            path.rename(path.with_suffix(".processed"))

    def learning_round(self, seed=0, learner=learn):
        adapter = self.query_adapter()
        previous = self.models.current_snapshot()
        epoch = self.governance.snapshot["epoch"]
        revision = previous.artifact.revision + 1 if previous else 1
        artifact, export = learner(adapter, self.store.namespace, revision, seed, self.limits)
        self.store.set("learning", "export", {"hash": self.store.put_blob(export)}, "learner.exported")
        report = self.models.validate(artifact, adapter)
        if report.status == "PASS":
            if self.hooks.get("before_promotion"):
                self.hooks["before_promotion"]()
            self.models.promote(artifact, report, epoch)
        return artifact, report, export

    def synthesize(self, admit=True):
        if self.hooks.get("before_plan"):
            self.hooks["before_plan"]()
        self.sync_monitor()
        decision_basis_ref = self.bind_decision()
        model = self.models.current_snapshot()
        if not model:
            return {"status": "NO_PLAN_WITHIN_BOUND", "reason": "NO_ACTIVE_MODEL"}
        task = self.governance.task
        result = self.planner(model, self.belief.snapshot, task, self.governance.snapshot["permissions"][task.resource_id])
        result["decision_basis_ref"] = decision_basis_ref
        self.sync_monitor()
        if not self.decision_is_current(decision_basis_ref):
            self.store.append("planner", "planner.stale", {"decision_basis_ref": decision_basis_ref})
            return {"status": "STALE", "decision_basis_ref": decision_basis_ref}
        if result.get("worker_usage"):
            self.store.append("planner", "planner.usage", result["worker_usage"])
        if result["status"] == "PLANNED":
            artifact_hash = self.store.put_blob(result["procedure"])
            self.store.append("planner", "planner.proposed", {"artifact_hash": artifact_hash,
                "registry_promotion_requested": admit, "kind": "TRANSIENT_PLAN_PROPOSAL"})
        if result["status"] == "PLANNED" and admit:
            self.procedures.admit(result["procedure"], self.belief.snapshot.possible_model_states, self.query_adapter())
        return result

    def run_procedure(self, procedure, before_step=None, decision_basis_ref=None):
        registered = self.store.get("procedure", digest(procedure))
        if not registered or registered["status"] != "ACTIVE" or registered["model_hash"] != self.models.current_snapshot().hash:
            raise PermissionError("ACTIVE_PROCEDURE_REQUIRED")
        if set(registered["start_states"]) != set(self.belief.snapshot.possible_model_states):
            raise ValueError("PROCEDURE_PRECONDITION_FAILED")
        if (procedure.goal_artifact != self.governance.task.artifact
                or procedure.goal_revision != self.governance.task.revision):
            raise ValueError("STALE_GOAL")
        runner = ProcedureRunner(self, procedure, decision_basis_ref=decision_basis_ref)
        for index in range(procedure.max_steps + 1):
            if before_step:
                before_step(index)
            outcome = runner.step()
            if outcome["status"] not in ("READY", "RUNNING"):
                return outcome
        return {"status": "FUEL_EXHAUSTED"}

    def simulate(self, symbols):
        model = self.models.current_snapshot()
        if not model:
            raise ValueError("NO_MODEL")
        # Branch storage is a separate artifact owner; no real-state/journal writes during simulation.
        return simulate(self.belief.snapshot, model, symbols)

    def finish(self, decision_basis_ref=None):
        decision_basis_ref = decision_basis_ref or self.bind_decision()
        self.sync_monitor()
        if not self.decision_is_current(decision_basis_ref):
            return {"status": "STALE", "decision_basis_ref": decision_basis_ref}
        last_redirect = 0
        for event in self.store.events():
            if event["kind"] == "control.accepted" and event["payload"]["event"]["verb"] == "REDIRECT":
                last_redirect = event["seq"]
        result = self.belief.completion(self.governance.task, last_redirect)
        self.store.append("completion", "completion.checked", {**result,
            "goal_rev": self.governance.task.revision, "decision_basis_ref": decision_basis_ref})
        return result

    def freeze(self):
        self.models.frozen = self.procedures.frozen = True
        if self.adapter:
            self.adapter.frozen = True
        self.store.set("runtime", "phase", "F_SUFFIX", "evaluation.frozen")

    def checkpoint(self, monitor_anchor=None):
        with self.store.transaction():
            self.store.verify()
            state = [{"owner": r["owner"], "key": r["key"], "revision": r["revision"],
                      "payload_hash": digest(r["payload"].encode())}
                     for r in self.store.db.execute("SELECT * FROM state ORDER BY owner,key")]
            payload = {"schema_version": "0.1", "journal_anchor": list(self.store.tail),
                       "monitor_anchor": monitor_anchor, "state": state,
                       "model": self.store.get("model", "active"),
                       "procedure": self.store.get("procedure", "active"),
                       "belief": self.belief.snapshot.model_dump(), "control": self.governance.snapshot,
                       "pending_commands": [r[0] for r in self.store.db.execute("SELECT command_id FROM actions WHERE status IN ('DISPATCHED','DISPATCH_UNCERTAIN','READY')")],
                       "pending_clock_commands": [r[0] for r in self.store.db.execute("SELECT command_id FROM clock_actions WHERE status IN ('DISPATCHED','DISPATCH_UNCERTAIN')")],
                       "query_trace_refs": [r[0] for r in self.store.db.execute("SELECT trace_id FROM query_traces")],
                       "learner_export": self.store.get("learning", "export")}
            key = self.store.put_blob(payload)
            self.store.set("checkpoint", "latest", {"hash": key}, "checkpoint.committed")
            return key

    def recover(self, checkpoint_hash):
        try:
            checkpoint = self.store.blob(checkpoint_hash)
            self.store.verify(checkpoint["journal_anchor"])
            if checkpoint["monitor_anchor"]:
                self.store.verify(checkpoint["monitor_anchor"])
            for row in self.store.db.execute("SELECT hash FROM blobs"):
                self.store.blob(row[0], raw=True)
            # Reconcile every durable command, including those dispatched after the checkpoint.
            pending = self.store.db.execute("SELECT command_id,dispatch_seq,'action' AS kind FROM actions WHERE status IN ('DISPATCHED','DISPATCH_UNCERTAIN') UNION ALL SELECT command_id,dispatch_seq,'clock' AS kind FROM clock_actions WHERE status IN ('DISPATCHED','DISPATCH_UNCERTAIN') ORDER BY dispatch_seq").fetchall()
            for row in pending:
                if row["kind"] == "action":
                    self.broker.reconcile(row["command_id"])
                else:
                    self.broker.reconcile_clock(row["command_id"])
            model = self.models.current_snapshot()
            if model:
                observations = [self.capture.read_observation(ref) for ref in self.belief.snapshot.observation_refs]
                self.belief.rebase(model, observations, self.belief.snapshot.complete_history)
            state = self.governance.snapshot
            state["epoch"] += 1
            self.store.set("governance", "control", state, "recovery.revalidated")
            self.store.db.execute("UPDATE actions SET status='STALE' WHERE status='READY'")
            initialized = self.store.get("runtime", "initialized")
            initialized["live_turns"] = self.store.db.execute("SELECT COUNT(*) FROM clock_actions WHERE status='ACKNOWLEDGED'").fetchone()[0]
            self.store.set("runtime", "initialized", initialized, "recovery.clock_reconciled")
            self.models.frozen = self.procedures.frozen = self.store.get("runtime", "phase") == "F_SUFFIX"
            self.store.append("runtime", "recovery.completed", {"checkpoint_hash": checkpoint_hash})
            if self.hooks.get("after_restart"):
                self.hooks["after_restart"]()
            return "RECOVERED"
        except Exception as exc:
            self.governance.recovery_required(str(exc))
            return "RECOVERY_REQUIRED"

    def close(self):
        self.store.close()
        self._lease.close()
