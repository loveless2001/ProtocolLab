# Native engineering evidence

These are replay and scoring artifacts, not confirmatory research results.
The ZIPs exclude private signing keys, control inboxes, leases and SQLite
WAL/SHM files. Each archive includes an allowlist manifest and SHA-256 hashes.
`private/world.sqlite` is retained exclusively for evaluator replay and scoring;
never provide it to an actor or learner.

Here, `private` means hidden from the actor during the simulator experiment.
The [database-content audit](simulator-data-audit.json) checks all eight bundled
databases: exact simulator schemas, 22,988 reproducible synthetic effects,
only R/replica resources, finite A/B/BASE/NONE states, and empty receipt notes.
They contain generated toy-protocol data rather than real deployment records.

## Fresh run of the final code

- [Fresh native bundle](fresh-native.zip) · [SHA-256](fresh-native.zip.sha256)
- [Run summary and source hash](fresh-native-summary.json)
- [Bundle verification, replay and rescore](fresh-native-bundle-verification.json)
- [Direct journal verification](fresh-native-verify.json) · [replay](fresh-native-replay.json) · [rescore](fresh-native-score.json)
- [Reproducible validation source](validation-source.zip) · [SHA-256](validation-source.zip.sha256)

The final C3/G3/Track F run is ACTIVE/SUCCESS, with 10/10 predicted live
transitions, zero policy violations, zero false confirmations and zero model
calls. It used 9,578 learning symbols, 308 admission symbols and 1,517 resets.
The source hash is `08f4f8d7ee69c4ad09b5e3758d6ec544416f3d60bc3003b73bab9f827a6e3139`.
Bundle verification returns VERIFIED and its recomputed scores return MATCH.
The validation source ZIP includes the final tests, workflow, dependency lock
and source fixtures so a new checkout can run the full checks without ignored
local files. JUnit exports replace only the local hostname.

```bash
uv run protocollab verify-bundle artifacts/prebenchmark/fresh-native.zip
```

## Reviewed smoke evidence

- [Reviewed native run](reviewed-native.zip) · [SHA-256](reviewed-native.zip.sha256) · [verification and rescore](reviewed-native-verification.json)
- [Reviewed six-episode harness](reviewed-harness.zip) · [SHA-256](reviewed-harness.zip.sha256) · [verification and rescore](reviewed-harness-verification.json)

These preserve the runs previously referenced as `runs/final-smoke` and
`runs/final-harness-smoke`. Their recorded metrics and original source ZIPs
are retained. Recomputed metrics use the corrected scorer and expose differences
explicitly. They do not upgrade the earlier engineering evidence into research
findings or CI passes.

## Independent replay

From the repository root:

```bash
(cd artifacts/prebenchmark && sha256sum -c reviewed-native.zip.sha256)
uv run protocollab verify-bundle artifacts/prebenchmark/reviewed-native.zip
uv run protocollab verify-bundle artifacts/prebenchmark/reviewed-harness.zip
```

`verify-bundle` checks the outer hash, every manifest file, the journal chain,
all stored blobs and the exported public journal. It replays live observations
against the retained model, then recomputes episode scores from simulator effects.
It performs zero world actions. The archive's `source-*.zip` and experiment lock
retain the exact runtime implementation and dependency lock used by that run.

For direct inspection, extract an archive into a fresh directory. The native
archive can then be passed to `protocollab verify`, `replay`, or `score`.
Historical scoring differences cause `score` to exit nonzero; `verify-bundle`
reports them without altering recorded metrics.
