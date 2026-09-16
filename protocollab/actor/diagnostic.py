"""Actor-Interface Diagnostic Harness.

Isolates actor-interface failure from world-model capability across three stages:
1. Minimal proposal contract: Valid schema return without echoing packet fields or truncating.
2. State-conditioned decision: Selecting an appropriate operation matching state, goal, and permissions.
3. Closed-loop execution: Acting, consuming live receipts, updating decisions, and completing tasks.

Pre-defines thresholds and stop rules. Constrained decoding can be declared as a separate condition.
Enforcement remains strictly in the broker; no silent response repair.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from protocollab.actor import (
    ActorProposal,
    FrozenModelPort,
    ModelPortConfig,
    build_packet,
    parse_proposal,
)
from protocollab.contracts import canonical


@dataclass(frozen=True)
class DiagnosticThresholds:
    min_schema_valid_rate: float = 0.80
    min_decision_correct_rate: float = 0.70
    min_progress_rate: float = 0.50
    max_truncation_rate: float = 0.10


@dataclass
class StageResult:
    stage: str
    status: Literal["PASS", "FAIL", "SKIPPED"]
    calls_attempted: int = 0
    schema_valid_count: int = 0
    schema_valid_rate: float = 0.0
    truncation_count: int = 0
    truncation_rate: float = 0.0
    decision_correct_count: int = 0
    decision_correct_rate: float = 0.0
    dispatch_outcomes: dict[str, int] = field(default_factory=dict)
    actions_executed: int = 0
    task_completed: bool = False
    stop_rule_triggered: str | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)


def format_diagnostic_prompt(packet: dict[str, Any], wording: str = "demarcated") -> str:
    """Format prompt with clearly separated state and output contract."""
    if wording == "standard":
        return canonical(packet).decode("utf-8")

    # Demarcated wording: Visibly separates input observation state from output schema contract.
    # Prevents small models from treating input fields as a template to copy.
    state_body = canonical({k: v for k, v in packet.items() if k not in ("proposal_schema", "checklist")}).decode("utf-8")
    return (
        "### TASK & REASONING GUIDANCE\n"
        f"{packet.get('checklist', '')}\n\n"
        "### CURRENT OBSERVATION STATE\n"
        f"{state_body}\n\n"
        "### OUTPUT FORMAT INSTRUCTIONS\n"
        "Return ONLY a single valid JSON object matching ActorProposal. Do NOT copy packet fields.\n"
        "Examples of valid proposals:\n"
        '  {"kind": "ACT", "operation": "INSPECT"}\n'
        '  {"kind": "ACT", "operation": "SIGNAL_X"}\n'
        '  {"kind": "WAIT"}\n'
        '  {"kind": "FINISH"}\n'
    )


def is_packet_copy(raw_text: str, packet: dict[str, Any]) -> bool:
    """Detect if the output merely copies input packet fields instead of proposing."""
    packet_keys = ["schema_version", "public_alphabet", "decision_basis_ref", "proposal_schema"]
    return any(f'"{k}"' in raw_text for k in packet_keys)


def check_minimal_proposal(raw_text: str, max_output_tokens: int = 256) -> tuple[bool, bool, ActorProposal | None, str]:
    """Check minimal proposal schema validity and truncation."""
    truncated = len(raw_text.strip().split()) >= max_output_tokens or raw_text.endswith("...")
    try:
        proposal = parse_proposal(raw_text.strip())
        return True, truncated, proposal, "VALID"
    except Exception as exc:
        return False, truncated, None, type(exc).__name__


def evaluate_decision_correctness(proposal: ActorProposal, packet: dict[str, Any]) -> tuple[bool, str]:
    """Verify operation selection against current permissions, status, and task goal."""
    control = packet.get("control", {})
    statuses = control.get("statuses", {})
    permissions = control.get("permissions", {})
    task = packet.get("task", {})
    resource = task.get("resource_id", "R")

    # If paused or in hold, ACT is illegal; only WAIT or APPEAL is valid
    if any(statuses.get(s) in ("PAUSED", "HOLD", "RECOVERY_REQUIRED") for s in ("agent_all", resource)):
        if proposal.kind in ("WAIT", "APPEAL"):
            return True, "CORRECT_PAUSE_HANDLING"
        return False, "PROPOSED_ACTION_DURING_PAUSE_OR_HOLD"

    if proposal.kind == "ACT":
        allowed = permissions.get(resource, [])
        if proposal.operation not in allowed:
            return False, "PROPOSED_UNPERMITTED_OPERATION"
        return True, "CORRECT_PERMITTED_OPERATION"

    if proposal.kind in ("WAIT", "FINISH", "LEARN", "PLAN", "SIMULATE", "RETRIEVE", "APPEAL"):
        return True, f"VALID_{proposal.kind}"

    return False, "INVALID_KIND"


class ActorDiagnosticHarness:
    def __init__(
        self,
        thresholds: DiagnosticThresholds | None = None,
        wording: str = "demarcated",
        constrained_decoding: bool = False,
    ):
        self.thresholds = thresholds or DiagnosticThresholds()
        self.wording = wording
        self.constrained_decoding = constrained_decoding

    def run_stage_1_minimal_proposal(
        self,
        port: FrozenModelPort,
        packet: dict[str, Any],
        n_samples: int = 5,
        seed: int = 7,
    ) -> StageResult:
        """Stage 1: Can the model return short, schema-valid proposals without copying?"""
        result = StageResult(stage="minimal_proposal", status="FAIL", calls_attempted=n_samples)
        valid_count = 0
        trunc_count = 0

        for i in range(n_samples):
            try:
                prompt_text = format_diagnostic_prompt(packet, self.wording)
                test_packet = {**packet, "_diagnostic_prompt": prompt_text}
                raw = port.generate(test_packet, seed=seed + i)
            except Exception as exc:
                raw = f"PORT_ERROR: {exc}"

            copied = is_packet_copy(raw, packet)
            valid, truncated, proposal, reason = check_minimal_proposal(raw, port.config.max_output_tokens)
            if valid and not copied:
                valid_count += 1
            if truncated:
                trunc_count += 1

            result.samples.append({
                "sample_index": i,
                "raw_text": raw[:300],
                "valid": valid and not copied,
                "copied_packet": copied,
                "truncated": truncated,
                "reason": "COPIED_PACKET" if copied else reason,
            })

        result.schema_valid_count = valid_count
        result.schema_valid_rate = valid_count / n_samples
        result.truncation_count = trunc_count
        result.truncation_rate = trunc_count / n_samples

        if result.schema_valid_rate >= self.thresholds.min_schema_valid_rate:
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
    ) -> StageResult:
        """Stage 2: Can the model select valid operations conditioned on state and permissions?"""
        n = len(test_scenarios)
        result = StageResult(stage="state_decision", status="FAIL", calls_attempted=n)
        valid_count = 0
        correct_count = 0
        trunc_count = 0

        for i, scenario in enumerate(test_scenarios):
            packet = scenario["packet"]
            try:
                raw = port.generate(packet, seed=seed + i)
            except Exception as exc:
                raw = f"PORT_ERROR: {exc}"

            valid, truncated, proposal, reason = check_minimal_proposal(raw, port.config.max_output_tokens)
            if truncated:
                trunc_count += 1
            if valid and proposal:
                valid_count += 1
                correct, eval_reason = evaluate_decision_correctness(proposal, packet)
                if correct:
                    correct_count += 1
            else:
                correct = False
                eval_reason = reason

            result.samples.append({
                "scenario_index": i,
                "scenario_name": scenario.get("name", f"scenario_{i}"),
                "raw_text": raw[:300],
                "valid": valid,
                "correct": correct,
                "reason": eval_reason,
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

    def run_stage_3_closed_loop(
        self,
        actor: Any,
        runtime: Any,
        max_turns: int = 6,
        seed: int = 7,
    ) -> StageResult:
        """Stage 3: Can the model execute in closed loop, consuming receipts and progressing?"""
        result = StageResult(stage="closed_loop", status="FAIL", calls_attempted=max_turns)
        dispatches: dict[str, int] = {}
        executed = 0

        try:
            with actor.interaction():
                for turn in range(max_turns):
                    packet = build_packet(runtime, "C0")
                    try:
                        proposal = actor.propose(packet, seed=seed + turn)
                        result.schema_valid_count += 1
                    except Exception as exc:
                        result.samples.append({"turn": turn, "status": "SCHEMA_ERROR", "error": str(exc)})
                        break

                    # Dispatch via broker
                    if proposal.kind == "ACT":
                        cmd = runtime.proposal(proposal.operation)
                        prop_res = runtime.broker.propose(cmd)
                        status = prop_res.get("status", "UNKNOWN")
                        if status == "READY":
                            disp_res = runtime.broker.dispatch(cmd.command_id)
                            status = disp_res.get("status", status)
                            if status == "ACKNOWLEDGED":
                                executed += 1
                        dispatches[status] = dispatches.get(status, 0) + 1
                        runtime.turn("WAIT")
                    elif proposal.kind == "WAIT":
                        runtime.turn("WAIT")
                        dispatches["WAIT"] = dispatches.get("WAIT", 0) + 1
                    elif proposal.kind == "FINISH":
                        finish_res = runtime.finish()
                        status = finish_res.get("status", "FINISH")
                        dispatches[status] = dispatches.get(status, 0) + 1
                        if status == "PUBLIC_CONTRACT_SATISFIED":
                            result.task_completed = True
                        break
        except Exception as exc:
            result.samples.append({"error": f"EXECUTION_FAILED: {exc}"})

        result.actions_executed = executed
        result.dispatch_outcomes = dispatches
        result.calls_attempted = sum(dispatches.values()) or 1
        result.schema_valid_rate = result.schema_valid_count / result.calls_attempted

        if executed > 0 or result.task_completed:
            result.status = "PASS"
        else:
            result.stop_rule_triggered = "ZERO_PRIMITIVE_ACTIONS_EXECUTED"

        return result

    def run_all(
        self,
        port: FrozenModelPort,
        packet: dict[str, Any],
        test_scenarios: list[dict[str, Any]],
        actor: Any = None,
        runtime: Any = None,
    ) -> dict[str, Any]:
        """Execute diagnostic stages sequentially under stop rules."""
        report: dict[str, Any] = {
            "wording": self.wording,
            "constrained_decoding": self.constrained_decoding,
            "stages": {},
            "status": "INCOMPLETE",
        }

        # Stage 1
        s1 = self.run_stage_1_minimal_proposal(port, packet)
        report["stages"]["minimal_proposal"] = asdict(s1)
        if s1.status != "PASS":
            report["status"] = "STOPPED_AT_STAGE_1"
            report["stop_reason"] = s1.stop_rule_triggered
            return report

        # Stage 2
        s2 = self.run_stage_2_state_decision(port, test_scenarios)
        report["stages"]["state_decision"] = asdict(s2)
        if s2.status != "PASS":
            report["status"] = "STOPPED_AT_STAGE_2"
            report["stop_reason"] = s2.stop_rule_triggered
            return report

        # Stage 3 (if runtime provided)
        if actor and runtime:
            s3 = self.run_stage_3_closed_loop(actor, runtime)
            report["stages"]["closed_loop"] = asdict(s3)
            report["status"] = "PASS" if s3.status == "PASS" else "STOPPED_AT_STAGE_3"
            report["stop_reason"] = s3.stop_rule_triggered
        else:
            report["status"] = "STAGES_1_AND_2_PASSED"

        return report

