"""Lifecycle owner coordinating verified Bend kernel and effectful Python shell."""

from __future__ import annotations
import logging
from typing import Any
import uuid

from protocollab.actor.budget import (
    DiagnosticBudgetAllocation,
    DiagnosticLedger,
    DiagnosticLedgerState,
    StageBudgetLimits,
    StageUsageRecord,
)
from protocollab.learning import BudgetExhausted
from protocollab.verified.bridge import VerifiedKernelBridge
from protocollab.verified.codec import decode_state, encode_state
from protocollab.verified.protocol import (
    ChargeKind,
    LedgerState,
    LifecycleMode,
    StageLimit,
    TransportState,
    VerdictKind,
)

logger = logging.getLogger(__name__)


class VerifiedLifecycleOwner:
    """Owner of inference-request lifecycle and budget accounting.

    Can operate in SHADOW mode (default, legacy DiagnosticLedger authoritative,
    verified Bend kernel run in parallel for auditing) or AUTHORITATIVE mode
    (verified Bend kernel is single source of truth).
    """

    def __init__(
        self,
        store: Any,
        run_id: str,
        allocation: DiagnosticBudgetAllocation,
        config_hash: str,
        mode: LifecycleMode = LifecycleMode.SHADOW,
        bridge: VerifiedKernelBridge | None = None,
    ):
        self.store = store
        self.run_id = run_id
        self.allocation = allocation
        self.config_hash = config_hash
        self.mode = mode
        self.bridge = bridge or VerifiedKernelBridge.get_default()

        # Legacy ledger maintained for shadow comparison / legacy authoritativeness
        self.legacy_ledger = DiagnosticLedger(store, run_id, allocation, config_hash)

        # Active request ID tracking for stage-level legacy calls
        self._current_req_id: str | None = None
        self._request_stages: dict[str, str] = {}
        self._stage_active_req_ids: dict[str, list[str]] = {}
        self._stage_last_completed: dict[str, str] = {}

        # Initialize or load verified ledger state
        stored = store.get("verified_lifecycle_ledger", run_id)
        if stored is None:
            stage_limits = [
                StageLimit(
                    stage=s_name,
                    max_calls=s_lim.max_calls,
                    max_tokens=s_lim.max_tokens,
                )
                for s_name, s_lim in allocation.stages.items()
            ]
            try:
                self.bend_state = self.bridge.init(
                    run_id=run_id,
                    config_hash=config_hash,
                    agg_max_calls=allocation.aggregate_max_calls,
                    agg_max_tokens=allocation.aggregate_max_tokens,
                    stage_limits=stage_limits,
                )
                store.set(
                    "verified_lifecycle_ledger",
                    run_id,
                    encode_state(self.bend_state),
                    "verified.lifecycle_initialized",
                )
            except Exception as init_err:
                if self.mode == LifecycleMode.SHADOW:
                    logger.warning("Verified shadow kernel initialization failed: %s", init_err)
                    self.bend_state = LedgerState(
                        run_id=run_id,
                        config_hash=config_hash,
                        agg_max_calls=allocation.aggregate_max_calls,
                        agg_max_tokens=allocation.aggregate_max_tokens,
                        stage_limits=stage_limits,
                    )
                else:
                    raise init_err
        else:
            self.bend_state = decode_state(stored)

    @property
    def state(self) -> DiagnosticLedgerState:
        """Project the current verified Bend state as a DiagnosticLedgerState."""
        if self.mode == LifecycleMode.SHADOW:
            return self.legacy_ledger.state
        return self.project_diagnostic_state()

    def project_diagnostic_state(self) -> DiagnosticLedgerState:
        """Direct projection from Bend LedgerState into DiagnosticLedgerState."""
        stages: dict[str, StageUsageRecord] = {
            s: StageUsageRecord() for s in self.allocation.stages
        }
        agg = StageUsageRecord()

        for req in self.bend_state.requests:
            s_rec = stages.get(req.stage)
            if s_rec is None:
                s_rec = StageUsageRecord()
                stages[req.stage] = s_rec

            s_rec.calls_attempted += 1
            agg.calls_attempted += 1

            s_rec.reservations += 1
            agg.reservations += 1

            if req.transport in (
                TransportState.DISPATCHED_INTENT,
                TransportState.SENT,
                TransportState.RESPONSE_RECEIVED,
                TransportState.OUTCOME_UNKNOWN,
            ):
                s_rec.dispatched_inference += 1
                agg.dispatched_inference += 1

            if req.charge.kind == ChargeKind.PENDING:
                held = req.charge.tokens or 0
                s_rec.reserved_tokens += held
                agg.reserved_tokens += held
            elif req.charge.kind == ChargeKind.SETTLED:
                s_rec.completed_calls += 1
                agg.completed_calls += 1
                inp = req.charge.input_tokens or 0
                out = req.charge.output_tokens or 0
                s_rec.input_tokens += inp
                s_rec.output_tokens += out
                agg.input_tokens += inp
                agg.output_tokens += out
            elif req.charge.kind == ChargeKind.RELEASED:
                s_rec.failures += 1
                agg.failures += 1

        return DiagnosticLedgerState(
            run_id=self.bend_state.run_id,
            config_hash=self.bend_state.config_hash,
            allocation=self.allocation,
            stages=stages,
            aggregate=agg,
        )

    def _sync(self, event_kind: str = "verified.lifecycle_updated"):
        encoded = encode_state(self.bend_state)
        if hasattr(self.store, "lock"):
            with self.store.lock:
                self.store.set(
                    "verified_lifecycle_ledger",
                    self.run_id,
                    encoded,
                    event_kind,
                )
        else:
            self.store.set(
                "verified_lifecycle_ledger",
                self.run_id,
                encoded,
                event_kind,
            )

    def record_admission_attempt(self, stage: str):
        if self.mode == LifecycleMode.SHADOW:
            self.legacy_ledger.record_admission_attempt(stage)
        else:
            stage_limits = self.allocation.stages.get(stage)
            if stage_limits is None:
                raise ValueError(f"UNDECLARED_DIAGNOSTIC_STAGE: {stage}")
            proj = self.project_diagnostic_state()
            s_rec = proj.stages.get(stage, StageUsageRecord())
            if s_rec.calls_attempted >= stage_limits.max_calls:
                raise BudgetExhausted(f"stage_{stage}_max_calls_exceeded")
            if proj.aggregate.calls_attempted >= self.allocation.aggregate_max_calls:
                raise BudgetExhausted("aggregate_diagnostic_max_calls_exceeded")

    def reserve(
        self,
        stage: str,
        reservation_tokens: int,
        req_id: str | None = None,
        basis_ref: str = "",
        max_input: int | None = None,
        max_output: int | None = None,
    ):
        if req_id is None:
            req_id = f"req_{uuid.uuid4().hex[:12]}"
        self._current_req_id = req_id
        self._request_stages[req_id] = stage
        stage_queue = self._stage_active_req_ids.setdefault(stage, [])
        if req_id not in stage_queue:
            stage_queue.append(req_id)

        if max_input is None or max_output is None:
            mi = reservation_tokens // 2
            mo = reservation_tokens - mi
        else:
            mi = max_input
            mo = max_output

        ev = {
            "kind": "Reserve",
            "req_id": req_id,
            "stage": stage,
            "basis_ref": basis_ref,
            "config_hash": self.config_hash,
            "max_input": mi,
            "max_output": mo,
        }

        # In shadow mode, run legacy ledger
        if self.mode == LifecycleMode.SHADOW:
            try:
                self.legacy_ledger.reserve(stage, reservation_tokens)
            except Exception as legacy_err:
                # Also execute shadow transition
                try:
                    res = self.bridge.apply(self.bend_state, ev)
                    self.bend_state = res.state
                    self._sync("verified.call_reserved_shadow")
                except Exception as shadow_err:
                    logger.warning("Verified shadow kernel reserve failed: %s", shadow_err)
                raise legacy_err

            # Shadow transition
            try:
                res = self.bridge.apply(self.bend_state, ev)
                self.bend_state = res.state
                self._sync("verified.call_reserved_shadow")
            except Exception as shadow_err:
                logger.warning("Verified shadow kernel reserve failed: %s", shadow_err)
        else:
            res = self.bridge.apply(self.bend_state, ev)
            self.bend_state = res.state
            self._sync("verified.call_reserved")
            if res.verdict.kind == VerdictKind.REJECTED:
                raise BudgetExhausted(res.verdict.reason or "REJECTED")
            if res.verdict.kind == VerdictKind.CONFLICT_FAULT:
                raise RuntimeError(res.verdict.reason or "CONFLICT_FAULT")

    def record_dispatched(self, stage: str, req_id: str | None = None):
        target_id = req_id
        if target_id is None:
            stage_queue = self._stage_active_req_ids.get(stage)
            if stage_queue:
                target_id = stage_queue[0]
            elif stage in self._stage_last_completed:
                target_id = self._stage_last_completed[stage]
            elif self._current_req_id and self._request_stages.get(self._current_req_id) == stage:
                target_id = self._current_req_id
            else:
                target_id = f"req_{uuid.uuid4().hex[:12]}"

        ev = {"kind": "DispatchIntent", "req_id": target_id}

        if self.mode == LifecycleMode.SHADOW:
            self.legacy_ledger.record_dispatched(stage)
            try:
                res = self.bridge.apply(self.bend_state, ev)
                self.bend_state = res.state
                self._sync("verified.call_dispatched_shadow")
            except Exception as shadow_err:
                logger.warning("Verified shadow kernel record_dispatched failed: %s", shadow_err)
        else:
            res = self.bridge.apply(self.bend_state, ev)
            self.bend_state = res.state
            self._sync("verified.call_dispatched")
            if res.verdict.kind == VerdictKind.REJECTED:
                raise BudgetExhausted(res.verdict.reason or "REJECTED")
            if res.verdict.kind == VerdictKind.CONFLICT_FAULT:
                raise RuntimeError(res.verdict.reason or "CONFLICT_FAULT")

    def record_completed(
        self,
        stage: str,
        input_tokens: int,
        output_tokens: int,
        reservation_tokens: int,
        req_id: str | None = None,
        receipt_hash: str = "receipt_default",
    ):
        target_id = req_id
        if target_id is None:
            stage_queue = self._stage_active_req_ids.get(stage)
            if stage_queue:
                target_id = stage_queue.pop(0)
            elif stage in self._stage_last_completed:
                target_id = self._stage_last_completed[stage]
            elif self._current_req_id and self._request_stages.get(self._current_req_id) == stage:
                target_id = self._current_req_id
            else:
                target_id = f"req_{uuid.uuid4().hex[:12]}"
        else:
            stage_queue = self._stage_active_req_ids.get(stage)
            if stage_queue and target_id in stage_queue:
                stage_queue.remove(target_id)

        self._stage_last_completed[stage] = target_id

        ev = {
            "kind": "SettleUsage",
            "req_id": target_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "receipt_hash": receipt_hash,
        }

        if self.mode == LifecycleMode.SHADOW:
            self.legacy_ledger.record_completed(
                stage, input_tokens, output_tokens, reservation_tokens
            )
            try:
                res = self.bridge.apply(self.bend_state, ev)
                self.bend_state = res.state
                self._sync("verified.call_completed_shadow")
            except Exception as shadow_err:
                logger.warning("Verified shadow kernel record_completed failed: %s", shadow_err)
        else:
            res = self.bridge.apply(self.bend_state, ev)
            self.bend_state = res.state
            self._sync("verified.call_completed")
            if res.verdict.kind == VerdictKind.CONFLICT_FAULT:
                raise RuntimeError(res.verdict.reason or "CONFLICT_FAULT")
            if res.verdict.kind == VerdictKind.REJECTED:
                raise BudgetExhausted(res.verdict.reason or "REJECTED")

    def record_failed(
        self,
        stage: str,
        reservation_tokens: int,
        req_id: str | None = None,
        evidence_hash: str = "conclusive_failure",
    ):
        target_id = req_id
        if target_id is None:
            stage_queue = self._stage_active_req_ids.get(stage)
            if stage_queue:
                target_id = stage_queue.pop(0)
            elif stage in self._stage_last_completed:
                target_id = self._stage_last_completed[stage]
            elif self._current_req_id and self._request_stages.get(self._current_req_id) == stage:
                target_id = self._current_req_id
            else:
                target_id = f"req_{uuid.uuid4().hex[:12]}"
        else:
            stage_queue = self._stage_active_req_ids.get(stage)
            if stage_queue and target_id in stage_queue:
                stage_queue.remove(target_id)

        self._stage_last_completed[stage] = target_id

        ev = {
            "kind": "FailureConclusive",
            "req_id": target_id,
            "evidence_hash": evidence_hash,
        }

        if self.mode == LifecycleMode.SHADOW:
            self.legacy_ledger.record_failed(stage, reservation_tokens)
            try:
                res = self.bridge.apply(self.bend_state, ev)
                self.bend_state = res.state
                self._sync("verified.call_failed_shadow")
            except Exception as shadow_err:
                logger.warning("Verified shadow kernel record_failed failed: %s", shadow_err)
        else:
            res = self.bridge.apply(self.bend_state, ev)
            self.bend_state = res.state
            self._sync("verified.call_failed")
            if res.verdict.kind == VerdictKind.CONFLICT_FAULT:
                raise RuntimeError(res.verdict.reason or "CONFLICT_FAULT")
            if res.verdict.kind == VerdictKind.REJECTED:
                raise BudgetExhausted(res.verdict.reason or "REJECTED")

    def record_timeout(
        self,
        stage: str,
        reservation_tokens: int,
        req_id: str | None = None,
        reason: str = "timeout",
    ):
        target_id = req_id
        if target_id is None:
            stage_queue = self._stage_active_req_ids.get(stage)
            if stage_queue:
                target_id = stage_queue.pop(0)
            elif stage in self._stage_last_completed:
                target_id = self._stage_last_completed[stage]
            elif self._current_req_id and self._request_stages.get(self._current_req_id) == stage:
                target_id = self._current_req_id
            else:
                target_id = f"req_{uuid.uuid4().hex[:12]}"
        else:
            stage_queue = self._stage_active_req_ids.get(stage)
            if stage_queue and target_id in stage_queue:
                stage_queue.remove(target_id)

        self._stage_last_completed[stage] = target_id

        ev = {
            "kind": "TimeoutUnknown",
            "req_id": target_id,
            "reason": reason,
        }

        if self.mode == LifecycleMode.SHADOW:
            # In legacy ledger, timeouts were zeroed via record_failed
            self.legacy_ledger.record_failed(stage, reservation_tokens)
            try:
                res = self.bridge.apply(self.bend_state, ev)
                self.bend_state = res.state
                self._sync("verified.call_timeout_shadow")
            except Exception as shadow_err:
                logger.warning("Verified shadow kernel record_timeout failed: %s", shadow_err)
        else:
            res = self.bridge.apply(self.bend_state, ev)
            self.bend_state = res.state
            self._sync("verified.call_timeout")
            if res.verdict.kind == VerdictKind.CONFLICT_FAULT:
                raise RuntimeError(res.verdict.reason or "CONFLICT_FAULT")
            if res.verdict.kind == VerdictKind.REJECTED:
                raise BudgetExhausted(res.verdict.reason or "REJECTED")
