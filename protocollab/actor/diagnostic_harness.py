"""Three-stage actor-interface diagnostic harness with persistent budgeting and unified configuration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from protocollab.actor import FrozenModelPort, ModelPortConfig, build_packet
from protocollab.actor.diagnostic import (
    DiagnosticThresholds,
    StageResult,
    check_minimal_proposal,
    evaluate_authorization_compliance,
    evaluate_decision_correctness,
    is_packet_copy,
)
from protocollab.actor.diagnostic_config import DiagnosticConfig
from protocollab.actor.modes import DecisionAdapter, UnsupportedConfiguration
from protocollab.contracts import MUTATIONS, digest


class ActorDiagnosticHarness:
    def __init__(
        self,
        thresholds: DiagnosticThresholds | None = None,
        wording: str = "demarcated",
        constrained_decoding: bool = False,
        config: DiagnosticConfig | None = None,
    ):
        self.thresholds = thresholds or (config.thresholds if config else DiagnosticThresholds())
        self.wording = config.renderer if config else wording
        self.constrained_decoding = (config.decision_mode == "constrained_json") if config else constrained_decoding
        self.config = config

    def _ensure_config(self, model_config: ModelPortConfig | None = None) -> DiagnosticConfig:
        if self.config is not None:
            return self.config
        mode = "constrained_json" if self.constrained_decoding else "free_json"
        port_cfg = model_config or ModelPortConfig(
            backend="api", model_id="default-diagnostic-port", endpoint="https://model.invalid"
        )
        self.config = DiagnosticConfig(
            renderer=self.wording,
            decision_mode=mode,
            thresholds=self.thresholds,
            model_port_config=port_cfg,
        )
        return self.config

    def _get_ledger(self, store: Any, config: DiagnosticConfig) -> Any:
        config_hash = digest(config.model_dump())
        from protocollab.verified import LifecycleMode, VerifiedLifecycleOwner

        mode = getattr(config, "lifecycle_mode", LifecycleMode.SHADOW)
        return VerifiedLifecycleOwner(store, config.run_id, config.budget_allocation, config_hash, mode=mode)

    def _accumulate_compute(self, summary: dict[str, int], outcome: Any) -> None:
        meta = getattr(outcome, "backend_meta", {}) or {}
        summary["logical_decisions"] = summary.get("logical_decisions", 0) + 1
        summary["candidate_evaluations"] = summary.get("candidate_evaluations", 0) + meta.get("candidate_evaluations", 1)
        summary["prompt_tokens_logically_supplied"] = (
            summary.get("prompt_tokens_logically_supplied", 0)
            + meta.get("prompt_tokens_logically_supplied", getattr(outcome, "request_input_tokens", 0))
        )
        summary["total_tokens_processed"] = (
            summary.get("total_tokens_processed", 0)
            + meta.get("total_tokens_processed", getattr(outcome, "total_tokens_evaluated", 0))
        )
        summary["forward_passes"] = summary.get("forward_passes", 0) + meta.get("forward_passes", 1)
        summary["prefill_recomputations"] = summary.get("prefill_recomputations", 0) + meta.get("prefill_recomputations", 0)

    def run_stage_1_minimal_proposal(
        self,
        port: FrozenModelPort,
        packet: dict[str, Any],
        n_samples: int = 5,
        seed: int = 7,
        config: DiagnosticConfig | None = None,
    ) -> StageResult:
        cfg = config or self._ensure_config(port.config)
        ledger = self._get_ledger(port.store, cfg)
        stage_name = "minimal_proposal"
        adapter = DecisionAdapter(cfg)

        result = StageResult(stage=stage_name, status="FAIL")
        valid_count = trunc_count = 0
        gen_statuses: dict[str, int] = {}
        compute_summary: dict[str, int] = {}

        try:
            for i in range(n_samples):
                outcome = adapter.decide(port, packet, seed=seed, ledger=ledger, stage=stage_name)
                self._accumulate_compute(compute_summary, outcome)
                raw = outcome.raw_response
                backend_meta = outcome.backend_meta

                copied = is_packet_copy(raw, packet)
                check = check_minimal_proposal(raw, port.config.max_output_tokens, backend_meta=backend_meta)
                valid, truncated, reason = check.valid, check.truncated, check.reason
                status_name = getattr(check, "generation_status", "COMPLETED")
                gen_statuses[status_name] = gen_statuses.get(status_name, 0) + 1

                if valid and not copied:
                    valid_count += 1
                if truncated:
                    trunc_count += 1

                result.samples.append({
                    "sample_index": i,
                    "raw_text": raw[:2000],
                    "valid": valid and not copied,
                    "copied_packet": copied,
                    "truncated": truncated,
                    "generation_status": status_name,
                    "reason": "COPIED_PACKET" if copied else reason,
                })
        except UnsupportedConfiguration as exc:
            result.status = "UNSUPPORTED"
            result.stop_rule_triggered = f"UNSUPPORTED_CONFIGURATION: {exc}"
            return result

        stage_usage = ledger.state.stages[stage_name]
        result.calls_attempted = stage_usage.calls_attempted
        result.dispatched_inference = stage_usage.dispatched_inference
        result.completed_calls = stage_usage.completed_calls
        result.failures = stage_usage.failures
        result.generation_statuses = gen_statuses

        denom = max(result.calls_attempted, 1)
        result.schema_valid_count = valid_count
        result.schema_valid_rate = valid_count / denom
        result.truncation_count = trunc_count
        result.truncation_rate = trunc_count / denom
        result.compute_summary = compute_summary

        if result.truncation_rate > self.thresholds.max_truncation_rate:
            result.stop_rule_triggered = (
                f"TRUNCATION_RATE_ABOVE_THRESHOLD: {result.truncation_rate:.2f} > "
                f"{self.thresholds.max_truncation_rate:.2f}"
            )
        elif result.schema_valid_rate >= self.thresholds.min_schema_valid_rate:
            result.status = "PASS"
        else:
            result.stop_rule_triggered = (
                f"SCHEMA_VALIDITY_BELOW_THRESHOLD: {result.schema_valid_rate:.2f} < "
                f"{self.thresholds.min_schema_valid_rate:.2f}"
            )
        return result

    def run_stage_2_state_decision(
        self,
        port: FrozenModelPort,
        test_scenarios: list[dict[str, Any]],
        seed: int = 7,
        config: DiagnosticConfig | None = None,
    ) -> StageResult:
        cfg = config or self._ensure_config(port.config)
        ledger = self._get_ledger(port.store, cfg)
        stage_name = "state_decision"
        adapter = DecisionAdapter(cfg)

        result = StageResult(stage=stage_name, status="FAIL")
        valid_count = correct_count = compliant_count = trunc_count = 0
        gen_statuses: dict[str, int] = {}
        compute_summary: dict[str, int] = {}

        try:
            for i, scenario in enumerate(test_scenarios):
                packet = scenario["packet"]
                outcome = adapter.decide(port, packet, seed=seed, ledger=ledger, stage=stage_name)
                self._accumulate_compute(compute_summary, outcome)
                raw = outcome.raw_response
                backend_meta = outcome.backend_meta

                check = check_minimal_proposal(raw, port.config.max_output_tokens, backend_meta=backend_meta)
                valid, truncated, proposal, reason = check.valid, check.truncated, check.proposal, check.reason
                status_name = getattr(check, "generation_status", "COMPLETED")
                gen_statuses[status_name] = gen_statuses.get(status_name, 0) + 1

                if truncated:
                    trunc_count += 1
                if valid and proposal:
                    valid_count += 1
                    compliant, comp_reason = evaluate_authorization_compliance(proposal, packet)
                    if compliant:
                        compliant_count += 1
                    correct, eval_reason = evaluate_decision_correctness(proposal, packet, scenario.get("expected"))
                else:
                    compliant, comp_reason = False, reason
                    correct, eval_reason = False, reason

                if correct:
                    correct_count += 1

                result.samples.append({
                    "scenario_index": i,
                    "scenario_name": scenario.get("name", f"scenario_{i}"),
                    "raw_text": raw[:2000],
                    "valid": valid,
                    "compliant": compliant,
                    "compliance_reason": comp_reason,
                    "correct": correct,
                    "reason": eval_reason,
                    "generation_status": status_name,
                })
        except UnsupportedConfiguration as exc:
            result.status = "UNSUPPORTED"
            result.stop_rule_triggered = f"UNSUPPORTED_CONFIGURATION: {exc}"
            return result

        stage_usage = ledger.state.stages[stage_name]
        result.calls_attempted = stage_usage.calls_attempted
        result.dispatched_inference = stage_usage.dispatched_inference
        result.completed_calls = stage_usage.completed_calls
        result.failures = stage_usage.failures
        result.generation_statuses = gen_statuses

        denom = max(result.calls_attempted, 1)
        result.schema_valid_count = valid_count
        result.schema_valid_rate = valid_count / denom
        result.authorization_compliant_count = compliant_count
        result.authorization_compliant_rate = compliant_count / denom
        result.decision_correct_count = correct_count
        result.decision_correct_rate = correct_count / denom
        result.truncation_count = trunc_count
        result.truncation_rate = trunc_count / denom
        result.compute_summary = compute_summary

        if result.decision_correct_rate >= self.thresholds.min_decision_correct_rate:
            result.status = "PASS"
        else:
            result.stop_rule_triggered = (
                f"DECISION_CORRECTNESS_BELOW_THRESHOLD: {result.decision_correct_rate:.2f} < "
                f"{self.thresholds.min_decision_correct_rate:.2f}"
            )
        return result

    def run_stage_3_closed_loop(
        self,
        model_config: ModelPortConfig,
        runtime: Any,
        max_turns: int = 6,
        seed: int = 7,
        condition: str = "C0",
        track: str = "F",
        config: DiagnosticConfig | None = None,
        port: FrozenModelPort | None = None,
    ) -> StageResult:
        cfg = config or self.config
        if cfg is None:
            mode = "constrained_json" if self.constrained_decoding else "free_json"
            cfg = DiagnosticConfig(
                condition=condition,
                track=track,
                seed=seed,
                renderer=self.wording,
                decision_mode=mode,
                model_port_config=model_config,
                thresholds=self.thresholds,
            )
        self.config = cfg

        stage_name = "closed_loop"
        ledger = self._get_ledger(runtime.store, cfg)
        port = port or FrozenModelPort(model_config, runtime.store, max_calls=max_turns, phase="suffix")
        adapter = DecisionAdapter(cfg)

        result = StageResult(stage=stage_name, status="FAIL")
        cursor, live_cursor = 0, runtime.store.tail[0]
        previous_turns = runtime.max_turns
        runtime.max_turns = max_turns

        valid_count = 0
        proposed_count = 0
        denied_or_stale_count = 0
        dispatched_count = 0
        acknowledged_count = 0
        environment_effect_count = 0
        effective_count = 0
        seen_observations: set[str] = set()
        outcomes: dict[str, int] = {}
        compute_summary: dict[str, int] = {}

        try:
            for turn_idx in range(max_turns):
                if runtime.finish()["status"] == "PUBLIC_CONTRACT_SATISFIED":
                    result.task_completed = True
                    break

                packet = build_packet(runtime, condition=cfg.condition, cursor=cursor, live_cursor=live_cursor)
                outcome = adapter.decide(
                    port, packet, seed=cfg.seed, ledger=ledger, stage=stage_name
                )
                self._accumulate_compute(compute_summary, outcome)
                proposal = outcome.proposal
                live_cursor = packet["live_feedback"]["next_cursor"]
                req_seq = port.last_request_seq
                basis_ref = packet["decision_basis_ref"]

                if outcome.validation_outcome == "VALID":
                    valid_count += 1

                # Standardized actor interaction lifecycle (§5)
                text_ref = runtime.store.put_blob(outcome.raw_response.encode("utf-8"))
                final_payload_ref = runtime.store.put_blob(outcome.final_payload.encode("utf-8"))
                reasoning_ref = (
                    runtime.store.put_blob(outcome.reasoning.encode("utf-8"))
                    if outcome.reasoning is not None
                    else None
                )
                runtime.store.append("actor", "actor.raw_proposal", {
                    "request_seq": req_seq,
                    "decision_basis_ref": basis_ref,
                    "text_ref": text_ref,
                    "final_payload_ref": final_payload_ref,
                    "reasoning_ref": reasoning_ref,
                    "extraction_policy": outcome.extraction_policy,
                })

                if outcome.validation_outcome == "VALID" and not outcome.is_fallback:
                    runtime.store.append("actor", "actor.proposal_returned", {
                        "request_seq": req_seq,
                        "decision_basis_ref": basis_ref,
                        "validation_outcome": "VALID",
                        **proposal.model_dump(),
                    })
                else:
                    runtime.store.append("actor", "actor.proposal_rejected", {
                        "request_seq": req_seq,
                        "decision_basis_ref": basis_ref,
                        "validation_outcome": outcome.validation_outcome,
                        "reason": outcome.raw_response[:500],
                    })

                runtime.store.append("actor", "actor.proposed", {
                    **proposal.model_dump(),
                    "request_seq": req_seq,
                    "decision_basis_ref": basis_ref,
                    "decision_mode": cfg.decision_mode,
                    "is_fallback": outcome.is_fallback,
                    "claims": outcome.claim_refs,
                })
                runtime.sync_monitor()

                proposed_count += 1

                if outcome.is_fallback:
                    # Fallback to WAIT must be labeled fallback, not model-selected abstention
                    runtime.turn("WAIT", decision_basis_ref=basis_ref)
                    outcomes["WAIT (fallback)"] = outcomes.get("WAIT (fallback)", 0) + 1
                    runtime.store.append("actor", "actor.interaction_completed", {
                        "request_seq": req_seq,
                        "decision_basis_ref": basis_ref,
                        "phase": port.phase,
                    })
                    continue

                if proposal.kind == "WAIT":
                    # Explicit WAIT: proposed, but 0 dispatches (§1)
                    runtime.turn("WAIT", decision_basis_ref=basis_ref)
                    outcomes["WAIT"] = outcomes.get("WAIT", 0) + 1
                    runtime.store.append("actor", "actor.interaction_completed", {
                        "request_seq": req_seq,
                        "decision_basis_ref": basis_ref,
                        "phase": port.phase,
                    })
                    continue

                if proposal.kind == "ACT":
                    op = proposal.operation or "INSPECT"
                    outcomes[op] = outcomes.get(op, 0) + 1
                    turn_return = runtime.turn(op, decision_basis_ref=basis_ref)

                    # Close actor interaction (§5)
                    runtime.store.append("actor", "actor.interaction_completed", {
                        "request_seq": req_seq,
                        "decision_basis_ref": basis_ref,
                        "phase": port.phase,
                    })

                    # Runtime.turn() returns {"action": result, "tick": tick} (§1)
                    action_res = turn_return.get("action")
                    if action_res is None:
                        continue

                    status = action_res.get("status")
                    if status in ("DENIED", "STALE", "CANCELLED_BEFORE_DISPATCH", "REJECTED"):
                        denied_or_stale_count += 1
                        continue

                    if status == "DISPATCH_UNCERTAIN":
                        dispatched_count += 1
                        continue

                    if status in ("ACKNOWLEDGED", "OUTCOME_OBSERVED"):
                        dispatched_count += 1
                        acknowledged_count += 1

                        # Evaluator-owned milestones (§6)
                        if op == "INSPECT":
                            # Milestone: discovery of new observation evidence
                            receipt_data = action_res.get("receipt") or {}
                            causal_cmd = receipt_data.get("causal_command_id")
                            cmd_id = action_res.get("command_id")
                            if causal_cmd == cmd_id:
                                domain_output = receipt_data.get("domain_output")
                                if domain_output is not None and domain_output not in seen_observations:
                                    seen_observations.add(domain_output)
                                    environment_effect_count += 1
                                    effective_count += 1

                        elif op in MUTATIONS:
                            # Milestone: meaningful domain state transition
                            cmd_id = action_res.get("command_id")
                            state_changed = False
                            if hasattr(runtime, "backend") and hasattr(runtime.backend, "db"):
                                row = runtime.backend.db.execute(
                                    "SELECT before_state, after_state FROM effects WHERE command_id=?",
                                    (cmd_id,),
                                ).fetchone()
                                if row and row["before_state"] != row["after_state"]:
                                    state_changed = True
                            else:
                                state_changed = (status == "ACKNOWLEDGED")

                            if state_changed:
                                environment_effect_count += 1
                                effective_count += 1

                elif proposal.kind == "FINISH":
                    outcomes["FINISH"] = outcomes.get("FINISH", 0) + 1
                    runtime.store.append("actor", "actor.interaction_completed", {
                        "request_seq": req_seq,
                        "decision_basis_ref": basis_ref,
                        "phase": port.phase,
                    })
                    break

            if runtime.finish()["status"] == "PUBLIC_CONTRACT_SATISFIED":
                result.task_completed = True

        except Exception as exc:
            result.samples.append({"error": f"EXECUTION_FAILED: {exc}"})
            if isinstance(exc, UnsupportedConfiguration):
                result.stop_rule_triggered = f"UNSUPPORTED_CONFIGURATION: {exc}"
            else:
                result.stop_rule_triggered = f"EXECUTION_FAILED: {type(exc).__name__}"
        finally:
            runtime.max_turns = previous_turns

        stage_usage = ledger.state.stages[stage_name]
        result.calls_attempted = max(stage_usage.calls_attempted, 1)
        result.dispatched_inference = stage_usage.dispatched_inference
        result.completed_calls = stage_usage.completed_calls
        result.failures = stage_usage.failures
        result.dispatch_outcomes = outcomes
        result.actions_executed = dispatched_count
        result.actuation_success = dispatched_count > 0
        result.task_progress_count = effective_count
        result.task_progress_rate = effective_count / result.calls_attempted
        result.schema_valid_count = valid_count
        result.schema_valid_rate = valid_count / result.calls_attempted
        # Action lifecycle accounting (§1, §6)
        result.proposed_actions = proposed_count
        result.denied_or_stale_actions = denied_or_stale_count
        result.dispatched_actions = dispatched_count
        result.acknowledged_actions = acknowledged_count
        result.environment_effect_observed = environment_effect_count
        result.effective_actions = effective_count
        result.compute_summary = compute_summary

        if result.stop_rule_triggered and result.stop_rule_triggered.startswith("UNSUPPORTED_CONFIGURATION"):
            result.status = "UNSUPPORTED"
        elif result.stop_rule_triggered:
            result.status = "FAIL"
        elif result.task_completed or result.task_progress_rate >= self.thresholds.min_progress_rate:
            result.status = "PASS"
        else:
            result.status = "FAIL"
            result.stop_rule_triggered = (
                f"PROGRESS_RATE_BELOW_THRESHOLD: {result.task_progress_rate:.2f} < "
                f"{self.thresholds.min_progress_rate:.2f}"
            )
        return result

    def run_all(
        self,
        port: FrozenModelPort,
        packet: dict[str, Any],
        test_scenarios: list[dict[str, Any]],
        runtime: Any = None,
        model_config: ModelPortConfig | None = None,
        config: DiagnosticConfig | None = None,
    ) -> dict[str, Any]:
        cfg = config or self.config or self._ensure_config(model_config or port.config)
        self.config = cfg

        report: dict[str, Any] = {
            "wording": cfg.renderer,
            "condition": cfg.condition,
            "track": cfg.track,
            "decision_mode": cfg.decision_mode,
            "constrained_decoding": (cfg.decision_mode == "constrained_json"),
            "run_id": cfg.run_id,
            "stages": {},
            "status": "INCOMPLETE",
        }

        s1 = self.run_stage_1_minimal_proposal(port, packet, config=cfg)
        report["stages"]["minimal_proposal"] = asdict(s1)
        if s1.status == "UNSUPPORTED":
            report["status"] = "UNSUPPORTED"
            report["stop_reason"] = s1.stop_rule_triggered
            return report
        if s1.status != "PASS":
            report["status"] = "STOPPED_AT_STAGE_1"
            report["stop_reason"] = s1.stop_rule_triggered
            return report

        s2 = self.run_stage_2_state_decision(port, test_scenarios, config=cfg)
        report["stages"]["state_decision"] = asdict(s2)
        if s2.status == "UNSUPPORTED":
            report["status"] = "UNSUPPORTED"
            report["stop_reason"] = s2.stop_rule_triggered
            return report
        if s2.status != "PASS":
            report["status"] = "STOPPED_AT_STAGE_2"
            report["stop_reason"] = s2.stop_rule_triggered
            return report

        if runtime is not None:
            s3 = self.run_stage_3_closed_loop(
                cfg.model_port_config, runtime, config=cfg
            )
            report["stages"]["closed_loop"] = asdict(s3)
            if s3.status == "UNSUPPORTED":
                report["status"] = "UNSUPPORTED"
                report["stop_reason"] = s3.stop_rule_triggered
                return report
            report["status"] = "PASS" if s3.status == "PASS" else "STOPPED_AT_STAGE_3"
            report["stop_reason"] = s3.stop_rule_triggered
        else:
            report["status"] = "STAGES_1_AND_2_PASSED"

        # Attach persistent ledger summary
        ledger = self._get_ledger(runtime.store if runtime else port.store, cfg)
        report["budget_ledger"] = ledger.state.model_dump()

        # Attach aggregate compute summary across stages (§8)
        total_compute: dict[str, int] = {}
        for st in (s1, s2, s3 if runtime is not None else None):
            if st and getattr(st, "compute_summary", None):
                for k, v in st.compute_summary.items():
                    total_compute[k] = total_compute.get(k, 0) + v
        report["compute_summary"] = total_compute
        return report
