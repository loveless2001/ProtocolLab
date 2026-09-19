# ProtocolLab — Bend Migration Discovery & Audit

> Historical pre-migration snapshot. The current architecture and validation
> evidence are recorded in `MIGRATION_REPORT.md`; this document preserves the
> defects and call graph observed before the gateway migration.

**Date:** 2026-09-18
**Repository Branch:** `experiment/bend-lifecycle-kernel`
**Base Commit:** `46f1c34d3400492ae527083a16e0b7120d84e1f5`
**Working Tree Status:** Clean
**Toolchain Baseline:** Bend `2.0.5` (`0b7e2b11c1054f5d0f4eb955cadb47997ef1115d`), Bun `1.3.8`, Node `25.5.0`

---

## 1. System Call Graph & Dispatch Paths

The current lifecycle and accounting flow passes through three layers:

```text
ActorDiagnosticHarness (run_stage_1 / run_stage_2 / run_stage_3)
  │
  ▼
DecisionAdapter.decide(port, packet, seed, ledger, stage)
  │
  ├── 1. preflight_backend()
  ├── 2. ledger.record_admission_attempt(stage)
  ├── 3. compose_and_admit_input()
  ├── 4. ledger.reserve(stage, reservation_tokens)
  │      ledger.record_dispatched(stage)
  ├── 5. port.generate() / port.score_candidates()
  │      ├── Store.set("model_port", phase, ...) [llm.call_reserved]
  │      ├── Store.append("model_port", "llm.requested")
  │      ├── delivered() -> Store.append("model_port", "llm.input_delivered")
  │      ├── Store.set("model_port", phase, ...) [llm.usage_recorded]
  │      └── Store.append("model_port", "llm.completed") / "llm.failed"
  ├── 6. ledger.record_completed(stage, input_tokens, output_tokens, reservation_tokens)
  │      [on exception: ledger.record_failed(stage, reservation_tokens)]
  └── 7. extract_channels() / parse_proposal()
         ├── Store.append("actor", "actor.raw_proposal")
         ├── Store.append("actor", "actor.proposal_returned" / "actor.proposal_rejected")
         ├── Store.append("actor", "actor.proposed")
         └── Store.append("actor", "actor.interaction_completed")
```

### Ledger Writers & State Owners
1. **`DiagnosticLedger` (`protocollab/actor/budget.py`):**
   - Stores mutable `DiagnosticLedgerState` in SQLite `Store` under key `("diagnostic_ledger", run_id)`.
   - Mutates `stage_usage` and `agg_usage` independently via separate methods (`record_admission_attempt`, `reserve`, `record_dispatched`, `record_completed`, `record_failed`).
2. **`FrozenModelPort` (`protocollab/actor/__init__.py`):**
   - Stores mutable token usage in SQLite `Store` under key `("model_port", phase)` where phase is `"prefix"` or `"suffix"`.
   - Records discrete append-only journal events (`llm.budget_initialized`, `llm.call_reserved`, `llm.requested`, `llm.input_delivered`, `llm.usage_recorded`, `llm.completed`, `llm.failed`).

---

## 2. Downstream Consumers & Dependency Mapping

### Negative Case Scoring (`protocollab/evaluation/negative_cases.py`)
Consumes raw journal events directly from `Store.events()`:
- `epistemic.claim`
- `llm.requested` (owner: `model_port`, correlates with `request_hash` and `claim_refs`)
- `llm.input_delivered` (owner: `model_port`, verifies `boundary` and matching claim hashes)
- `llm.completed` (owner: `model_port`, records token usage and response hash)
- `actor.proposal_returned` / `actor.proposal_rejected` (owner: `actor`, checks schema validity)
- `actor.interaction_completed` (owner: `actor`, marks the end boundary of the interaction window)

**Constraint:** The migration kernel must NOT rename, eliminate, or reorder these Store events. The verified kernel governs the lifecycle state machine and accounting invariants, while the Python shell preserves the event emission contract.

### Diagnostic Harness (`protocollab/actor/diagnostic_harness.py`)
Reads `ledger.state.stages[stage_name]` and `ledger.state.aggregate` to compute:
- `calls_attempted`
- `dispatched_inference`
- `completed_calls`
- `failures`
- `schema_valid_rate = valid_count / calls_attempted`
- `truncation_rate = trunc_count / calls_attempted`
- `task_progress_rate = effective_count / calls_attempted`

**Constraint:** The verified lifecycle owner must project a compatible `DiagnosticLedgerState` view containing these exact attributes so that `StageResult` calculations remain identical.

---

## 3. Existing Flaws & Architectural Defects

1. **Independent Mutable Counter Drift:**
   In `DiagnosticLedger`, `record_completed` increments both `stage_usage` and `agg_usage` independently. If an exception occurs between operations or if stages are mismatched, counters desynchronize.
2. **Zeroing Reservations on Uncertain Transport:**
   In `DiagnosticLedger.record_failed`, the method blindly subtracts `reservation_tokens` with `max(0, ...)`. In `DecisionAdapter.decide`, if a network timeout or transport failure occurs, `record_failed` is called. This prematurely refunds reserved capacity even though the provider may have processed the prompt or may return a late reply.
3. **Absence of Request Identity & Deduplication:**
   `DiagnosticLedger` tracks anonymous counts, not request records. If a duplicate receipt is delivered, `record_completed` executes a second time, double-counting `completed_calls`, `input_tokens`, and `output_tokens`.
4. **No Conflict Detection:**
   Reusing a request ID with a different stage, basis reference, or configuration is not detected by the legacy ledger.
5. **No Fault Latching on Bound Violation:**
   If a provider reports tokens exceeding the declared upper bound, `record_completed` checks limits only *after* recording the usage, and raises `BudgetExhausted` without latching an immutable fault state. A subsequent call could still attempt admission.

---

## 4. Migration Boundary & Safe Interface Contract

### In Scope for Bend Kernel:
- Request record ADT: `req_id`, `stage`, `basis_ref`, `config_hash`, `max_input`, `max_output`, `transport_state`, `charge`.
- Charge lifecycle: `Pending(bound) -> Settled(input, output, receipt) | Released(evidence)`.
- Transport transitions: `Prepared -> DispatchedIntent -> Sent -> ResponseReceived / OutcomeUnknown / ProvenNotSent`.
- Total transition functions: `apply(State, Event) -> TransitionResult`, `fold(State, EventList) -> State`, `summarize(State) -> AccountingSummary`.
- Derived totals: spent, held, committed; strictly computed by folding unique request records.
- Immutable fault latching on bound violation or conflict.

### Out of Scope (Retained in Python Shell):
- Neural network inference and tokenization.
- OS process management and subprocess execution (`WorkerProcess`, `subprocess.run`).
- SQLite storage transactions, append-only hashing, and WAL mode (`protocollab.storage.Store`).
- Cryptographic signatures and governance delegation checks (`cryptography.hazmat`).
- AALpy automata learning algorithms.
