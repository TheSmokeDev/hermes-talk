"""Owner-fenced recipient selection and durable delivery attempts."""

from __future__ import annotations

import json
import time

try:
    from .talk_dashboard_gateway import DashboardTaskError
    from .talk_passive import digest, identifier
except ImportError:  # pragma: no cover - flat plugin load
    from talk_dashboard_gateway import DashboardTaskError
    from talk_passive import digest, identifier


class RecipientStore:
    def __init__(self, bound, *, clock=time.time):
        self.outbox, self.token = bound.outbox, bound.token
        self.owner, self.tab = self.token.owner, bound.browser_tab
        self.clock = clock
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS talk_recipient_selections (
                owner TEXT NOT NULL, tab TEXT NOT NULL, record TEXT NOT NULL,
                PRIMARY KEY(owner,tab))""")
            db.execute("""CREATE TABLE IF NOT EXISTS talk_recipient_operations (
                owner TEXT NOT NULL, operation_id TEXT NOT NULL, record TEXT NOT NULL,
                PRIMARY KEY(owner,operation_id))""")

    def _db(self):
        return self.outbox.fenced(self.owner, self.token.connection_id, self.token.generation)

    @staticmethod
    def _encode(record):
        value = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if len(value.encode("utf-8")) > 64 * 1024:
            raise DashboardTaskError("capacity", 413)
        return value

    @staticmethod
    def _capacity(db):
        count, size = db.execute(
            "SELECT count(*),coalesce(sum(length(CAST(record AS BLOB))),0) "
            "FROM talk_recipient_operations"
        ).fetchone()
        selections = db.execute("SELECT count(*) FROM talk_recipient_selections").fetchone()[0]
        # Delivery tombstones are never pruned into permission to send again.
        if count > 1024 or size > 8 * 1024 * 1024 or selections > 256:
            raise DashboardTaskError("capacity", 409)

    def _selection(self, db):
        row = db.execute(
            "SELECT record FROM talk_recipient_selections WHERE owner=? AND tab=?",
            (self.owner.key, self.tab),
        ).fetchone()
        return (
            json.loads(row[0])
            if row
            else {
                "selected": None,
                "revision": 0,
                "capabilities": {},
                "observed_at": None,
            }
        )

    def _save_selection(self, db, record):
        db.execute(
            "INSERT OR REPLACE INTO talk_recipient_selections VALUES (?,?,?)",
            (self.owner.key, self.tab, self._encode(record)),
        )
        self._capacity(db)

    def snapshot(self):
        with self._db() as db:
            return self._selection(db)

    def cache_capabilities(self, capabilities):
        with self._db() as db:
            record = self._selection(db)
            record.update(capabilities=capabilities, observed_at=self.clock())
            self._save_selection(db, record)

    def operation(self, operation_id, name, arguments):
        with self._db() as db:
            return self._operation(db, operation_id, name, arguments)

    def _operation(self, db, operation_id, name, arguments):
        row = db.execute(
            "SELECT record FROM talk_recipient_operations WHERE owner=? AND operation_id=?",
            (self.owner.key, identifier(operation_id)),
        ).fetchone()
        if row is None:
            return None
        record = json.loads(row[0])
        if record["fingerprint"] != digest([name, arguments]):
            raise DashboardTaskError("event_conflict", 409)
        return record

    def prepare(self, operation_id, name, arguments, *, target=None, result=None):
        with self._db() as db:
            existing = self._operation(db, operation_id, name, arguments)
            if existing:
                return existing, False
            selection = self._selection(db)
            if name in {"send_agent_message", "inspect_screen"}:
                target = selection["selected"]
            revision = None
            if name == "select_recipient":
                revision = selection["revision"] + 1
                selection.update(revision=revision, selected=None)
                self._save_selection(db, selection)
            record = {
                "operation_id": operation_id,
                "name": name,
                "fingerprint": digest([name, arguments]),
                "host_id": self.owner.host,
                "target": target,
                "tab": self.tab,
                "revision": revision,
                "status": "queued",
                "attempted": False,
                "result": result,
                "commit_token": None,
                "commit_attempted": False,
                "created_at": self.clock(),
                "receipts": [],
            }
            db.execute(
                "INSERT INTO talk_recipient_operations VALUES (?,?,?)",
                (self.owner.key, operation_id, self._encode(record)),
            )
            self._capacity(db)
            return record, True

    def claim(self, record):
        with self._db() as db:
            current = self._load(db, record)
            if current["attempted"] or current["result"] is not None:
                return current, False
            current["attempted"] = True
            self._save(db, current)
            return current, True

    def claim_commit(self, record):
        with self._db() as db:
            current = self._load(db, record)
            if (
                not current["commit_token"]
                or current["commit_attempted"]
                or current["status"] != "queued"
            ):
                return current, False
            current["commit_attempted"] = True
            self._save(db, current)
            return current, True

    def _load(self, db, original):
        row = db.execute(
            "SELECT record FROM talk_recipient_operations WHERE owner=? AND operation_id=?",
            (self.owner.key, original["operation_id"]),
        ).fetchone()
        if row is None:
            raise DashboardTaskError("result_unavailable", 404)
        current = json.loads(row[0])
        if any(current[key] != original[key] for key in ("fingerprint", "host_id", "target")):
            raise DashboardTaskError("event_conflict", 409)
        return current

    def _save(self, db, record):
        db.execute(
            "UPDATE talk_recipient_operations SET record=? WHERE owner=? AND operation_id=?",
            (self._encode(record), self.owner.key, record["operation_id"]),
        )
        self._capacity(db)

    def finish(self, original, result, *, selected=None, commit_token=None):
        # Save a factual late receipt to its original owner even if presentation
        # disconnected. The service reauthorizes before returning private output.
        with self.outbox._db() as db:
            current = self._load(db, original)
            if commit_token is not None:
                if current["commit_token"] not in (None, commit_token):
                    raise DashboardTaskError("event_conflict", 409)
                current["commit_token"] = commit_token
            previous = current.get("result")
            if previous is not None:
                old_status, new_status = previous["status"], result["status"]
                if (
                    old_status in {"completed", "failed"}
                    or (
                        old_status in {"posted", "accepted"} and new_status in {"queued", "unknown"}
                    )
                    or (old_status == "accepted" and new_status == "posted")
                ):
                    return current
            current.update(status=result["status"], result=result)
            receipt = {"status": result["status"], "observed_at": self.clock()}
            if not current["receipts"] or current["receipts"][-1]["status"] != result["status"]:
                current["receipts"] = (current["receipts"] + [receipt])[-16:]
            if selected is not None:
                self.outbox._check(db, self.owner, self.token.connection_id, self.token.generation)
                selection = self._selection(db)
                if selection["revision"] != original["revision"]:
                    raise DashboardTaskError("event_conflict", 409)
                selection["selected"] = selected
                self._save_selection(db, selection)
            self._save(db, current)
            return current
