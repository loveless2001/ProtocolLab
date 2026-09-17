"""Three-stage actor-interface diagnostic harness with persistent budgeting and unified configuration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from protocollab.actor import FrozenModelPort, ModelPortConfig, build_packet
from protocollab.actor.budget import DiagnosticLedger
from protocollab.actor.diagnostic import (
    DiagnosticThresholds,
    StageResult,
    check_minimal_proposal,
    evaluate_authorization_compliance,
    evaluate_decision_correctness,
    is_packet_copy,
)
from protocollab.actor.diagnostic_config import DiagnosticConfig
from protocollab.actor.modes import DecisionAdapter
from protocollab.actor.pipeline import compose_and_admit_input
from protocollab.contracts import MUTATIONS, digest
from protocollab.learning import BudgetExhausted


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

    def _get_ledger(self, store: Any, config: DiagnosticConfig) -> DiagnosticLedger:
        config_hash = digest(config.model_dump())
        return DiagnosticLedger(store, config.run_id, config.budget_allocation, config_hash)

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

        result = StageResult(stage=stage_name, status="FAIL")
        valid_count = trunc_count = 0
        gen_statuses: dict[str, int] = {}

        for i in range(n_samples):
            try:
                ledger.record_admission_attempt(stage_name)
                unformatted, formatted, evidence = compose_and_admit_input(
                    packet, port.config, renderer=cfg.renderer, store=port.store, phase=port.phase
                )
                reservation = port.config.max_input_tokens + port.config.max_output_tokens
                ledger.reserve(stage_name, reservation)
                ledger.record_dispatched(stage_name)

                raw = port.generate(packet, seed=seed, prompt_override=unformatted)
                backend_meta = {
                    "output_tokens": port.output_tokens,
                    "max_output_tokens": port.config.max_output_tokens,
                }
                ledger.record_completed(stage_name, port.input_tokens, port.output_tokens, reservation)
            except Exception as exc:
                raw = f"PORT_ERROR: {exc}"
                if isinstance(exc, BudgetExhausted) and ("token" in str(exc) or "cap" in str(exc)):
                    backend_meta = {"stop_reason": "token_limit_reached"}
                else:
                    backend_meta = {"error_type": type(exc).__name__}
                if "reservation" in locals():
                    ledger.record_failed(stage_name, reservation)

            copied = is_packet_copy(raw, packet)
            check = check_minimal_proposal(raw, port.config.max_output_tokens, backend_meta=backend_meta)
            valid, truncated, proposal, reason = check.valid, check.truncated, check.proposal, check.reason
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

        result = StageResult(stage=stage_name, status="FAIL")
        valid_count = correct_count = compliant_count = trunc_count = 0
        gen_statuses: dict[str, int] = {}

        for i, scenario in enumerate(test_scenarios):
            packet = scenario["packet"]
            try:
                ledger.record_admission_attempt(stage_name)
                unformatted, formatted, evidence = compose_and_admit_input(
                    packet, port.config, renderer=cfg.renderer, store=port.store, phase=port.phase
                )
                reservation = port.config.max_input_tokens + port.config.max_output_tokens
                ledger.reserve(stage_name, reservation)
                ledger.record_dispatched(stage_name)

                raw = port.generate(packet, seed=seed, prompt_override=unformatted)
                backend_meta = {
                    "output_tokens": port.output_tokens,
                    "max_output_tokens": port.config.max_output_tokens,
                }
                ledger.record_completed(stage_name, port.input_tokens, port.output_tokens, reservation)
            except Exception as exc:
                raw = f"PORT_ERROR: {exc}"
                if isinstance(exc, BudgetExhausted) and ("token" in str(exc) or "cap" in str(exc)):
                    backend_meta = {"stop_reason": "token_limit_reached"}
                else:
                    backend_meta = {"error_type": type(exc).__name__}
                if "reservation" in locals():
                    ledger.record_failed(stage_name, reservation)

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
        dispatches_executed = 0
        seen_observations = set()
        progress_steps = 0
        outcomes: dict[str, int] = {}

        try:
            for turn_idx in range(max_turns):
                if runtime.finish()["status"] == "PUBLIC_CONTRACT_SATISFIED":
                    result.task_completed = True
                    break

                packet = build_packet(runtime, condition=cfg.condition, cursor=cursor, live_cursor=live_cursor)
                outcome = adapter.decide(
                    port, packet, seed=cfg.seed, ledger=ledger, stage=stage_name
                )
                proposal = outcome.proposal
                live_cursor = packet["live_feedback"]["next_cursor"]

                if outcome.validation_outcome == "VALID":
                    valid_count += 1

                runtime.store.append("actor", "actor.proposed", {
                    **proposal.model_dump(),
                    "decision_basis_ref": packet["decision_basis_ref"],
                    "decision_mode": cfg.decision_mode,
                    "is_fallback": outcome.is_fallback,
                })
                runtime.sync_monitor()

                if outcome.is_fallback:
                    # Fallback to WAIT must be labeled fallback, not model-selected abstention
                    runtime.turn("WAIT", decision_basis_ref=packet["decision_basis_ref"])
                    outcomes["WAIT (fallback)"] = outcomes.get("WAIT (fallback)", 0) + 1
                    continue

                if proposal.kind in ("ACT", "WAIT"):
                    op = proposal.operation or "WAIT"
                    outcomes[op] = outcomes.get(op, 0) + 1
                    receipt = runtime.turn(op, decision_basis_ref=packet["decision_basis_ref"])
                    dispatches_executed += 1

                    # Track meaningful progress vs repeated identical reads or no-ops:
                    if op in MUTATIONS and receipt.get("status") not in ("STALE", "DENIED", "REJECTED"):
                        progress_steps += 1
                    elif op == "INSPECT":
                        new_inspect_obs = [
                            e for e in runtime.store.events(after=receipt.get("seq", 0) - 1)
                            if e["kind"] == "epistemic.observation" and e["payload"].get("input_symbol") == "INSPECT"
                        ]
                        for obs in new_inspect_obs:
                            obs_hash = obs.get("payload_hash") or obs["payload"].get("raw_hash")
                            if obs_hash and obs_hash not in seen_observations:
                                seen_observations.add(obs_hash)
                                progress_steps += 1

                elif proposal.kind == "FINISH":
                    outcomes["FINISH"] = outcomes.get("FINISH", 0) + 1
                    break

            if runtime.finish()["status"] == "PUBLIC_CONTRACT_SATISFIED":
                result.task_completed = True

        except Exception as exc:
            result.samples.append({"error": f"EXECUTION_FAILED: {exc}"})
            result.stop_rule_triggered = "EXECUTION_FAILED"
        finally:
            runtime.max_turns = previous_turns

        stage_usage = ledger.state.stages[stage_name]
        result.calls_attempted = max(stage_usage.calls_attempted, 1)
        result.dispatched_inference = stage_usage.dispatched_inference
        result.completed_calls = stage_usage.completed_calls
        result.failures = stage_usage.failures
        result.dispatch_outcomes = outcomes
        result.actions_executed = dispatches_executed
        result.actuation_success = dispatches_executed > 0
        result.task_progress_count = progress_steps
        result.task_progress_rate = progress_steps / result.calls_attempted
        result.schema_valid_count = valid_count
        result.schema_valid_rate = valid_count / result.calls_attempted

        if result.task_completed or result.task_progress_rate >= self.thresholds.min_progress_rate:
            result.status = "PASS"
        else:
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
        if s1.status != "PASS":
            report["status"] = "STOPPED_AT_STAGE_1"
            report["stop_reason"] = s1.stop_rule_triggered
            return report

        s2 = self.run_stage_2_state_decision(port, test_scenarios, config=cfg)
        report["stages"]["state_decision"] = asdict(s2)
        if s2.status != "PASS":
            report["status"] = "STOPPED_AT_STAGE_2"
            report["stop_reason"] = s2.stop_rule_triggered
            return report

        if runtime is not None:
            s3 = self.run_stage_3_closed_loop(
                cfg.model_port_config, runtime, config=cfg
            )
            report["stages"]["closed_loop"] = asdict(s3)
            report["status"] = "PASS" if s3.status == "PASS" else "STOPPED_AT_STAGE_3"
            report["stop_reason"] = s3.stop_rule_triggered
        else:
            report["status"] = "STAGES_1_AND_2_PASSED"

        # Attach persistent ledger summary
        ledger = self._get_ledger(runtime.store if runtime else port.store, cfg)
        report["budget_ledger"] = ledger.state.model_dump()
        return report
