import Base
import ./Types.bend as T
import ./Definitions.bend as D

def check_tokens(
  tokens_ok: Bool,
  state: T.LedgerState,
  +req_id: String,
  +stage: String,
  +basis_ref: String,
  +config_hash: String,
  +max_input: Nat,
  +max_output: Nat,
  req_tokens: Nat
) -> T.TransitionResult:
  match tokens_ok:
    case False{}:
      T.TransResult{state, T.Rejected{"TOKEN_LIMIT_EXCEEDED"}}
    case True{}:
      match state:
        case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
          new_rec = {T.ReqRecord{
            req_id, stage, basis_ref, config_hash, max_input, max_output,
            T.Prepared{},
            T.ChargePending{req_tokens}
          } : T.RequestRecord}
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, new_rec <> requests, fault},
            T.Accepted{Some{T.InferenceDispatchIntent{
              req_id, stage, basis_ref, config_hash, max_input, max_output
            }}}
          }

def check_calls(
  calls_ok: Bool,
  state: T.LedgerState,
  max_tokens: Nat,
  req_id: String,
  +stage: String,
  basis_ref: String,
  config_hash: String,
  +max_input: Nat,
  +max_output: Nat
) -> T.TransitionResult:
  match calls_ok:
    case False{}:
      T.TransResult{state, T.Rejected{"CALL_LIMIT_EXCEEDED"}}
    case True{}:
      match state:
        case T.LState{run_id, cfg_hash, agg_max_calls, +agg_max_tokens, stage_limits, +requests, fault}:
          +req_tokens = Nat.add(max_input, max_output)
          +stg = stage
          stage_spent = D.sum_stage_spent(requests, stg)
          stage_held  = D.sum_stage_held(requests, stg)
          stage_comm  = Nat.add(stage_spent, stage_held)
          agg_spent   = D.sum_agg_spent(requests)
          agg_held    = D.sum_agg_held(requests)
          agg_comm    = Nat.add(agg_spent, agg_held)
          tokens_ok = Bool.and(
            Nat.is_le(Nat.add(stage_comm, req_tokens), max_tokens),
            Nat.is_le(Nat.add(agg_comm, req_tokens), agg_max_tokens)
          )
          check_tokens(
            tokens_ok,
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault},
            req_id, stage, basis_ref, config_hash, max_input, max_output, req_tokens
          )

def apply_reserve.step(
  limit: Maybe<&2, T.StageLimit>,
  state: T.LedgerState,
  req_id: String,
  stage: String,
  basis_ref: String,
  config_hash: String,
  max_input: Nat,
  max_output: Nat
) -> T.TransitionResult:
  match limit:
    case None{}:
      T.TransResult{state, T.Rejected{"UNDECLARED_STAGE"}}
    case Some{slimit}:
      match slimit:
        case T.SLimit{s_name, max_calls, max_tokens}:
          match state:
            case T.LState{run_id, cfg_hash, +agg_max_calls, agg_max_tokens, stage_limits, +requests, fault}:
              stage_calls = D.count_stage_calls(requests, s_name)
              agg_calls   = D.count_agg_calls(requests)
              calls_ok = Bool.and(
                Nat.is_le(1n+stage_calls, max_calls),
                Nat.is_le(1n+agg_calls, agg_max_calls)
              )
              check_calls(
                calls_ok,
                T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault},
                max_tokens, req_id, stage, basis_ref, config_hash, max_input, max_output
              )

def check_existing_match(
  same: Bool,
  state: T.LedgerState
) -> T.TransitionResult:
  match same:
    case True{}:
      T.TransResult{state, T.DuplicateNoop{}}
    case False{}:
      match state:
        case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, Some{"CONFLICTING_REQUEST_IDENTITY"}},
            T.ConflictFault{"CONFLICTING_REQUEST_IDENTITY"}
          }

def apply_reserve.check_existing(
  existing: Maybe<&2, T.RequestRecord>,
  state: T.LedgerState,
  req_id: String,
  +stage: String,
  basis_ref: String,
  config_hash: String,
  max_input: Nat,
  max_output: Nat
) -> T.TransitionResult:
  match existing:
    case Some{rec}:
      match rec:
        case T.ReqRecord{e_id, e_stage, e_basis, e_cfg, e_mi, e_mo, e_tr, e_ch}:
          same = Bool.and(
            String.eq(stage, e_stage),
            Bool.and(
              String.eq(basis_ref, e_basis),
              Bool.and(
                String.eq(config_hash, e_cfg),
                Bool.and(
                  Nat.is_eq(max_input, e_mi),
                  Nat.is_eq(max_output, e_mo)
                )
              )
            )
          )
          check_existing_match(same, state)
    case None{}:
      match state:
        case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, +stage_limits, requests, fault}:
          limit = D.get_stage_limit(stage_limits, stage)
          apply_reserve.step(
            limit,
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault},
            req_id, stage, basis_ref, config_hash, max_input, max_output
          )

def apply_reserve(
  state: T.LedgerState,
  +req_id: String,
  stage: String,
  basis_ref: String,
  config_hash: String,
  max_input: Nat,
  max_output: Nat
) -> T.TransitionResult:
  match state:
    case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, +requests, fault}:
      match fault:
        case Some{reason}:
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, Some{reason}},
            T.Rejected{"STATE_FAULT_LATCHED"}
          }
        case None{}:
          apply_reserve.check_existing(
            D.find_request(requests, req_id),
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, None{}},
            req_id, stage, basis_ref, config_hash, max_input, max_output
          )

def apply_dispatch.step(
  existing: Maybe<&2, T.RequestRecord>,
  state: T.LedgerState
) -> T.TransitionResult:
  match existing:
    case None{}:
      T.TransResult{state, T.Rejected{"UNKNOWN_REQUEST"}}
    case Some{rec}:
      match rec:
        case T.ReqRecord{id, stage, basis_ref, config_hash, max_input, max_output, transport, charge}:
          match transport:
            case T.Prepared{}:
              match state:
                case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
                  updated = {T.ReqRecord{
                    id, stage, basis_ref, config_hash, max_input, max_output,
                    T.DispatchedIntent{},
                    charge
                  } : T.RequestRecord}
                  T.TransResult{
                    T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, D.update_request(requests, updated), fault},
                    T.Accepted{None{}}
                  }
            case T.DispatchedIntent{}:
              T.TransResult{state, T.DuplicateNoop{}}
            case T.Sent{}:
              T.TransResult{state, T.DuplicateNoop{}}
            case T.ResponseReceived{}:
              T.TransResult{state, T.DuplicateNoop{}}
            case T.ProvenNotSent{}:
              T.TransResult{state, T.Rejected{"ALREADY_FAILED"}}
            case T.OutcomeUnknown{}:
              T.TransResult{state, T.Rejected{"OUTCOME_UNKNOWN_CANNOT_REDISPATCH"}}

def apply_dispatch(state: T.LedgerState, req_id: String) -> T.TransitionResult:
  match state:
    case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, +requests, fault}:
      match fault:
        case Some{reason}:
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, Some{reason}},
            T.Rejected{"STATE_FAULT_LATCHED"}
          }
        case None{}:
          apply_dispatch.step(
            D.find_request(requests, req_id),
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, None{}}
          )

def check_settle_same(
  same: Bool,
  state: T.LedgerState
) -> T.TransitionResult:
  match same:
    case True{}:
      T.TransResult{state, T.DuplicateNoop{}}
    case False{}:
      match state:
        case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, Some{"CONFLICTING_USAGE_RECEIPT"}},
            T.ConflictFault{"CONFLICTING_USAGE_RECEIPT"}
          }

def check_settle_overflow(
  is_overflow: Bool,
  state: T.LedgerState,
  id: String,
  stage: String,
  basis_ref: String,
  config_hash: String,
  max_input: Nat,
  max_output: Nat,
  input_tokens: Nat,
  output_tokens: Nat,
  receipt_hash: String
) -> T.TransitionResult:
  match is_overflow:
    case True{}:
      match state:
        case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
          updated = {T.ReqRecord{
            id, stage, basis_ref, config_hash, max_input, max_output,
            T.ResponseReceived{},
            T.ChargeSettled{input_tokens, output_tokens, receipt_hash}
          } : T.RequestRecord}
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, D.update_request(requests, updated), Some{"BOUND_VIOLATION_FAULT"}},
            T.ConflictFault{"BOUND_VIOLATION_FAULT"}
          }
    case False{}:
      match state:
        case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
          updated = {T.ReqRecord{
            id, stage, basis_ref, config_hash, max_input, max_output,
            T.ResponseReceived{},
            T.ChargeSettled{input_tokens, output_tokens, receipt_hash}
          } : T.RequestRecord}
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, D.update_request(requests, updated), None{}},
            T.Accepted{None{}}
          }

def apply_settle.step(
  rec: T.RequestRecord,
  state: T.LedgerState,
  +input_tokens: Nat,
  +output_tokens: Nat,
  receipt_hash: String
) -> T.TransitionResult:
  match rec:
    case T.ReqRecord{id, stage, basis_ref, config_hash, +max_input, +max_output, transport, charge}:
      match charge:
        case T.ChargeSettled{ex_in, ex_out, ex_hash}:
          same = Bool.and(
            Nat.is_eq(input_tokens, ex_in),
            Bool.and(
              Nat.is_eq(output_tokens, ex_out),
              String.eq(receipt_hash, ex_hash)
            )
          )
          check_settle_same(same, state)
        case T.ChargePending{bound}:
          actual = Nat.add(input_tokens, output_tokens)
          is_overflow = Nat.is_gt(actual, bound)
          check_settle_overflow(
            is_overflow, state, id, stage, basis_ref, config_hash,
            max_input, max_output, input_tokens, output_tokens, receipt_hash
          )
        case T.ChargeReleased{ev_hash}:
          match state:
            case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
              T.TransResult{
                T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, Some{"CONFLICTING_USAGE_AFTER_RELEASE"}},
                T.ConflictFault{"CONFLICTING_USAGE_AFTER_RELEASE"}
              }

def apply_settle.find_step(
  existing: Maybe<&2, T.RequestRecord>,
  state: T.LedgerState,
  input_tokens: Nat,
  output_tokens: Nat,
  receipt_hash: String
) -> T.TransitionResult:
  match existing:
    case None{}:
      T.TransResult{state, T.Rejected{"UNKNOWN_REQUEST"}}
    case Some{rec}:
      apply_settle.step(rec, state, input_tokens, output_tokens, receipt_hash)

def apply_settle(
  state: T.LedgerState,
  req_id: String,
  input_tokens: Nat,
  output_tokens: Nat,
  receipt_hash: String
) -> T.TransitionResult:
  match state:
    case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, +requests, fault}:
      match fault:
        case Some{reason}:
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, Some{reason}},
            T.Rejected{"STATE_FAULT_LATCHED"}
          }
        case None{}:
          apply_settle.find_step(
            D.find_request(requests, req_id),
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, None{}},
            input_tokens, output_tokens, receipt_hash
          )

def apply_timeout.step(
  existing: Maybe<&2, T.RequestRecord>,
  state: T.LedgerState
) -> T.TransitionResult:
  match existing:
    case None{}:
      T.TransResult{state, T.Rejected{"UNKNOWN_REQUEST"}}
    case Some{rec}:
      match rec:
        case T.ReqRecord{id, stage, basis_ref, config_hash, max_input, max_output, transport, charge}:
          match transport:
            case T.ResponseReceived{}:
              T.TransResult{state, T.DuplicateNoop{}}
            case T.ProvenNotSent{}:
              T.TransResult{state, T.Rejected{"ALREADY_FAILED"}}
            case T.OutcomeUnknown{}:
              T.TransResult{state, T.DuplicateNoop{}}
            case T.Prepared{}:
              match state:
                case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
                  updated = {T.ReqRecord{
                    id, stage, basis_ref, config_hash, max_input, max_output,
                    T.OutcomeUnknown{},
                    charge
                  } : T.RequestRecord}
                  T.TransResult{
                    T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, D.update_request(requests, updated), fault},
                    T.Accepted{None{}}
                  }
            case T.DispatchedIntent{}:
              match state:
                case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
                  updated = {T.ReqRecord{
                    id, stage, basis_ref, config_hash, max_input, max_output,
                    T.OutcomeUnknown{},
                    charge
                  } : T.RequestRecord}
                  T.TransResult{
                    T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, D.update_request(requests, updated), fault},
                    T.Accepted{None{}}
                  }
            case T.Sent{}:
              match state:
                case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
                  updated = {T.ReqRecord{
                    id, stage, basis_ref, config_hash, max_input, max_output,
                    T.OutcomeUnknown{},
                    charge
                  } : T.RequestRecord}
                  T.TransResult{
                    T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, D.update_request(requests, updated), fault},
                    T.Accepted{None{}}
                  }

def apply_timeout(state: T.LedgerState, req_id: String, reason: String) -> T.TransitionResult:
  match state:
    case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, +requests, fault}:
      match fault:
        case Some{f_reason}:
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, Some{f_reason}},
            T.Rejected{"STATE_FAULT_LATCHED"}
          }
        case None{}:
          apply_timeout.step(
            D.find_request(requests, req_id),
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, None{}}
          )

def apply_failure.step(
  existing: Maybe<&2, T.RequestRecord>,
  state: T.LedgerState,
  evidence_hash: String
) -> T.TransitionResult:
  match existing:
    case None{}:
      T.TransResult{state, T.Rejected{"UNKNOWN_REQUEST"}}
    case Some{rec}:
      match rec:
        case T.ReqRecord{id, stage, basis_ref, config_hash, max_input, max_output, transport, charge}:
          match charge:
            case T.ChargePending{tokens}:
              match state:
                case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
                  updated = {T.ReqRecord{
                    id, stage, basis_ref, config_hash, max_input, max_output,
                    T.ProvenNotSent{},
                    T.ChargeReleased{evidence_hash}
                  } : T.RequestRecord}
                  T.TransResult{
                    T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, D.update_request(requests, updated), fault},
                    T.Accepted{None{}}
                  }
            case T.ChargeReleased{ex_hash}:
              T.TransResult{state, T.DuplicateNoop{}}
            case T.ChargeSettled{inp, out, r_hash}:
              T.TransResult{state, T.Rejected{"CANNOT_RELEASE_SETTLED_REQUEST"}}

def apply_failure(state: T.LedgerState, req_id: String, evidence_hash: String) -> T.TransitionResult:
  match state:
    case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, +requests, fault}:
      match fault:
        case Some{f_reason}:
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, Some{f_reason}},
            T.Rejected{"STATE_FAULT_LATCHED"}
          }
        case None{}:
          apply_failure.step(
            D.find_request(requests, req_id),
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, None{}},
            evidence_hash
          )

def apply_transport.step(
  existing: Maybe<&2, T.RequestRecord>,
  state: T.LedgerState,
  boundary: String
) -> T.TransitionResult:
  match existing:
    case None{}:
      T.TransResult{state, T.Rejected{"UNKNOWN_REQUEST"}}
    case Some{rec}:
      match rec:
        case T.ReqRecord{id, stage, basis_ref, config_hash, max_input, max_output, transport, charge}:
          match transport:
            case T.ResponseReceived{}:
              T.TransResult{state, T.DuplicateNoop{}}
            case T.ProvenNotSent{}:
              T.TransResult{state, T.Rejected{"ALREADY_FAILED"}}
            case T.Sent{}:
              T.TransResult{state, T.DuplicateNoop{}}
            case T.Prepared{}:
              match state:
                case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
                  updated = {T.ReqRecord{
                    id, stage, basis_ref, config_hash, max_input, max_output,
                    T.Sent{},
                    charge
                  } : T.RequestRecord}
                  T.TransResult{
                    T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, D.update_request(requests, updated), fault},
                    T.Accepted{None{}}
                  }
            case T.DispatchedIntent{}:
              match state:
                case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
                  updated = {T.ReqRecord{
                    id, stage, basis_ref, config_hash, max_input, max_output,
                    T.Sent{},
                    charge
                  } : T.RequestRecord}
                  T.TransResult{
                    T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, D.update_request(requests, updated), fault},
                    T.Accepted{None{}}
                  }
            case T.OutcomeUnknown{}:
              match state:
                case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
                  updated = {T.ReqRecord{
                    id, stage, basis_ref, config_hash, max_input, max_output,
                    T.Sent{},
                    charge
                  } : T.RequestRecord}
                  T.TransResult{
                    T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, D.update_request(requests, updated), fault},
                    T.Accepted{None{}}
                  }

def apply_transport(state: T.LedgerState, req_id: String, boundary: String) -> T.TransitionResult:
  match state:
    case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, +requests, fault}:
      match fault:
        case Some{f_reason}:
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, Some{f_reason}},
            T.Rejected{"STATE_FAULT_LATCHED"}
          }
        case None{}:
          apply_transport.step(
            D.find_request(requests, req_id),
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, None{}},
            boundary
          )

def apply_validation.step(
  rec: Maybe<&2, T.RequestRecord>,
  state: T.LedgerState,
  outcome: String
) -> T.TransitionResult:
  match rec:
    case None{}:
      T.TransResult{state, T.Rejected{"REQUEST_NOT_FOUND"}}
    case Some{r}:
      T.TransResult{state, T.Accepted{None{}}}

def apply_validation(state: T.LedgerState, req_id: String, outcome: String) -> T.TransitionResult:
  match state:
    case T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, +requests, fault}:
      match fault:
        case Some{f_reason}:
          T.TransResult{
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, Some{f_reason}},
            T.Rejected{"STATE_FAULT_LATCHED"}
          }
        case None{}:
          apply_validation.step(
            D.find_request(requests, req_id),
            T.LState{run_id, cfg_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, None{}},
            outcome
          )

def apply(state: T.LedgerState, event: T.LifecycleEvent) -> T.TransitionResult:
  match event:
    case T.EvReserve{req_id, stage, basis_ref, config_hash, max_input, max_output}:
      apply_reserve(state, req_id, stage, basis_ref, config_hash, max_input, max_output)
    case T.EvDispatchIntent{req_id}:
      apply_dispatch(state, req_id)
    case T.EvTransportObserved{req_id, boundary}:
      apply_transport(state, req_id, boundary)
    case T.EvSettleUsage{req_id, input_tokens, output_tokens, receipt_hash}:
      apply_settle(state, req_id, input_tokens, output_tokens, receipt_hash)
    case T.EvFailureConclusive{req_id, evidence_hash}:
      apply_failure(state, req_id, evidence_hash)
    case T.EvTimeoutUnknown{req_id, reason}:
      apply_timeout(state, req_id, reason)
    case T.EvValidationRecorded{req_id, outcome}:
      apply_validation(state, req_id, outcome)

def result_state(res: T.TransitionResult) -> T.LedgerState:
  match res:
    case T.TransResult{next_state, verdict}:
      next_state

def result_verdict(res: T.TransitionResult) -> T.TransitionVerdict:
  match res:
    case T.TransResult{next_state, verdict}:
      verdict

def fold(events: List<&2, T.LifecycleEvent>, initial: T.LedgerState) -> T.LedgerState:
  match events:
    case Nil{}:
      initial
    case h <> t:
      fold(t, result_state(apply(initial, h)))

def summarize(state: T.LedgerState) -> T.AccountingSummary:
  D.summarize(state)
