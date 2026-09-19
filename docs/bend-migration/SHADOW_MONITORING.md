# Verified Lifecycle Phase 2 Shadow Monitoring

Phase 2 is an operational observation gate. It does not expand the Bend proof
claim. Its purpose is to retain enough evidence to determine whether the active
Python lifecycle and the Bend observer produce the same verdict and normalized
lifecycle/accounting state on real workloads.

## Recorded evidence

For every shadowed lifecycle transition, ProtocolLab durably records:

- the run, request, lifecycle event, and stable comparison identifier;
- the active Python verdict and Bend observer verdict;
- SHA-256 references for the normalized active and observer states;
- independent verdict/state match flags;
- enqueue and observation timestamps.

Admission-only `calls_attempted` counters are excluded from the state digest
because they are diagnostic intake metrics rather than lifecycle transitions.
Reservations, dispatches, charges, releases, settled usage, faults, per-stage
totals, attempt identities, and transport states remain in the digest.

The observer apply, immutable comparison append, and pending-queue removal share
one SQLite transaction. A crash or audit-write failure leaves the event pending
and rolls the observer state back, allowing exact replay after restart.

## Start the window

Use one persistent owner store for each monitored production instance. The start
command verifies the hash-chained journal and refuses to begin while any shadow
event is pending.

```bash
protocollab shadow-monitor start /path/to/owner.sqlite \
  --monitor-id lifecycle-phase2-20260920
```

Starting the same monitor ID again is idempotent and returns the original start
record. The operational host's UTC clock is part of the trusted boundary.

## Evaluate the gate

```bash
protocollab shadow-monitor report /path/to/owner.sqlite \
  --monitor-id lifecycle-phase2-20260920 \
  --output artifacts/lifecycle-phase2-20260920.json
```

The command exits successfully only for `PASS`. Before the full window elapses it
returns `IN_PROGRESS`; missing observations, pending events, old uncomparable
records, or invalid timestamps return `INCOMPLETE`; any mismatch returns `FAIL`.

A report passes only when all of these conditions hold:

1. At least 14 full days have elapsed from the immutable start record.
2. At least one real lifecycle comparison was retained.
3. Every comparison has equal active/observer verdicts and state hashes.
4. No observer event remains pending.
5. The complete SQLite journal and state linkage pass integrity verification.

For a deployment with multiple owner stores, every in-scope store needs its own
window and `PASS` report. Retain each report with the database journal anchor.
Do not describe Phase 2 as complete from a subset of instances, from process logs,
or from an `IN_PROGRESS`/`INCOMPLETE` report.

## Stop rule

Any `DIVERGENCE` ends the zero-divergence claim for that window. Preserve the
store and report, identify the comparison ID, and investigate before starting a
new window. Do not erase the mismatch by restarting the monitor or deleting the
pending/audit state.
