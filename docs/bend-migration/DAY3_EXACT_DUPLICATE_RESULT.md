# Day 3 exact-duplicate shadow result

The Day 3 rotation case ran on **24 September 2026 UTC** against the persistent
Phase 2 soak store. The run used deterministic loopback transport, with one
physical call per request. Its 25 request pairs added 200 shadow comparisons;
all 200 matched between the active Python lifecycle and Bend observer. The
monitored total after this run was **600/600 matches**, with zero divergence,
uncomparable records, or pending observations. The 14-day monitor remains
`IN_PROGRESS`.

| Replay path | Observed result across 25 pairs |
|---|---:|
| Exact `Reserve`, `DispatchIntent`, and `SettleUsage` replays | 75 `DuplicateNoop` verdicts; no second charge |
| Identical validation report through `AttemptGateway` | 25 `RecordValidationDuplicate` results; no new lifecycle event |
| Separate direct-owner `ValidationRecorded` replay | 25 `Accepted` comparisons; unchanged ledger state |

The [daily traffic contract](SHADOW_TRAFFIC_MENU.md) distinguishes the gateway
retry from the direct-owner diagnostic. The original menu described every
replayed event as `DuplicateNoop`; the retained evidence was initially marked
partial on that literal reading. The corrected assessment reclassifies this
run as **PASS** under the clarified contract. The original runner output and
initial partial assessment are preserved alongside the corrected assessment.
No additional traffic was sent for the reassessment.

## Retained evidence

The artifacts are in the local ignored run directory
`runs/lifecycle-phase2-qwen35-4b-20260920/daily/2026-09-24-day-03-exact-duplicate/`.
The public repository does not include the owner SQLite store or raw run
artifacts. The following hashes identify the retained files:

| Artifact | SHA-256 |
|---|---|
| `manifest.json` | `6e814efc309d35719b7ac16ca5b1e908e6a4cad3312b34e8a8ee61650bf5a15e` |
| `comparison-report.json` | `41896ba2a67298fd92570cb679d3c4eb5454f084c5a3ae6d018c8f28dfb90d31` |
| `daily-record.json` | `400ee3c84881b9d1eb4b5c2ba38ea8343b94d2cb04d8bd85013cf5f0157200c4` |
| `case-assessment.corrected.json` | `3e30b2af3dce3dc21844abc4c87c6b3194e240244b30d55bc36d5900c56a8677` |
| `run_exact_duplicate.py` | `d2a2f395bf7a20fecfb544a8bdfdb9457496c8ada0d7d96313b5e720dfd3dec4` |

The run used repository commit `5c7e1938e18ef85b715464701884dfd5a5095f96`
and monitor ID `lifecycle-phase2-20260920`. Its journal anchor is sequence
`3898`, event hash
`48e8767b8cc25ca370d4b386f1b0a2ed931211e9108b755e994658d87c16ea90`.
These are lifecycle agreement and accounting observations. They do not establish
model capability or completion of the Phase 2 observation gate.
