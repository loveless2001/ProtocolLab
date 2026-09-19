"""Lifecycle owner coordinating verified Bend kernel and effectful Python shell."""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from typing import Any

from protocollab.actor.budget import (
    DiagnosticBudgetAllocation,
    DiagnosticLedger,
    DiagnosticLedgerState,
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
    TransitionVerdict,
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
        self._receipt_to_req_id: dict[str, str] = {}

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
            self._reconstruct_routing()
            if self.mode == LifecycleMode.SHADOW:
                for req in self.bend_state.requests:
                    self.legacy_ledger._seen_req_ids.add(req.req_id)

        from protocollab.verified.attempt import AttemptGateway
        self.gateway = AttemptGateway(self)

    def get_request_record(self, req_id: str):
        """Retrieve request record by req_id from verified state."""
        self._refresh_state()
        if self.bend_state and hasattr(self.bend_state, "requests"):
            for req in self.bend_state.requests:
                if req.req_id == req_id:
                    return req
        return None

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

    def _reconstruct_routing(self):
        self._request_stages.clear()
        self._stage_active_req_ids.clear()
        self._stage_last_completed.clear()
        self._receipt_to_req_id.clear()
        for req in reversed(self.bend_state.requests):
            self._request_stages[req.req_id] = req.stage
            if req.charge.kind == ChargeKind.PENDING:
                queue = self._stage_active_req_ids.setdefault(req.stage, [])
                if req.req_id not in queue:
                    queue.append(req.req_id)
            elif req.charge.kind == ChargeKind.SETTLED:
                self._stage_last_completed[req.stage] = req.req_id
                if req.charge.receipt_hash:
                    self._receipt_to_req_id[req.charge.receipt_hash] = req.req_id
            elif req.charge.kind == ChargeKind.RELEASED:
                self._stage_last_completed[req.stage] = req.req_id

    def _refresh_state(self):
        stored = self.store.get("verified_lifecycle_ledger", self.run_id)
        if stored is not None:
            latest = decode_state(stored)
            if latest != self.bend_state:
                self.bend_state = latest
                self._reconstruct_routing()
                if self.mode == LifecycleMode.SHADOW:
                    for req in self.bend_state.requests:
                        self.legacy_ledger._seen_req_ids.add(req.req_id)

    def _sync(self, event_kind: str = "verified.lifecycle_updated"):
        encoded = encode_state(self.bend_state)
        self.store.set(
            "verified_lifecycle_ledger",
            self.run_id,
            encoded,
            event_kind,
        )

    @contextmanager
    def _store_transaction(self):
        if hasattr(self.store, "transaction"):
            with self.store.transaction():
                yield
        elif hasattr(self.store, "lock"):
            with self.store.lock:
                yield
        else:
            yield

    def _apply_bridge(self, ev: dict[str, Any], event_kind: str) -> Any:
        with self._store_transaction():
            self._refresh_state()
            res = self.bridge.apply(self.bend_state, ev)

            # If conflict fault occurred, record quarantine evidence BEFORE persisting faulted ledger state.
            # This ensures that if recording quarantine evidence fails, the faulted ledger is not latched,
            # allowing retry/replay without losing evidence.
            if res.verdict.kind == VerdictKind.CONFLICT_FAULT:
                target_id = ev.get("req_id") or self._stage_last_completed.get(
                    ev.get("stage", ""), "unknown"
                )
                quarantine_payload = {
                    "run_id": self.run_id,
                    "req_id": target_id,
                    "stage": ev.get("stage", ""),
                    "receipt_hash": ev.get("receipt_hash"),
                    "input_tokens": ev.get("input_tokens"),
                    "output_tokens": ev.get("output_tokens"),
                    "fault": res.verdict.reason or "CONFLICTING_USAGE_AFTER_RELEASE",
                }
                self.store.set(
                    "verified_quarantine_records",
                    f"{self.run_id}:{target_id}",
                    quarantine_payload,
                    "verified.quarantine_conflict_recorded",
                )

            self.bend_state = res.state
            self._sync(event_kind)
            return res

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
            legacy_res = None
            try:
                legacy_res = self.legacy_ledger.reserve(stage, reservation_tokens, req_id=req_id)
            except Exception as legacy_err:
                try:
                    self._apply_bridge(ev, "verified.call_reserved_shadow")
                except Exception as shadow_err:
                    logger.warning("Verified shadow kernel reserve failed: %s", shadow_err)
                raise legacy_err

            try:
                res = self._apply_bridge(ev, "verified.call_reserved_shadow")
                if getattr(res.verdict, "kind", None) != legacy_res:
                    logger.warning(
                        "Verified shadow kernel reserve divergence: legacy=%s, observer=%s",
                        legacy_res,
                        getattr(res.verdict, "kind", None),
                    )
            except Exception as shadow_err:
                logger.warning("Verified shadow kernel reserve failed: %s", shadow_err)

            if legacy_res in ("DuplicateNoop", VerdictKind.DUPLICATE_NOOP):
                return VerdictKind.DUPLICATE_NOOP
            return VerdictKind.ACCEPTED
        else:
            res = self._apply_bridge(ev, "verified.call_reserved")
            if res.verdict.kind == VerdictKind.REJECTED:
                raise BudgetExhausted(res.verdict.reason or "REJECTED")
            if res.verdict.kind == VerdictKind.CONFLICT_FAULT:
                raise RuntimeError(res.verdict.reason or "CONFLICT_FAULT")
            return res.verdict.kind

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
            res = None
            try:
                res = self._apply_bridge(ev, "verified.call_dispatched_shadow")
            except Exception as shadow_err:
                logger.warning("Verified shadow kernel record_dispatched failed: %s", shadow_err)

            observer_kind = getattr(res.verdict, "kind", None) if res is not None else None
            if observer_kind == VerdictKind.DUPLICATE_NOOP:
                return VerdictKind.DUPLICATE_NOOP

            legacy_disp = self.legacy_ledger.record_dispatched(stage, req_id=target_id)
            if observer_kind is not None and observer_kind != legacy_disp:
                logger.warning(
                    "Verified shadow kernel record_dispatched divergence: legacy=%s, observer=%s",
                    legacy_disp,
                    observer_kind,
                )

            if legacy_disp in ("DuplicateNoop", VerdictKind.DUPLICATE_NOOP):
                return VerdictKind.DUPLICATE_NOOP
            return VerdictKind.ACCEPTED
        else:
            res = self._apply_bridge(ev, "verified.call_dispatched")
            if res.verdict.kind == VerdictKind.REJECTED:
                raise BudgetExhausted(res.verdict.reason or "REJECTED")
            if res.verdict.kind == VerdictKind.CONFLICT_FAULT:
                raise RuntimeError(res.verdict.reason or "CONFLICT_FAULT")
            return res.verdict.kind

    def record_completed(
        self,
        stage: str,
        input_tokens: int,
        output_tokens: int,
        reservation_tokens: int = 0,
        req_id: str | None = None,
        receipt_hash: str = "receipt_default",
    ):
        target_id = req_id
        if target_id is None:
            if receipt_hash and receipt_hash != "receipt_default" and receipt_hash in self._receipt_to_req_id:
                target_id = self._receipt_to_req_id[receipt_hash]
            else:
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
        if receipt_hash and receipt_hash != "receipt_default":
            self._receipt_to_req_id[receipt_hash] = target_id

        ev = {
            "kind": "SettleUsage",
            "req_id": target_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "receipt_hash": receipt_hash,
        }

        if self.mode == LifecycleMode.SHADOW:
            # Prevent double-billing active legacy ledger on duplicate receipt (§3, §5)
            rec = self.get_request_record(target_id)
            is_dup = False
            if rec is not None:
                charge_kind = getattr(rec.charge, "kind", getattr(rec.charge, "value", str(rec.charge)))
                if charge_kind in ("Settled", "SETTLED"):
                    if (
                        getattr(rec.charge, "input_tokens", None) == input_tokens
                        and getattr(rec.charge, "output_tokens", None) == output_tokens
                        and (getattr(rec.charge, "receipt_hash", None) == receipt_hash or not getattr(rec.charge, "receipt_hash", None))
                    ):
                        is_dup = True

            if not is_dup:
                self.legacy_ledger.record_completed(
                    stage, input_tokens, output_tokens, reservation_tokens
                )
            try:
                self._apply_bridge(ev, "verified.call_completed_shadow")
            except Exception as shadow_err:
                logger.warning("Verified shadow kernel record_completed failed: %s", shadow_err)
        else:
            res = self._apply_bridge(ev, "verified.call_completed")
            if res.verdict.kind == VerdictKind.CONFLICT_FAULT:
                raise RuntimeError(res.verdict.reason or "CONFLICT_FAULT")
            if res.verdict.kind == VerdictKind.REJECTED:
                raise BudgetExhausted(res.verdict.reason or "REJECTED")

    def record_failed(
        self,
        stage: str,
        reservation_tokens: int = 0,
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

        if reservation_tokens == 0:
            rec = self.get_request_record(target_id)
            if rec is not None:
                charge_kind = getattr(rec.charge, "kind", getattr(rec.charge, "value", str(rec.charge)))
                if charge_kind in ("Pending", "PENDING"):
                    reservation_tokens = getattr(rec.charge, "tokens", 0) or 0

        ev = {
            "kind": "FailureConclusive",
            "req_id": target_id,
            "evidence_hash": evidence_hash,
        }

        if self.mode == LifecycleMode.SHADOW:
            self.legacy_ledger.record_failed(stage, reservation_tokens)
            try:
                self._apply_bridge(ev, "verified.call_failed_shadow")
            except Exception as shadow_err:
                logger.warning("Verified shadow kernel record_failed failed: %s", shadow_err)
        else:
            res = self._apply_bridge(ev, "verified.call_failed")
            if res.verdict.kind == VerdictKind.CONFLICT_FAULT:
                raise RuntimeError(res.verdict.reason or "CONFLICT_FAULT")
            if res.verdict.kind == VerdictKind.REJECTED:
                raise BudgetExhausted(res.verdict.reason or "REJECTED")

    def record_timeout(
        self,
        stage: str,
        reservation_tokens: int = 0,
        req_id: str | None = None,
        reason: str = "timeout",
    ):
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

        self._stage_last_completed[stage] = target_id

        ev = {
            "kind": "TimeoutUnknown",
            "req_id": target_id,
            "reason": reason,
        }

        if self.mode == LifecycleMode.SHADOW:
            if hasattr(self.legacy_ledger, "record_timeout"):
                self.legacy_ledger.record_timeout(
                    stage,
                    reservation_tokens,
                    req_id=target_id,
                    reason=reason,
                )
            else:
                self.legacy_ledger.record_failed(stage, reservation_tokens)
            try:
                self._apply_bridge(ev, "verified.call_timeout_shadow")
            except Exception as shadow_err:
                logger.warning("Verified shadow kernel record_timeout failed: %s", shadow_err)
        else:
            res = self._apply_bridge(ev, "verified.call_timeout")
            if res.verdict.kind == VerdictKind.CONFLICT_FAULT:
                raise RuntimeError(res.verdict.reason or "CONFLICT_FAULT")
            if res.verdict.kind == VerdictKind.REJECTED:
                raise BudgetExhausted(res.verdict.reason or "REJECTED")

    def record_validation(self, req_id: str, outcome: str) -> TransitionVerdict:
        """Record validation outcome for an attempt. Leaves accounting invariants unchanged."""
        self._refresh_state()
        ev = {
            "kind": "ValidationRecorded",
            "req_id": req_id,
            "outcome": outcome,
        }
        if self.mode == LifecycleMode.SHADOW:
            self.store.append(
                "verified_lifecycle_ledger",
                "verified.validation_recorded",
                {"req_id": req_id, "outcome": outcome, "run_id": self.run_id},
            )
            try:
                self._apply_bridge(ev, "verified.validation_recorded_shadow")
            except Exception as shadow_err:
                logger.warning("Shadow bridge error recording validation: %s", shadow_err)
            return TransitionVerdict(kind=VerdictKind.ACCEPTED)
        else:
            res = self._apply_bridge(ev, "verified.validation_recorded")
            if res.verdict.kind == VerdictKind.CONFLICT_FAULT:
                raise RuntimeError(res.verdict.reason or "CONFLICT_FAULT")
            if res.verdict.kind == VerdictKind.REJECTED:
                raise BudgetExhausted(res.verdict.reason or "REJECTED")
            return res.verdict

