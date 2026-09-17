"""Smoke-driver entry that runs the three-stage actor diagnostic."""

from protocollab.actor.diagnostic import (
    ActorDiagnosticHarness,
    DiagnosticThresholds,
    StageResult,
    build_state_decision_scenarios,
    check_minimal_proposal,
    evaluate_decision_correctness,
    format_diagnostic_prompt,
    is_packet_copy,
    run_actor_diagnostic,
)

__all__ = [
    "ActorDiagnosticHarness",
    "DiagnosticThresholds",
    "StageResult",
    "build_state_decision_scenarios",
    "check_minimal_proposal",
    "evaluate_decision_correctness",
    "format_diagnostic_prompt",
    "is_packet_copy",
    "run_actor_diagnostic",
]


def main():
    raise SystemExit("Call run_actor_diagnostic(runtime, model_config) from a smoke driver.")


if __name__ == "__main__":
    main()
