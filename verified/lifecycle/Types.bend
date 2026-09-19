import Base

type TransportState is Data:
  Prepared{}
  DispatchedIntent{}
  Sent{}
  ResponseReceived{}
  ProvenNotSent{}
  OutcomeUnknown{}

type Charge is Data:
  ChargePending{tokens: Nat}
  ChargeSettled{input_tokens: Nat, output_tokens: Nat, receipt_hash: String}
  ChargeReleased{evidence_hash: String}

type RequestRecord is Data:
  ReqRecord{
    req_id: String,
    stage: String,
    basis_ref: String,
    config_hash: String,
    max_input: Nat,
    max_output: Nat,
    transport: TransportState,
    charge: Charge
  }

type StageLimit is Data:
  SLimit{stage: String, max_calls: Nat, max_tokens: Nat}

type LedgerState is Data:
  LState{
    run_id: String,
    config_hash: String,
    agg_max_calls: Nat,
    agg_max_tokens: Nat,
    stage_limits: List<&2, StageLimit>,
    requests: List<&2, RequestRecord>,
    fault: Maybe<&2, String>
  }

type EffectIntent is Data:
  InferenceDispatchIntent{
    req_id: String,
    stage: String,
    basis_ref: String,
    config_hash: String,
    max_input: Nat,
    max_output: Nat
  }

type LifecycleEvent is Data:
  EvReserve{
    req_id: String,
    stage: String,
    basis_ref: String,
    config_hash: String,
    max_input: Nat,
    max_output: Nat
  }
  EvDispatchIntent{req_id: String}
  EvTransportObserved{req_id: String, boundary: String}
  EvSettleUsage{
    req_id: String,
    input_tokens: Nat,
    output_tokens: Nat,
    receipt_hash: String
  }
  EvFailureConclusive{req_id: String, evidence_hash: String}
  EvTimeoutUnknown{req_id: String, reason: String}
  EvValidationRecorded{req_id: String, outcome: String}

type TransitionVerdict is Data:
  Accepted{intent: Maybe<&2, EffectIntent>}
  DuplicateNoop{}
  Rejected{reason: String}
  ConflictFault{reason: String}

type TransitionResult is Data:
  TransResult{state: LedgerState, verdict: TransitionVerdict}

type StageSummary is Data:
  SSummary{
    stage: String,
    calls_admitted: Nat,
    spent_tokens: Nat,
    held_tokens: Nat,
    committed_tokens: Nat
  }

type AccountingSummary is Data:
  AccSummary{
    total_spent: Nat,
    total_held: Nat,
    total_committed: Nat,
    stage_summaries: List<&2, StageSummary>,
    fault: Maybe<&2, String>
  }
