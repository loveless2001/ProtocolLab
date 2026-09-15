# Frozen model-port contract

The model port produces text only. It cannot dispatch, approve a model, change
governance, or write an observation. Its output is parsed in the isolated actor
process into the fixed proposal schema. Invalid output or timeout is a recorded
failure followed by a local WAIT; the harness clock continues.

## API

Configure `backend: api`, a stable `model_id`, an HTTPS `endpoint`, and optionally
`credential_env` and `expected_fingerprint`. Do not put credentials in a manifest.
The credential holder sends `Authorization: Bearer ...` when configured.

Request body:

```json
{
  "model_id": "provider-model-snapshot-id",
  "prompt": "serialized public task/belief packet",
  "seed": 7,
  "max_input_tokens": 16384,
  "max_output_tokens": 1024
}
```

Response body:

```json
{
  "model_id": "provider-model-snapshot-id",
  "fingerprint": "provider-fingerprint",
  "text": "{\"kind\":\"ACT\",\"operation\":\"INSPECT\"}",
  "input_tokens": 800,
  "output_tokens": 14
}
```

A provider adapter must enforce the token caps and return actual usage. ProtocolLab
checks identifier/fingerprint consistency and usage, retains request and response
hashes, and records call time and latency. Failed calls may have unknown provider
usage; they still consume a call. API packet admission currently uses UTF-8 byte
length as a conservative preflight bound, rather than claiming exact tokenization.
API runs retain the provider's reproducibility limitations.

## Local checkpoint

Configure `backend: local_frozen_checkpoint`, an existing checkpoint directory,
`model_id`, and `weights_sha256`, `tokenizer_sha256`, `config_sha256`.
`checkpoint_hashes(path)` in `protocollab.actor` computes the required values.
Weights and tokenizer hashes bind canonical maps of filename to file SHA-256;
the config hash binds the raw `config.json` bytes.

The optional worker loads only local Safetensors with `trust_remote_code=False`.
It uses evaluation/inference mode, disables gradients, fixes the random seed,
and counts the tokenizer's actual input/output tokens. The parent enforces a
subprocess deadline. No model training or weight updates are part of v0.1.

## Actor proposals

`ACT` requires an exact registered operation. Other proposals are WAIT, LEARN,
PLAN, SIMULATE, RETRIEVE, APPEAL, and FINISH. LEARN may supply a bounded input
word; SIMULATE changes only a hypothetical branch. RETRIEVE exposes the complete
public history through bounded pages. The displayed page is never called the full
transcript. All execution still goes through current resource, permission,
revision, epoch, quota and per-primitive checks.
