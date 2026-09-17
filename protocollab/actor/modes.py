"""Three decision modes on a shared candidate space."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from protocollab.actor import (
    ActorProposal,
    FrozenModelPort,
    extract_channels,
    parse_proposal,
)
from protocollab.actor.diagnostic_config import DiagnosticConfig
from protocollab.actor.pipeline import compose_and_admit_input


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate_json: str
    log_likelihood: float
    token_count: int
    proposal: ActorProposal


@dataclass
class DecisionOutcome:
    mode: Literal["free_json", "constrained_json", "candidate_score"]
    proposal: ActorProposal
    raw_response: str
    reasoning: str | None
    final_payload: str
    extraction_policy: str
    validation_outcome: str
    is_fallback: bool
    candidate_evaluations: list[dict[str, Any]] | None = None
    total_tokens_evaluated: int = 0
    decision_basis_ref: str = ""


class DecisionAdapter:
    """Unified adapter executing free_json, constrained_json, or candidate_score."""

    def __init__(self, config: DiagnosticConfig):
        self.config = config

    def decide(
        self,
        port: FrozenModelPort,
        packet: dict[str, Any],
        seed: int,
        ledger: Any = None,
        stage: str = "closed_loop",
    ) -> DecisionOutcome:
        basis_ref = packet.get("decision_basis_ref", "")
        mode = self.config.decision_mode

        if ledger is not None:
            ledger.record_admission_attempt(stage)

        # Compose input through unified pipeline
        unformatted_prompt, formatted_prompt, evidence = compose_and_admit_input(
            packet,
            self.config.model_port_config,
            renderer=self.config.renderer,
            store=port.store,
            phase=port.phase,
        )

        if ledger is not None:
            reservation = self.config.model_port_config.max_input_tokens + self.config.model_port_config.max_output_tokens
            ledger.reserve(stage, reservation)
            ledger.record_dispatched(stage)

        try:
            if mode == "free_json":
                raw = port.generate(packet, seed=seed, prompt_override=unformatted_prompt)
                raw_text, reasoning, final_payload, policy = extract_channels(raw)
                try:
                    proposal = parse_proposal(final_payload)
                    val_outcome = "VALID"
                except Exception as exc:
                    val_outcome = type(exc).__name__
                    raise

                if ledger is not None:
                    ledger.record_completed(stage, port.input_tokens, port.output_tokens, reservation)

                return DecisionOutcome(
                    mode=mode,
                    proposal=proposal,
                    raw_response=raw,
                    reasoning=reasoning,
                    final_payload=final_payload,
                    extraction_policy=policy,
                    validation_outcome=val_outcome,
                    is_fallback=False,
                    total_tokens_evaluated=port.input_tokens + port.output_tokens,
                    decision_basis_ref=basis_ref,
                )

            elif mode == "constrained_json":
                # Constrained generation to the declared finite proposal set
                raw = port.generate(
                    packet,
                    seed=seed,
                    prompt_override=unformatted_prompt,
                    decision_mode="constrained_json",
                    candidates=self.config.candidate_registry,
                )
                raw_text, reasoning, final_payload, policy = extract_channels(raw)
                proposal = parse_proposal(final_payload)
                if ledger is not None:
                    ledger.record_completed(stage, port.input_tokens, port.output_tokens, reservation)

                return DecisionOutcome(
                    mode=mode,
                    proposal=proposal,
                    raw_response=raw,
                    reasoning=reasoning,
                    final_payload=final_payload,
                    extraction_policy=policy,
                    validation_outcome="VALID",
                    is_fallback=False,
                    total_tokens_evaluated=port.input_tokens + port.output_tokens,
                    decision_basis_ref=basis_ref,
                )

            elif mode == "candidate_score":
                # Score every complete candidate continuation deterministically
                result = port.score_candidates(
                    unformatted_prompt,
                    self.config.candidate_registry,
                    seed=seed,
                )
                # Deterministic selection: highest log-likelihood, tie-break by candidate index
                evaluations = result["evaluations"]
                best = max(evaluations, key=lambda e: (e["score"], -e["index"]))
                winning_json = best["candidate"]
                proposal = parse_proposal(winning_json)

                if ledger is not None:
                    ledger.record_completed(stage, result["input_tokens"], result["output_tokens"], reservation)

                return DecisionOutcome(
                    mode=mode,
                    proposal=proposal,
                    raw_response=winning_json,
                    reasoning=None,
                    final_payload=winning_json,
                    extraction_policy="candidate_score/v1",
                    validation_outcome="VALID",
                    is_fallback=False,
                    candidate_evaluations=evaluations,
                    total_tokens_evaluated=result["input_tokens"] + result["output_tokens"],
                    decision_basis_ref=basis_ref,
                )

            else:
                raise ValueError(f"UNSUPPORTED_DECISION_MODE: {mode}")

        except Exception as exc:
            if ledger is not None:
                ledger.record_failed(stage, reservation)
            # Record fallback when execution fails
            fallback_prop = ActorProposal(kind="WAIT")
            return DecisionOutcome(
                mode=mode,
                proposal=fallback_prop,
                raw_response=f"FALLBACK: {type(exc).__name__}: {exc}",
                reasoning=None,
                final_payload='{"kind":"WAIT"}',
                extraction_policy="fallback",
                validation_outcome="FALLBACK",
                is_fallback=True,
                total_tokens_evaluated=0,
                decision_basis_ref=basis_ref,
            )

