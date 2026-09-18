import Base
import ./Types.bend as T
import ./Definitions.bend as D
import ./Kernel.bend as K

def fixture_limits() -> List<&2, T.StageLimit>:
  T.SLimit{"stage1", 10n, 1000n} <> (T.SLimit{"stage2", 10n, 1000n} <> Nil{})

def fixture_clean_state() -> T.LedgerState:
  D.empty_state("run_1", "cfg_hash_1", 20n, 2000n, fixture_limits())

def state_reserved() -> T.LedgerState:
  K.result_state(K.apply_reserve(fixture_clean_state(), "req_1", "stage1", "basis_1", "cfg_hash_1", 100n, 50n))

def state_dispatched() -> T.LedgerState:
  K.result_state(K.apply_dispatch(state_reserved(), "req_1"))

def state_transported() -> T.LedgerState:
  K.result_state(K.apply_transport(state_dispatched(), "req_1", "http://provider"))

def state_settled() -> T.LedgerState:
  K.result_state(K.apply_settle(state_transported(), "req_1", 40n, 30n, "receipt_1"))

def state_failed() -> T.LedgerState:
  K.result_state(K.apply_failure(state_reserved(), "req_1", "fail_proof_1"))

def state_timed_out() -> T.LedgerState:
  K.result_state(K.apply_timeout(state_reserved(), "req_1", "read_timeout"))

def latched_state() -> T.LedgerState:
  K.result_state(K.apply_reserve(state_reserved(), "req_1", "stage1", "basis_1", "cfg_hash_1", 200n, 50n))

def state_multi_stage() -> T.LedgerState:
  s1 = state_settled()
  s2 = K.result_state(K.apply_reserve(s1, "req_2", "stage2", "basis_2", "cfg_hash_1", 80n, 40n))
  s3 = K.result_state(K.apply_dispatch(s2, "req_2"))
  s4 = K.result_state(K.apply_transport(s3, "req_2", "http://provider"))
  K.result_state(K.apply_settle(s4, "req_2", 30n, 20n, "receipt_2"))

# LAW 1: Replay Determinism
law replay_deterministic:
  for -events: List<&2, T.LifecycleEvent>
  for -initial: T.LedgerState
  {K.fold(events, initial) == K.fold(events, initial) : T.LedgerState}

# LAW 2: Non-negative Accounting Totals
law nonnegative_totals:
  {Bool.and(
    D.state_totals_nonnegative(fixture_clean_state()),
    Bool.and(
      D.state_totals_nonnegative(state_reserved()),
      D.state_totals_nonnegative(state_settled())
    )
  ) == True{} : Bool}

# LAW 3: Reservation Conservation
law reservation_conservation:
  {Bool.and(
    Nat.is_eq(D.sum_agg_held(D.get_requests(state_reserved())), 150n),
    Bool.and(
      Nat.is_eq(D.sum_agg_spent(D.get_requests(state_reserved())), 0n),
      Bool.and(
        Nat.is_eq(D.sum_agg_held(D.get_requests(state_settled())), 0n),
        Bool.and(
          Nat.is_eq(D.sum_agg_spent(D.get_requests(state_settled())), 70n),
          Bool.and(
            Nat.is_eq(D.sum_agg_held(D.get_requests(state_failed())), 0n),
            Nat.is_eq(D.sum_agg_spent(D.get_requests(state_failed())), 0n)
          )
        )
      )
    )
  ) == True{} : Bool}

# LAW 4: Settlement Monotonicity
law settlement_monotonic:
  {Nat.is_ge(
    D.sum_agg_spent(D.get_requests(state_settled())),
    D.sum_agg_spent(D.get_requests(state_transported()))
  ) == True{} : Bool}

# LAW 5: Single-Flight Dispatch Idempotency
law single_flight_dispatch:
  {K.apply_dispatch(state_dispatched(), "req_1")
    == T.TransResult{state_dispatched(), T.DuplicateNoop{}} : T.TransitionResult}

# LAW 6: Unresolved Charge Isolation
law unresolved_isolation:
  {Bool.and(
    Nat.is_eq(D.sum_agg_held(D.get_requests(state_timed_out())), 150n),
    Nat.is_eq(D.sum_agg_spent(D.get_requests(state_timed_out())), 0n)
  ) == True{} : Bool}

# LAW 7: Request Identity Conflict Immunity
law conflict_immunity:
  {D.get_fault(K.result_state(
    K.apply_reserve(state_reserved(), "req_1", "stage1", "basis_1", "cfg_hash_1", 200n, 50n)
  )) == Some{"CONFLICTING_REQUEST_IDENTITY"} : Maybe<&2, String>}

# LAW 8: Bound Honoring & Overrun Accounting
law bound_honoring:
  {Bool.and(
    D.fault_is(D.get_fault(K.result_state(
      K.apply_settle(state_transported(), "req_1", 120n, 50n, "over_receipt")
    )), "BOUND_VIOLATION_FAULT"),
    Nat.is_eq(D.sum_agg_spent(D.get_requests(K.result_state(
      K.apply_settle(state_transported(), "req_1", 120n, 50n, "over_receipt")
    ))), 170n)
  ) == True{} : Bool}

# LAW 9: Terminal State Finality
law terminal_finality:
  {K.apply_failure(state_settled(), "req_1", "fail_proof")
    == T.TransResult{state_settled(), T.Rejected{"CANNOT_RELEASE_SETTLED_REQUEST"}} : T.TransitionResult}

# LAW 10: Latched State Fault Immunity
law fault_latching:
  {K.apply_reserve(latched_state(), "req_2", "stage1", "basis_2", "cfg_hash_1", 10n, 10n)
    == T.TransResult{latched_state(), T.Rejected{"STATE_FAULT_LATCHED"}} : T.TransitionResult}

# LAW 11: Cross-Stage Independence
law cross_stage_independence:
  {Bool.and(
    Nat.is_eq(D.count_stage_calls(D.get_requests(state_settled()), "stage2"), 0n),
    Bool.and(
      Nat.is_eq(D.sum_stage_spent(D.get_requests(state_settled()), "stage2"), 0n),
      Nat.is_eq(D.sum_stage_held(D.get_requests(state_settled()), "stage2"), 0n)
    )
  ) == True{} : Bool}

# LAW 12: Evidence Integrity & Idempotency
law evidence_integrity:
  {K.apply_failure(state_failed(), "req_1", "fail_proof_1")
    == T.TransResult{state_failed(), T.DuplicateNoop{}} : T.TransitionResult}

# WITNESS 1: Positive Settlement Flow
law witness_positive_settlement:
  {Bool.and(
    Nat.is_eq(D.sum_agg_spent(D.get_requests(state_settled())), 70n),
    Bool.and(
      Nat.is_eq(D.sum_agg_held(D.get_requests(state_settled())), 0n),
      Nat.is_eq(D.count_stage_calls(D.get_requests(state_settled()), "stage1"), 1n)
    )
  ) == True{} : Bool}

# WITNESS 2: Positive Conclusive Failure Release Flow
law witness_positive_release:
  {Bool.and(
    Nat.is_eq(D.sum_agg_spent(D.get_requests(state_failed())), 0n),
    Bool.and(
      Nat.is_eq(D.sum_agg_held(D.get_requests(state_failed())), 0n),
      Nat.is_eq(D.count_stage_calls(D.get_requests(state_failed()), "stage1"), 1n)
    )
  ) == True{} : Bool}

# WITNESS 3: Positive Timeout Isolation Flow
law witness_positive_timeout:
  {Bool.and(
    Nat.is_eq(D.sum_agg_spent(D.get_requests(state_timed_out())), 0n),
    Nat.is_eq(D.sum_agg_held(D.get_requests(state_timed_out())), 150n)
  ) == True{} : Bool}

# WITNESS 4: Multi-Stage Independent Completion
law witness_multi_stage_completion:
  {Bool.and(
    Nat.is_eq(D.sum_stage_spent(D.get_requests(state_multi_stage()), "stage1"), 70n),
    Bool.and(
      Nat.is_eq(D.sum_stage_spent(D.get_requests(state_multi_stage()), "stage2"), 50n),
      Bool.and(
        Nat.is_eq(D.sum_agg_spent(D.get_requests(state_multi_stage())), 120n),
        Nat.is_eq(D.count_agg_calls(D.get_requests(state_multi_stage())), 2n)
      )
    )
  ) == True{} : Bool}
