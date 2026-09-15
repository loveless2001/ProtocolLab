# ProtocolLab v0.1 implementation ledger

The authoritative scope is `ProtocolLab_Design_Spec_v0.1_vi.md`, including its
acceptance matrix and required artifacts. Original supplied assets are preserved
in `design/`. The supplied Pete paper and VWMA document inform provenance and
observational/hypothetical separation. `correctable_agency.pdf` is referenced by
the design but is absent from the supplied directory; the explicit ProtocolLab
contracts remain implementable from the complete design.

Implementation and verification work, in dependency order:

- [x] M0: versioned contracts, private simulator/generator/scorer, capture, registry,
  independent public sensor, real process/OS boundaries.
- [x] M1: Ed25519 control, delegated authority, typed updates, scope/epoch fences,
  per-primitive authorization, durable deduplication, crash reconciliation.
- [x] M2: budgeted and interruptible AALpy L* queries, evidence cache, deterministic
  assumption checks, canonical exports, adaptive fresh-probe admission.
- [x] M3: state-set belief, prediction before dispatch, branch isolation, bounded
  live-clock AND-OR planning, admitted procedures, migration and restart.
- [x] M4: frozen model port and native actor, immutable packets/history retrieval,
  independent monitor/holds, scoped review, paired corrections.
- [x] M5: F/O tracks, C0–C4 and G0–G3, causal ablations, topology splits, locked
  manifests, resource accounting, clustered statistics, trace/report/replay tools.
- [x] Run acceptance matrix B01–O03 with meaningful unit, stateful, race, crash,
  leakage and integrated tests. Retain inspectable native smoke-run artifacts.
- [x] Document reproducible commands, actual validation, failed cases, limitations,
  and remaining requirements. Do not turn implementation tests into H1–H5 claims.

No paid model calls, GPU training, confirmatory study, publishing, or external
resource deployment is needed to implement the runtime. A real frozen checkpoint
or pinned API configuration is a run input, never replaced by a fake LLM score.

Final validation on 2026-09-15: **49 tests passed, zero failures/errors/skips;
42/42 acceptance IDs passed**. The fresh native run completed successfully, and
retained replay and restart checks passed. See [the validation record](docs/VALIDATION.md)
and [machine-readable evidence](artifacts/validation.json) for commands, metrics,
source hashes and the limits of these engineering results.

## Review map

| Design sections | Implementation and retained evidence |
|---|---|
| 3–6, 10; Appendices A/C | `contracts`, `capture`, `storage`, private environment service; strict schemas, transport-attributed observations, append-only journal and blob hashes |
| 7, 9.1–9.4 | `learning`, `modeling/admission`; public L* queries, counted cache/replay, candidate lock, unpredictable fresh probes and attributable retired counterexamples |
| 8, 9.5 | `belief`, `modeling`, `planning`, `procedures`; state-set replay, pure hypothetical simulation, clock-aware search, public execution probes and interpreter interruption checks |
| 11–13, 15–17 | `governance`, `gateway`, `review`, `runtime`, `operator`; signatures, delegation, revision/epoch fences, durable dispatch/clock reconciliation, leases and checkpoints |
| 6, 14, 17 | `isolation`, `worker`, `monitor`; Linux namespaces, restricted mounts, no actor environment route, independent journal anchor and delegated holds |
| 18–22; Appendix E | `evaluation`; F/O, C0–C4, G0–G3, paired interventions, evaluator forks, disjoint topology splits, manifests and clustered metrics |
| 24, 27 | `pyproject.toml`, `uv.lock`, CLI, example manifests, declared priors, source snapshots, exported traces and reports |
| Appendix D | `docs/acceptance-matrix.json` maps all 42 IDs to passing-test requirements; `scripts/check_acceptance.py` consumes actual JUnit results |

The private scorer and true-model C4 control are evaluator-only diagnostics.
In-process environment fixtures are used for unit fault injection; the production
runner and integrated acceptance tests enforce the real OS boundary.

The Pete and VWMA references inform the separation of actual effects, attributed
evidence, inferred state, hypothetical branches, and protected normative state.
Their broader architectures and research claims are not substituted for the
explicit ProtocolLab v0.1 contracts. Original reference files are unchanged.
