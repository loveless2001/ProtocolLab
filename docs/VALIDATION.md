# Engineering validation

This record concerns the implementation of ProtocolLab v0.1. Engineering tests
do not establish H1–H5, comparative LLM benefit, or the confirmatory thresholds.

On 2026-09-15, the full suite passed **49 tests in 394.23 seconds**, with zero
failures, errors or skips. All **42 acceptance IDs** passed. Ruff, compilation,
schema export and the unchanged design fixture checks also passed.
The [machine-readable validation record](../artifacts/validation.json) binds
these results to the current source hash and installed package versions.

## Evidence

- [Acceptance results](../artifacts/acceptance-report.json) map the 42 Appendix D
  requirements to passing, non-skipped cases in the
  [complete JUnit result](../artifacts/acceptance.junit.xml).
- [Native run](../runs/final-smoke/report.md): a fresh isolated C3/G3/Track F
  run with public-query learning, fresh admission, planning, live execution,
  independent monitoring, checkpoint and raw traces.
- [Paired harness run](../runs/final-harness-smoke/report.json): one engineering
  topology, C4 diagnostic control, G0/G3, clean and valid/invalid pause cases.
  All six suffixes completed without harness failures. The two accepted pauses
  are recorded as constrained partial progress, not capability failures.
- [Input hashes](../artifacts/source-inputs.sha256) identify the five original
  supplied files. The extracted design package is preserved under `design/`.

The original fixture checker passes as an illustrative logic check. The separate
implementation suite adds real namespace boundaries, public L* learning,
cryptographic controls, dispatch races, interruptions, persistent deduplication,
restart, stateful governance, transport timing, and model-port checks.

The final suite reuses the explicitly retained public-query prefix in
`runs/acceptance-prefix` for four isolated suffix tests. The direct learning test
and `runs/final-smoke` independently run the learner against fresh environments.
The older prefix is declared reused engineering evidence, not a new sealed result.

## Retained native result

| Measure | Result |
|---|---|
| Adaptation / execution | ACTIVE / SUCCESS |
| Learned model size | 15 states |
| Raw / compliant task success | true / true |
| Prediction accuracy / coverage | 10/10 / 10/10 |
| Policy violations / false confirmations | 0 / 0 |
| Learning symbols | 9,578 |
| Fresh admission symbols | 308 |
| Additional procedure probe symbols | 10 |
| Total resets including probes | 1,517 |
| Model calls / tokens | 0 / 0 |
| Verified journal entries / artifacts | 41,453 / 9,918 |
| Recorded wall time | 251.93 seconds |

The wall time was measured while acceptance tests ran concurrently; it is not a
standalone performance benchmark. Per-process CPU and memory are retained in
`runs/final-smoke/episode.json`.

[Read-only replay](../artifacts/native-replay.json) matches all ten live
transitions and performs zero world actions.
[Restart verification](../artifacts/recovery-verification.json) on
`runs/final-recovery` reports RECOVERED at epoch 1, with the same live-world hash
and all 11,425 existing backend command records unchanged.

## Calibration and fixes

The design's default reset budget is 1,000. The alias fixture exceeded it with
this AALpy prefix/consistency configuration. The demo and acceptance configuration
therefore explicitly allow 2,000 resets; the default is unchanged. Learning and
fresh-admission symbol caps remain 10,000 and 2,000 respectively. Exhaustion is
tested to preserve the incumbent and retain partial evidence.

Earlier integration attempts exposed a monitor replay packet exceeding the IPC
limit, nested report values that needed serialization, and delayed IPC replies
that could be attributed to the wrong request. Bounded stream batches, explicit
report serialization, and buffered request-ID matching fix these failures.
Their regression checks are included in the retained suite. Model rollback now
replays actual observations rather than clearing their inferred history.

## Scope and limits

- The actual optional CPU inference worker is tested with a locally constructed
  tiny **untrained** Safetensors checkpoint. This checks immutable weights,
  token accounting and the inference path; it supplies no task-performance score.
  API accounting uses a controlled response fixture. No pretrained checkpoint,
  paid API experiment, GPU job, or confirmatory study was run.
- C4 uses privileged evaluator state and is explicitly non-deployable. Its paired
  smoke cases validate harness/report behavior, not learned-model capability.
  Native planning also voluntarily honors pause in G0; a separate broker test
  verifies that the log-only configuration can dispatch an actor proposal which
  full governance denies.
- Admission means `CONSISTENT_WITH_TESTED_TRACES`. Fresh probes are sampled after
  the immutable candidate is locked; their realized seeds and traces are retained
  for replay. Finite testing does not prove untested dynamics.
- OS isolation requires Linux, bubblewrap and usable user namespaces. The runner
  fails closed when these are unavailable. The acceptance suite ran with namespace
  syscalls enabled outside the enclosing tool sandbox.
- The bundled SQLite backend atomically commits an effect and its command-ID
  receipt. A remote backend needs an equivalent idempotency/reconciliation
  contract; this implementation makes no distributed exactly-once claim.
- Source locks now retain the exact implementation, dependency lock and declared
  priors as a hashed ZIP. Keep the ZIP, experiment lock and scenario archive with
  exported results. Older development runs predate this source-retention feature.
- `correctable_agency.pdf`, named by the specification, was absent. The supplied
  ProtocolLab spec, Pete paper, VWMA document and design package were used without
  modifying the originals.

## Reproduce

```bash
uv sync --locked --extra local-model
uv run pytest --junitxml=artifacts/acceptance.junit.xml
uv run python scripts/check_acceptance.py artifacts/acceptance.junit.xml \
  --output artifacts/acceptance-report.json
uv run ruff check protocollab protocollab_environment tests
python3 design/fixtures/check_design_fixture.py
uv run protocollab demo --output runs/new-demo --seed 7
uv run protocollab verify runs/new-demo
uv run protocollab replay runs/new-demo
```

Use a fresh output directory. Replay verifies retained public history without
performing new world actions. The test suite's optional model-port case skips if
the `local-model` extra is not installed; the recorded full validation installs it.
