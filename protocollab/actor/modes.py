"""Three decision modes on a shared candidate space."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from protocollab.actor import (
    ActorProposal,
    FrozenModelPort,
    extract_channels,
    parse_proposal,
)
from protocollab.actor.diagnostic_config import DiagnosticConfig
from protocollab.actor.pipeline import compose_and_admit_input


class UnsupportedConfiguration(ValueError):
    """Backend does not support the requested decision mode."""


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
    # v2: per-request token deltas (§2)
    request_input_tokens: int = 0
    request_output_tokens: int = 0
    # v2: scoring semantics (§4)
    scoring_semantics: str | None = None
    # v2: claim references from evidence packet (§5)
    claim_refs: list[dict[str, Any]] = field(default_factory=list)
    # v2: backend metadata for truncation detection
    backend_meta: dict[str, Any] = field(default_factory=dict)


class DecisionAdapter:
    """Unified adapter executing free_json, constrained_json, or candidate_score."""

    def __init__(self, config: DiagnosticConfig):
        self.config = config

    def _extract_claim_refs(self, packet: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract claim references from the evidence packet."""
        return [
            {"seq": c["seq"], "payload_hash": c["payload_hash"]}
            for c in packet.get("incoming_claims", [])
        ]

    def _preflight_backend(self, port: FrozenModelPort, mode: str) -> None:
        """Verify backend supports the requested decision mode."""
        supported = getattr(port.config, "supported_decision_modes", None)
        if supported is not None and mode not in supported:
            raise UnsupportedConfiguration(
                f"Port does not support decision mode {mode!r}; supported modes: {supported}"
            )
        if mode in ("constrained_json", "candidate_score") and not self.config.candidate_registry:
            raise UnsupportedConfiguration(
                f"Decision mode {mode!r} requires a non-empty candidate_registry"
            )

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
        claim_refs = self._extract_claim_refs(packet)

        # 1. Backend capability preflight (§3) - raises UnsupportedConfiguration
        self._preflight_backend(port, mode)

        # 2. Admission attempt
        if ledger is not None:
            ledger.record_admission_attempt(stage)

        reservation = None
        try:
            # 3. Unified input composition and admission
            unformatted_prompt, formatted_prompt, evidence = compose_and_admit_input(
                packet,
                self.config.model_port_config,
                renderer=self.config.renderer,
                store=port.store,
                phase=port.phase,
            )

            # 4. Token reservation & dispatch
            if ledger is not None:
                reservation = self.config.model_port_config.max_input_tokens + self.config.model_port_config.max_output_tokens
                ledger.reserve(stage, reservation)
                ledger.record_dispatched(stage)

            # Snapshot cumulative tokens before call for delta computation (§2)
            prev_input = port.input_tokens
            prev_output = port.output_tokens

            if mode == "free_json":
                raw = port.generate(packet, seed=seed, prompt_override=unformatted_prompt)
                delta_input = port.input_tokens - prev_input
                delta_output = port.output_tokens - prev_output
                raw_text, reasoning, final_payload, policy = extract_channels(raw)
                try:
                    proposal = parse_proposal(final_payload)
                    val_outcome = "VALID"
                except Exception as exc:
                    val_outcome = type(exc).__name__
                    raise

                if ledger is not None:
                    ledger.record_completed(stage, delta_input, delta_output, reservation)

                return DecisionOutcome(
                    mode=mode,
                    proposal=proposal,
                    raw_response=raw,
                    reasoning=reasoning,
                    final_payload=final_payload,
                    extraction_policy=policy,
                    validation_outcome=val_outcome,
                    is_fallback=False,
                    total_tokens_evaluated=delta_input + delta_output,
                    decision_basis_ref=basis_ref,
                    request_input_tokens=delta_input,
                    request_output_tokens=delta_output,
                    claim_refs=claim_refs,
                    backend_meta={
                        "output_tokens": delta_output,
                        "max_output_tokens": port.config.max_output_tokens,
                    },
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
                delta_input = port.input_tokens - prev_input
                delta_output = port.output_tokens - prev_output
                raw_text, reasoning, final_payload, policy = extract_channels(raw)
                proposal = parse_proposal(final_payload)
                if ledger is not None:
                    ledger.record_completed(stage, delta_input, delta_output, reservation)

                return DecisionOutcome(
                    mode=mode,
                    proposal=proposal,
                    raw_response=raw,
                    reasoning=reasoning,
                    final_payload=final_payload,
                    extraction_policy=policy,
                    validation_outcome="VALID",
                    is_fallback=False,
                    total_tokens_evaluated=delta_input + delta_output,
                    decision_basis_ref=basis_ref,
                    request_input_tokens=delta_input,
                    request_output_tokens=delta_output,
                    claim_refs=claim_refs,
                    backend_meta={
                        "output_tokens": delta_output,
                        "max_output_tokens": port.config.max_output_tokens,
                    },
                )

            elif mode == "candidate_score":
                # Score every complete candidate continuation deterministically
                try:
                    result = port.score_candidates(
                        unformatted_prompt,
                        self.config.candidate_registry,
                        seed=seed,
                        claims=claim_refs,
                    )
                except TypeError:
                    result = port.score_candidates(
                        unformatted_prompt,
                        self.config.candidate_registry,
                        seed=seed,
                    )

                # Identity and fingerprint validation (§4)
                model_id = result.get("model_id", port.config.model_id)
                if model_id != port.config.model_id:
                    raise ValueError(
                        f"MODEL_IDENTIFIER_MISMATCH: expected {port.config.model_id!r}, "
                        f"got {model_id!r}"
                    )
                if port.config.expected_fingerprint and result.get("fingerprint") != port.config.expected_fingerprint:
                    raise ValueError(
                        f"MODEL_FINGERPRINT_CHANGED: expected {port.config.expected_fingerprint!r}, "
                        f"got {result.get('fingerprint')!r}"
                    )

                # Candidate completeness and score validation (§4)
                evaluations = result["evaluations"]
                if len(evaluations) != len(self.config.candidate_registry):
                    raise ValueError(
                        f"CANDIDATE_COUNT_MISMATCH: expected {len(self.config.candidate_registry)} "
                        f"evaluations, got {len(evaluations)}"
                    )
                for i, ev in enumerate(evaluations):
                    if not math.isfinite(ev["score"]):
                        raise ValueError(
                            f"NON_FINITE_SCORE: candidate {i} has score {ev['score']}"
                        )

                # Deterministic selection: highest log-likelihood, tie-break by candidate index
                best = max(evaluations, key=lambda e: (e["score"], -e["index"]))
                winning_json = best["candidate"]
                proposal = parse_proposal(winning_json)

                # score_candidates returns per-request tokens, no delta needed
                sc_input = result["input_tokens"]
                sc_output = result["output_tokens"]
                if ledger is not None:
                    ledger.record_completed(stage, sc_input, sc_output, reservation)

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
                    total_tokens_evaluated=sc_input + sc_output,
                    decision_basis_ref=basis_ref,
                    request_input_tokens=sc_input,
                    request_output_tokens=sc_output,
                    scoring_semantics="full_continuation_log_likelihood",
                    claim_refs=claim_refs,
                    backend_meta={
                        "output_tokens": sc_output,
                        "max_output_tokens": port.config.max_output_tokens,
                    },
                )

            else:
                raise ValueError(f"UNSUPPORTED_DECISION_MODE: {mode}")

        except Exception as exc:
            if ledger is not None and reservation is not None:
                ledger.record_failed(stage, reservation)

            # Build backend_meta and classify outcome into lifecycle states
            from protocollab.learning import BudgetExhausted
            error_meta: dict[str, Any] = {}
            if "formatted_input_byte_bound" in str(exc):
                val_outcome = "ADMISSION_REJECTED"
                error_meta = {"error_type": "AdmissionRejected", "reason": "formatted_input_byte_bound"}
            elif isinstance(exc, BudgetExhausted):
                val_outcome = "BUDGET_EXHAUSTED"
                error_meta = {"stop_reason": "token_limit_reached"}
            else:
                val_outcome = "INFERENCE_FAILED"
                error_meta = {"error_type": type(exc).__name__}

            # Record fallback when execution fails
            fallback_prop = ActorProposal(kind="WAIT")
            return DecisionOutcome(
                mode=mode,
                proposal=fallback_prop,
                raw_response=f"FALLBACK: {type(exc).__name__}: {exc}",
                reasoning=None,
                final_payload='{"kind":"WAIT"}',
                extraction_policy="fallback",
                validation_outcome=val_outcome,
                is_fallback=True,
                total_tokens_evaluated=0,
                decision_basis_ref=basis_ref,
                claim_refs=claim_refs,
                backend_meta=error_meta,
            )
