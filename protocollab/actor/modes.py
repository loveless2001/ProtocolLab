"""Three decision modes on a shared candidate space."""

from __future__ import annotations

import inspect
import math
import uuid
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
from protocollab.contracts import canonical, digest


class UnsupportedConfiguration(ValueError):
    """Backend does not support the requested decision mode."""


@dataclass(frozen=True)
class InferenceResult:
    dispatched: bool
    delivered: bool
    completed: bool
    input_tokens: int
    output_tokens: int
    raw_response: str
    stop_reason: str | None = None
    compute_meta: dict[str, Any] = field(default_factory=dict)
    evaluations: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class ProposalResult:
    channels_extracted: bool
    schema_valid: bool
    candidate_membership: bool
    proposal: ActorProposal | None
    validation_outcome: str
    reasoning: str | None = None
    final_payload: str = ""
    extraction_policy: str = "default"
    reason: str | None = None


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


def is_transport_timeout_or_drop(exc: Exception) -> bool:
    """Classify exceptions indicating network timeout, connection drop, or subprocess timeout after dispatch."""
    import subprocess
    import urllib.error

    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired, ConnectionError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, ConnectionError, OSError)):
            return True
        if "timed out" in str(reason).lower() or "connection" in str(reason).lower():
            return True
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "connection" in name or "timeouterror" in msg or "timed out" in msg:
        return True
    if getattr(exc, "__cause__", None) and is_transport_timeout_or_drop(exc.__cause__):
        return True
    if getattr(exc, "__context__", None) and is_transport_timeout_or_drop(exc.__context__):
        return True
    return False


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

    def _preflight_backend(self, port: FrozenModelPort, mode: str, claims: list[dict[str, Any]] | None = None) -> None:
        """Verify backend supports the requested decision mode and capabilities."""
        supported = getattr(port.config, "supported_decision_modes", None)
        if supported is not None and mode not in supported:
            raise UnsupportedConfiguration(
                f"Port does not support decision mode {mode!r}; supported modes: {supported}"
            )

        if mode == "candidate_score":
            if not getattr(port.config, "supports_candidate_scoring_v1", True):
                raise UnsupportedConfiguration(
                    "Port does not support candidate_score (supports_candidate_scoring_v1 is False)"
                )
            if not self.config.candidate_registry:
                raise UnsupportedConfiguration(
                    "Decision mode 'candidate_score' requires a non-empty candidate_registry"
                )
        elif mode == "constrained_json":
            if not getattr(port.config, "supports_constrained_candidates_v1", True):
                raise UnsupportedConfiguration(
                    "Port does not support constrained_json (supports_constrained_candidates_v1 is False)"
                )
            if not self.config.candidate_registry:
                raise UnsupportedConfiguration(
                    "Decision mode 'constrained_json' requires a non-empty candidate_registry"
                )

        if claims and not getattr(port.config, "supports_claim_evidence", True):
            raise UnsupportedConfiguration(
                "Port does not support claim evidence (supports_claim_evidence is False)"
            )

    def decide(
        self,
        port: FrozenModelPort,
        packet: dict[str, Any],
        seed: int,
        ledger: Any = None,
        stage: str = "closed_loop",
        req_id: str | None = None,
    ) -> DecisionOutcome:
        basis_ref = packet.get("decision_basis_ref", "")
        mode = self.config.decision_mode
        claim_refs = self._extract_claim_refs(packet)

        # 1. Backend capability preflight (§3) - raises UnsupportedConfiguration upfront
        self._preflight_backend(port, mode, claims=claim_refs)

        # 2. Admission attempt
        if ledger is not None:
            ledger.record_admission_attempt(stage)

        reservation = None
        assigned_req_id = req_id
        prev_input = getattr(port, "input_tokens", 0)
        prev_output = getattr(port, "output_tokens", 0)
        sc_input = 0
        sc_output = 0
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
            if assigned_req_id is None:
                assigned_req_id = f"req_{digest(formatted_prompt)[:12]}_{uuid.uuid4().hex[:8]}"
            req_id = assigned_req_id
            max_input = self.config.model_port_config.max_input_tokens
            max_output = self.config.model_port_config.max_output_tokens
            if ledger is not None:
                reservation = max_input + max_output
                v_res = ledger.reserve(
                    stage,
                    reservation,
                    req_id=req_id,
                    basis_ref=basis_ref,
                    max_input=max_input,
                    max_output=max_output,
                )
                v_disp = ledger.record_dispatched(stage, req_id=req_id)
                res_kind = getattr(v_res, "value", v_res)
                disp_kind = getattr(v_disp, "value", v_disp)
                if res_kind == "DuplicateNoop" or disp_kind == "DuplicateNoop":
                    raise ValueError(
                        f"DUPLICATE_REQUEST_DISPATCH: req_id {req_id!r} has already been reserved or dispatched"
                    )

            # Snapshot cumulative tokens before call for delta computation (§2)
            prev_input = port.input_tokens
            prev_output = port.output_tokens

            # 5. Phase 1: Inference Execution (§2)
            if mode == "free_json":
                raw = port.generate(packet, seed=seed, prompt_override=unformatted_prompt)
                delta_input = port.input_tokens - prev_input
                delta_output = port.output_tokens - prev_output
                inf_res = InferenceResult(
                    dispatched=True,
                    delivered=True,
                    completed=True,
                    input_tokens=delta_input,
                    output_tokens=delta_output,
                    raw_response=raw,
                    compute_meta={
                        "logical_decisions": 1,
                        "candidate_evaluations": 1,
                        "prompt_tokens_logically_supplied": delta_input,
                        "total_tokens_processed": delta_input + delta_output,
                        "forward_passes": 1,
                        "prefill_recomputations": 0,
                    },
                )

            elif mode == "constrained_json":
                raw = port.generate(
                    packet,
                    seed=seed,
                    prompt_override=unformatted_prompt,
                    decision_mode="constrained_json",
                    candidates=self.config.candidate_registry,
                )
                delta_input = port.input_tokens - prev_input
                delta_output = port.output_tokens - prev_output
                inf_res = InferenceResult(
                    dispatched=True,
                    delivered=True,
                    completed=True,
                    input_tokens=delta_input,
                    output_tokens=delta_output,
                    raw_response=raw,
                    compute_meta={
                        "logical_decisions": 1,
                        "candidate_evaluations": 1,
                        "prompt_tokens_logically_supplied": delta_input,
                        "total_tokens_processed": delta_input + delta_output,
                        "forward_passes": 1,
                        "prefill_recomputations": 0,
                    },
                )

            elif mode == "candidate_score":
                score_kwargs: dict[str, Any] = {"seed": seed}
                sig = inspect.signature(port.score_candidates)
                if "claims" in sig.parameters:
                    score_kwargs["claims"] = packet.get("incoming_claims", [])
                result = port.score_candidates(
                    unformatted_prompt,
                    self.config.candidate_registry,
                    **score_kwargs,
                )

                sc_input = result.get("input_tokens", 0) if isinstance(result, dict) else 0
                sc_output = result.get("output_tokens", 0) if isinstance(result, dict) else 0

                # Strict Identity and fingerprint validation (§4)
                if not isinstance(result, dict) or "model_id" not in result:
                    raise ValueError("MODEL_IDENTIFIER_MISSING: result must include model_id")
                if result["model_id"] != port.config.model_id:
                    raise ValueError(
                        f"MODEL_IDENTIFIER_MISMATCH: expected {port.config.model_id!r}, "
                        f"got {result['model_id']!r}"
                    )
                if (
                    port.config.expected_fingerprint
                    and result.get("fingerprint") != port.config.expected_fingerprint
                ):
                    raise ValueError(
                        f"MODEL_FINGERPRINT_CHANGED: expected {port.config.expected_fingerprint!r}, "
                        f"got {result.get('fingerprint')!r}"
                    )

                # Strict Candidate completeness and score validation (§4)
                evaluations = result.get("evaluations")
                if not isinstance(evaluations, list):
                    raise ValueError("INVALID_EVALUATIONS: evaluations must be a list")
                if len(evaluations) != len(self.config.candidate_registry):
                    raise ValueError(
                        f"CANDIDATE_COUNT_MISMATCH: expected {len(self.config.candidate_registry)} "
                        f"evaluations, got {len(evaluations)}"
                    )

                eval_cands = [ev.get("candidate") for ev in evaluations]
                if set(eval_cands) != set(self.config.candidate_registry):
                    raise ValueError(
                        "CANDIDATE_SET_MISMATCH: evaluated candidate set does not equal candidate_registry"
                    )

                for i, ev in enumerate(evaluations):
                    if ev.get("index") != i:
                        raise ValueError(f"INDEX_MISMATCH: candidate at index {i} has index {ev.get('index')}")
                    if ev.get("candidate") != self.config.candidate_registry[i]:
                        raise ValueError(f"CANDIDATE_ORDER_MISMATCH: candidate at index {i} does not match registry")
                    score = ev.get("score")
                    if score is None or not math.isfinite(score):
                        raise ValueError(f"NON_FINITE_SCORE: candidate {i} has non-finite score {score}")
                    tcount = ev.get("token_count")
                    if tcount is None:
                        tcount = ev.get("tokens")
                    if not isinstance(tcount, int) or tcount < 0:
                        raise ValueError(f"INVALID_TOKEN_COUNT: candidate {i} has invalid token_count {tcount}")
                    ev["token_count"] = tcount

                best = max(evaluations, key=lambda e: (e["score"], -e["index"]))
                winning_json = best["candidate"]
                compute_meta = result.get("compute", {
                    "logical_decisions": 1,
                    "candidate_evaluations": len(self.config.candidate_registry),
                    "prompt_tokens_logically_supplied": sc_input,
                    "total_tokens_processed": sc_input + sc_output,
                    "forward_passes": len(self.config.candidate_registry),
                    "prefill_recomputations": len(self.config.candidate_registry),
                })
                inf_res = InferenceResult(
                    dispatched=True,
                    delivered=True,
                    completed=True,
                    input_tokens=sc_input,
                    output_tokens=sc_output,
                    raw_response=winning_json,
                    compute_meta=compute_meta,
                    evaluations=evaluations,
                )
            else:
                raise ValueError(f"UNSUPPORTED_DECISION_MODE: {mode}")

        except Exception as exc:
            # Inference stage failed (admission, budget, transport, or candidate validation)
            delta_input = (getattr(port, "input_tokens", 0) - prev_input) if hasattr(port, "input_tokens") else 0
            delta_output = (getattr(port, "output_tokens", 0) - prev_output) if hasattr(port, "output_tokens") else 0
            if sc_input > 0:
                delta_input = sc_input
            if sc_output > 0:
                delta_output = sc_output

            is_duplicate = "DUPLICATE_REQUEST_DISPATCH" in str(exc)

            if ledger is not None and reservation is not None and not is_duplicate:
                if delta_input > 0 or delta_output > 0:
                    # Model inference executed and consumed physical tokens before failure occurred.
                    # Settle consumed usage on ledger so physical consumption is not unbilled.
                    receipt_hash = digest(
                        f"{req_id}:{delta_input}:{delta_output}:FAILURE:{type(exc).__name__}:{exc}"
                    )
                    ledger.record_completed(
                        stage,
                        delta_input,
                        delta_output,
                        reservation,
                        req_id=req_id,
                        receipt_hash=receipt_hash,
                    )
                elif is_transport_timeout_or_drop(exc):
                    if hasattr(ledger, "record_timeout"):
                        ledger.record_timeout(
                            stage,
                            reservation,
                            req_id=req_id,
                            reason=f"timeout:{type(exc).__name__}:{exc}",
                        )
                    else:
                        ev_hash = digest(f"{req_id or 'unknown'}:TIMEOUT:{type(exc).__name__}:{exc}")
                        ledger.record_failed(
                            stage,
                            reservation,
                            req_id=req_id,
                            evidence_hash=ev_hash,
                        )
                else:
                    ev_hash = digest(f"{req_id or 'unknown'}:FAILED:{type(exc).__name__}:{exc}")
                    ledger.record_failed(
                        stage,
                        reservation,
                        req_id=req_id,
                        evidence_hash=ev_hash,
                    )

            from protocollab.learning import BudgetExhausted
            error_meta: dict[str, Any] = {}
            if "formatted_input_byte_bound" in str(exc):
                val_outcome = "ADMISSION_REJECTED"
                error_meta = {"error_type": "AdmissionRejected", "reason": "formatted_input_byte_bound"}
            elif is_duplicate:
                val_outcome = "DUPLICATE_REQUEST"
                error_meta = {"error_type": "DuplicateRequest", "reason": str(exc)}
            elif isinstance(exc, BudgetExhausted):
                val_outcome = "BUDGET_EXHAUSTED"
                error_meta = {"stop_reason": "token_limit_reached"}
            elif isinstance(exc, UnsupportedConfiguration):
                raise
            else:
                val_outcome = "INFERENCE_FAILED"
                error_meta = {"error_type": type(exc).__name__, "reason": str(exc)}

            return DecisionOutcome(
                mode=mode,
                proposal=ActorProposal(kind="WAIT"),
                raw_response=f"FALLBACK: {type(exc).__name__}: {exc}",
                reasoning=None,
                final_payload='{"kind":"WAIT"}',
                extraction_policy="fallback",
                validation_outcome=val_outcome,
                is_fallback=True,
                total_tokens_evaluated=delta_input + delta_output,
                decision_basis_ref=basis_ref,
                claim_refs=claim_refs,
                backend_meta=error_meta,
            )

        # 6. Settle inference usage immediately on ledger (§2)
        if ledger is not None:
            raw_val = inf_res.raw_response
            resp_str = raw_val if isinstance(raw_val, str) else canonical(raw_val).decode("utf-8")
            resp_hash = digest(resp_str)
            receipt_hash = digest(
                f"{req_id}:{inf_res.input_tokens}:{inf_res.output_tokens}:{resp_hash}"
            )
            ledger.record_completed(
                stage,
                inf_res.input_tokens,
                inf_res.output_tokens,
                reservation,
                req_id=req_id,
                receipt_hash=receipt_hash,
            )

        # 7. Phase 2: Proposal Parsing and Registry Membership Validation (§2, §4)
        if mode in ("free_json", "constrained_json"):
            raw_text, reasoning, final_payload, policy = extract_channels(inf_res.raw_response)
            try:
                proposal = parse_proposal(final_payload)
                prop_dict: dict[str, Any] = {"kind": proposal.kind}
                if proposal.operation is not None:
                    prop_dict["operation"] = proposal.operation
                canon_str = canonical(prop_dict).decode("utf-8")

                if self.config.candidate_registry and canon_str not in self.config.candidate_registry:
                    prop_res = ProposalResult(
                        channels_extracted=True,
                        schema_valid=False,
                        candidate_membership=False,
                        proposal=None,
                        validation_outcome="NOT_IN_CANDIDATE_REGISTRY",
                        reasoning=reasoning,
                        final_payload=final_payload,
                        extraction_policy=policy,
                        reason=f"Proposal {canon_str} not in candidate_registry",
                    )
                else:
                    prop_res = ProposalResult(
                        channels_extracted=True,
                        schema_valid=True,
                        candidate_membership=True,
                        proposal=proposal,
                        validation_outcome="VALID",
                        reasoning=reasoning,
                        final_payload=final_payload,
                        extraction_policy=policy,
                    )
            except Exception as exc:
                prop_res = ProposalResult(
                    channels_extracted=True,
                    schema_valid=False,
                    candidate_membership=False,
                    proposal=None,
                    validation_outcome=type(exc).__name__,
                    reasoning=reasoning,
                    final_payload=final_payload,
                    extraction_policy=policy,
                    reason=str(exc),
                )
        else:
            # candidate_score
            winning_json = inf_res.raw_response
            proposal = parse_proposal(winning_json)
            prop_res = ProposalResult(
                channels_extracted=True,
                schema_valid=True,
                candidate_membership=True,
                proposal=proposal,
                validation_outcome="VALID",
                reasoning=None,
                final_payload=winning_json,
                extraction_policy="candidate_score/v1",
            )

        is_fallback = not prop_res.schema_valid or prop_res.proposal is None
        final_proposal = prop_res.proposal if not is_fallback else ActorProposal(kind="WAIT")

        return DecisionOutcome(
            mode=mode,
            proposal=final_proposal,
            raw_response=inf_res.raw_response,
            reasoning=prop_res.reasoning,
            final_payload=prop_res.final_payload,
            extraction_policy=prop_res.extraction_policy,
            validation_outcome=prop_res.validation_outcome,
            is_fallback=is_fallback,
            candidate_evaluations=inf_res.evaluations,
            total_tokens_evaluated=inf_res.input_tokens + inf_res.output_tokens,
            decision_basis_ref=basis_ref,
            request_input_tokens=inf_res.input_tokens,
            request_output_tokens=inf_res.output_tokens,
            scoring_semantics="full_continuation_log_likelihood" if mode == "candidate_score" else None,
            claim_refs=claim_refs,
            backend_meta={
                "output_tokens": inf_res.output_tokens,
                "max_output_tokens": port.config.max_output_tokens,
                **inf_res.compute_meta,
            },
        )
