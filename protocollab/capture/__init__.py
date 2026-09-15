"""Capture is invoked by authenticated gateway transport, never by actor payload."""

from __future__ import annotations

from protocollab.contracts import ObservationRecord, uid, validate_output

NORMALIZER_REV = "finite-domain/v1"


def normalize(symbol, packet):
    domain = packet["domain"]
    if symbol == "INSPECT" and domain.get("result_code") != "OK":
        raise ValueError("INVALID_SENSOR_RESULT")
    output = (f"INSPECT:{domain['artifact']}:{domain['health']}"
              if symbol == "INSPECT" else domain["result_code"])
    validate_output(symbol, output)
    kept = {"result_code", "artifact", "health"} if symbol == "INSPECT" else {"result_code"}
    losses = [f"unsupported_domain_field:{key}" for key in sorted(set(domain) - kept)]
    if packet.get("note"):
        losses.append("free_text_excluded")
    losses += ["transport_metadata_excluded"]
    known = {"event_id", "resource_id", "causal_command_id", "sequence", "logical_tick", "transport_status", "domain", "note"}
    losses += [f"unsupported_packet_field:{key}" for key in sorted(set(packet) - known)]
    return output, losses


class Capture:
    def __init__(self, store):
        self.store = store

    def receive(self, symbol, packet, resource, command_id, namespace=None):
        # source is assigned by this transport endpoint, not any source field in packet.
        raw_hash = self.store.put_blob(packet)
        if packet.get("resource_id") != resource or packet.get("causal_command_id") != command_id:
            self.store.append("capture", "receipt.invalid_binding", {"raw_hash": raw_hash})
            raise ValueError("RECEIPT_BINDING_MISMATCH")
        if packet.get("transport_status") != "ACKNOWLEDGED":
            self.store.append("capture", "receipt.transport_failure", {"raw_hash": raw_hash})
            raise ValueError("TRANSPORT_FAILURE")
        try:
            output, losses = normalize(symbol, packet)
        except Exception:
            self.store.append("capture", "receipt.normalization_failure", {"raw_hash": raw_hash})
            raise
        with self.store.transaction():
            observation = ObservationRecord(
                observation_id=uid("obs"), namespace=namespace or self.store.namespace,
                resource_id=resource, source_principal_ref="effect_sensor" if symbol == "INSPECT" else "environment_adapter",
                causal_command_id=command_id, seq=self.store.tail[0] + 1,
                logical_tick=packet["logical_tick"], raw_hash=raw_hash,
                normalizer_rev=NORMALIZER_REV, input_symbol=symbol, domain_output=output,
                note_ref=raw_hash if packet.get("note") else None, loss_flags=losses,
            )
            self.store.append("capture", "epistemic.observation", observation.model_dump(),
                              observation.source_principal_ref)
            return observation

    def read_observation(self, observation_id):
        row = self.store.db.execute("SELECT payload FROM events WHERE kind='epistemic.observation' AND json_extract(payload,'$.observation_id')=?",
                                    (observation_id,)).fetchone()
        if row:
            return ObservationRecord.model_validate_json(row[0])
        raise KeyError(observation_id)

    def claim(self, text, principal="actor"):
        return self.store.append("belief", "epistemic.claim", {"text": text, "truth_status": "UNVERIFIED"}, principal)

    def reject_forged(self, payload, principal="actor"):
        self.store.append("capture", "observation.forgery_attempt", {"payload": payload}, principal)
        raise PermissionError("CAPTURE_SOURCE_REQUIRED")
