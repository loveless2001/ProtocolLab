"""Private SQLite simulator: primitive application and dedup are one transaction."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from protocollab.contracts import ALPHABET, canonical, digest, uid
from protocollab_environment.generator import ProtocolConfig, World, transition


class EnvironmentService:
    def __init__(self, path, config: ProtocolConfig | None = None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS config (payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS instances (
            resource TEXT PRIMARY KEY, disposable INTEGER NOT NULL, world TEXT NOT NULL,
            tick INTEGER NOT NULL, seq INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS commands (
            command_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, receipt TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS effects (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, resource TEXT NOT NULL,
            command_id TEXT NOT NULL, symbol TEXT NOT NULL, before_state TEXT NOT NULL,
            after_state TEXT NOT NULL, tick INTEGER NOT NULL);
        """)
        row = self.db.execute("SELECT payload FROM config").fetchone()
        if row:
            self.config = ProtocolConfig.from_dict(json.loads(row[0]))
            if config and config != self.config:
                raise ValueError("ENVIRONMENT_CONFIG_CHANGED")
        elif config:
            self.config = config
            self.db.execute("INSERT INTO config VALUES(?)", (canonical(config.to_dict()).decode(),))
        else:
            raise ValueError("MISSING_ENVIRONMENT_CONFIG")

    def create(self, resource, disposable, command_id):
        return self._apply(resource, "CREATE_REPLICA" if disposable else "CREATE_LIVE", command_id)

    def reset(self, resource, command_id):
        return self._apply(resource, "RESET_REPLICA", command_id)

    def apply(self, resource, symbol, command_id):
        if symbol not in ALPHABET:
            raise ValueError("UNKNOWN_OPERATION")
        return self._apply(resource, symbol, command_id)

    def _apply(self, resource, symbol, command_id):
        payload_hash = digest([resource, symbol])
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cached = self.db.execute("SELECT * FROM commands WHERE command_id=?", (command_id,)).fetchone()
            if cached:
                if cached["payload_hash"] != payload_hash:
                    raise ValueError("IDEMPOTENCY_PAYLOAD_MISMATCH")
                self.db.execute("COMMIT")
                return json.loads(cached["receipt"])
            row = self.db.execute("SELECT * FROM instances WHERE resource=?", (resource,)).fetchone()
            if symbol.startswith("CREATE_"):
                if row:
                    raise ValueError("RESOURCE_EXISTS")
                state, tick, seq = World(), 0, 0
                self.db.execute("INSERT INTO instances VALUES(?,?,?,?,?)",
                                (resource, int(symbol == "CREATE_REPLICA"), canonical(asdict(state)).decode(), tick, seq))
                output, before = "OK", state
            else:
                if row is None:
                    raise ValueError("UNKNOWN_RESOURCE")
                before = World(**json.loads(row["world"]))
                tick, seq = row["tick"], row["seq"] + 1
                if symbol == "RESET_REPLICA":
                    if not row["disposable"]:
                        raise ValueError("LIVE_RESET_FORBIDDEN")
                    state, tick, output = World(), 0, "OK"
                else:
                    state, output = transition(self.config, before, symbol)
                    tick += symbol == "TICK"
                self.db.execute("UPDATE instances SET world=?,tick=?,seq=? WHERE resource=?",
                                (canonical(asdict(state)).decode(), tick, seq, resource))
            domain = {"result_code": output}
            if symbol == "INSPECT":
                # Independent effect sensor reads actual serving state; STATUS is not an input.
                domain = {"result_code": "OK", "artifact": state.served, "health": "HEALTHY"}
            packet = {"event_id": uid("receipt"), "resource_id": resource,
                      "causal_command_id": command_id, "sequence": seq, "logical_tick": tick,
                      "transport_status": "ACKNOWLEDGED", "domain": domain, "note": ""}
            self.db.execute("INSERT INTO commands VALUES(?,?,?)",
                            (command_id, payload_hash, canonical(packet).decode()))
            self.db.execute("INSERT INTO effects(resource,command_id,symbol,before_state,after_state,tick) VALUES(?,?,?,?,?,?)",
                            (resource, command_id, symbol, canonical(asdict(before)).decode(), canonical(asdict(state)).decode(), tick))
            self.db.execute("COMMIT")
            return packet
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def reconcile(self, command_id):
        row = self.db.execute("SELECT receipt FROM commands WHERE command_id=?", (command_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()
