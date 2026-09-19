"""Phase 2 durable shadow comparison and monitoring gates."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from protocollab.actor.budget import DiagnosticBudgetAllocation, StageBudgetLimits
from protocollab.learning import BudgetExhausted
from protocollab.storage import Store
from protocollab.verified.bridge import VerifiedKernelBridge
from protocollab.verified.lifecycle_owner import VerifiedLifecycleOwner
from protocollab.verified.protocol import (
    LifecycleMode,
    TransitionVerdict,
    VerdictKind,
)
from protocollab.verified.shadow_audit import (
    SHADOW_COMPARISON_KIND,
    SHADOW_COMPARISON_OWNER,
    analyze_shadow_monitor,
    build_shadow_comparison,
    start_shadow_monitor,
)


def allocation() -> DiagnosticBudgetAllocation:
    return DiagnosticBudgetAllocation(
        stages={"stage1": StageBudgetLimits(max_calls=5, max_tokens=1000)},
        aggregate_max_calls=5,
        aggregate_max_tokens=1000,
    )


def owner(store: Store, bridge=None, run_id: str = "shadow-run") -> VerifiedLifecycleOwner:
    return VerifiedLifecycleOwner(
        store,
        run_id,
        allocation(),
        "shadow-config",
        mode=LifecycleMode.SHADOW,
        bridge=bridge or VerifiedKernelBridge.get_default(),
    )


def comparisons(store: Store) -> list[dict]:
    return [
        event["payload"]
        for event in store.events()
        if event["owner"] == SHADOW_COMPARISON_OWNER
        and event["kind"] == SHADOW_COMPARISON_KIND
    ]


def test_real_bridge_records_matching_verdict_and_state_hashes(tmp_path: Path):
    store = Store(tmp_path / "owner.sqlite")
    lifecycle = owner(store)
    lifecycle.record_admission_attempt("stage1")
    lifecycle.reserve(
        "stage1",
        110,
        req_id="req-match",
        basis_ref="basis",
        max_input=100,
        max_output=10,
    )
    lifecycle.record_dispatched("stage1", req_id="req-match")
    lifecycle.record_completed(
        "stage1",
        80,
        7,
        110,
        req_id="req-match",
        receipt_hash="receipt-match",
    )
    lifecycle.record_validation("req-match", "ACCEPTED")

    rows = comparisons(store)
    assert [row["lifecycle_event_kind"] for row in rows] == [
        "Reserve",
        "DispatchIntent",
        "SettleUsage",
        "ValidationRecorded",
    ]
    assert all(row["status"] == "MATCH" for row in rows)
    assert all(row["verdict_match"] and row["state_match"] for row in rows)
    assert store.get("verified_shadow_pending", lifecycle.run_id) == []


class DivergentVerdictBridge:
    def __init__(self):
        self.delegate = VerifiedKernelBridge.get_default()

    def init(self, *args, **kwargs):
        return self.delegate.init(*args, **kwargs)

    def apply(self, state, event):
        result = self.delegate.apply(state, event)
        return result.model_copy(
            update={
                "verdict": TransitionVerdict(kind=VerdictKind.DUPLICATE_NOOP)
            }
        )


def test_shadow_verdict_divergence_is_retained(tmp_path: Path):
    store = Store(tmp_path / "owner.sqlite")
    lifecycle = owner(store, DivergentVerdictBridge())

    assert lifecycle.reserve("stage1", 10, req_id="req-diverge") == VerdictKind.ACCEPTED
    [row] = comparisons(store)
    assert row["status"] == "DIVERGENCE"
    assert row["active_verdict"] == "Accepted"
    assert row["observer_verdict"] == "DuplicateNoop"
    assert row["state_match"] is True


def test_rejected_reservation_is_compared(tmp_path: Path):
    store = Store(tmp_path / "owner.sqlite")
    lifecycle = owner(store)

    with pytest.raises(BudgetExhausted):
        lifecycle.reserve(
            "stage1",
            1001,
            req_id="req-rejected",
            max_input=1000,
            max_output=1,
        )
    [row] = comparisons(store)
    assert row["status"] == "MATCH"
    assert row["active_verdict"] == "Rejected"
    assert row["observer_verdict"] == "Rejected"
    assert row["state_match"] is True


def test_observer_apply_audit_and_queue_removal_are_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = Store(tmp_path / "owner.sqlite")
    lifecycle = owner(store, run_id="atomic-shadow")
    original_append = store.append

    def fail_comparison(owner_name, kind, payload, source=None):
        if owner_name == SHADOW_COMPARISON_OWNER:
            raise RuntimeError("INJECTED_SHADOW_AUDIT_FAILURE")
        return original_append(owner_name, kind, payload, source)

    monkeypatch.setattr(store, "append", fail_comparison)
    assert lifecycle.reserve("stage1", 10, req_id="req-atomic") == VerdictKind.ACCEPTED
    assert comparisons(store) == []
    assert len(store.get("verified_shadow_pending", "atomic-shadow")) == 1

    monkeypatch.setattr(store, "append", original_append)
    restarted = owner(store, run_id="atomic-shadow")
    [row] = comparisons(store)
    assert row["status"] == "MATCH"
    assert store.get("verified_shadow_pending", restarted.run_id) == []


def test_concurrent_shadow_enqueue_retains_every_observation(tmp_path: Path):
    for round_index in range(5):
        store = Store(tmp_path / f"owner-{round_index}.sqlite")
        lifecycle = owner(store, run_id=f"concurrent-shadow-{round_index}")
        lifecycle.reserve("stage1", 10, req_id="req-concurrent")
        baseline = len(comparisons(store))

        lifecycle._defer_shadow_observation = True
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda index: lifecycle.record_validation(
                        "req-concurrent", f"ACCEPTED_{index}"
                    ),
                    range(8),
                )
            )
        pending = store.get("verified_shadow_pending", lifecycle.run_id)
        assert len(pending) == 8
        assert len({item["comparison_id"] for item in pending}) == 8

        lifecycle._defer_shadow_observation = False
        lifecycle._drain_shadow_observations()
        new_rows = comparisons(store)[baseline:]
        assert len(new_rows) == 8
        assert all(row["status"] == "MATCH" for row in new_rows)
        assert store.get("verified_shadow_pending", lifecycle.run_id) == []
        store.close()


def test_monitor_gate_passes_only_after_complete_fourteen_day_window(tmp_path: Path):
    store = Store(tmp_path / "owner.sqlite")
    start_shadow_monitor(
        store,
        "phase2",
        started_at="2026-09-01T00:00:00Z",
    )
    item = {
        "comparison_id": "comparison-1",
        "run_id": "run-1",
        "enqueued_at": "2026-09-02T00:00:00Z",
        "event_kind": "verified.call_reserved_shadow",
        "event": {"kind": "Reserve", "req_id": "req-1"},
        "active_verdict": "Accepted",
        "active_state_hash": "same-state",
    }
    store.append(
        SHADOW_COMPARISON_OWNER,
        SHADOW_COMPARISON_KIND,
        build_shadow_comparison(
            item,
            "Accepted",
            "same-state",
            observed_at="2026-09-02T00:00:01Z",
        ),
    )

    early = analyze_shadow_monitor(
        store, "phase2", ended_at="2026-09-14T23:59:59Z"
    )
    assert early["status"] == "IN_PROGRESS"
    assert early["comparison_count"] == 1

    complete = analyze_shadow_monitor(
        store, "phase2", ended_at="2026-09-15T00:00:00Z"
    )
    assert complete["status"] == "PASS"
    assert complete["divergence_count"] == 0
    assert complete["pending_count"] == 0
    assert complete["journal_anchor"]["seq"] == store.tail[0]


def test_monitor_rejects_existing_backlog_and_fails_on_divergence(tmp_path: Path):
    backlog_store = Store(tmp_path / "backlog.sqlite")
    backlog_store.set(
        "verified_shadow_pending",
        "run-backlog",
        [{"comparison_id": "pending-1"}],
        "verified.shadow_pending_updated",
    )
    with pytest.raises(ValueError, match="SHADOW_MONITOR_BACKLOG_NOT_EMPTY"):
        start_shadow_monitor(backlog_store, "phase2")

    store = Store(tmp_path / "divergence.sqlite")
    start_shadow_monitor(
        store,
        "phase2",
        started_at="2026-09-01T00:00:00Z",
    )
    item = {
        "comparison_id": "comparison-divergent",
        "run_id": "run-1",
        "enqueued_at": "2026-09-02T00:00:00Z",
        "event_kind": "verified.call_reserved_shadow",
        "event": {"kind": "Reserve", "req_id": "req-1"},
        "active_verdict": "Accepted",
        "active_state_hash": "active-state",
    }
    forged_match = build_shadow_comparison(
        item,
        "Accepted",
        "observer-state",
        observed_at="2026-09-02T00:00:01Z",
    )
    forged_match["status"] = "MATCH"
    forged_match["state_match"] = True
    store.append(
        SHADOW_COMPARISON_OWNER,
        SHADOW_COMPARISON_KIND,
        forged_match,
    )
    report = analyze_shadow_monitor(
        store, "phase2", ended_at="2026-09-15T00:00:00Z"
    )
    assert report["status"] == "FAIL"
    assert report["divergence_count"] == 1
    assert report["uncomparable_count"] == 1
    assert report["divergence_ids"] == ["comparison-divergent"]


def test_shadow_monitor_cli_starts_and_retains_incomplete_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    database = tmp_path / "owner.sqlite"
    Store(database).close()
    from protocollab.cli import main

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "protocollab",
            "shadow-monitor",
            "start",
            str(database),
            "--monitor-id",
            "phase2-cli",
        ],
    )
    main()
    started = json.loads(capsys.readouterr().out)
    assert started["monitor_id"] == "phase2-cli"
    assert started["minimum_days"] == 14

    output = tmp_path / "shadow-report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "protocollab",
            "shadow-monitor",
            "report",
            str(database),
            "--monitor-id",
            "phase2-cli",
            "--output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    report = json.loads(output.read_text())
    assert report["status"] == "INCOMPLETE"
    assert "NO_SHADOW_COMPARISONS_RECORDED" in report["reasons"]
