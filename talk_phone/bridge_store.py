"""Call-bound inbox. The remote MCP process never launches Hermes work."""

from __future__ import annotations

import hashlib
import json
import secrets
from uuid import uuid4

from .policy import PhoneError, text
from .store import Store

PAIR_TTL = 120
REQUEST_TTL = 90
WORK_APPROVAL_TTL = 60


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
            """)

    def connect(self, transport_session):
        key = connection_key(transport_session)
        with self.connection(write=True) as db:
            self.check_switch(db)
            row = db.execute(
                "SELECT * FROM bridge_connections WHERE connection=?", (key,)
            ).fetchone()
            if row is not None:
                if row["revoked"] or row["expires"] <= self.clock():
                    raise PhoneError(
                        "This bridge connection expired or was revoked. Reconnect and pair again."
                    )
                return dict(row)
            # Bound authenticated discovery abuse; expired rows remain as an audit trail.
            count = db.execute(
                "SELECT count(*) FROM bridge_connections WHERE expires>? AND revoked=0",
                (self.clock(),),
            ).fetchone()[0]
            if count >= 24:
                raise PhoneError("Too many pending bridge connections. Wait for pairing expiry.")
            code = secrets.token_hex(5).upper()
            db.execute(
                "INSERT INTO bridge_connections(connection,code,created,expires) VALUES(?,?,?,?)",
                (key, code, self.clock(), self.clock() + PAIR_TTL),
            )
            return {"connection": key, "code": code, "call_id": None}

    def active_grant(self, db, key):
        self.check_switch(db)
        row = db.execute("SELECT * FROM bridge_connections WHERE connection=?", (key,)).fetchone()
        if row is None or row["revoked"] or row["expires"] <= self.clock() or not row["call_id"]:
            raise PhoneError("No active owner-paired call for this connection.")
        call = db.execute("SELECT * FROM calls WHERE id=?", (row["call_id"],)).fetchone()
        if call is None or call["status"] != "started" or not call["run_id"]:
            raise PhoneError("The paired call is no longer active.")
        return row, call

    def pair(self, code, call_id, session):
        with self.connection(write=True) as db:
            self.check_switch(db)
            call = self.owned(db, call_id, session)
            if call["status"] != "started" or not call["run_id"] or call["started_at"] is None:
                raise PhoneError(
                    "Pairing requires a started, approved outbound call with a known run ID."
                )
            deadline = call["started_at"] + json.loads(call["draft"])["max_duration_seconds"]
            if deadline <= self.clock():
                raise PhoneError("The call's bridge duration has expired.")
            row = db.execute(
                "SELECT * FROM bridge_connections WHERE code=?", (code.upper(),)
            ).fetchone()
            if row is None or row["expires"] <= self.clock() or row["revoked"] or row["call_id"]:
                raise PhoneError("Pairing code is expired, unknown, or already used.")
            if db.execute(
                "SELECT 1 FROM bridge_connections WHERE call_id=?", (call_id,)
            ).fetchone():
                raise PhoneError(
                    "This call already has a bridge connection. End it before "
                    "preparing another call."
                )
            db.execute(
                "UPDATE bridge_connections SET call_id=?,expires=? WHERE connection=?",
                (call_id, deadline, row["connection"]),
            )
        return {"call_id": call_id, "status": "paired", "expires_at": deadline}

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
            count = db.execute(
                "SELECT count(*) FROM bridge_requests WHERE call_id=?", (call["id"],)
            ).fetchone()[0]
            if count >= 20:
                raise PhoneError("This call has reached its bridge request limit.")
            pending = db.execute(
                "SELECT count(*) FROM bridge_requests WHERE call_id=? "
                "AND state IN ('pending','approved','launching','running') AND expires>?",
                (call["id"], self.clock()),
            ).fetchone()[0]
            if pending >= 3:
                raise PhoneError("Finish the pending requests before asking for more.")
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
                    min(grant["expires"], self.clock() + REQUEST_TTL),
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
            if state in {"launching", "running"}:
                result["status_source"] = (
                    "Last known Hermes status; the owner chat refreshes it with phone_work_status."
                )
            if row["kind"] == "question" and state == "answered":
                result["owner_answer"] = row["answer"]
            elif row["kind"] == "task" and row["shared"]:
                result["owner_shared_result"] = row["summary"]
            elif state == "succeeded":
                result["state"] = "awaiting_owner_release"
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
                    "untrusted_content": r["content"],
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

    def approve_work(self, request_id, session, toolsets):
        if not toolsets or any(not isinstance(v, str) or not v for v in toolsets):
            raise PhoneError(
                "Configure a nonempty delegation_toolsets allowlist before approving work."
            )
        with self.connection(write=True) as db:
            row = self.owner_request(db, request_id, session, require_live=True)
            if row["kind"] != "task" or row["state"] != "pending":
                raise PhoneError("Only a pending task can be approved.")
            db.execute(
                "UPDATE bridge_requests SET "
                "state='approved',approved_until=?,toolsets=? WHERE id=?",
                (
                    min(row["expires"], self.clock() + WORK_APPROVAL_TTL),
                    json.dumps(list(toolsets)),
                    request_id,
                ),
            )

    def claim_work(self, request_id, session, toolsets):
        with self.connection(write=True) as db:
            row = self.owner_request(db, request_id, session, require_live=True)
            if (
                row["kind"] != "task"
                or row["state"] != "approved"
                or row["approved_until"] <= self.clock()
            ):
                raise PhoneError("A fresh owner /phone allow-work approval is required.")
            if json.loads(row["toolsets"]) != list(toolsets) or not toolsets:
                raise PhoneError("Delegation permissions changed; this approval cannot be used.")
            active = db.execute(
                "SELECT 1 FROM bridge_requests WHERE call_id=? AND state IN "
                "('launching','running','unknown')",
                (row["call_id"],),
            ).fetchone()
            if active:
                raise PhoneError("A task is already active or unresolved for this call.")
            db.execute("UPDATE bridge_requests SET state='launching' WHERE id=?", (request_id,))
            return dict(row)

    def save_work(self, request_id, session, state, *, handle=None, summary=None):
        with self.connection(write=True) as db:
            self.owner_request(db, request_id, session)
            db.execute(
                "UPDATE bridge_requests SET state=?,handle=COALESCE(?,handle),summary=? WHERE id=?",
                (state, json.dumps(handle) if handle is not None else None, summary, request_id),
            )

    def work(self, request_id, session):
        with self.connection() as db:
            row = dict(self.owner_request(db, request_id, session))
        row["handle"] = json.loads(row["handle"]) if row["handle"] else None
        return row

    def share(self, request_id, session):
        with self.connection(write=True) as db:
            row = self.owner_request(db, request_id, session, require_live=True)
            if row["kind"] != "task" or row["state"] != "succeeded" or not row["summary"]:
                raise PhoneError("Only a successful result reviewed by the owner can be shared.")
            db.execute("UPDATE bridge_requests SET shared=1 WHERE id=?", (request_id,))

    def deny(self, request_id, session):
        with self.connection(write=True) as db:
            row = self.owner_request(db, request_id, session)
            if row["state"] not in {"pending", "approved"}:
                raise PhoneError(
                    "This request already started. Use phone_work_cancel to cancel a child."
                )
            db.execute("UPDATE bridge_requests SET state='denied' WHERE id=?", (request_id,))
