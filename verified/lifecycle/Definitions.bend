import Base
import ./Types.bend as T

def add_if(cond: Bool, val: Nat, acc: Nat) -> Nat:
  match cond:
    case True{}:
      Nat.add(val, acc)
    case False{}:
      acc

def count_agg_calls(requests: List<&2, T.RequestRecord>) -> Nat:
  match requests:
    case Nil{}:
      0n
    case h <> t:
      1n+count_agg_calls(t)

def count_stage_calls(requests: List<&2, T.RequestRecord>, +stage: String) -> Nat:
  match requests:
    case Nil{}:
      0n
    case h <> t:
      match h:
        case T.ReqRecord{id, r_stage, basis_ref, config_hash, max_input, max_output, transport, charge}:
          add_if(String.eq(stage, r_stage), 1n, count_stage_calls(t, stage))

def sum_agg_spent(requests: List<&2, T.RequestRecord>) -> Nat:
  match requests:
    case Nil{}:
      0n
    case h <> t:
      match h:
        case T.ReqRecord{id, r_stage, basis_ref, config_hash, max_input, max_output, transport, charge}:
          match charge:
            case T.ChargeSettled{inp, out, receipt_hash}:
              Nat.add(Nat.add(inp, out), sum_agg_spent(t))
            case T.ChargePending{tokens}:
              sum_agg_spent(t)
            case T.ChargeReleased{evidence_hash}:
              sum_agg_spent(t)

def sum_agg_held(requests: List<&2, T.RequestRecord>) -> Nat:
  match requests:
    case Nil{}:
      0n
    case h <> t:
      match h:
        case T.ReqRecord{id, r_stage, basis_ref, config_hash, max_input, max_output, transport, charge}:
          match charge:
            case T.ChargePending{tokens}:
              Nat.add(tokens, sum_agg_held(t))
            case T.ChargeSettled{inp, out, receipt_hash}:
              sum_agg_held(t)
            case T.ChargeReleased{evidence_hash}:
              sum_agg_held(t)

def sum_stage_spent.put(same: Bool, charge: T.Charge, rest: Nat) -> Nat:
  match charge:
    case T.ChargeSettled{inp, out, receipt_hash}:
      add_if(same, Nat.add(inp, out), rest)
    case T.ChargePending{tokens}:
      rest
    case T.ChargeReleased{evidence_hash}:
      rest

def sum_stage_spent(requests: List<&2, T.RequestRecord>, +stage: String) -> Nat:
  match requests:
    case Nil{}:
      0n
    case h <> t:
      match h:
        case T.ReqRecord{id, r_stage, basis_ref, config_hash, max_input, max_output, transport, charge}:
          sum_stage_spent.put(String.eq(stage, r_stage), charge, sum_stage_spent(t, stage))

def sum_stage_held.put(same: Bool, charge: T.Charge, rest: Nat) -> Nat:
  match charge:
    case T.ChargePending{tokens}:
      add_if(same, tokens, rest)
    case T.ChargeSettled{inp, out, receipt_hash}:
      rest
    case T.ChargeReleased{evidence_hash}:
      rest

def sum_stage_held(requests: List<&2, T.RequestRecord>, +stage: String) -> Nat:
  match requests:
    case Nil{}:
      0n
    case h <> t:
      match h:
        case T.ReqRecord{id, r_stage, basis_ref, config_hash, max_input, max_output, transport, charge}:
          sum_stage_held.put(String.eq(stage, r_stage), charge, sum_stage_held(t, stage))

def find_request.put(h: T.RequestRecord, rest: Maybe<&2, T.RequestRecord>, hit: Bool) -> Maybe<&2, T.RequestRecord>:
  match hit:
    case False{}:
      rest
    case True{}:
      Some{h}

def find_request.match(+h: T.RequestRecord, target_id: String) -> Bool:
  match h:
    case T.ReqRecord{id, stage, basis_ref, config_hash, max_input, max_output, transport, charge}:
      String.eq(target_id, id)

def find_request(requests: List<&2, T.RequestRecord>, +target_id: String) -> Maybe<&2, T.RequestRecord>:
  match requests:
    case Nil{}:
      None{}
    case +h <> t:
      find_request.put(h, find_request(t, target_id), find_request.match(h, target_id))

def update_request.put(new_rec: T.RequestRecord, old_rec: T.RequestRecord, rest: List<&2, T.RequestRecord>, hit: Bool) -> List<&2, T.RequestRecord>:
  match hit:
    case False{}:
      old_rec <> rest
    case True{}:
      new_rec <> rest

def update_request.same(+a: T.RequestRecord, +b: T.RequestRecord) -> Bool:
  match a:
    case T.ReqRecord{id_a, stage_a, basis_a, cfg_a, mi_a, mo_a, tr_a, ch_a}:
      match b:
        case T.ReqRecord{id_b, stage_b, basis_b, cfg_b, mi_b, mo_b, tr_b, ch_b}:
          String.eq(id_a, id_b)

def update_request(requests: List<&2, T.RequestRecord>, +new_rec: T.RequestRecord) -> List<&2, T.RequestRecord>:
  match requests:
    case Nil{}:
      Nil{}
    case +h <> t:
      update_request.put(new_rec, h, update_request(t, new_rec), update_request.same(new_rec, h))

def get_stage_limit.put(h: T.StageLimit, rest: Maybe<&2, T.StageLimit>, hit: Bool) -> Maybe<&2, T.StageLimit>:
  match hit:
    case False{}:
      rest
    case True{}:
      Some{h}

def get_stage_limit.match(+h: T.StageLimit, target_stage: String) -> Bool:
  match h:
    case T.SLimit{s_name, max_calls, max_tokens}:
      String.eq(target_stage, s_name)

def get_stage_limit(limits: List<&2, T.StageLimit>, +stage_name: String) -> Maybe<&2, T.StageLimit>:
  match limits:
    case Nil{}:
      None{}
    case +h <> t:
      get_stage_limit.put(h, get_stage_limit(t, stage_name), get_stage_limit.match(h, stage_name))

def summarize_stage(+requests: List<&2, T.RequestRecord>, limit: T.StageLimit) -> T.StageSummary:
  match limit:
    case T.SLimit{+s_name, max_calls, max_tokens}:
      +r = requests
      calls = count_stage_calls(r, s_name)
      +spent = sum_stage_spent(r, s_name)
      +held  = sum_stage_held(r, s_name)
      comm  = Nat.add(spent, held)
      T.SSummary{s_name, calls, spent, held, comm}

def summarize_stage_list(limits: List<&2, T.StageLimit>, +requests: List<&2, T.RequestRecord>) -> List<&2, T.StageSummary>:
  match limits:
    case Nil{}:
      Nil{}
    case h <> t:
      +reqs = requests
      summarize_stage(reqs, h) <> summarize_stage_list(t, requests)

def summarize(state: T.LedgerState) -> T.AccountingSummary:
  match state:
    case T.LState{run_id, config_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
      +r = requests
      +tot_spent = sum_agg_spent(r)
      +tot_held  = sum_agg_held(r)
      tot_comm  = Nat.add(tot_spent, tot_held)
      stg_sums  = summarize_stage_list(stage_limits, r)
      T.AccSummary{tot_spent, tot_held, tot_comm, stg_sums, fault}

def empty_state(
  run_id: String,
  cfg_hash: String,
  max_calls: Nat,
  max_tokens: Nat,
  limits: List<&2, T.StageLimit>
) -> T.LedgerState:
  T.LState{run_id, cfg_hash, max_calls, max_tokens, limits, Nil{}, None{}}

def get_requests(state: T.LedgerState) -> List<&2, T.RequestRecord>:
  match state:
    case T.LState{run_id, config_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
      requests

def get_fault(state: T.LedgerState) -> Maybe<&2, String>:
  match state:
    case T.LState{run_id, config_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
      fault

def state_totals_nonnegative(state: T.LedgerState) -> Bool:
  match state:
    case T.LState{run_id, config_hash, agg_max_calls, agg_max_tokens, stage_limits, requests, fault}:
      +r = requests
      Bool.and(
        Nat.is_le(0n, sum_agg_spent(r)),
        Nat.is_le(0n, sum_agg_held(r))
      )

def fault_is(fault: Maybe<&2, String>, expected: String) -> Bool:
  match fault:
    case None{}:
      False{}
    case Some{reason}:
      String.eq(reason, expected)

def result_verdict(res: T.TransitionResult) -> T.TransitionVerdict:
  match res:
    case T.TransResult{next_state, verdict}:
      verdict

def verdict_emits_permit(v: T.TransitionVerdict) -> Bool:
  match v:
    case T.Accepted{intent}:
      match intent:
        case None{}:
          False{}
        case Some{i}:
          True{}
    case T.DuplicateNoop{}:
      False{}
    case T.Rejected{reason}:
      False{}
    case T.ConflictFault{reason}:
      False{}

def accounting_equal(a: T.AccountingSummary, b: T.AccountingSummary) -> Bool:
  match a:
    case T.AccSummary{a_spent, a_held, a_comm, a_stages, a_fault}:
      match b:
        case T.AccSummary{b_spent, b_held, b_comm, b_stages, b_fault}:
          Bool.and(
            Nat.is_eq(a_spent, b_spent),
            Bool.and(
              Nat.is_eq(a_held, b_held),
              Nat.is_eq(a_comm, b_comm)
            )
          )
