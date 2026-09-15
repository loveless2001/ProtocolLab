-- ProtocolLab v0.1 design-time starter DDL, not a finished storage implementation.
-- Owner services must have separate permissions. Never mount this DB in the actor.
-- PRIVATE environment/scorer state is not in this store.
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK(seq >= 0),
    owner TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    source_principal_ref TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    previous_event_hash TEXT,
    event_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(namespace, seq)
);
CREATE INDEX IF NOT EXISTS events_by_kind ON events(namespace, event_kind, seq);

CREATE TABLE IF NOT EXISTS state_revisions (
    owner TEXT NOT NULL,
    namespace TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    manifest_hash TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    PRIMARY KEY(owner, namespace, revision)
);
CREATE TABLE IF NOT EXISTS active_state (
    owner TEXT NOT NULL,
    namespace TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY(owner, namespace),
    FOREIGN KEY(owner, namespace, revision)
      REFERENCES state_revisions(owner, namespace, revision)
);
CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    namespace TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    source_principal_ref TEXT NOT NULL,
    causal_command_id TEXT,
    logical_tick INTEGER NOT NULL,
    raw_hash TEXT NOT NULL,
    normalizer_rev TEXT NOT NULL,
    input_symbol TEXT NOT NULL,
    domain_output TEXT NOT NULL,
    note_ref TEXT
);
CREATE INDEX IF NOT EXISTS observation_namespace ON observations(namespace, event_id);

CREATE TABLE IF NOT EXISTS model_artifacts (
    model_hash TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    revision INTEGER NOT NULL,
    artifact_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('DRAFT','REPLAY_VALIDATED','PROBE_VALIDATED','ACTIVE','SUPERSEDED','QUARANTINED')),
    admission_report_ref TEXT,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    UNIQUE(namespace, revision)
);
CREATE TABLE IF NOT EXISTS procedure_artifacts (
    procedure_hash TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    model_hash TEXT NOT NULL,
    artifact_ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('DRAFT','ACTIVE','STALE','REJECTED','QUARANTINED')),
    event_id TEXT NOT NULL REFERENCES events(event_id)
);
CREATE TABLE IF NOT EXISTS query_traces (
    trace_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    reset_contract_rev TEXT NOT NULL,
    normalizer_rev TEXT NOT NULL,
    input_word_hash TEXT NOT NULL,
    output_word_hash TEXT NOT NULL,
    raw_event_refs TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('COMPLETE','PARTIAL','DETERMINISM_VIOLATION','INVALID')),
    event_id TEXT NOT NULL REFERENCES events(event_id)
);
-- Do not UNIQUE(namespace, word_hash): contradictory traces must not be overwritten.
CREATE INDEX IF NOT EXISTS query_by_word
  ON query_traces(namespace, reset_contract_rev, normalizer_rev, input_word_hash);

CREATE TABLE IF NOT EXISTS control_state (
    scope_ref TEXT PRIMARY KEY,
    control_epoch INTEGER NOT NULL CHECK(control_epoch >= 0),
    run_status TEXT NOT NULL CHECK(run_status IN ('RUNNING','PAUSED','HOLD','RECOVERY_REQUIRED')),
    goal_rev INTEGER NOT NULL CHECK(goal_rev >= 0),
    authority_rev INTEGER NOT NULL CHECK(authority_rev >= 0),
    event_id TEXT NOT NULL REFERENCES events(event_id)
);
CREATE TABLE IF NOT EXISTS control_replay_guard (
    issuer_principal_ref TEXT NOT NULL,
    issuer_seq INTEGER NOT NULL,
    nonce TEXT NOT NULL,
    accepted_event_id TEXT NOT NULL REFERENCES events(event_id),
    PRIMARY KEY(issuer_principal_ref, issuer_seq),
    UNIQUE(issuer_principal_ref, nonce)
);

CREATE TABLE IF NOT EXISTS actions (
    command_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL UNIQUE,
    namespace TEXT NOT NULL,
    scope_ref TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    control_epoch INTEGER NOT NULL,
    goal_rev INTEGER NOT NULL,
    model_rev INTEGER NOT NULL,
    belief_rev INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
      'PROPOSED','VALIDATED','READY','DISPATCHED','ACKNOWLEDGED','OUTCOME_OBSERVED',
      'DENIED','STALE','CANCELLED_BEFORE_DISPATCH','DISPATCH_UNCERTAIN','FAILED')),
    dispatch_seq INTEGER,
    receipt_ref TEXT,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    UNIQUE(namespace, scope_ref, idempotency_key)
);
CREATE INDEX IF NOT EXISTS actions_pending ON actions(scope_ref, status);
CREATE TABLE IF NOT EXISTS hypothetical_branches (
    branch_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    base_belief_rev INTEGER NOT NULL,
    base_model_hash TEXT NOT NULL,
    artifact_ref TEXT NOT NULL,
    branch_kind TEXT NOT NULL CHECK(branch_kind = 'HYPOTHETICAL'),
    event_id TEXT NOT NULL REFERENCES events(event_id)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    journal_seq INTEGER NOT NULL,
    external_monitor_digest TEXT,
    event_id TEXT NOT NULL REFERENCES events(event_id)
);

-- Server-side command-ID dedup + actual environment apply must be atomic in the
-- reference simulator's PRIVATE store. That store is separate, and this DDL does
-- not create a cross-database transaction or a network exactly-once guarantee.
