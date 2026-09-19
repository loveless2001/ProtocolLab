"""Durable evidence and reporting for verified lifecycle shadow monitoring."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from protocollab.contracts import digest

SHADOW_AUDIT_SCHEMA_VERSION = "1.0"
SHADOW_COMPARISON_OWNER = "verified_shadow_audit"
SHADOW_COMPARISON_KIND = "verified.shadow_comparison"
SHADOW_MONITOR_OWNER = "verified_shadow_monitor_sessions"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("SHADOW_MONITOR_TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    return parsed.astimezone(timezone.utc)


def normalize_verdict(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "kind"):
        value = value.kind
    if isinstance(value, Enum):
        value = value.value
    return str(value)


def normalized_diagnostic_state(state: Any) -> dict[str, Any]:
    """Remove admission-only counters before comparing lifecycle projections."""
    payload = state.model_dump(mode="json")
    for usage in payload.get("stages", {}).values():
        usage.pop("calls_attempted", None)
    payload.get("aggregate", {}).pop("calls_attempted", None)
    return payload


def diagnostic_state_hash(state: Any) -> str:
    return digest(normalized_diagnostic_state(state))


def build_shadow_comparison(
    item: dict[str, Any],
    observer_verdict: Any,
    observer_state_hash: str,
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    active_verdict = normalize_verdict(item.get("active_verdict"))
    observed_verdict = normalize_verdict(observer_verdict)
    active_state_hash = item.get("active_state_hash")
    comparable = active_verdict is not None and active_state_hash is not None
    verdict_match = comparable and active_verdict == observed_verdict
    state_match = comparable and active_state_hash == observer_state_hash
    status = "MATCH" if verdict_match and state_match else "DIVERGENCE"
    if not comparable:
        status = "UNCOMPARABLE"
    event = item.get("event", {})
    return {
        "schema_version": SHADOW_AUDIT_SCHEMA_VERSION,
        "comparison_id": item.get("comparison_id"),
        "run_id": item.get("run_id"),
        "req_id": event.get("req_id"),
        "lifecycle_event_kind": event.get("kind"),
        "journal_event_kind": item.get("event_kind"),
        "enqueued_at": item.get("enqueued_at"),
        "observed_at": observed_at or utc_now(),
        "active_verdict": active_verdict,
        "observer_verdict": observed_verdict,
        "active_state_hash": active_state_hash,
        "observer_state_hash": observer_state_hash,
        "verdict_match": verdict_match,
        "state_match": state_match,
        "status": status,
    }


def effective_comparison_status(payload: dict[str, Any]) -> str:
    fields = (
        payload.get("active_verdict"),
        payload.get("observer_verdict"),
        payload.get("active_state_hash"),
        payload.get("observer_state_hash"),
    )
    if any(value is None for value in fields):
        return "UNCOMPARABLE"
    if fields[0] == fields[1] and fields[2] == fields[3]:
        return "MATCH"
    return "DIVERGENCE"


def start_shadow_monitor(
    store: Any,
    monitor_id: str,
    *,
    minimum_days: int = 14,
    started_at: str | None = None,
) -> dict[str, Any]:
    if not monitor_id:
        raise ValueError("SHADOW_MONITOR_ID_REQUIRED")
    if minimum_days < 1:
        raise ValueError("SHADOW_MONITOR_MINIMUM_DAYS_MUST_BE_POSITIVE")
    store.verify()
    timestamp = started_at or utc_now()
    parse_utc(timestamp)
    with store.transaction():
        existing = store.get(SHADOW_MONITOR_OWNER, monitor_id)
        if existing is not None:
            return existing
        pending_rows = [
            value
            for _, value in _state_items(store, "verified_shadow_pending")
            if value
        ]
        if pending_rows:
            raise ValueError("SHADOW_MONITOR_BACKLOG_NOT_EMPTY")
        start_event_seq = store.tail[0] + 1
        session = {
            "schema_version": SHADOW_AUDIT_SCHEMA_VERSION,
            "monitor_id": monitor_id,
            "started_at": timestamp,
            "minimum_days": minimum_days,
            "start_event_seq": start_event_seq,
        }
        store.set(
            SHADOW_MONITOR_OWNER,
            monitor_id,
            session,
            "verified.shadow_monitor_started",
        )
    return session


def _state_items(store: Any, owner_filter: str | None = None):
    query = "SELECT owner,key,payload FROM state"
    params: tuple[Any, ...] = ()
    if owner_filter is not None:
        query += " WHERE owner=?"
        params = (owner_filter,)
    for row in store.db.execute(query, params):
        yield (row["owner"], row["key"]), json.loads(row["payload"])


def analyze_shadow_monitor(
    store: Any,
    monitor_id: str,
    *,
    ended_at: str | None = None,
) -> dict[str, Any]:
    if getattr(store, "_depth", 0) > 0:
        return _analyze_shadow_monitor_snapshot(
            store, monitor_id, ended_at=ended_at
        )
    with store.lock:
        store.db.execute("BEGIN")
        try:
            return _analyze_shadow_monitor_snapshot(
                store, monitor_id, ended_at=ended_at
            )
        finally:
            store.db.execute("ROLLBACK")


def _analyze_shadow_monitor_snapshot(
    store: Any,
    monitor_id: str,
    *,
    ended_at: str | None = None,
) -> dict[str, Any]:
    session = store.get(SHADOW_MONITOR_OWNER, monitor_id)
    if session is None:
        raise ValueError("SHADOW_MONITOR_SESSION_NOT_FOUND")
    started = parse_utc(session["started_at"])
    ended_text = ended_at or utc_now()
    ended = parse_utc(ended_text)
    if ended < started:
        raise ValueError("SHADOW_MONITOR_END_BEFORE_START")
    anchor = store.verify()

    comparisons = []
    effective_statuses = []
    invalid_comparisons = []
    seen_ids: set[str] = set()
    for event in store.events(after=session["start_event_seq"]):
        if (
            event["owner"] != SHADOW_COMPARISON_OWNER
            or event["kind"] != SHADOW_COMPARISON_KIND
        ):
            continue
        payload = event["payload"]
        comparison_id = payload.get("comparison_id")
        invalid_ref = comparison_id or f"event_seq:{event['seq']}"
        metadata_valid = True
        if (
            payload.get("schema_version") != SHADOW_AUDIT_SCHEMA_VERSION
            or not isinstance(comparison_id, str)
            or not comparison_id
            or comparison_id in seen_ids
        ):
            metadata_valid = False
        if isinstance(comparison_id, str):
            seen_ids.add(comparison_id)
        try:
            enqueued = parse_utc(payload["enqueued_at"])
            observed = parse_utc(payload["observed_at"])
        except (KeyError, TypeError, ValueError):
            metadata_valid = False
        else:
            if (
                enqueued < started
                or enqueued > ended
                or observed < enqueued
                or observed > ended
            ):
                metadata_valid = False
        effective_status = effective_comparison_status(payload)
        comparable = effective_status != "UNCOMPARABLE"
        expected_verdict_match = (
            comparable
            and payload.get("active_verdict") == payload.get("observer_verdict")
        )
        expected_state_match = (
            comparable
            and payload.get("active_state_hash")
            == payload.get("observer_state_hash")
        )
        if (
            payload.get("status") != effective_status
            or payload.get("verdict_match") != expected_verdict_match
            or payload.get("state_match") != expected_state_match
        ):
            metadata_valid = False
        if not metadata_valid:
            invalid_comparisons.append(invalid_ref)
        comparisons.append(payload)
        effective_statuses.append(effective_status)

    pending = []
    for (_, run_id), value in _state_items(store, "verified_shadow_pending"):
        if value:
            pending.extend(
                {"run_id": run_id, "comparison_id": item.get("comparison_id")}
                for item in value
            )

    divergences = [
        row
        for row, status in zip(comparisons, effective_statuses, strict=True)
        if status == "DIVERGENCE"
    ]
    uncomparable = [
        row
        for row, status in zip(comparisons, effective_statuses, strict=True)
        if status == "UNCOMPARABLE"
    ]
    uncomparable_ids = {
        row.get("comparison_id") for row in uncomparable
    } | set(invalid_comparisons)
    elapsed_seconds = int((ended - started).total_seconds())
    required_seconds = int(session["minimum_days"]) * 24 * 60 * 60
    reasons = []
    if divergences:
        reasons.append("SHADOW_DIVERGENCE_RECORDED")
    if uncomparable or invalid_comparisons:
        reasons.append("SHADOW_COMPARISON_UNCOMPARABLE")
    if pending:
        reasons.append("SHADOW_OBSERVER_BACKLOG_NOT_EMPTY")
    if not comparisons:
        reasons.append("NO_SHADOW_COMPARISONS_RECORDED")
    if elapsed_seconds < required_seconds:
        reasons.append("MINIMUM_MONITORING_WINDOW_NOT_REACHED")

    if divergences:
        status = "FAIL"
    elif uncomparable_ids or pending or not comparisons:
        status = "INCOMPLETE"
    elif elapsed_seconds < required_seconds:
        status = "IN_PROGRESS"
    else:
        status = "PASS"

    event_counts: dict[str, int] = {}
    for row in comparisons:
        kind = row.get("lifecycle_event_kind") or "UNKNOWN"
        event_counts[kind] = event_counts.get(kind, 0) + 1
    return {
        "schema_version": SHADOW_AUDIT_SCHEMA_VERSION,
        "monitor_id": monitor_id,
        "status": status,
        "reasons": reasons,
        "started_at": session["started_at"],
        "ended_at": ended_text,
        "minimum_days": session["minimum_days"],
        "elapsed_seconds": elapsed_seconds,
        "comparison_count": len(comparisons),
        "match_count": sum(status == "MATCH" for status in effective_statuses),
        "divergence_count": len(divergences),
        "uncomparable_count": len(uncomparable_ids),
        "pending_count": len(pending),
        "event_counts": dict(sorted(event_counts.items())),
        "run_ids": sorted({row.get("run_id") for row in comparisons if row.get("run_id")}),
        "divergence_ids": [row.get("comparison_id") for row in divergences],
        "pending": pending,
        "journal_anchor": {"seq": anchor[0], "event_hash": anchor[1]},
    }
