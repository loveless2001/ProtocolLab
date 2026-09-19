"""Owner-only durable state, content-addressed artifacts and hash-chained history."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from protocollab.contracts import canonical, digest, uid


class RecoveryRequired(RuntimeError):
    pass


DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
 owner TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
 namespace TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
 previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_namespace ON events(namespace,seq);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind,seq);
CREATE INDEX IF NOT EXISTS observation_lookup ON events(json_extract(payload,'$.observation_id'))
 WHERE kind='epistemic.observation';
CREATE TABLE IF NOT EXISTS blobs (hash TEXT PRIMARY KEY, payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS state (
 owner TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL,
 payload TEXT NOT NULL, event_seq INTEGER NOT NULL REFERENCES events(seq),
 PRIMARY KEY(owner,key));
CREATE TABLE IF NOT EXISTS actions (
 command_id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL,
 namespace TEXT NOT NULL, scope TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 payload_hash TEXT NOT NULL, proposal TEXT NOT NULL, status TEXT NOT NULL,
 receipt TEXT, dispatch_seq INTEGER, UNIQUE(namespace,scope,idempotency_key));
CREATE INDEX IF NOT EXISTS pending_actions ON actions(scope,status);
CREATE TABLE IF NOT EXISTS clock_actions (
 command_id TEXT PRIMARY KEY,resource TEXT NOT NULL,dispatch_seq INTEGER NOT NULL,
 status TEXT NOT NULL,receipt TEXT);
CREATE TABLE IF NOT EXISTS query_traces (
 trace_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, reset_rev TEXT NOT NULL,
 normalizer_rev TEXT NOT NULL, word_hash TEXT NOT NULL, word TEXT NOT NULL,
 outputs TEXT NOT NULL, refs TEXT NOT NULL, status TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS query_lookup ON query_traces(namespace,reset_rev,normalizer_rev,word_hash);
CREATE TABLE IF NOT EXISTS replay_guard (
 principal TEXT NOT NULL, issuer_seq INTEGER NOT NULL, nonce TEXT NOT NULL,
 event_id TEXT NOT NULL UNIQUE, PRIMARY KEY(principal,issuer_seq), UNIQUE(principal,nonce));
CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT,'append-only events'); END;
CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events
 BEGIN SELECT RAISE(ABORT,'append-only events'); END;
CREATE TRIGGER IF NOT EXISTS immutable_blobs_update BEFORE UPDATE ON blobs
 BEGIN SELECT RAISE(ABORT,'immutable blob'); END;
CREATE TRIGGER IF NOT EXISTS immutable_blobs_delete BEFORE DELETE ON blobs
 BEGIN SELECT RAISE(ABORT,'immutable blob'); END;
"""


class Store:
    def __init__(self, path: str | Path, namespace="run", readonly=False):
        self.path = Path(path)
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self.lock = threading.RLock()
        target = f"file:{self.path.resolve()}?mode=ro" if readonly else str(path)
        self.db = sqlite3.connect(target, isolation_level=None, check_same_thread=False, uri=readonly)
        self.db.row_factory = sqlite3.Row
        if not readonly:
            self.db.executescript(DDL)
        self._depth = 0

    @contextmanager
    def transaction(self):
        with self.lock:
            outer = self._depth == 0
            if outer:
                self.db.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
                if outer:
                    self.db.execute("COMMIT")
            except BaseException:
                if outer:
                    self.db.execute("ROLLBACK")
                raise
            finally:
                self._depth -= 1

    @property
    def tail(self):
        row = self.db.execute("SELECT seq,event_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else (0, "0" * 64)

    def append(self, owner, kind, payload, source=None):
        with self.transaction():
            seq, previous = self.tail
            event = {"seq": seq + 1, "event_id": uid(), "owner": owner, "kind": kind,
                     "source": source or owner, "namespace": self.namespace,
                     "payload_hash": digest(payload), "previous_hash": previous}
            event["event_hash"] = digest(event)
            self.db.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event["seq"], event["event_id"], owner, kind, event["source"], self.namespace,
                 canonical(payload).decode(), event["payload_hash"], previous, event["event_hash"]),
            )
            return event

    def put_blob(self, value):
        raw = value if isinstance(value, bytes) else canonical(value)
        key = digest(raw)
        self.db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?)", (key, raw))
        return key

    def blob(self, key, raw=False):
        row = self.db.execute("SELECT payload FROM blobs WHERE hash=?", (key,)).fetchone()
        if row is None or digest(row[0]) != key:
            raise RecoveryRequired("MISSING_OR_CORRUPT_ARTIFACT")
        return row[0] if raw else json.loads(row[0])

    def get(self, owner, key, default=None):
        with self.lock:
            row = self.db.execute(
                "SELECT payload FROM state WHERE owner=? AND key=?", (owner, key)
            ).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, owner, key, payload, kind="state.revision", source=None):
        with self.transaction():
            row = self.db.execute("SELECT revision FROM state WHERE owner=? AND key=?", (owner, key)).fetchone()
            revision = row[0] + 1 if row else 0
            event = self.append(owner, kind, {"key": key, "revision": revision, "value": payload}, source)
            self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?,?,?,?)",
                            (owner, key, revision, canonical(payload).decode(), event["seq"]))
            return event

    def events(self, after=0, limit=None):
        rows = self.db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit if limit is not None else -1))
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def verify(self, anchor=None):
        previous = "0" * 64
        for seq, event in enumerate(self.events(), 1):
            payload = event.pop("payload")
            event_hash = event.pop("event_hash")
            if event["seq"] != seq or event["previous_hash"] != previous or digest(payload) != event["payload_hash"] or digest(event) != event_hash:
                raise RecoveryRequired("JOURNAL_INTEGRITY_FAILURE")
            previous = event_hash
        if anchor:
            seq, expected = anchor
            row = self.db.execute("SELECT event_hash FROM events WHERE seq=?", (seq,)).fetchone()
            if seq and (row is None or row[0] != expected):
                raise RecoveryRequired("MONITOR_ANCHOR_MISMATCH")
        for row in self.db.execute("SELECT state.*,events.payload AS event_payload FROM state LEFT JOIN events ON state.event_seq=events.seq"):
            if row["event_payload"] is None:
                raise RecoveryRequired("STATE_EVENT_MISSING")
            committed = json.loads(row["event_payload"])
            if committed.get("value") != json.loads(row["payload"]) or committed.get("revision") != row["revision"] or committed.get("key") != row["key"]:
                raise RecoveryRequired("STATE_JOURNAL_DIVERGENCE")
        return self.tail

    def export(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "journal.jsonl").write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in self.events()))
        artifacts = directory / "artifacts"
        artifacts.mkdir(exist_ok=True)
        for row in self.db.execute("SELECT hash,payload FROM blobs"):
            try:
                json.loads(row[1])
                suffix = "json"
            except (ValueError, UnicodeDecodeError):
                suffix = "bin"
            (artifacts / f"{row[0]}.{suffix}").write_bytes(row[1])

    def close(self):
        self.db.close()
