# ProtocolLab

A Python implementation of the supplied [Vietnamese design specification](ProtocolLab_Design_Spec_v0.1_vi.md).
It learns a black-box deployment protocol through public I/O, admits a finite
Mealy model, and executes bounded plans through signed control and dispatch fences.

The runtime separates observations, beliefs, hypothetical branches, learned
models, procedures, commitments, authority, and history. A command acknowledgment
does not count as deployment success. Completion needs two public INSPECT readings
at least `Task.minimum_tick_gap` logical ticks apart (default: two), with no
newer contradictory reading and independent privileged scoring afterward.

## Run locally

Requires Linux with unprivileged user namespaces, `bubblewrap`, Python 3.12.3,
and `uv`. Dependencies are pinned in `uv.lock`.

```bash
uv sync --locked
uv run protocollab demo --output runs/demo --seed 7
uv run protocollab verify runs/demo
uv run protocollab replay runs/demo
uv run protocollab score runs/demo
```

The demo uses the **native C3 control**, with no model API calls or GPU work.
The environment, learner, planner, and monitor run in separate user/PID/network
namespaces. Actor and learner mounts exclude private environment code, databases,
signing keys and credentials. An unavailable namespace facility is a hard error;
there is no automatic unrestricted runtime fallback.

The design's initial reset cap remains 1,000. The alias-fixture demo explicitly
uses 2,000: the implemented prefix/consistency L* configuration required 1,517
resets, including validation and procedure probes, in the initial smoke run.
The 10,000 learning-symbol and 2,000 admission-symbol caps remain unchanged.
This calibration is recorded in the demo manifest.

## What a run retains

```text
runs/demo/
  experiment.lock.json        code/configuration lock
  source-<hash>.zip            exact implementation and dependency lock for replay
  episode.json                adaptation, execution and resource accounting
  metrics.json                capability/evidence/compliance metrics
  owner/owner.sqlite          owner state, actions, query cache, journal, artifacts
  private/world.sqlite        simulator state and privileged scoring evidence
  monitor/monitor.sqlite      independent stream digest and alerts
  keys/                      operator keys and public delegation manifest
  public/journal.jsonl        attributable event stream
  public/artifacts/           content-addressed models, probes, receipts, checkpoint
```

`private/` and `keys/` are not actor inputs or public report assets. Historical
receipts, predictions, corrections, and candidate failures remain in the journal.
Model admission has the label `CONSISTENT_WITH_TESTED_TRACES`; it never certifies
untested dynamics. Budget exhaustion retains the incumbent and the partial evidence.

## Control and recovery

The demo creates separate Ed25519 keys for the operator, permission owner,
reviewer and monitor. The owner processes signed files from its control inbox
at sequencer boundaries. For example, while an episode is running:

```bash
uv run protocollab control runs/demo \
  --private-key runs/demo/keys/operator.key --key-id operator-key \
  --principal operator --verb PAUSE_DISPATCH --scope R
```

The command returns a receipt-file path. `QUEUED` means the event was submitted;
only an `ACCEPTED` receipt establishes that its signature, delegation, replay
guard and expected revision passed. If another control wins the revision race,
resubmit a newly signed event against the current revision.

Supported verbs are PAUSE_DISPATCH, RESUME, GRANT, REVOKE, REDIRECT,
REVIEW_RESOLUTION, and delegated HOLD. A pending appeal does not lift a correction.
Resume does not restore revoked capabilities. Pause stops new actor mutations;
already-started work may still finish on subsequent TICKs.

```bash
uv run protocollab recover runs/demo
```

Recovery verifies artifacts and journal anchors, reconciles durable command IDs,
replays belief and invalidates outstanding plans. A damaged or incomplete history
produces `RECOVERY_REQUIRED`. A process lease prevents two owners from running
against the same store. The simulator's application and command-ID ledger are
atomic in its own database; no distributed exactly-once claim is made.

## Frozen model port

C0, C1 and C2 require an explicit model configuration. C3 needs none; C4 is a
privileged upper-bound diagnostic. The API port uses the small provider-neutral
JSON contract described in [the model-port guide](docs/model-port.md).
The local port accepts a hash-pinned Safetensors checkpoint and runs Transformers
with local files only, remote code disabled, inference mode and frozen parameters.

```bash
uv sync --locked --extra local-model
```

Optional model packages and weights are not needed for the native runtime.
No actual checkpoint or API credentials were supplied with the design package.
An unconfigured model condition is rejected rather than replaced with a mock LLM.

## Experiments

```bash
uv run protocollab generate --output scenarios.private.json --seed 0
uv run protocollab lock experiments/native-pilot.yaml scenarios.private.json \
  --output experiments/native-pilot.lock.json
uv run protocollab run experiments/native-pilot.lock.json scenarios.private.json \
  --split development --output runs/pilot
```

The default generator creates 6 development, 6 validation, and 48 sealed
topologies across three strata. Topology hashes cannot overlap across splits.
Code/configuration changes after locking are rejected before execution. Preparing
an archive does not run or open the sealed evaluation.

Each experiment lock retains a content-addressed source ZIP and checks its hash.
Keep the lock, source ZIP, and scenario archive together when copying a study.

Track F freezes M/P at the prefix boundary; belief still updates. Track O allows
subsequent adaptation. Shared-prefix runs clone an evaluator-owned prefix into
fresh episode instances so compared conditions get the same raw evidence and
prefix accounting. The supplied shared policy explicitly includes fixed public
coverage and native L* queries; this source advantage is declared in the manifest.
Autonomous-query results are reported separately.

Reports preserve per-topology, condition, governance setting, scenario class and
track. Paired confidence intervals resample topology clusters, not individual
transitions. Missing research comparisons do not pass research gates. The initial
engineering smoke result does **not** establish H1–H5 or the proposed capability
and correction thresholds.

## Tests and source assets

```bash
uv run pytest
uv run ruff check protocollab protocollab_environment tests
uv run protocollab schema
python3 design/fixtures/check_design_fixture.py
```

To produce the machine-readable acceptance report:

```bash
uv run pytest --junitxml=artifacts/acceptance.junit.xml
uv run python scripts/check_acceptance.py artifacts/acceptance.junit.xml \
  --output artifacts/acceptance-report.json
```

The suite includes real namespace-isolation and integrated learning tests as well
as stateful governance, race, crash, admission, evidence, planning and reporting
checks. In an outer sandbox that denies namespace/network syscalls, run the suite
in a Linux session where bubblewrap is allowed; the isolation checks do not silently skip.

The unchanged supplied package is under `design/`. The implementation follows
the explicit ProtocolLab design, informed by the local Pete and VWMA references.
Implementation status and requirement coverage are recorded in
[IMPLEMENTATION.md](IMPLEMENTATION.md).
The recorded engineering results and their limits are in
[docs/VALIDATION.md](docs/VALIDATION.md).

## Fixes before benchmarking

The [review fixes](docs/REVIEW-FIXES.md) describe immutable decision provenance,
equal live feedback, scoped correction scoring and sandbox startup checks.
The [sanitized evidence bundles](artifacts/prebenchmark/README.md) make the
previously referenced smoke runs available for independent replay and scoring.
See [validation](docs/VALIDATION.md) for separate local, CI and research status.
