"""Profile-local SQLite journal with atomic, one-use approval consumption."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .policy import PhoneError, text

APPROVAL_SECONDS = 120
DRAFT_SECONDS = 600


class Store:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = path, clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS calls (
                  id TEXT PRIMARY KEY, session TEXT NOT NULL, draft TEXT NOT NULL,
                  status TEXT NOT NULL, created REAL NOT NULL, approved_until REAL,
                  approved_by TEXT, run_id INTEGER, result TEXT
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
        with self.connection(write=True) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(calls)")}
            if "started_at" not in columns:
                db.execute("ALTER TABLE calls ADD COLUMN started_at REAL")

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def owned(db, call_id, session):
        text(session, "Hermes session ID", 256)
        row = db.execute(
            "SELECT * FROM calls WHERE id=? AND session=?", (call_id, session)
        ).fetchone()
        if row is None:
            raise PhoneError("Call not found in this Hermes session.")
        return row

    @staticmethod
    def check_switch(db):
        row = db.execute("SELECT value FROM settings WHERE key='killed'").fetchone()
        if row and row[0] == "true":
            raise PhoneError("Kill switch is on. New dialing is blocked.")

    def prepare(self, session, draft):
        text(session, "Hermes session ID", 256)
        call_id = str(uuid4())
        with self.connection(write=True) as db:
            self.check_switch(db)
            db.execute(
                "INSERT INTO calls(id,session,draft,status,created) VALUES(?,?,?,'prepared',?)",
                (call_id, session, json.dumps(draft), self.clock()),
            )
        return call_id

    def get(self, call_id, session):
        with self.connection() as db:
            row = dict(self.owned(db, call_id, session))
        row["draft"] = json.loads(row["draft"])
        row["result"] = json.loads(row["result"]) if row["result"] else None
        return row

    def active_call(self, session):
        text(session, "Hermes session ID", 256)
        with self.connection() as db:
            rows = db.execute(
                "SELECT id FROM calls WHERE session=? AND status='started'", (session,)
            ).fetchall()
        if len(rows) != 1:
            raise PhoneError(
                "Specify the call ID; this conversation needs exactly one active call."
            )
        return rows[0]["id"]

    def approve(self, call_id, session, owner_id, fingerprint):
        with self.connection(write=True) as db:
            self.check_switch(db)
            row = self.owned(db, call_id, session)
            if row["status"] != "prepared" or row["created"] + DRAFT_SECONDS <= self.clock():
                raise PhoneError(
                    "This draft is expired or no longer awaiting approval. Prepare a new call."
                )
            if json.loads(row["draft"])["config_fingerprint"] != fingerprint:
                raise PhoneError("Settings changed. Prepare and review a new call.")
            db.execute(
                "UPDATE calls SET status='approved', approved_until=?, approved_by=? WHERE id=?",
                (self.clock() + APPROVAL_SECONDS, owner_id, call_id),
            )

    def claim(self, call_id, session, fingerprint):
        with self.connection(write=True) as db:
            self.check_switch(db)
            row = self.owned(db, call_id, session)
            if row["status"] != "approved" or row["approved_until"] <= self.clock():
                raise PhoneError("A fresh, one-use owner approval is required for this call.")
            draft = json.loads(row["draft"])
            if draft["config_fingerprint"] != fingerprint:
                raise PhoneError("Settings changed after approval. Prepare a new call.")
            active = db.execute(
                "SELECT 1 FROM calls WHERE status IN ('dialing','started','unknown') LIMIT 1"
            ).fetchone()
            if active:
                raise PhoneError(
                    "Another call is active or unresolved. Reconcile it before dialing again."
                )
            db.execute("UPDATE calls SET status='dialing' WHERE id=?", (call_id,))
            return draft

    def finish(self, call_id, session, status, *, run_id=None, result=None):
        with self.connection(write=True) as db:
            self.owned(db, call_id, session)
            db.execute(
                "UPDATE calls SET status=?,run_id=COALESCE(?,run_id),result=?,"
                "started_at=CASE WHEN ?='started' THEN COALESCE(started_at,?) ELSE started_at END "
                "WHERE id=?",
                (
                    status,
                    run_id,
                    json.dumps(result) if result is not None else None,
                    status,
                    self.clock(),
                    call_id,
                ),
            )

    def cancel(self, call_id, session):
        with self.connection(write=True) as db:
            row = self.owned(db, call_id, session)
            if row["status"] not in {"prepared", "approved"}:
                raise PhoneError(
                    "Only an undialed draft can be cancelled. End active calls in Dograh."
                )
            db.execute(
                "UPDATE calls SET status='cancelled',approved_until=NULL WHERE id=?", (call_id,)
            )

    def kill(self, enabled=True):
        with self.connection(write=True) as db:
            db.execute(
                "INSERT INTO settings VALUES('killed',?) ON CONFLICT(key) DO "
                "UPDATE SET value=excluded.value",
                ("true" if enabled else "false",),
            )
            if enabled:
                db.execute(
                    "UPDATE calls SET status='cancelled',approved_until=NULL WHERE "
                    "status IN ('prepared','approved')"
                )
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bridge_connections'"
                ).fetchone():
                    db.execute("UPDATE bridge_connections SET revoked=1")

    def reconcile(self, call_id, session):
        """Owner-only recovery after checking Dograh for a call whose POST result was lost."""
        with self.connection(write=True) as db:
            row = self.owned(db, call_id, session)
            if row["status"] not in {"unknown", "dialing"}:
                raise PhoneError("Only an unknown or interrupted call needs manual reconciliation.")
            db.execute("UPDATE calls SET status='reconciled' WHERE id=?", (call_id,))
