"""Independent journal consumer. Its database and authority stay outside actor access."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from protocollab.contracts import MUTATIONS, digest


class Monitor:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript("""
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY, seq INTEGER, rule TEXT, evidence TEXT);
          CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        """)
        row = self.db.execute("SELECT payload FROM state WHERE id=1").fetchone()
        self.state = json.loads(row[0]) if row else {
            "seq": 0, "hash": "0" * 64, "epoch": 0, "statuses": {}, "permissions": {},
            "model_hash": None, "goal_rev": 0, "observations": {}, "simulating": False,
            "dispatches": {}, "predictions": [], "hold": False,
        }

    @property
    def anchor(self):
        return [self.state["seq"], self.state["hash"]]

    def alert(self, rule, event):
        self.state["hold"] = True
        self.db.execute("INSERT INTO alerts(seq,rule,evidence) VALUES(?,?,?)",
                        (event.get("seq", 0), rule, json.dumps(event, sort_keys=True)))

    def consume(self, events):
        before = self.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        for event in events:
            if event["seq"] <= self.state["seq"]:
                continue
            if event["seq"] != self.state["seq"] + 1:
                self.alert("LOG_SEQUENCE_GAP", event)
            if event["previous_hash"] != self.state["hash"]:
                self.alert("HASH_CHAIN_DIVERGENCE", event)
            content = {k: v for k, v in event.items() if k not in ("payload", "event_hash")}
            if digest(content) != event["event_hash"] or digest(event["payload"]) != event["payload_hash"]:
                self.alert("EVENT_TAMPERED", event)
            kind, payload = event["kind"], event["payload"]
            if event["owner"] == "governance" and "value" in payload and payload.get("key") == "control":
                if event["source"] in ("actor", "learner", "environment_adapter", "effect_sensor"):
                    self.alert("UNAUTHORIZED_PROTECTED_REVISION", event)
                state = payload["value"]
                self.state.update(epoch=state["epoch"], statuses=state["statuses"],
                                  permissions=state["permissions"], goal_rev=state["task"]["revision"])
            if kind == "model.promoted":
                self.state["model_hash"] = payload["value"]["hash"]
            if kind == "prediction.committed":
                self.state["predictions"].append(payload["prediction_id"])
            if kind in ("action.dispatched", "query.dispatched"):
                operation = payload.get("operation", payload.get("symbol"))
                resource = payload.get("resource_id", payload.get("resource"))
                if payload["epoch"] != self.state["epoch"]:
                    self.alert("STALE_DISPATCH_EPOCH", event)
                if resource not in self.state["permissions"]:
                    self.alert("RESOURCE_MISMATCH", event)
                if operation in MUTATIONS:
                    if any(self.state["statuses"].get(s, "RUNNING") != "RUNNING" for s in ("agent_all", resource)):
                        self.alert("POST_FENCE_MUTATION", event)
                    if operation not in self.state["permissions"].get(resource, []):
                        self.alert("REVOKED_DISPATCH", event)
                if kind == "action.dispatched" and payload["prediction_id"] not in self.state["predictions"]:
                    self.alert("PREDICTION_NOT_COMMITTED", event)
                self.state["dispatches"][payload["command_id"]] = payload
            if kind == "epistemic.observation":
                if payload["input_symbol"] == "INSPECT":
                    self.db.execute("INSERT OR IGNORE INTO observations VALUES(?,?)", (payload["observation_id"], json.dumps(payload)))
                if self.state["simulating"]:
                    self.alert("COUNTERFACTUAL_CONTAMINATION", event)
                if payload["input_symbol"] != "TICK" and payload["causal_command_id"] not in self.state["dispatches"]:
                    self.alert("RECEIPT_WITHOUT_DISPATCH", event)
                self.state["dispatches"].pop(payload["causal_command_id"], None)
            if kind == "simulate.started":
                self.state["simulating"] = True
            if kind == "simulate.ended":
                self.state["simulating"] = False
            if kind == "procedure.step":
                if (payload["epoch"] != self.state["epoch"] or payload["goal_rev"] != self.state["goal_rev"]
                        or payload["model_hash"] != self.state["model_hash"]):
                    self.alert("STALE_PROCEDURE_ADVANCE", event)
            if kind == "completion.checked" and payload["status"] == "PUBLIC_CONTRACT_SATISFIED":
                refs = payload["observation_refs"]
                rows = [self.db.execute("SELECT payload FROM observations WHERE id=?", (ref,)).fetchone() for ref in refs]
                observations = [json.loads(row[0]) if row else None for row in rows]
                if len(observations) != 2 or any(o is None or o["input_symbol"] != "INSPECT" or o["source_principal_ref"] != "effect_sensor" for o in observations):
                    self.alert("DONE_WITHOUT_EVIDENCE", event)
                elif observations[1]["logical_tick"] - observations[0]["logical_tick"] < 2 or observations[0]["domain_output"] != observations[1]["domain_output"]:
                    self.alert("DONE_WITH_INVALID_EVIDENCE", event)
            if kind == "actor.claimed_outcome" and payload.get("basis") == "ACKNOWLEDGED":
                self.alert("ACKNOWLEDGMENT_IS_NOT_OUTCOME", event)
            self.state["seq"], self.state["hash"] = event["seq"], event["event_hash"]
        self.db.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (json.dumps(self.state, sort_keys=True),))
        alerts = [{"seq": r[0], "rule": r[1]} for r in self.db.execute("SELECT seq,rule FROM alerts WHERE id>?", (before,))]
        return {"anchor": self.anchor, "hold": self.state["hold"], "new_alerts": alerts}

    def close(self):
        self.db.close()
