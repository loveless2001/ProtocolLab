# ProtocolLab — Verified Request Lifecycle & Accounting Semantics

**Version:** 1.0.0-draft
**Status:** Frozen Baseline (Commit B)
**Verification Target:** Pure Bend Kernel (`verified/lifecycle/`)

---

## 1. State Invariants & Record Identity

### 1.1 Request Record Identity
Every request handled by the lifecycle kernel is uniquely identified by `req_id: String`.
Its immutable parameters are bound at admission and cannot change:
- `stage: String`: Diagnostic or evaluation stage (e.g. `"minimal_proposal"`, `"closed_loop"`).
- `basis_ref: String`: Content digest binding the exact governance and epistemic snapshot under which the proposal was decided.
- `config_hash: String`: Digest of the model port configuration and limits.
- `max_input: Nat`: Declared upper bound of prompt tokens for this attempt.
- `max_output: Nat`: Declared upper bound of generated tokens for this attempt.

### 1.2 Transport State
The physical transport progress is tracked independently of validation and accounting:
```text
Prepared -> DispatchedIntent -> Sent -> ResponseReceived
                                     -> OutcomeUnknown
                                     -> ProvenNotSent
```
- `Prepared`: Request reserved and admitted into budget.
- `DispatchedIntent`: An inference intention was emitted for the shell to persist and execute.
- `Sent`: Shell provided attributable evidence that the request crossed the adapter boundary.
- `ResponseReceived`: Raw response bytes or candidate evaluations received from provider.
- `OutcomeUnknown`: Network timeout or connection drop occurred after send; charge retained.
- `ProvenNotSent`: Conclusive local pre-dispatch failure (e.g., token limit exceeded before dispatch); capacity released.

### 1.3 Charge States
For each request, its charge is in exactly one of three states:
- `ChargePending{tokens: Nat}`: Conservative token hold equal to `max_input + max_output`.
- `ChargeSettled{input_tokens: Nat, output_tokens: Nat, receipt_hash: String}`: Authoritative confirmed usage.
- `ChargeReleased{evidence_hash: String}`: Zero-cost release backed by conclusive `ProvenNotSent` evidence.

---

## 2. Derived Totals & Admission Laws

### 2.1 Stage & Run Totals
Totals are **mathematical reductions** over the unique list of request records; there are no separate counter variables:
```text
spent(stage)     = Σ (r.input_tokens + r.output_tokens) for r in Settled requests in stage
held(stage)      = Σ (r.tokens) for r in Pending requests in stage
committed(stage) = spent(stage) + held(stage)

total_spent     = Σ spent(stage)
total_held      = Σ held(stage)
total_committed = total_spent + total_held
```

**Law of Conservation:** Sum each request exactly once. Stage identity cannot mutate to shift charges between budgets. The run aggregate total strictly equals the sum of stage totals.

### 2.2 Admission Rules
An `EvReserve` event for request `r` into stage `s` with reservation `R = r.max_input + r.max_output` is admitted if and only if:
1. `req_id` is fresh (not previously recorded in `requests`).
2. `committed(s) + R <= stage_limit.max_tokens`.
3. `total_committed + R <= agg_max_tokens`.
4. `admitted_calls(s) + 1 <= stage_limit.max_calls`.
5. `total_admitted_calls + 1 <= agg_max_calls`.
6. `state.fault` is `None{}`.

### 2.3 Settlement & Monotonicity
Settlement replaces a `ChargePending{R}` with `ChargeSettled{input, output, receipt}`:
- `spent` increases by `input + output`.
- `held` decreases by `R`.
- `committed` changes by `(input + output) - R`. Since `input + output <= R` for compliant calls, `committed` decreases or remains equal, releasing unused capacity for subsequent requests.
- **Monotonicity:** `spent` is strictly non-decreasing across valid transitions.

### 2.4 Bound Violations (Overflow)
If a provider reports actual usage `input + output > r.max_input + r.max_output`:
- The real reported usage is retained in the record.
- An immutable fault `BoundViolationFault` is latched onto `state.fault`.
- All subsequent reservations and dispatches are blocked. The usage is never truncated or discarded.

---

## 3. Duplication, Replay & Conflicts

1. **Idempotence of Duplicate Receipts:**
   If an `EvSettleUsage` arrives for an already settled request with matching token usage and receipt hash, the kernel returns `DuplicateNoop{}`. State and totals are unchanged.
2. **Conflicting Receipts:**
   If a receipt arrives for an existing request with mismatched token usage or stage parameters, the kernel returns `ConflictFault{}` and latches a fault state, halting automated processing for that request and run.
3. **Deterministic Replay:**
   Folding an initial state over any event trace $E_1 ++ E_2$ is identical to folding over $E_1$ and then folding the resulting state over $E_2$:
   `fold(fold(S, E1), E2) == fold(S, E1 ++ E2)`.
