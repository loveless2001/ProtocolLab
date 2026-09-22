# Test suite review — 2026-09-22

Keep the safety, accounting, recovery, isolation, and evaluation-integrity scenarios. Simplify the diagnostic scaffolding, consolidate overlapping scenarios, and avoid repeating full learning runs merely to prepare reporting tests. There is one clear duplicate test to delete immediately, several obsolete or ineffective test bodies to replace, and a larger set of safe merges once their unique assertions move to the surviving tests.

This is a review, not an implemented cleanup. Production code and repository tests were left unchanged. Existing uncommitted work was preserved.

Fresh collection found **329 cases in 35 test modules**, implemented by 282 named test functions plus the Hypothesis state machine. The retained full run took **1,173.85 seconds**. Its 93 source/test/proof hashes still match this checkout. Timings below come from that run; this review did not rerun the full suite. Passing results alone do not establish that each test exercises the behavior in its name.

## Findings to address before pruning

1. **Two response helpers leak their HTTP mock between tests.** [The budget helper](../tests/test_budget_per_request.py#L44) and [the scenario helper](../tests/test_scenario_discrimination.py#L38) assign `urllib.request.urlopen` directly. Their callers subsequently use `monkeypatch`, whose teardown restores the helper's fake instead of the original function. Review probes executed the existing tests and confirmed the leak after teardown in both cases. Replace the duplicate response classes with a small shared helper that uses `monkeypatch.setattr` exactly once. This is a correctness repair, not just tidying.

2. **A06 can pass without a response ever starting.** [The response-started regression](../tests/verified/test_attempt_contract_a01_a14.py#L281) catches any exception from `port.generate` and substitutes its own `JSON_DECODE_TRUNCATED` label. A probe supplied a pre-send `ConnectionRefusedError` and an unused server stand-in; the existing test still passed. Together with the leaked HTTP mock, this can conceal failure to exercise the real response-reading boundary. Keep A06, but assert the server received the request and retain/assert the actual response-started evidence and decoding failure. An unrelated connection or identity failure must fail this test. The attempt contract explicitly requires this real boundary.

3. **The purported task-completion test passes on failure.** [The Stage 3 test](../tests/test_stage3_accounting.py#L124) sends `INSPECT`, `SUBMIT_A`, `FINISH` and asserts only lower bounds on action counts. The probe observed `status=FAIL`, `task_completed=False`, and `PREMATURE_FINISH: INSUFFICIENT_EVIDENCE`, while every assertion passed. Replace this test with a valid completion sequence and exact completion/status assertions. Its current counter assertions can be absorbed by the existing accounting cases. The real-runtime completion tests elsewhere do not establish the diagnostic harness's positive completion result.

4. **The mutation-classifier test exercises a copy of the implementation.** [This test](../tests/verified/test_boundary_hardening.py#L297) defines and tests its own local `classify` function. It never calls [the production mutation checker](../scripts/verified_kernel/mutate.py#L268). A probe made the production entry point fail if called; the test still passed. Remove the local copy and feed controlled subprocess results through the real checker, retaining mismatch, syntax/import failure, crash, and successful-check cases. The executable mutation tests remain necessary.

5. **Two acceptance helpers do not establish their advertised end-to-end behavior.** [The positive broker case](../tests/verified/test_attempt_contract_a01_a14.py#L1239) manually constructs a proposal, uses dummy authorization and `backend=None`, and accepts any of five status strings. It never connects a successful model attempt to broker execution. [The no-execution case](../tests/verified/test_attempt_contract_a01_a14.py#L1292) checks gateway replay, not action dispatch. Preserve their useful assertions, but replace the positive shell with a real successful adapter → attempt → proposal → broker test and assert the failed attempt produces no broker action. Neither can serve as a reason to remove the real runtime success tests.

6. **One scoring case fails for the wrong reason.** [Case B of strict candidate validation](../tests/test_prebenchmark_diagnostic_integrity.py#L231) supplies the string `"NaN"` and accepts generic `INFERENCE_FAILED`. Its observed cause was `TypeError: must be real number, not str`. [The focused scoring test](../tests/test_scoring_hardening.py#L112) actually supplies floating-point NaN and asserts `NON_FINITE_SCORE`; keep that coverage. Consolidate these into explicitly named invalid-type and nonfinite-number rows, asserting the intended rejection reason. Do not count the string case as another numeric-NaN regression.

## Concrete deletion and merge candidates

“Merge” means preserve the listed assertions in the destination before removing the original function. Parameterization reduces duplicated code; it need not reduce the number of executed scenarios.

| Current tests / helper | Recommendation | Assertions or distinctions to retain |
| --- | --- | --- |
| `test_actor_diagnostic.py::test_unknown_chat_template_rejected` | **Delete now.** | The identical invalid `qwen-chat` configuration and exception are already checked by `test_input_admission.py::test_chatml_omits_qwen_think_tags_and_unknown_template_is_rejected`. |
| `test_reference_traces.py::InMemoryStore` | **Delete unused helper.** | No test instantiates or imports it; remove the now-unused typing import too. This changes no cases. |
| `test_actor_diagnostic.py::test_stage_3_closed_loop_inspect_passes` and `::test_stage_3_fails_wait_only` | **Merge into Stage 3 accounting.** | The richer destination cases are `test_stage_3_novel_inspect_counts_effective` and `test_stage_3_wait_only_yields_zero_dispatches`. Preserve the source's port-construction path (`port=None`) and `INSPECT` dispatch-outcome assertion; a parameter for supplied/default port keeps that distinction explicit. |
| `test_actor_and_experiments.py::test_parse_proposal_strips_think_channel_and_rejects_incomplete_think` | **Merge into `test_reasoning_channels.py`.** | Valid and incomplete envelopes are already covered. Move the direct `proposal_text` assertion and wrapped forbidden `GRANT` case before deleting the old test. Preserve literal-tag and duplicate-envelope adversarial cases. |
| `test_boundary_hardening.py::test_lifecycle_owner_interleaved_stage_routing` and `::test_lifecycle_owner_duplicate_completion_routes_to_same_stage` | **Merge into the latter.** | Same two-stage setup and first completion. Retain the first test's `SettleUsage` event-kind assertion. Same-stage FIFO and receipt-bound duplicate routing remain different scenarios. |
| `test_boundary_hardening.py` three `test_find_bend_app_*` functions | **One parameterized discovery table.** | Latest semantic version, direct-layout precedence, environment override, and vendored fallback. The multi-version scenario is currently duplicated; use exact expected paths. |
| `test_boundary_hardening.py::test_timeout_triggers_record_timeout_preserves_reservation` and `::test_transport_failures_route_to_timeout_unknown` | **One parameterized exception table.** | Include `TimeoutError`, subprocess timeout, wrapped URL error, and connection reset; preserve error evidence, `TimeoutUnknown`, and absence of conclusive release. Keep SHADOW ledger comparison and cyclic exception-chain termination separate. |
| `test_attempt_contract_a01_a14.py::test_a02_replay_completed_attempt_and_receipt`, `::test_replay_does_not_mutate_returned_outcome`, and `::test_restart_recovers_retained_raw_response_and_usage` | **Consolidate under A02 with same-owner/reopened-owner cases.** | Backend-call count, unchanged cost, no-new-execution flag, original object's immutability, retained raw response and tokens. Do not merge unfinished-validation recovery or cross-owner late settlement into this simpler completed-replay scenario. |
| `test_attempt_contract_a01_a14.py::test_a14_replay_under_changed_immutable_binding` and `::test_binding_restart_conflict` | **Parameterized A14 binding matrix.** | Changed input/backend/caps, same owner and reconstructed owner, and conflict before any invocation. Vary binding fields individually so one detected change cannot mask an unchecked field. |
| `test_attempt_contract_a01_a14.py::test_a09_genuine_not_sent_proof` and `::test_not_sent_shadow_releases_reservation` | **Merge under A09, covering both modes.** | Zero held/spent/confirmed tokens plus SHADOW active-ledger release/failure accounting. Keep the new duplicate-validation, permit-consumption order, and failed-commit regressions independent. |
| `test_attempt_contract_a01_a14.py::test_a10_timeout_then_late_verified_receipt` and `::test_late_receipt_duplicate_does_not_double_count_active` | **Merge under A10.** | Active and Bend totals after both deliveries, then replay without another backend call, updated raw response/usage, and conflict retained if usage exceeds an individual cap. Both current fixtures use output 15 against cap 10; make the intended normal/over-bound distinction explicit. |
| Four negative tests in `test_scoring_hardening.py`, plus `test_candidate_score_strict_validation` | **A parameterized rejection matrix with shared valid response builder.** | Missing/wrong identity, changed fingerprint, missing/substituted candidates, invalid score type, and numeric nonfinite scores. Preserve one actual HTTP response path and one direct adapter validation path; they exercise different layers. Keep successful selection/scoring semantics. |
| `test_scenario_discrimination.py` four cases | **Share response fixture; parameterize version/policy.** | Historical v1 behavior, both constant-policy failures in v2, and a positive harness case. The positive fixture selects by call index, so describe it as harness scoring of scripted answers, not proof of an input-sensitive model. Preserve separate v3 field/evidence checks. |
| Boundary charge-validation cases, reference traces 15/16, reasoning-envelope rejection variants | **Optional small parameterizations.** | Keep every input/state distinction and useful case IDs. These are already cheap; prioritize more substantial duplicated setup first. |

The three `compose_and_admit_input` tests in `test_diagnostic_modes_and_pipeline.py` and the tests in `test_input_admission.py` look similar but call different entry points. Keep focused pipeline-unit coverage plus port integration: the latter proves the transport is not invoked and audit evidence is recorded. Likewise, ledger arithmetic, adapter accounting, real SQLite lifecycle recovery, and executable Bend traces are separate boundaries.

## Coverage that should survive any cleanup

| Coverage | Keep because |
| --- | --- |
| Design acceptance tests across `test_acceptance_boundaries.py`, `test_vertical_slice.py`, `test_recovery_planning_metrics.py`, and the clock-crash case in `test_actor_and_experiments.py` | The 42 acceptance IDs map to 30 named functions. Preserve their behavior and update `docs/acceptance-matrix.json` if names move; a name alone is not coverage. |
| Real attempt A01–A14 tests and the later evidence/validation regressions | Wrong identity, uncertainty, release, final settlement, restart, concurrent owners, rollback and validation ordering fail differently. SHADOW and AUTHORITATIVE implementations both need coverage. |
| The recent not-sent replay, cancellation/permit ordering and failed-consumption-commit tests | These reproduced real defects or check a transaction boundary. Both serial orders and separate SQLite connections are essential; fake-store tests cannot replace them. |
| `test_prebenchmark_regressions.py` | Controls arriving during inference, queued at turn entry, before procedure dispatch, and during step-result processing are different interleavings. Also preserves evidence-gap and stale-completion rules. |
| `test_negative_cases.py`, `test_correction_scoring.py`, `test_paired_statistics.py` | Prevent false resistance, false correction success, and invalid capability estimates. Missing rows, missing scores, ineligible pairs, duplicate rows and wrong comparison context are not interchangeable. All 13 paired-statistics cases together took about 0.05 seconds. |
| `test_stateful_governance.py` | Generated action/control sequences cover combinations absent from isolated examples. This run took about 8 seconds; it was not the performance bottleneck. |
| `test_worker_transport.py`, real sandbox checks, and a real isolated learner/planner/monitor path | Preserve stale-response correlation and actual process/network/private-state separation. Keep success paths as well as denial paths. |
| `test_evidence_bundle.py` | Secret exclusion, tamper rejection, replay without world actions and independent rescoring are distinct from ordinary episode success. |
| `test_reference_traces.py`, `test_law_mutations.py`, `test_shadow_monitoring.py` | Executable bridge behavior, proof sensitivity, and durable monitoring/promotion eligibility are different claims. Proof success does not replace Python/SQLite tests. |
| `test_local_model_port.py`, `test_typesafe_jev_port.py`, `test_smoke_ollama_port.py` | Each exercises a distinct supported transport/backend. Keep one real tiny local checkpoint test rather than replacing all inference interfaces with mocks. |

## Where runtime simplification matters

These three cases account for **989.43 seconds, or 84.3%** of the retained full-suite wall time. JUnit case time includes fixture setup, so the F-track row includes the shared isolated learning prefix.

| Case | Retained time | Recommended simplification |
| --- | ---: | --- |
| `test_study_reports_unfired_cases_and_preserves_full_delivery_path` | 527.80 s | Separate fast scheduler/coverage accounting from one real shared-prefix delivery/replay integration. The current case learns a model, runs ten suffixes, and replays/rescores every suffix. Retain valid, invalid, and unfired interventions and both C0/C2 paths; use synthetic episodes for combinatorial reporting assertions. |
| `test_isolated_full_learner_planner_monitor[F]` | 253.78 s | Keep this real integration boundary. Reuse one freshly generated, immutable learned prefix for compatible tests, giving every suffix its own store. Do not share mutable runtimes or seed acceptance with the privileged oracle model. |
| `test_learn_admit_plan_and_execute` | 207.85 s | It independently relearns the same seed/configuration. Move its unique fresh-probe, learned-alias and structural-growth assertions to the retained real learning fixture/integration before considering removal of the second full learning pass. Preserve direct in-process learner wiring with a smaller focused test. |

The exact savings from fixture sharing require a prototype and measurement: manifests, admission provenance, configuration, and isolation must remain valid. Removing three expensive tests outright is not justified. Similarly, rerunning replay/rescore on fewer suffixes must still cover the distinct audit paths.

For local iteration, an explicit slow/integration marker would allow a quick selection while the complete suite remains required for release/acceptance. CI additionally runs a fresh native CLI demo after pytest; keep one CLI wiring/evidence smoke, and avoid making it a further full learner validation if the same boundary can be covered by a supported smaller fixture. The entire `tests/verified/` group took only **27.73 seconds**; shrinking its safety scenarios would do little for runtime.

## Module disposition

| Module under `tests/` | Decision |
| --- | --- |
| `test_acceptance_boundaries.py` | Keep acceptance scenarios and signed-input boundary tests. |
| `test_actor_and_experiments.py` | Keep experiment locks, actor language, governance shim, operator inbox and clock recovery; move overlapping reasoning-envelope case. |
| `test_actor_diagnostic.py` | Keep schema/truncation, stage gating, prompt rendering and scoring-window checks; prune/merge the identified smoke duplicates. |
| `test_budget_per_request.py` | Keep per-request rather than cumulative billing and admission/preflight checks; repair duplicated HTTP helper. |
| `test_correction_scoring.py` | Keep all distinct raw-trace scenarios. |
| `test_decision_routing.py` | Keep stage routing and empty-registry boundary; share response setup. |
| `test_diagnostic_budget_ledger.py` | Keep arithmetic, restart and configuration binding. Rename the failure test if useful: it calls `record_failed`, not a timeout path. |
| `test_diagnostic_modes_and_pipeline.py` | Keep pipeline units, authorization-versus-correctness distinction, mode execution and C2/renderer/seed propagation. |
| `test_evidence_bundle.py` | Keep both cases. |
| `test_evidence_preservation.py` | Keep adapter claim forwarding and actual port delivery evidence. |
| `test_input_admission.py` | Keep formatted-byte limits, exact boundary, Unicode, templates, preserved claims and transport suppression. |
| `test_isolated_integration.py` | Keep real integration; simplify learning setup and reuse immutable prefix. |
| `test_local_model_port.py` | Keep tiny real CPU inference. Assert CPU placement/use rather than globally requiring that the host has no CUDA device. |
| `test_negative_cases.py` | Keep delivery stages, provenance and adversarial authorization scenarios. |
| `test_paired_statistics.py` | Keep every scenario; no useful deletion identified. |
| `test_prebenchmark_diagnostic_integrity.py` | Keep delivery/usage/lifecycle/progress/v3/compute boundaries; merge scoring validation with precise reasons. The free-JSON test name mentions unknown operations but its body only sends `PLAN`; correct the name or cover that input explicitly. |
| `test_prebenchmark_regressions.py` | Keep distinct interleavings and evidence-contract regressions. |
| `test_reasoning_channels.py` | Keep parser adversaries and retained-channel integration; absorb earlier envelope smoke. |
| `test_recovery_planning_metrics.py` | Keep all acceptance semantics. The E04/E05 test's matched-budget and pseudoreplication guards remain useful alongside the new statistics tests. |
| `test_scenario_discrimination.py` | Consolidate version/policy cases; repair global mock leak. |
| `test_scoring_hardening.py` | Consolidate rejection matrix; keep exact reasons and successful selection. |
| `test_smoke_design.py` | Keep design/artifact authorization locks; cheap and distinct from runtime behavior. |
| `test_smoke_ollama_port.py` | Keep actual request shape, identity/budget locks and retained transport evidence. |
| `test_stage3_accounting.py` | Keep exact accounting, novelty and premature finish; replace ineffective positive completion case. |
| `test_stateful_governance.py` | Keep generated sequence coverage. |
| `test_study_coverage.py` | Keep plan and delivery semantics; split expensive integration from reporting combinations. |
| `test_typesafe_jev_port.py` | Keep backend mapping, secret exclusion and complete probability vector. |
| `test_vertical_slice.py` | Keep acceptance behavior; consolidate repeated learning only after transferring unique assertions. |
| `test_worker_transport.py` | Keep both delayed success/error response cases. |
| `verified/test_attempt_contract_a01_a14.py` | Keep safety scenarios; consolidate A02/A09/A10/A14 overlaps and strengthen A06/positive broker evidence. |
| `verified/test_boundary_hardening.py` | Share owner/port setup, parameterize related inputs, replace copied classifier; keep fault injection distinct from real-kernel integration. |
| `verified/test_ci_toolchain_pinning.py` | Keep a small configuration guard. Strengthen to inspect actual workflow env/install/check commands if revised; raw substring presence alone can pass for unused values or comments. |
| `verified/test_law_mutations.py` | Keep proof identity and all semantic mutations. Make the source-hash test independent of compiler availability; only actual compiler tests need the toolchain. |
| `verified/test_reference_traces.py` | Keep all transition scenarios, simplify event builders, remove unused store; traces 15/16 can share a parameterized settled-state setup. |
| `verified/test_shadow_monitoring.py` | Keep atomic queue/audit, concurrency, divergence, rejection and time-gate cases. |

## Evidence and cleanup order

Review artifacts are local under `runs/test-suite-review-20260922/`: `inventory.json`, `collection.log`, `test_review_probes.py`, `probe-observations.jsonl`, and two probe JUnit/log pairs. The first probe invocation passed four review assertions in 3.65 seconds; the second passed two in 2.52 seconds. These probes confirm weaknesses in existing tests; they are not additional product acceptance coverage. Performance evidence is `runs/spec-alignment-20260922/final-acceptance.junit.xml` and its matching source manifest.

To reproduce all six review probes from the repository root:

```sh
PYTHONPATH=tests:. .venv/bin/python -m pytest -q runs/test-suite-review-20260922/test_review_probes.py
```

Recommended order: fix test isolation and false-positive assertions; remove the exact duplicate and unused helper; consolidate diagnostic and lifecycle setup while preserving named cases; then prototype learning-prefix reuse and remeasure. After cleanup, run affected tests alone and in combinations that previously leaked mocks, then the complete suite and acceptance checker. Preserve the real A01–A14 integration gate, both lifecycle modes, and all 42 design acceptance IDs. No tests should disappear solely to hit a smaller numerical target.
