"""Call-bound inbox. The remote MCP process never launches Hermes work."""

from __future__ import annotations

import hashlib
import json
import secrets
from uuid import uuid4

from .policy import PhoneError, text
from .store import Store


def connection_key(session_id):
    # This is the SDK-validated stateful transport session, never a tool argument.
    value = text(session_id, "Stateful MCP session", 256)
    return hashlib.sha256(value.encode()).hexdigest()


class BridgeStore(Store):
    def __init__(self, path, **kwargs):
        super().__init__(path, **kwargs)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bridge_connections (
                  connection TEXT PRIMARY KEY, code TEXT UNIQUE NOT NULL,
                  created REAL NOT NULL, call_id TEXT, expires REAL NOT NULL,
                  revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS bridge_one_connection_per_call
                  ON bridge_connections(call_id) WHERE call_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS bridge_requests (
                  id TEXT PRIMARY KEY, connection TEXT NOT NULL, request_key TEXT NOT NULL,
                  call_id TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
                  state TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                  answer TEXT, approved_until REAL, toolsets TEXT, handle TEXT, summary TEXT,
                  shared INTEGER NOT NULL DEFAULT 0,
                  UNIQUE(connection,request_key)
                );
                CREATE TABLE IF NOT EXISTS phone_call_routes (
                  capability_hash TEXT PRIMARY KEY, call_id TEXT UNIQUE NOT NULL,
                  created REAL NOT NULL
                );
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(bridge_requests)")}
            if "dispatched" not in columns:
                db.execute("ALTER TABLE bridge_requests ADD COLUMN dispatched INTEGER DEFAULT 0")

    def issue_route(self, call_id, session):
        """Create an opaque transport address for the call's existing Hermes session.

        The owner never sees or enters this value. Only its hash is journalled;
        Dograh receives the original through the server-side initial context.
        This routes input, without changing the session's tool permissions.
        """
        capability = secrets.token_urlsafe(32)
        digest = hashlib.sha256(capability.encode()).hexdigest()
        with self.connection(write=True) as db:
            call = self.owned(db, call_id, session)
            if call["status"] != "dialing":
                raise PhoneError("A call route can only be created while starting this call.")
            db.execute(
                "INSERT INTO phone_call_routes VALUES(?,?,?)", (digest, call_id, self.clock())
            )
        return capability

    def route(self, capability):
        text(capability, "Call route", 128)
        digest = hashlib.sha256(capability.encode()).hexdigest()
        with self.connection(write=True) as db:
            self.check_switch(db)
            route = db.execute(
                "SELECT call_id FROM phone_call_routes WHERE capability_hash=?", (digest,)
            ).fetchone()
            if route is None:
                raise PhoneError("Call route is unavailable.")
            call = db.execute("SELECT * FROM calls WHERE id=?", (route["call_id"],)).fetchone()
            if call is None or call["status"] != "started" or not call["run_id"]:
                raise PhoneError("This call is no longer active.")
            deadline = call["started_at"] + json.loads(call["draft"])["max_duration_seconds"]
            if deadline <= self.clock():
                raise PhoneError("This call ended.")
            key = connection_key("phone-call:" + call["id"])
            db.execute(
                "INSERT OR IGNORE INTO bridge_connections"
                "(connection,code,created,call_id,expires) VALUES(?,?,?,?,?)",
                (key, "route:" + call["id"], self.clock(), call["id"], deadline),
            )
            self.active_grant(db, key)
            return key

    def claim_turn(self, request_id, session):
        """Persist one dispatch claim before handing input to the host's normal FIFO."""
        with self.connection(write=True) as db:
            row = self.owner_request(db, request_id, session, require_live=True)
            if row["state"] != "pending" or row["dispatched"]:
                return None
            db.execute("UPDATE bridge_requests SET dispatched=1 WHERE id=?", (request_id,))
            return dict(row)

    def dispatch_failed(self, request_id, session):
        with self.connection(write=True) as db:
            self.owner_request(db, request_id, session)
            db.execute("UPDATE bridge_requests SET state='unavailable' WHERE id=?", (request_id,))

    def active_grant(self, db, key):
        self.check_switch(db)
        row = db.execute("SELECT * FROM bridge_connections WHERE connection=?", (key,)).fetchone()
        if row is None or row["revoked"] or row["expires"] <= self.clock() or not row["call_id"]:
            raise PhoneError("No active call for this connection.")
        call = db.execute("SELECT * FROM calls WHERE id=?", (row["call_id"],)).fetchone()
        if call is None or call["status"] != "started" or not call["run_id"]:
            raise PhoneError("The call is no longer active.")
        return row, call

    def revoke(self, call_id, session):
        with self.connection(write=True) as db:
            self.owned(db, call_id, session)
            db.execute("UPDATE bridge_connections SET revoked=1 WHERE call_id=?", (call_id,))

    def enqueue(self, key, request_key, kind, content):
        text(request_key, "Request key", 64)
        content = text(content, "Question" if kind == "question" else "Task", 2000)
        if kind not in {"question", "task"}:
            raise PhoneError("Unsupported request kind.")
        with self.connection(write=True) as db:
            grant, call = self.active_grant(db, key)
            existing = db.execute(
                "SELECT * FROM bridge_requests WHERE connection=? AND request_key=?",
                (key, request_key),
            ).fetchone()
            if existing:
                if existing["kind"] != kind or existing["content"] != content:
                    raise PhoneError("Request key already used for different content.")
                return existing["id"]
            rid = str(uuid4())
            db.execute(
                "INSERT INTO "
                "bridge_requests(id,connection,request_key,call_id,kind,content,state,created,expires)"
                " "
                "VALUES(?,?,?,?,?,?,'pending',?,?)",
                (
                    rid,
                    key,
                    request_key,
                    call["id"],
                    kind,
                    content,
                    self.clock(),
                    grant["expires"],
                ),
            )
            return rid

    def owner_request(self, db, request_id, session, *, require_live=False):
        row = db.execute("SELECT * FROM bridge_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise PhoneError("Request not found in this Hermes session.")
        self.owned(db, row["call_id"], session)
        if require_live:
            self.active_grant(db, row["connection"])
            if row["expires"] <= self.clock():
                raise PhoneError("This request expired.")
        return row

    def remote_result(self, key, request_id):
        with self.connection() as db:
            self.active_grant(db, key)
            row = db.execute(
                "SELECT * FROM bridge_requests WHERE id=? AND connection=?", (request_id, key)
            ).fetchone()
            if row is None:
                raise PhoneError("Unknown request for this call.")
            state = row["state"]
            if row["expires"] <= self.clock():
                return {
                    "request_id": request_id,
                    "state": "expired",
                    "message": "No answer is available; continue without it.",
                }
            result = {"request_id": request_id, "state": state}
            if row["kind"] == "question" and state == "answered":
                result["owner_answer"] = row["answer"]
            return result

    def inbox(self, session):
        text(session, "Hermes session ID", 256)
        with self.connection() as db:
            rows = db.execute(
                "SELECT r.* FROM bridge_requests r JOIN calls c ON c.id=r.call_id "
                "WHERE c.session=? ORDER BY r.created DESC LIMIT 20",
                (session,),
            ).fetchall()
            return [
                {
                    "request_id": r["id"],
                    "call_id": r["call_id"],
                    "kind": r["kind"],
                    "content": r["content"],
                    "state": r["state"],
                    "expired": r["expires"] <= self.clock(),
                    "summary_for_owner": r["summary"],
                    "shared": bool(r["shared"]),
                }
                for r in rows
            ]

    def answer(self, request_id, session, answer):
        answer = text(answer, "Owner answer", 2000)
        with self.connection(write=True) as db:
            row = self.owner_request(db, request_id, session, require_live=True)
            if row["kind"] != "question" or row["state"] != "pending":
                raise PhoneError("Only a pending question can be answered.")
            db.execute(
                "UPDATE bridge_requests SET state='answered',answer=? WHERE id=?",
                (answer, request_id),
            )
