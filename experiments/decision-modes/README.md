# Decision Modes Benchmark Experiment (v1)

This experiment configuration is **deliberately execution-disabled by default** (`execution_disabled: true`, `model_port: null`).

## Purpose
Prebenchmark evaluation comparing three decision modes on a shared candidate space:
1. `free_json`: Unconstrained generative JSON with channel extraction and schema validation.
2. `constrained_json`: Constrained generation to the declared finite candidate registry.
3. `candidate_score`: Deterministic log-likelihood evaluation of full candidate continuations.

## Experimental Matrix
- Conditions: `C0`, `C2`
- Decision modes: `free_json`, `constrained_json`, `candidate_score`
- Scenario version: `v3` (purely visible-state-driven labels, no artificial completion hints)

## Integrity Rules
- No silent fallbacks to `WAIT` when a capability is unsupported; unsupported configurations record status `UNSUPPORTED`.
- Distinct lifecycle event accounting: proposed, denied/stale, dispatched, acknowledged, environment_effect_observed, task_relevant_progress.
- Full actor interaction lifecycle emitted across all modes (`actor.raw_proposal`, `actor.proposal_returned` / `actor.proposal_rejected`, `actor.proposed`, `actor.interaction_completed`).
