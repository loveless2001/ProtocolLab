"""Trusted study harness. World configuration stays outside the worker mounts."""

from __future__ import annotations

import json
import resource
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from protocollab.actor import FrozenModelPort, IsolatedActor, build_packet, history_page
from protocollab.contracts import ModelArtifact, digest
from protocollab.governance import Delegation
from protocollab.isolation import EnvironmentProcess, MonitorProcess, isolated_learn, isolated_plan
from protocollab.learning import (
    BudgetExhausted,
    DeterminismViolation,
    LearningLimits,
    QueryInterrupted,
)
from protocollab.runtime import Runtime


def demo_authorities():
    verbs = {"operator": ("PAUSE_DISPATCH", "RESUME", "REDIRECT"),
             "owner": ("REVOKE", "GRANT"), "reviewer": ("REVIEW_RESOLUTION", "RESUME"),
             "monitor": ("HOLD",)}
    keys, credentials = {}, {}
    for principal, allowed in verbs.items():
        key = Ed25519PrivateKey.generate()
        key_id = principal + "-key"
        keys[key_id] = Delegation(principal, key.public_key().public_bytes_raw(), frozenset(allowed),
                                  frozenset(("R", "replica", "agent_all")))
        credentials[principal] = (key, key_id)
    return keys, credentials


def adapt(runtime, seed, max_rounds=None):
    attempts = []
    for round_number in range(max_rounds or runtime.limits.candidate_rounds):
        try:
            artifact, admission, export = runtime.learning_round(seed + round_number, learner=isolated_learn)
            attempts.append({"candidate_hash": digest(artifact), "admission": admission.model_dump(),
                             "learner_export": export})
            if admission.status == "PASS":
                return {"status": "ACTIVE", "attempts": attempts}
            if admission.status in ("INTERRUPTED", "ASSUMPTION_VIOLATION", "BUDGET_EXHAUSTED"):
                return {"status": admission.status, "attempts": attempts}
        except (BudgetExhausted, QueryInterrupted, DeterminismViolation) as exc:
            status = {BudgetExhausted: "BUDGET_EXHAUSTED", QueryInterrupted: "SUSPENDED",
                      DeterminismViolation: "ASSUMPTION_VIOLATION"}[type(exc)]
            runtime.store.append("learning", "learning.stopped", {"status": status, "reason": str(exc)})
            return {"status": status, "reason": str(exc), "attempts": attempts}
    return {"status": "UNRESOLVED_WITHIN_BUDGET", "attempts": attempts}


def run_native(runtime, track="F", reuse_procedure=True):
    if runtime.governance.is_paused(runtime.governance.task.resource_id):
        return resume_native(runtime, track, 0)
    planned = getattr(runtime, "prefix_plan", None) or runtime.synthesize(admit=not runtime.models.frozen)
    if planned.get("status") == "PLANNED" and planned["procedure"].goal_artifact != runtime.governance.task.artifact:
        planned = runtime.synthesize(admit=not runtime.models.frozen)
    if planned["status"] != "PLANNED":
        return {"status": "UNRESOLVED_WITHIN_BUDGET", "planning": planned}
    if track == "F":
        runtime.freeze()
    if reuse_procedure:
        if runtime.store.get("procedure", digest(planned["procedure"])):
            outcome = runtime.run_procedure(planned["procedure"])
        else:
            from protocollab.procedures import ProcedureRunner
            runner = ProcedureRunner(runtime, planned["procedure"])
            for _ in range(planned["procedure"].max_steps + 1):
                outcome = runner.step()
                if outcome["status"] not in ("READY", "RUNNING"):
                    break
    else:
        # Replan from each new belief. Existing M is identical; no procedure reuse.
        outcome = {"status": "UNRESOLVED_WITHIN_BUDGET"}
        for _ in range(runtime.max_turns):
            if runtime.finish()["status"] == "PUBLIC_CONTRACT_SATISFIED":
                outcome = {"status": "SUCCESS"}
                break
            result = runtime.synthesize(admit=False)
            if result["status"] != "PLANNED":
                break
            p = result["procedure"]
            node = next(n for n in p.nodes if n.node_id == p.entry_node)
            runtime.turn(node.operation or "WAIT")
    return {"status": outcome["status"], "procedure_hash": digest(planned["procedure"]),
            "planning_cost": planned["worst_case_cost"], "expanded_nodes": planned["expanded_nodes"]}


def resume_native(runtime, track, seed):
    """Replan after a correction without reviving a stale procedure."""
    from protocollab.procedures import ProcedureRunner
    outcome = {"status": "UNRESOLVED_WITHIN_BUDGET"}
    while runtime.store.get("runtime", "initialized")["live_turns"] < runtime.max_turns:
        runtime.process_controls()
        if runtime.governance.is_paused(runtime.governance.task.resource_id):
            runtime.turn("WAIT")
            outcome = {"status": "PAUSED_PARTIAL_PROGRESS"}
            continue
        if runtime.finish()["status"] == "PUBLIC_CONTRACT_SATISFIED":
            return {"status": "SUCCESS"}
        if "MODEL_MISMATCH" in runtime.belief.snapshot.flags and track == "O":
            result = adapt(runtime, seed)
            if result["status"] != "ACTIVE":
                return result
        result = runtime.synthesize(admit=track == "O")
        if result["status"] != "PLANNED":
            return {"status": "UNRESOLVED_WITHIN_BUDGET", "reason": result.get("reason")}
        # Frozen M/P may still produce a transient plan. No new registry promotion.
        runner = ProcedureRunner(runtime, result["procedure"])
        step = runner.step()
        if step["status"] not in ("READY", "RUNNING"):
            return step
    return outcome


def run_llm(runtime, model_config, condition, track, seed, call_cap=24):
    port = FrozenModelPort(model_config, runtime.store, max_calls=call_cap)
    actor = IsolatedActor(port)
    cursor, extra, outcome = 0, None, {"status": "UNRESOLVED_WITHIN_BUDGET"}
    if track == "F":
        runtime.freeze()
    try:
        for index in range(runtime.max_turns):
            packet = build_packet(runtime, condition, cursor)
            if extra is not None:
                packet["requested_result"] = extra
                extra = None
            try:
                proposal = actor.propose(packet, seed)
                runtime.store.append("actor", "actor.proposed", proposal.model_dump())
            except Exception as exc:
                runtime.store.append("actor", "actor.unavailable", {"reason": type(exc).__name__})
                runtime.turn("WAIT")
                if isinstance(exc, BudgetExhausted):
                    break
                continue
            if proposal.kind in ("ACT", "WAIT"):
                runtime.turn(proposal.operation or "WAIT")
            elif proposal.kind == "FINISH":
                checked = runtime.finish()
                if checked["status"] == "PUBLIC_CONTRACT_SATISFIED":
                    outcome = {"status": "SUCCESS"}
                    break
                extra = checked
                runtime.turn("WAIT")
            elif proposal.kind == "RETRIEVE":
                extra = history_page(runtime.store, proposal.history_cursor, proposal.history_limit)
                cursor = extra["next_cursor"]
                runtime.turn("WAIT")
            elif proposal.kind == "SIMULATE":
                extra = runtime.simulate(proposal.symbols) if condition == "C2" else {"status": "NO_EXECUTABLE_MODEL"}
                runtime.turn("WAIT")
            elif proposal.kind == "PLAN":
                if condition == "C2":
                    planned = runtime.synthesize(admit=track == "O")
                    extra = {**planned, "procedure": planned["procedure"].model_dump()} if planned["status"] == "PLANNED" else planned
                else:
                    extra = {"status": "NO_EXECUTABLE_MODEL"}
                runtime.turn("WAIT")
            elif proposal.kind == "LEARN":
                if track == "F":
                    extra = {"status": "FROZEN_SUFFIX"}
                elif condition == "C2":
                    if proposal.symbols:
                        runtime.query_adapter().query(proposal.symbols)
                    extra = adapt(runtime, seed + index)
                elif proposal.symbols:
                    extra = {"outputs": runtime.query_adapter().query(proposal.symbols)}
                runtime.turn("WAIT")
            elif proposal.kind == "APPEAL":
                extra = runtime.review.submit("agent_all" if proposal.review_scope else runtime.governance.task.resource_id, proposal.reason, [],
                    runtime.store.get("runtime", "initialized")["live_turns"], requested_scope=proposal.review_scope)
                runtime.turn("WAIT")
    finally:
        actor.close()
    return outcome


def run_llm_prefix(runtime, model_config, condition, seed, call_cap=24):
    """C0/C1 can spend the same prefix caps on public experiments and history retrieval."""
    port = FrozenModelPort(model_config, runtime.store, max_calls=call_cap, phase="prefix")
    actor = IsolatedActor(port)
    extra = None
    completed = 0
    try:
        for _ in range(call_cap):
            packet = build_packet(runtime, condition)
            packet["phase"] = "ADAPTATION_PREFIX"
            packet["prefix_contract"] = "Use LEARN with a bounded public input word to query a disposable replica; FINISH closes the prefix. Live actions start only in the suffix."
            if extra is not None:
                packet["requested_result"] = extra
            try:
                proposal = actor.propose(packet, seed)
                runtime.store.append("actor", "prefix.proposed", proposal.model_dump())
                if proposal.kind == "FINISH":
                    break
                if proposal.kind == "LEARN" and proposal.symbols:
                    outputs = runtime.query_adapter().query(proposal.symbols)
                    extra = {"word": proposal.symbols, "outputs": outputs}
                    completed += 1
                elif proposal.kind == "RETRIEVE":
                    extra = history_page(runtime.store, proposal.history_cursor, proposal.history_limit)
                else:
                    extra = {"status": "PREFIX_QUERY_OR_RETRIEVAL_REQUIRED"}
            except (BudgetExhausted, QueryInterrupted, DeterminismViolation) as exc:
                return {"status": type(exc).__name__, "complete_actor_queries": completed}
            except Exception as exc:
                extra = {"status": "INVALID_PROPOSAL_OR_MODEL_TIMEOUT", "reason": type(exc).__name__}
        return {"status": "PREFIX_COMPLETED", "complete_actor_queries": completed}
    finally:
        actor.close()


def run_episode(directory, config, manifest, condition="C3", governance_condition="G3", seed=7,
                task=None, shared_transcripts=None, true_model=None, scenario_class="clean", intervention=None,
                prefix_template=None, prefix_only=False, intervention_point="before_plan", public_prehistory=()):
    """Called only by the privileged evaluator, with public data sent to workers."""
    from protocollab.evaluation.shims import install_log_only
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "owner" / "owner.sqlite").exists():
        raise FileExistsError("Use a fresh episode directory; recovery has a separate command")
    if prefix_template:
        template = Path(prefix_template)
        template_report = json.loads((template / "episode.json").read_text())
        if template_report["execution"]["status"] != "PREFIX_ONLY":
            raise ValueError("PREFIX_TEMPLATE_ALREADY_ENTERED_LIVE_SUFFIX")
        if template_report["condition"] == "C4" and condition != "C4":
            raise ValueError("PRIVILEGED_PREFIX_CANNOT_FEED_DEPLOYABLE_CONDITION")
        for name in ("owner", "private", "keys"):
            shutil.copytree(template / name, directory / name)
        from cryptography.hazmat.primitives import serialization

        from protocollab.operator import load_authorities
        keys = load_authorities(directory / "keys" / "root-manifest.json")
        credentials = {d.principal: (serialization.load_pem_private_key(
            (directory / "keys" / f"{d.principal}.key").read_bytes(), password=None), key_id)
            for key_id, d in keys.items()}
    else:
        from cryptography.hazmat.primitives import serialization

        from protocollab.operator import generate_keys, load_authorities
        generate_keys(directory / "keys")
        keys = load_authorities(directory / "keys" / "root-manifest.json")
        credentials = {d.principal: (serialization.load_pem_private_key(
            (directory / "keys" / f"{d.principal}.key").read_bytes(), password=None), key_id)
            for key_id, d in keys.items()}
    started_wall, started_cpu = time.monotonic(), time.process_time()
    backend = EnvironmentProcess(directory / "private", config)
    runtime = Runtime(directory / "owner", backend, keys, namespace="episode", task=task,
                      limits=LearningLimits(**manifest.learning))
    runtime.max_turns = manifest.max_live_turns
    runtime.planner = isolated_plan
    initial_child_cpu = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime + resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime
    def check_budget():
        cpu = time.process_time() - started_cpu
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu += children.ru_utime + children.ru_stime - initial_child_cpu
        if cpu >= manifest.cpu_seconds_cap or time.monotonic() - started_wall >= manifest.wall_seconds_cap:
            raise BudgetExhausted("CPU_OR_WALL_TIME_CAP")
    runtime.budget_check = check_budget
    monitor = MonitorProcess(directory / "monitor") if governance_condition == "G3" else None
    runtime.monitor = monitor
    if governance_condition in ("G0", "G1"):
        install_log_only(runtime, governance_condition, disposable_benchmark=True)
    adaptation, execution = {"status": "NOT_REQUESTED"}, {"status": "NOT_STARTED"}
    try:
        runtime.store.put_blob(manifest)
        runtime.store.set("evaluation", "manifest", manifest.model_dump(), "evaluation.manifest_locked")
        if "disable_structural_growth" in manifest.ablations and not prefix_template:
            from dataclasses import replace

            from protocollab.evaluation.ablations import fixed_schema_predictor
            runtime.limits = replace(runtime.limits, model_states=1)
            incumbent = fixed_schema_predictor(runtime.store.namespace, {})
            key = runtime.store.put_blob(incumbent)
            runtime.store.set("model", "active", {"hash": key, "revision": 0}, "model.promoted")
            runtime.belief.rebase(runtime.models.current_snapshot(), [])
        if intervention and intervention_point in ("membership_query", "before_promotion"):
            intervention(runtime, credentials)
        if shared_transcripts:
            # Shared observations were collected by the fixed public policy; no hidden state labels.
            adapter = runtime.query_adapter()
            for trace in shared_transcripts:
                actual = adapter.query(trace["word"])
                if actual != trace["outputs"]:
                    raise ValueError("SHARED_TRANSCRIPT_RESET_CONTRACT_MISMATCH")
        if prefix_template:
            adaptation = {"status": "SHARED_PREFIX_REUSED", "source": str(prefix_template),
                          "source_policy": manifest.shared_prefix_policy,
                          "evidence_reuse": "identical owner journal and query cache, fresh evaluator-owned episode instance",
                          "cost_accounting": "prefix interactions charged once per topology/seed and amortized equally"}
            if condition in ("C0", "C1"):
                runtime.store.set("model", "active", None, "baseline.model_removed")
                runtime.store.set("procedure", "active", None, "baseline.procedure_removed")
                b = runtime.belief.snapshot.model_copy(update={"possible_model_states": [], "model_rev": 0})
                runtime.store.set("belief", runtime.belief.resource, b.model_dump(), "baseline.belief_reset")
            active_procedure = runtime.store.get("procedure", "active")
            if active_procedure:
                from protocollab.contracts import ProcedureArtifact
                runtime.prefix_plan = {"status": "PLANNED", "procedure": ProcedureArtifact.model_validate(runtime.store.blob(active_procedure["hash"])),
                                       "worst_case_cost": None, "expanded_nodes": 0}
        elif condition in ("C2", "C3"):
            adaptation = adapt(runtime, seed)
        elif condition in ("C0", "C1"):
            adaptation = run_llm_prefix(runtime, manifest.model_port, condition, seed, manifest.max_llm_calls_prefix)
        elif condition == "C4":
            if true_model is None:
                raise ValueError("PRIVILEGED_DIAGNOSTIC_REQUIRES_TRUE_MODEL")
            artifact = ModelArtifact.model_validate(true_model)
            key = runtime.store.put_blob(artifact)
            runtime.store.set("model", "active", {"hash": key, "revision": artifact.revision}, "model.promoted")
            runtime.belief.rebase(runtime.models.current_snapshot(), [])
            runtime.store.append("evaluation", "oracle.diagnostic", {"deployable": False, "hash": key})
        if condition == "C1" or "rollback_model" in manifest.ablations:
            from protocollab.evaluation.ablations import (
                fixed_schema_predictor,
                rollback_in_evaluator_fork,
            )
            incumbent = fixed_schema_predictor(runtime.store.namespace, {})
            rollback = rollback_in_evaluator_fork(runtime, incumbent, evaluator_fork=True)
            runtime.prefix_plan = None
            runtime.store.append("evaluation", "ablation.applied", {"kind": "fixed_schema_incumbent", "model_hash": rollback["new_hash"],
                                                                     "evidence_retained": True})
        if condition == "C4" and prefix_template:
            if true_model is None:
                raise ValueError("PRIVILEGED_DIAGNOSTIC_REQUIRES_TRUE_MODEL")
            artifact = ModelArtifact.model_validate(true_model)
            key = runtime.store.put_blob(artifact)
            runtime.store.set("model", "active", {"hash": key, "revision": artifact.revision}, "model.promoted")
            runtime.belief.rebase(runtime.models.current_snapshot(), [])
            runtime.prefix_plan = None
        if "remove_model" in manifest.ablations:
            runtime.store.set("model", "active", None, "ablation.model_removed")
            runtime.store.set("procedure", "active", None, "ablation.procedure_removed")
            runtime.prefix_plan = None
            b = runtime.belief.snapshot.model_copy(update={"possible_model_states": [], "model_rev": 0})
            runtime.store.set("belief", runtime.belief.resource, b.model_dump(), "ablation.belief_reset")
        if "counterfactual_isolation" in manifest.ablations and runtime.models.current_snapshot():
            from protocollab.evaluation.ablations import isolation_fingerprint
            before = isolation_fingerprint(runtime)
            runtime.simulate(["SUBMIT_A", "TICK", "INSPECT"])
            after = isolation_fingerprint(runtime)
            if before != after:
                raise AssertionError("COUNTERFACTUAL_CONTAMINATION")
            runtime.store.append("evaluation", "ablation.counterfactual_isolation", {"before": before, "after": after, "passed": True})
        if "restart" in manifest.ablations:
            checkpoint = runtime.checkpoint()
            before = runtime.models.current_snapshot()
            if runtime.recover(checkpoint) != "RECOVERED":
                raise RuntimeError("RESTART_FIDELITY_FAILED")
            runtime.store.append("evaluation", "ablation.restart", {"before_model_hash": before.hash if before else None,
                "after_model_hash": runtime.models.current_snapshot().hash if runtime.models.current_snapshot() else None})
        if condition in ("C2", "C3", "C4") and runtime.models.current_snapshot() and not prefix_template:
            runtime.prefix_plan = runtime.synthesize(admit=True)
        if task is not None and runtime.governance.task.artifact != task.artifact:
            from protocollab.evaluation.interventions import signed_control
            key, key_id = credentials["operator"]
            signed_control(runtime, key, key_id, "operator", "REDIRECT", artifact=task.artifact)
            runtime.prefix_plan = None
        if manifest.track == "F" and not prefix_only:
            runtime.freeze()
        if intervention and intervention_point not in ("membership_query", "before_promotion"):
            intervention(runtime, credentials)
        if runtime.hooks.get("before_plan"):
            runtime.hooks["before_plan"]()
        for command in public_prehistory:
            runtime.turn(command)
        if public_prehistory:
            runtime.prefix_plan = None
        if prefix_only:
            execution = {"status": "PREFIX_ONLY", "live_suffix_started": False}
        elif condition in ("C3", "C4"):
            if runtime.models.current_snapshot():
                execution = run_native(runtime, manifest.track, "disable_procedure_reuse" not in manifest.ablations)
                if execution["status"] in ("STALE", "INTERRUPTED", "ABORT"):
                    execution = resume_native(runtime, manifest.track, seed)
            elif runtime.governance.is_paused(runtime.governance.task.resource_id):
                execution = resume_native(runtime, manifest.track, seed)
        else:
            execution = run_llm(runtime, manifest.model_port, condition, manifest.track, seed,
                                manifest.max_llm_calls_suffix)
        runtime.sync_monitor()
        checkpoint = runtime.checkpoint(monitor.anchor if monitor else None)
        runtime.store.export(directory / "public")
        public = {"spec_version": "0.1", "condition": condition, "governance_condition": governance_condition,
                  "track": manifest.track, "seed": seed, "scenario_class": scenario_class,
                  "adaptation": adaptation, "execution": execution, "checkpoint_hash": checkpoint,
                  "query_usage": runtime.store.get("learning", "usage"),
                  "learning_limits": asdict(runtime.limits),
                  "resource_usage": {"wall_seconds": time.monotonic() - started_wall,
                                     "orchestrator_cpu_seconds": time.process_time() - started_cpu,
                                     "environment_worker": backend.usage(),
                                     "monitor_worker": monitor.usage() if monitor else None,
                                     "learner_workers": [a["learner_export"].get("worker_usage") for a in adaptation.get("attempts", [])],
                                     "planner_cpu_seconds": sum(e["payload"]["cpu_seconds"] for e in runtime.store.events() if e["kind"] == "planner.usage"),
                                     "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss},
                  "monitor_anchor": monitor.anchor if monitor else None,
                  "model_port_used": condition in ("C0", "C1", "C2"),
                  "prefix_policy": manifest.shared_prefix_policy or "condition_autonomous_queries",
                  "research_claims": "NOT_ESTABLISHED_BY_THIS_RUN"}
        from pydantic import BaseModel
        def encode_model(value):
            if isinstance(value, BaseModel):
                return value.model_dump()
            raise TypeError(f"Unserializable report value: {type(value).__name__}")
        public = json.loads(json.dumps(public, default=encode_model))
        (directory / "episode.json").write_text(json.dumps(public, indent=2, sort_keys=True) + "\n")
        return public, runtime.store.events(), runtime.governance.task
    finally:
        # A failed run retains its partial evidence too.
        if not (directory / "public" / "journal.jsonl").exists():
            runtime.store.export(directory / "public")
        runtime.close()
        backend.close()
        if monitor:
            monitor.close()
