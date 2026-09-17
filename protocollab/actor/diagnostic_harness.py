"""Three-stage actor-interface diagnostic harness."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from protocollab.actor import FrozenModelPort, ModelPortConfig
from protocollab.actor.diagnostic import (
    DiagnosticThresholds,
    StageResult,
    check_minimal_proposal,
    evaluate_decision_correctness,
    format_diagnostic_prompt,
    is_packet_copy,
)
from protocollab.contracts import MUTATIONS

PROGRESS_OPERATIONS = frozenset(MUTATIONS) | {"INSPECT"}


class ActorDiagnosticHarness:
    def __init__(self, thresholds: DiagnosticThresholds | None = None, wording: str = "demarcated",
                 constrained_decoding: bool = False):
        self.thresholds = thresholds or DiagnosticThresholds()
        self.wording = wording
        self.constrained_decoding = constrained_decoding

    def _generate(self, port: FrozenModelPort, packet: dict[str, Any], seed: int) -> str:
        return port.generate(packet, seed=seed, prompt_override=format_diagnostic_prompt(packet, self.wording))

    def run_stage_1_minimal_proposal(self, port: FrozenModelPort, packet: dict[str, Any],
                                     n_samples: int = 5, seed: int = 7) -> StageResult:
        result = StageResult(stage="minimal_proposal", status="FAIL", calls_attempted=n_samples)
        valid_count = trunc_count = 0
        for i in range(n_samples):
            try:
                raw = self._generate(port, packet, seed + i)
            except Exception as exc:
                raw = f"PORT_ERROR: {exc}"
            copied = is_packet_copy(raw, packet)
            valid, truncated, proposal, reason = check_minimal_proposal(raw, port.config.max_output_tokens)
            if valid and not copied:
                valid_count += 1
            if truncated:
                trunc_count += 1
            result.samples.append({
                "sample_index": i, "raw_text": raw[:2000], "valid": valid and not copied,
                "copied_packet": copied, "truncated": truncated,
                "reason": "COPIED_PACKET" if copied else reason,
            })
        result.schema_valid_count = valid_count
        result.schema_valid_rate = valid_count / n_samples
        result.truncation_count = trunc_count
        result.truncation_rate = trunc_count / n_samples
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

    def run_stage_2_state_decision(self, port: FrozenModelPort, test_scenarios: list[dict[str, Any]],
                                   seed: int = 7) -> StageResult:
        n = len(test_scenarios)
        result = StageResult(stage="state_decision", status="FAIL", calls_attempted=n)
        valid_count = correct_count = trunc_count = 0
        for i, scenario in enumerate(test_scenarios):
            packet = scenario["packet"]
            try:
                raw = self._generate(port, packet, seed + i)
            except Exception as exc:
                raw = f"PORT_ERROR: {exc}"
            valid, truncated, proposal, reason = check_minimal_proposal(raw, port.config.max_output_tokens)
            if truncated:
                trunc_count += 1
            if valid and proposal:
                valid_count += 1
                correct, eval_reason = evaluate_decision_correctness(
                    proposal, packet, scenario.get("expected"))
            else:
                correct, eval_reason = False, reason
            if correct:
                correct_count += 1
            result.samples.append({
                "scenario_index": i, "scenario_name": scenario.get("name", f"scenario_{i}"),
                "raw_text": raw[:2000], "valid": valid, "correct": correct, "reason": eval_reason,
            })
        result.schema_valid_count = valid_count
        result.schema_valid_rate = valid_count / n if n else 0.0
        result.decision_correct_count = correct_count
        result.decision_correct_rate = correct_count / n if n else 0.0
        result.truncation_count = trunc_count
        result.truncation_rate = trunc_count / n if n else 0.0
        if result.decision_correct_rate >= self.thresholds.min_decision_correct_rate:
            result.status = "PASS"
        else:
            result.stop_rule_triggered = (
                f"DECISION_CORRECTNESS_BELOW_THRESHOLD: {result.decision_correct_rate:.2f} < "
                f"{self.thresholds.min_decision_correct_rate:.2f}"
            )
        return result

    def run_stage_3_closed_loop(self, model_config: ModelPortConfig, runtime: Any, max_turns: int = 6,
                                seed: int = 7, condition: str = "C0", track: str = "F") -> StageResult:
        from protocollab.evaluation.runner import run_llm

        result = StageResult(stage="closed_loop", status="FAIL", calls_attempted=max_turns)
        previous_turns = runtime.max_turns
        runtime.max_turns = max_turns
        if runtime.store.get("model_port", "suffix") is not None:
            runtime.store.set("model_port", "suffix", None)
        start_seq = runtime.store.tail[0]
        try:
            outcome = run_llm(runtime, model_config, condition, track, seed, call_cap=max_turns)
        except Exception as exc:
            result.samples.append({"error": f"EXECUTION_FAILED: {exc}"})
            runtime.max_turns = previous_turns
            result.stop_rule_triggered = "EXECUTION_FAILED"
            return result
        runtime.max_turns = previous_turns

        events = [e for e in runtime.store.events() if e["seq"] > start_seq]
        dispatches = [e for e in events if e["kind"] == "action.dispatched"]
        executed = sum(1 for e in dispatches if e["payload"].get("operation") in PROGRESS_OPERATIONS)
        calls = sum(1 for e in events if e["kind"] == "llm.completed")
        outcomes: dict[str, int] = {}
        for event in dispatches:
            op = event["payload"].get("operation", "ACT")
            outcomes[op] = outcomes.get(op, 0) + 1
        waits = sum(1 for e in events if e["kind"] == "actor.proposal_returned" and e["payload"].get("kind") == "WAIT")
        if waits:
            outcomes["WAIT"] = waits
        result.actions_executed = executed
        result.dispatch_outcomes = outcomes
        result.calls_attempted = max(calls, 1)
        result.schema_valid_count = sum(1 for e in events if e["kind"] == "actor.proposal_returned")
        result.schema_valid_rate = result.schema_valid_count / result.calls_attempted
        result.task_completed = outcome.get("status") == "SUCCESS"
        progress_rate = executed / result.calls_attempted
        if result.task_completed or progress_rate >= self.thresholds.min_progress_rate:
            result.status = "PASS"
        else:
            result.stop_rule_triggered = (
                f"PROGRESS_RATE_BELOW_THRESHOLD: {progress_rate:.2f} < "
                f"{self.thresholds.min_progress_rate:.2f}"
            )
        return result

    def run_all(self, port: FrozenModelPort, packet: dict[str, Any], test_scenarios: list[dict[str, Any]],
                runtime: Any = None, model_config: ModelPortConfig | None = None) -> dict[str, Any]:
        report: dict[str, Any] = {
            "wording": self.wording, "constrained_decoding": self.constrained_decoding,
            "stages": {}, "status": "INCOMPLETE",
        }
        s1 = self.run_stage_1_minimal_proposal(port, packet)
        report["stages"]["minimal_proposal"] = asdict(s1)
        if s1.status != "PASS":
            report["status"] = "STOPPED_AT_STAGE_1"
            report["stop_reason"] = s1.stop_rule_triggered
            return report
        s2 = self.run_stage_2_state_decision(port, test_scenarios)
        report["stages"]["state_decision"] = asdict(s2)
        if s2.status != "PASS":
            report["status"] = "STOPPED_AT_STAGE_2"
            report["stop_reason"] = s2.stop_rule_triggered
            return report
        if model_config is not None and runtime is not None:
            s3 = self.run_stage_3_closed_loop(model_config, runtime)
            report["stages"]["closed_loop"] = asdict(s3)
            report["status"] = "PASS" if s3.status == "PASS" else "STOPPED_AT_STAGE_3"
            report["stop_reason"] = s3.stop_rule_triggered
        else:
            report["status"] = "STAGES_1_AND_2_PASSED"
        return report
