"""Derived Codex mappings under immutable, host-authorized Hermes job ownership."""

from __future__ import annotations

import json
import time
import uuid

try:
    from .talk_codex_wire import CodexWorkerError
    from .talk_passive import digest, identifier
except ImportError:
    from talk_codex_wire import CodexWorkerError
    from talk_passive import digest, identifier

TERMINAL = frozenset({"completed", "failed", "interrupted"})


class CodexJobs:
    def __init__(self, outbox, *, clock=time.time):
        self.outbox, self.clock = outbox, clock
        with outbox._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS codex_jobs (
                owner TEXT NOT NULL, job_id TEXT NOT NULL, record TEXT NOT NULL,
                updated REAL NOT NULL, PRIMARY KEY(owner,job_id))""")

    def _owner(self, owner):
        if owner.profile != self.outbox._profile:
            raise CodexWorkerError("foreign_owner")
        return owner.key

    def _read(self, db, owner, job_id):
        row = db.execute(
            "SELECT record FROM codex_jobs WHERE owner=? AND job_id=?",
            (self._owner(owner), identifier(job_id)),
        ).fetchone()
        if row is None:
            raise CodexWorkerError("job_unavailable")
        return json.loads(row[0])

    def _write(self, db, owner, job_id, record):
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
            raise CodexWorkerError("result_capacity")
        db.execute(
            "UPDATE codex_jobs SET record=?,updated=? WHERE owner=? AND job_id=?",
            (encoded, self.clock(), self._owner(owner), job_id),
        )
        total = db.execute("SELECT sum(length(CAST(record AS BLOB))) FROM codex_jobs").fetchone()[0]
        if total > 16 * 1024 * 1024:
            raise CodexWorkerError("store_capacity")

    def prepare(self, owner, job_id, request):
        identifier(job_id)
        if request.get("parent_session_id") != owner.session_id:
            raise CodexWorkerError("foreign_owner")
        fingerprint = digest(request)
        with self.outbox._db() as db:
            # Terminal records expire; unknown/active starts are never silently evicted.
            db.execute(
                "DELETE FROM codex_jobs WHERE updated<? AND "
                "json_extract(record,'$.state') IN ('completed','failed','interrupted')",
                (self.clock() - 7 * 86400,),
            )
            row = db.execute(
                "SELECT record FROM codex_jobs WHERE owner=? AND job_id=?",
                (self._owner(owner), job_id),
            ).fetchone()
            if row:
                record = json.loads(row[0])
                if record["fingerprint"] != fingerprint:
                    raise CodexWorkerError("event_conflict")
                return record
            if db.execute("SELECT count(*) FROM codex_jobs").fetchone()[0] >= 32:
                raise CodexWorkerError("store_capacity")
            record = {
                "request": request,
                "fingerprint": fingerprint,
                "state": "prepared",
                "thread_id": None,
                "turn_id": None,
                "items": [],
                "output": "",
                "lease": None,
                "lease_expires": 0,
                "controls": {},
                "error": None,
            }
            db.execute(
                "INSERT INTO codex_jobs VALUES (?,?,?,?)",
                (self._owner(owner), job_id, "{}", self.clock()),
            )
            self._write(db, owner, job_id, record)
            return record

    def read(self, owner, job_id):
        with self.outbox._db(write=False) as db:
            return self._read(db, owner, job_id)

    def claim(self, owner, job_id):
        with self.outbox._db() as db:
            record = self._read(db, owner, job_id)
            if record["lease"] and record["lease_expires"] > self.clock():
                raise CodexWorkerError("worker_busy")
            lease = uuid.uuid4().hex
            record.update(lease=lease, lease_expires=self.clock() + 30)
            self._write(db, owner, job_id, record)
            return lease, record

    def update(self, owner, job_id, lease, **fields):
        allowed = {"state", "thread_id", "turn_id", "items", "output", "controls", "error"}
        if set(fields) - allowed:
            raise CodexWorkerError("invalid_record")
        with self.outbox._db() as db:
            record = self._read(db, owner, job_id)
            if record["lease"] != lease or record["lease_expires"] <= self.clock():
                raise CodexWorkerError("worker_stale")
            for key in ("thread_id", "turn_id"):
                if key in fields and record[key] not in (None, fields[key]):
                    raise CodexWorkerError("foreign_thread")
            record.update(fields, lease_expires=self.clock() + 30)
            self._write(db, owner, job_id, record)
            return record

    def release(self, owner, job_id, lease):
        with self.outbox._db() as db:
            record = self._read(db, owner, job_id)
            if record["lease"] == lease:
                record.update(lease=None, lease_expires=0)
                self._write(db, owner, job_id, record)
