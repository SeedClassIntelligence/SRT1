"""State Store: SQLite persistence for sources, items, relationships, events, snapshots (spec 14).

A relational store is sufficient for V1 (14). All continuity mutations go through `tx()`, which is
BEGIN IMMEDIATE ... COMMIT/ROLLBACK, so a failure before completion leaves the previous valid state
authoritative (8.4, INV-036).

SRT1 -- Seed Reflection Tool. Specification and authorship: Seed Class Intelligence.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from .models import ProjectionCorrupt, SourceEvent, new_id, utcnow
from .state import Model, SCALAR_DEFAULTS, checksum

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams(
  stream_id TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE',
  current_state_version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_events(
  source_event_id TEXT PRIMARY KEY, stream_id TEXT NOT NULL REFERENCES streams(stream_id),
  seq INTEGER NOT NULL, source_type TEXT NOT NULL, content TEXT NOT NULL, content_sha TEXT NOT NULL,
  external_source_id TEXT, source_metadata TEXT, occurred_at TEXT, received_at TEXT NOT NULL,
  processing_status TEXT NOT NULL DEFAULT 'RECEIVED', error TEXT,
  UNIQUE(stream_id, seq));
CREATE UNIQUE INDEX IF NOT EXISTS ux_source_ext ON source_events(stream_id, external_source_id)
  WHERE external_source_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS items(
  item_id TEXT PRIMARY KEY, stream_id TEXT NOT NULL REFERENCES streams(stream_id),
  item_type TEXT NOT NULL, content TEXT NOT NULL, authority_status TEXT NOT NULL, confidence TEXT NOT NULL,
  lifecycle TEXT NOT NULL, created_state_version INTEGER NOT NULL, updated_state_version INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_items_stream ON items(stream_id, authority_status, item_type);
CREATE TABLE IF NOT EXISTS relationships(
  relationship_id TEXT PRIMARY KEY, stream_id TEXT NOT NULL REFERENCES streams(stream_id),
  from_item_id TEXT NOT NULL REFERENCES items(item_id), to_item_id TEXT NOT NULL REFERENCES items(item_id),
  relationship_type TEXT NOT NULL, created_state_version INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_rel_from ON relationships(from_item_id);
CREATE INDEX IF NOT EXISTS ix_rel_to ON relationships(to_item_id);
CREATE TABLE IF NOT EXISTS source_refs(
  source_ref_id TEXT PRIMARY KEY, source_event_id TEXT NOT NULL REFERENCES source_events(source_event_id),
  owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, ref_type TEXT NOT NULL, location TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_refs_owner ON source_refs(owner_kind, owner_id);
CREATE TABLE IF NOT EXISTS continuity_events(
  event_id TEXT PRIMARY KEY, stream_id TEXT NOT NULL REFERENCES streams(stream_id),
  delta_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL, classification TEXT NOT NULL,
  source_event_ids TEXT NOT NULL, affected_item_ids TEXT NOT NULL,
  previous_state_version INTEGER NOT NULL, resulting_state_version INTEGER NOT NULL,
  confidence TEXT NOT NULL, rationale TEXT, delta TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(stream_id, resulting_state_version));
CREATE TABLE IF NOT EXISTS event_claims(
  source_event_id TEXT PRIMARY KEY REFERENCES source_events(source_event_id),
  event_id TEXT NOT NULL REFERENCES continuity_events(event_id));
CREATE TABLE IF NOT EXISTS living_state(
  stream_id TEXT PRIMARY KEY REFERENCES streams(stream_id), state_version INTEGER NOT NULL,
  scalars TEXT NOT NULL, checksum TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(
  snapshot_id TEXT PRIMARY KEY, stream_id TEXT NOT NULL REFERENCES streams(stream_id),
  state_version INTEGER NOT NULL, snapshot_type TEXT NOT NULL, payload TEXT NOT NULL, checksum TEXT NOT NULL,
  triggering_event_ids TEXT NOT NULL, source_range TEXT, continuation_point TEXT, reason TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clarifications(
  clarification_id TEXT PRIMARY KEY, stream_id TEXT NOT NULL REFERENCES streams(stream_id),
  source_event_id TEXT NOT NULL, delta TEXT NOT NULL, reasons TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations(
  obs_id INTEGER PRIMARY KEY AUTOINCREMENT, stream_id TEXT, source_event_id TEXT, kind TEXT NOT NULL,
  detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS context_metrics(
  metric_id INTEGER PRIMARY KEY AUTOINCREMENT, stream_id TEXT NOT NULL, task TEXT, retrieval_depth TEXT,
  historical_tokens INTEGER, state_tokens INTEGER, lineage_tokens INTEGER, snapshot_tokens INTEGER,
  source_tokens INTEGER, package_tokens INTEGER, escalations INTEGER, state_only INTEGER, created_at TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._depth = 0

    # ------------------------------------------------------------------ transactions
    @contextmanager
    def tx(self):
        with self._lock:
            if self._depth:  # re-entrant: join the outer transaction
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self.db.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")
            finally:
                self._depth = 0

    def q(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    # ------------------------------------------------------------------ streams
    def create_stream(self, title=None) -> str:
        sid, now = new_id("stm"), utcnow()
        with self.tx():
            self.db.execute("INSERT INTO streams VALUES(?,?,?,?,?,?)", (sid, title, "ACTIVE", 0, now, now))
            self._write_scalars(sid, 0, SCALAR_DEFAULTS)
        return sid

    def get_stream(self, stream_id) -> dict:
        r = self.one("SELECT * FROM streams WHERE stream_id=?", (stream_id,))
        if r is None:
            raise KeyError(f"unknown stream {stream_id}")
        return dict(r)

    def set_stream_status(self, stream_id, status):
        self.db.execute("UPDATE streams SET status=?, updated_at=? WHERE stream_id=?",
                        (status, utcnow(), stream_id))

    # ------------------------------------------------------------------ source events
    def add_source_event(self, se: SourceEvent) -> tuple[dict, bool]:
        """Idempotent on source_event_id and on (stream, external_source_id)."""
        import hashlib
        with self.tx():
            self.get_stream(se.stream_id)
            row = self.one("SELECT * FROM source_events WHERE source_event_id=?", (se.source_event_id,))
            if row is None and se.external_source_id:
                row = self.one("SELECT * FROM source_events WHERE stream_id=? AND external_source_id=?",
                               (se.stream_id, se.external_source_id))
            if row is not None:
                return dict(row), False
            seq = (self.one("SELECT COALESCE(MAX(seq),0)+1 AS n FROM source_events WHERE stream_id=?",
                            (se.stream_id,))["n"])
            self.db.execute(
                "INSERT INTO source_events(source_event_id,stream_id,seq,source_type,content,content_sha,"
                "external_source_id,source_metadata,occurred_at,received_at,processing_status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (se.source_event_id, se.stream_id, seq, se.source_type, se.content,
                 hashlib.sha256(se.content.encode()).hexdigest(), se.external_source_id,
                 json.dumps(se.source_metadata) if se.source_metadata else None,
                 se.occurred_at, se.received_at or utcnow(), "RECEIVED"))
            return dict(self.one("SELECT * FROM source_events WHERE source_event_id=?",
                                 (se.source_event_id,))), True

    def get_source_event(self, source_event_id) -> dict | None:
        r = self.one("SELECT * FROM source_events WHERE source_event_id=?", (source_event_id,))
        return dict(r) if r else None

    def source_events(self, stream_id) -> dict:
        return {r["source_event_id"]: dict(r) for r in
                self.q("SELECT * FROM source_events WHERE stream_id=? ORDER BY seq", (stream_id,))}

    def set_processing(self, source_event_id, status, error=None):
        self.db.execute("UPDATE source_events SET processing_status=?, error=? WHERE source_event_id=?",
                        (status, error, source_event_id))

    # ------------------------------------------------------------------ model load / write
    def _write_scalars(self, stream_id, version, scalars):
        payload = json.dumps(scalars, sort_keys=True)
        self.db.execute(
            "INSERT INTO living_state VALUES(?,?,?,?,?) ON CONFLICT(stream_id) DO UPDATE SET "
            "state_version=excluded.state_version, scalars=excluded.scalars, checksum=excluded.checksum, "
            "updated_at=excluded.updated_at",
            (stream_id, version, payload, checksum({"v": version, "s": scalars}), utcnow()))

    def load_model(self, stream_id) -> Model:
        stream = self.get_stream(stream_id)
        ls = self.one("SELECT * FROM living_state WHERE stream_id=?", (stream_id,))
        if ls is None:
            raise ProjectionCorrupt(f"living state missing for {stream_id}")
        scalars = json.loads(ls["scalars"])
        if ls["checksum"] != checksum({"v": ls["state_version"], "s": scalars}) \
                or ls["state_version"] != stream["current_state_version"]:
            raise ProjectionCorrupt(f"living state fails integrity check for {stream_id}")
        refs: dict = {}
        for r in self.q("SELECT sr.* FROM source_refs sr WHERE sr.owner_id IN "
                        "(SELECT item_id FROM items WHERE stream_id=? UNION "
                        "SELECT relationship_id FROM relationships WHERE stream_id=?) ORDER BY sr.rowid",
                        (stream_id, stream_id)):
            refs.setdefault(r["owner_id"], []).append({
                "source_ref_id": r["source_ref_id"], "source_event_id": r["source_event_id"],
                "ref_type": r["ref_type"], "location": r["location"]})
        items = {}
        for r in self.q("SELECT * FROM items WHERE stream_id=? ORDER BY created_state_version, rowid", (stream_id,)):
            d = dict(r)
            d["content"] = json.loads(d["content"])
            d["source_refs"] = refs.get(d["item_id"], [])
            items[d["item_id"]] = d
        rels = {}
        for r in self.q("SELECT * FROM relationships WHERE stream_id=? ORDER BY created_state_version, rowid",
                        (stream_id,)):
            d = dict(r)
            d["source_refs"] = refs.get(d["relationship_id"], [])
            rels[d["relationship_id"]] = d
        return Model(stream_id, stream["current_state_version"], items, rels, scalars)

    def _upsert_item(self, it):
        self.db.execute(
            "INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET "
            "content=excluded.content, authority_status=excluded.authority_status, "
            "confidence=excluded.confidence, lifecycle=excluded.lifecycle, "
            "updated_state_version=excluded.updated_state_version, updated_at=excluded.updated_at",
            (it["item_id"], it["stream_id"], it["item_type"], json.dumps(it["content"], sort_keys=True),
             it["authority_status"], it["confidence"], it["lifecycle"], it["created_state_version"],
             it["updated_state_version"], it["created_at"], it["updated_at"]))
        for r in it["source_refs"]:
            self._insert_ref(r, "ITEM", it["item_id"], it["created_at"])

    def _insert_ref(self, r, kind, owner, at):
        self.db.execute("INSERT OR IGNORE INTO source_refs VALUES(?,?,?,?,?,?,?)",
                        (r["source_ref_id"], r["source_event_id"], kind, owner, r["ref_type"],
                         r.get("location"), at))

    def write_model_diff(self, before: Model, after: Model):
        """Persist exactly what changed between two models (inside an open transaction)."""
        for iid, it in after.items.items():
            if before.items.get(iid) != it:
                self._upsert_item(it)
        for rid in set(before.rels) - set(after.rels):
            self.db.execute("DELETE FROM source_refs WHERE owner_kind='REL' AND owner_id=?", (rid,))
            self.db.execute("DELETE FROM relationships WHERE relationship_id=?", (rid,))
        for rid, r in after.rels.items():
            if rid not in before.rels:
                self.db.execute("INSERT INTO relationships VALUES(?,?,?,?,?,?,?)",
                                (rid, r["stream_id"], r["from_item_id"], r["to_item_id"],
                                 r["relationship_type"], r["created_state_version"], r["created_at"]))
                for ref in r["source_refs"]:
                    self._insert_ref(ref, "REL", rid, r["created_at"])
        self.db.execute("UPDATE streams SET current_state_version=?, updated_at=? WHERE stream_id=?",
                        (after.version, utcnow(), after.stream_id))
        self._write_scalars(after.stream_id, after.version, after.scalars)

    def replace_model(self, m: Model):
        """Recovery: rebuild every derived row for a stream from a replayed model."""
        sid = m.stream_id
        with self.tx():
            self.db.execute("DELETE FROM source_refs WHERE owner_id IN (SELECT relationship_id FROM "
                            "relationships WHERE stream_id=?)", (sid,))
            self.db.execute("DELETE FROM relationships WHERE stream_id=?", (sid,))
            self.db.execute("DELETE FROM source_refs WHERE owner_id IN (SELECT item_id FROM items "
                            "WHERE stream_id=?)", (sid,))
            self.db.execute("DELETE FROM items WHERE stream_id=?", (sid,))
            for it in m.items.values():
                self._upsert_item(it)
            for rid, r in m.rels.items():
                self.db.execute("INSERT INTO relationships VALUES(?,?,?,?,?,?,?)",
                                (rid, sid, r["from_item_id"], r["to_item_id"], r["relationship_type"],
                                 r["created_state_version"], r["created_at"]))
                for ref in r["source_refs"]:
                    self._insert_ref(ref, "REL", rid, r["created_at"])
            self.db.execute("UPDATE streams SET current_state_version=?, updated_at=? WHERE stream_id=?",
                            (m.version, utcnow(), sid))
            self._write_scalars(sid, m.version, m.scalars)

    # ------------------------------------------------------------------ events
    def append_event(self, ev: dict, delta: dict):
        self.db.execute(
            "INSERT INTO continuity_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev["event_id"], ev["stream_id"], delta["delta_id"], ev["event_type"],
             delta["primary_classification"], json.dumps(ev["source_event_ids"]),
             json.dumps(ev["affected_item_ids"]), ev["previous_state_version"],
             ev["resulting_state_version"], ev["confidence"], ev.get("rationale"),
             json.dumps(delta, sort_keys=True), ev["created_at"]))
        for s in ev["source_event_ids"]:
            self.db.execute("INSERT INTO event_claims VALUES(?,?)", (s, ev["event_id"]))

    def delta_committed(self, delta_id) -> bool:
        return self.one("SELECT 1 FROM continuity_events WHERE delta_id=?", (delta_id,)) is not None

    def claimed(self, source_event_ids) -> list:
        if not source_event_ids:
            return []
        marks = ",".join("?" * len(source_event_ids))
        return [r["source_event_id"] for r in self.q(
            f"SELECT source_event_id FROM event_claims WHERE source_event_id IN ({marks})", tuple(source_event_ids))]

    def events(self, stream_id, after_version=0) -> list:
        out = []
        for r in self.q("SELECT * FROM continuity_events WHERE stream_id=? AND resulting_state_version>? "
                        "ORDER BY resulting_state_version", (stream_id, after_version)):
            d = dict(r)
            d["delta"] = json.loads(d["delta"])
            d["source_event_ids"] = json.loads(d["source_event_ids"])
            d["affected_item_ids"] = json.loads(d["affected_item_ids"])
            out.append(d)
        return out

    # ------------------------------------------------------------------ snapshots
    def add_snapshot(self, model: Model, stype, triggers, reason, continuation) -> str:
        sid = new_id("snp")
        payload = model.to_dict()
        self.db.execute(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sid, model.stream_id, model.version, stype, json.dumps(payload, sort_keys=True),
             checksum(payload), json.dumps(triggers), json.dumps({"to_state_version": model.version}),
             json.dumps(continuation), reason, utcnow()))
        return sid

    def snapshots(self, stream_id) -> list:
        return [dict(r) for r in self.q(
            "SELECT * FROM snapshots WHERE stream_id=? ORDER BY state_version, rowid", (stream_id,))]

    def last_snapshot_version(self, stream_id) -> int:
        r = self.one("SELECT COALESCE(MAX(state_version),0) AS v FROM snapshots WHERE stream_id=?", (stream_id,))
        return r["v"]

    # ------------------------------------------------------------------ clarifications / logs / metrics
    def add_clarification(self, stream_id, source_event_id, delta, reasons) -> str:
        cid = new_id("clr")
        self.db.execute("INSERT INTO clarifications VALUES(?,?,?,?,?,?,?)",
                        (cid, stream_id, source_event_id, json.dumps(delta), json.dumps(reasons), "OPEN", utcnow()))
        return cid

    def get_clarification(self, cid) -> dict:
        r = self.one("SELECT * FROM clarifications WHERE clarification_id=?", (cid,))
        if r is None:
            raise KeyError(cid)
        d = dict(r)
        d["delta"], d["reasons"] = json.loads(d["delta"]), json.loads(d["reasons"])
        return d

    def set_clarification_status(self, cid, status):
        self.db.execute("UPDATE clarifications SET status=? WHERE clarification_id=?", (status, cid))

    def open_clarifications(self, stream_id) -> list:
        return [self.get_clarification(r["clarification_id"]) for r in self.q(
            "SELECT clarification_id FROM clarifications WHERE stream_id=? AND status='OPEN'", (stream_id,))]

    def log(self, stream_id, kind, detail, source_event_id=None):
        """Observability (16). Details carry ids, classes and counts, never source content."""
        self.db.execute("INSERT INTO observations(stream_id,source_event_id,kind,detail,created_at) "
                        "VALUES(?,?,?,?,?)", (stream_id, source_event_id, kind, json.dumps(detail), utcnow()))

    def observations(self, stream_id=None, kind=None) -> list:
        sql, args = "SELECT * FROM observations WHERE 1=1", []
        if stream_id:
            sql += " AND stream_id=?"
            args.append(stream_id)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in self.q(sql + " ORDER BY obs_id", args)]
