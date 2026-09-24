# Phase 2 daily shadow-traffic menu

Choose one primary case for each UTC day of the shadow window. A case is a
batch of lifecycle requests, not one request. Repeating a case is useful for
temporal coverage, but completing many copies of one case does not replace path
coverage.

These cases test agreement between the active Python lifecycle and the Bend
observer. They do not measure model intelligence or task success. Use one
persistent monitored owner store for cases marked **soak store**. Cases marked
**isolated store** intentionally create unresolved or faulted states and must
never target the soak store.

## Daily cases

| ID | Case | Store | Default batch | Provider | Expected lifecycle coverage |
|---|---|---|---:|---|---|
| `normal-success` | Successful requests with exact usage settlement and accepted validation, distributed across every enabled diagnostic stage | Soak store | 60 requests | Deterministic loopback or local | `Reserve`, `DispatchIntent`, `SettleUsage`, `ValidationRecorded` |
| `validation-rejection` | Transport succeeds and usage settles, but the returned proposal is malformed, outside the candidate registry, or otherwise rejected | Soak store | 30 requests | Deterministic loopback | Successful accounting followed by rejected validation |
| `budget-rejection` | Reservations exceed a stage or aggregate call/token limit and are rejected before dispatch | Soak store | 30 attempts | None | Rejected `Reserve`; zero dispatch and zero charge |
| `exact-duplicate` | Retry each completed attempt and its identical validation report through the gateway; replay the same reservation, dispatch, and settlement identities through the lifecycle owner | Soak store | 25 request pairs | Deterministic loopback | One provider call and charge per pair; `DuplicateNoop` for repeated reservation, dispatch, and settlement; gateway validation duplicate without a second lifecycle event |
| `concurrent-batch` | Independent requests overlap across stages and complete in a different order from reservation order | Soak store | 64 requests, concurrency 4 | Deterministic loopback or local | Routing, isolation, settlement, and validation under concurrency |
| `restart-recovery` | Stop after durable shadow enqueue, reopen the owner, drain the queue, then finish the requests | Soak store only when the injected stop point is known recoverable | 10 recoveries | Deterministic loopback | Durable pending queue, restart reconstruction, atomic drain, and final match |
| `provider-boundary` | Small real transport sample using the currently supported local model port and, when explicitly selected, a paid API port | Soak store | 5 local plus at most 2 paid API requests | Local; paid API optional | Real serialization, timing, usage receipts, and response-boundary integration |
| `proven-not-sent` | Fail before any request bytes can cross the provider boundary and release the reservation using retained proof | Isolated store | 20 requests | Fault-injection adapter | `FailureConclusive`, released charge, and zero provider usage |
| `timeout-unknown` | Time out after dispatch may have occurred; preserve the reservation instead of claiming conclusive failure | Isolated store | 10 requests | Fault-injection adapter | `TimeoutUnknown`, `OutcomeUnknown`, and retained charge hold |
| `late-reconciliation` | Deliver a retained provider receipt after an earlier timeout and reconcile it exactly once | Isolated store | 10 timeout/receipt pairs | Fault-injection adapter | Unknown-to-settled transition, receipt binding, and duplicate suppression |
| `conflict-quarantine` | Reuse a request identity or receipt with different immutable fields and verify fault latching and quarantine | Disposable isolated store | 5 conflicts | None | `ConflictFault`; no continued automated processing in the faulted run |

For `exact-duplicate`, the gateway returns `RecordValidationDuplicate` for an
identical retained validation report and does not emit another
`ValidationRecorded` event. A direct call to the lifecycle owner's
`record_validation` is a separate diagnostic: the Bend ledger does not retain
validation history, so both Python and Bend return `Accepted` for that event
again while leaving lifecycle state and accounting unchanged. If the runner
includes this direct-owner diagnostic, record its extra shadow comparison
separately from the gateway retry. For 25 pairs, each of `Reserve`,
`DispatchIntent`, and `SettleUsage` should have 25 initial `Accepted` and 25
replayed `DuplicateNoop` comparisons. The gateway validation retry creates no
comparison; the optional direct-owner replay adds 25 `Accepted` validation
comparisons to the 25 initial ones.

## Selection rules

1. Run `normal-success` on at least four separate days. It establishes temporal
   coverage for the common path.
2. Run `provider-boundary` on at least two separate days. Keep paid calls within
   the batch cap and record billed spend as unknown unless an invoice or account
   record is queried.
3. Run every other case at least once during the window.
4. Run `timeout-unknown`, `late-reconciliation`, and `conflict-quarantine` only
   against isolated stores. Their expected unresolved or faulted states must not
   contaminate the persistent soak store.
5. Do not count pytest fixtures alone as soak traffic. Tests support the runner;
   the daily case must retain its own owner store, manifest, comparison report,
   and journal anchor.
6. A day counts as active only when its case retains at least one comparable
   lifecycle event and the daily report has zero divergence, zero uncomparable
   records, and zero pending shadow observations after any planned recovery.
7. Preserve negative results. Do not restart a window or delete a store to erase
   a divergence, unresolved charge, or fault.

## Suggested first 14-day rotation

| Day | Primary case |
|---:|---|
| 1 | `normal-success` |
| 2 | `validation-rejection` |
| 3 | `exact-duplicate` |
| 4 | `normal-success` |
| 5 | `concurrent-batch` |
| 6 | `restart-recovery` |
| 7 | `provider-boundary` |
| 8 | `normal-success` |
| 9 | `budget-rejection` |
| 10 | `proven-not-sent` on an isolated store |
| 11 | `timeout-unknown` on an isolated store |
| 12 | `late-reconciliation` on an isolated store |
| 13 | `provider-boundary`; run `conflict-quarantine` separately |
| 14 | `normal-success`, then produce the final gate report |

This rotation is a minimum coverage plan. If a case fails, preserve its evidence
and investigate it before selecting additional traffic intended to support
promotion.

## Daily record

Retain one record with the case artifacts:

```json
{
  "schema_version": "1.0",
  "date_utc": "YYYY-MM-DD",
  "case_id": "normal-success",
  "repository_commit": "full git commit",
  "runner_sha256": "sha256",
  "store": "absolute owner.sqlite path",
  "monitor_id": "phase2 monitor ID",
  "requested_batch": 60,
  "completed_requests": 60,
  "comparison_count_before": 0,
  "comparison_count_after": 240,
  "divergence_count": 0,
  "uncomparable_count": 0,
  "pending_count": 0,
  "journal_anchor": {"seq": 0, "event_hash": "sha256"},
  "provider_usage": {
    "calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "billed_spend": "NOT_APPLICABLE"
  },
  "status": "PASS"
}
```

The fields above are a recording contract for the future daily runner. Until
that runner exists, do not fabricate a daily record from unit-test output.
