"""Offline operator signing and owner-inbox control submission."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from protocollab.contracts import ControlEvent, canonical, uid
from protocollab.evaluation.runner import demo_authorities
from protocollab.governance import Delegation, sign_control


def generate_keys(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    keys, credentials = demo_authorities()
    manifest = {}
    for key_id, delegation in keys.items():
        key, _ = credentials[delegation.principal]
        path = directory / f"{delegation.principal}.key"
        raw = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        manifest[key_id] = {"principal": delegation.principal, "public_key_hex": delegation.public_key.hex(),
                            "verbs": sorted(delegation.verbs), "scopes": sorted(delegation.scopes)}
    with (directory / "root-manifest.json").open("x") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
    return manifest


def load_authorities(path):
    manifest = json.loads(Path(path).read_text())
    return {key_id: Delegation(entry["principal"], bytes.fromhex(entry["public_key_hex"]),
                               frozenset(entry["verbs"]), frozenset(entry["scopes"]), entry.get("revoked", False))
            for key_id, entry in manifest.items()}


def submit_control(run, private_key, key_id, principal, verb, scope="R", **fields):
    owner = Path(run) / "owner"
    if not (owner / "owner.sqlite").is_file():
        raise FileNotFoundError("OWNER_STORE_NOT_FOUND")
    connection = sqlite3.connect(f"file:{owner / 'owner.sqlite'}?mode=ro", uri=True)
    try:
        state = json.loads(connection.execute("SELECT payload FROM state WHERE owner='governance' AND key='control'").fetchone()[0])
        row = connection.execute("SELECT MAX(issuer_seq) FROM replay_guard WHERE principal=?", (principal,)).fetchone()
        issuer_seq = 0 if row[0] is None else row[0] + 1
    finally:
        connection.close()
    key = serialization.load_pem_private_key(Path(private_key).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("ED25519_KEY_REQUIRED")
    event = ControlEvent(event_id=uid("control"), issuer_principal_ref=principal, issuer_seq=issuer_seq,
        nonce=uid("nonce"), scope_ref=scope, expected_revision=state["revision"], verb=verb,
        auth_evidence_ref="signed-operator-envelope", **fields)
    envelope = sign_control(event, key, key_id)
    inbox = owner / "control-inbox"
    inbox.mkdir(exist_ok=True)
    temporary = inbox / f"{event.event_id}.pending"
    temporary.write_bytes(canonical(envelope))
    final = temporary.with_suffix(".json")
    temporary.rename(final)
    return {"status": "QUEUED", "event_id": event.event_id, "receipt_path": str(final.with_suffix('.receipt'))}
