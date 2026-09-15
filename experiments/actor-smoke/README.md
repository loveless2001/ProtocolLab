# Six-episode actor smoke

Status: **design locked; model unselected; execution disabled**. This package
does not contain an executable experiment lock or authorize paid inference.
`manifest.pending.yaml` intentionally fails `ExperimentManifest` validation until
`model_port` names an actual frozen trained model. The checkpoint used in CI is
an untrained plumbing fixture, not the model for this smoke.

## Fixed design

| Dimension | Setting |
|---|---|
| Source | Code hash in `design.lock.json` |
| Data | One generated development topology; zero validation/sealed scenarios |
| Conditions | C0 and C2; G3; frozen Track F |
| Public evidence | One native shared prefix, copied identically to every suffix |
| Seed | 7 |
| Cases per condition | Clean, authenticated pause, invalid pause claim |
| Timing | `before_plan`, reached by both actor conditions before the first request |
| Size | Six suffix episodes plus one native prefix |
| Model | One selected model, identical identity and decoding limits in both conditions |

This is a diagnostic, not an estimate of comparative capability or resistance.
One topology cannot support a topology-clustered confidence interval. The source
of the shared prefix is explicitly native L* collection, not model discovery.
The development archive contains evaluator-only dynamics and solutions; never
copy those fields into model input. No sealed data is generated or used here.

## Caps and stop rules

- Eight live turns and eight model calls per suffix: **48 model calls maximum**.
- Actual model prefix calls: **zero** under the declared native shared-prefix policy.
- `model-limits.json` fixes 16,384 input tokens, 256 output tokens and a 30-second
  call deadline. Across suffixes, reservation bounds are 786,432 input tokens and
  12,288 output tokens (**798,720 total**). API input admission uses the existing
  conservative UTF-8 byte bound; usage reporting still uses provider token counts.
- Each prefix/suffix invocation has a 300-second CPU and 600-second wall cap,
  checked by the existing runtime. These are cooperative runtime checks;
  subprocess/transport calls retain their own deadlines. The nominal sum across
  seven invocations is 2,100 CPU seconds and 4,200 wall seconds, not a hard
  operating-system kill deadline for the entire study.
- Paid spend authorized by this preparation: **zero**. Model identity, any API
  endpoint/fingerprint, rate-based worst-case price and a spending limit must be
  settled before preparing an executable lock or making real model calls.
- No automatic reruns, extra seeds, topology expansion or threshold tuning.
- Missing application, delivery evidence or completed negative-case interaction
  makes coverage incomplete. The study command exits nonzero and expansion stops.
- Inspect protected-state changes, dispatch violations, false completion,
  authenticated pause behavior, task outcomes, malformed proposals and cost.
  Keep measured failures; do not relabel them as harness passes. The valid pause
  binds to the horizon, so constrained task noncompletion is expected.

## Bind a model later

1. Verify each SHA-256 in `design.lock.json` and the current code fingerprint.
2. Copy the pending manifest to a new run directory. Fill `model_port` using the
   [existing contract](../../docs/model-port.md): a checkpoint with weight,
   tokenizer and config hashes, or a pinned API snapshot and compatible endpoint.
   Copy all three values from `model-limits.json` into that model configuration.
   Credentials stay in the named environment variable, not the manifest.
3. Review the resulting model identity, exact limits and maximum cost. Then use
   the existing `protocollab lock` command to create a **new executable lock**.
   Do not overwrite this design lock or substitute a dummy provider identifier.

After binding and authorizing execution, the existing commands are:

```bash
uv run --no-sync protocollab lock runs/actor-smoke/manifest.yaml \
  experiments/actor-smoke/development.json --output runs/actor-smoke/experiment.lock.json
uv run --no-sync protocollab run runs/actor-smoke/experiment.lock.json \
  experiments/actor-smoke/development.json --split development --output runs/actor-smoke/study
```

Check `study-plan.json`, `report.json` and `metrics.jsonl`. Expect four declared,
scheduled and applied interventions, with both invalid cases stored, delivered
and given a completed actor interaction. Authentication/application and ACA
cover the two valid controls; claim-delivery stages apply to the invalid cases.
For each suffix run `protocollab verify`, `protocollab replay` and `protocollab score`.
C0 has no executable learned model, so its model replay comparison is `null`;
journal verification and privileged rescoring still apply.

Retain final request/input evidence, responses, stage references, owner journal,
simulator effects and the executable lock locally. Audit and sanitize model
transcripts before publication: the native-only `bundle` command deliberately
rejects model-port runs. Successful transport is not proof of model comprehension.
