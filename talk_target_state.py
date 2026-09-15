"""Actor-scoped target catalog and bounded return stack. No credentials or history."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

try:
    from .talk_dashboard_gateway import DashboardTaskError
    from .talk_passive import digest, identifier
except ImportError:  # pragma: no cover - flat plugin load
    from talk_dashboard_gateway import DashboardTaskError
    from talk_passive import digest, identifier


class TargetState:
    def __init__(self, context, *, clock=time.time):
        self.actor = digest(context.principal_id)
        self.clock = clock
        self.path = context.profile_home / "state" / "talk-targets.sqlite3"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(mode=0o600, exist_ok=True)
            self.path.chmod(0o600)
        except OSError:
            raise DashboardTaskError("selection_store_unavailable", 503) from None
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS catalog (
                actor TEXT NOT NULL,id TEXT NOT NULL,record TEXT NOT NULL,expires REAL NOT NULL,
                PRIMARY KEY(actor,id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS selections (
                actor TEXT NOT NULL,tab TEXT NOT NULL,revision INTEGER NOT NULL,
                current TEXT,stack TEXT NOT NULL,pending TEXT,pending_until REAL,
                expires REAL NOT NULL,
                PRIMARY KEY(actor,tab))""")

    @contextmanager
    def _db(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=2)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except (sqlite3.Error, OSError):
            raise DashboardTaskError("selection_store_unavailable", 503) from None
        finally:
            if connection is not None:
                connection.close()

    def cache(self, records):
        if len(records) > 512:
            raise DashboardTaskError("capacity", 409)
        with self._db() as db:
            db.execute("DELETE FROM catalog WHERE expires<=?", (self.clock(),))
            for record in records:
                encoded = json.dumps(record, sort_keys=True)
                if len(encoded) > 4096:
                    raise DashboardTaskError("capacity", 409)
                db.execute(
                    "INSERT OR REPLACE INTO catalog VALUES (?,?,?,?)",
                    (self.actor, record["target_id"], encoded, self.clock() + 600),
                )
            if db.execute("SELECT count(*) FROM catalog").fetchone()[0] > 1024:
                raise DashboardTaskError("capacity", 409)

    def target(self, target_id):
        with self._db() as db:
            row = db.execute(
                "SELECT record FROM catalog WHERE actor=? AND id=? AND expires>?",
                (self.actor, identifier(target_id), self.clock()),
            ).fetchone()
            if row is None:
                raise DashboardTaskError("target_missing", 404)
            return json.loads(row[0])

    def targets(self):
        with self._db() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record FROM catalog WHERE actor=? AND expires>?",
                    (self.actor, self.clock()),
                )
            ]

    def snapshot(self, tab):
        identifier(tab)
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM selections WHERE actor=? AND tab=? AND expires>?",
                (self.actor, tab, self.clock()),
            ).fetchone()
            if row is None:
                return {"current": None, "stack": [], "revision": 0}
            return {
                "current": json.loads(row["current"]) if row["current"] else None,
                "stack": json.loads(row["stack"]),
                "revision": row["revision"],
            }

    def locate(self, connection_id, generation):
        with self._db() as db:
            rows = db.execute(
                "SELECT tab,current FROM selections WHERE actor=? AND expires>?",
                (self.actor, self.clock()),
            ).fetchall()
            for row in rows:
                current = json.loads(row["current"]) if row["current"] else None
                if current and (current.get("connection_id"), current.get("generation")) == (
                    connection_id,
                    generation,
                ):
                    return row["tab"]
        raise DashboardTaskError("connection_stale", 409)

    def reserve(self, tab, target, *, back=False, expected=None):
        identifier(tab)
        with self._db() as db:
            db.execute("DELETE FROM selections WHERE expires<=?", (self.clock(),))
            row = db.execute(
                "SELECT * FROM selections WHERE actor=? AND tab=?", (self.actor, tab)
            ).fetchone()
            current = json.loads(row["current"]) if row and row["current"] else None
            stack = json.loads(row["stack"]) if row else []
            revision = row["revision"] if row else 0
            if expected is not None and (
                not current or (current.get("connection_id"), current.get("generation")) != expected
            ):
                raise DashboardTaskError("connection_stale", 409)
            if row and row["pending"] and row["pending_until"] > self.clock():
                raise DashboardTaskError("selection_busy", 409)
            if back:
                if not stack:
                    raise DashboardTaskError("return_empty", 409)
                target = stack.pop()
            elif current and current["target"]["target_id"] != target["target_id"]:
                stack = [*stack, current["target"]][-8:]
            nonce = uuid.uuid4().hex
            if row is None and db.execute("SELECT count(*) FROM selections").fetchone()[0] >= 64:
                raise DashboardTaskError("capacity", 409)
            db.execute(
                "INSERT OR REPLACE INTO selections VALUES (?,?,?,?,?,?,?,?)",
                (
                    self.actor,
                    tab,
                    revision,
                    json.dumps(current) if current else None,
                    json.dumps(json.loads(row["stack"]) if row else []),
                    nonce,
                    self.clock() + 90,
                    self.clock() + 7 * 86400,
                ),
            )
            return {
                "nonce": nonce,
                "revision": revision,
                "tab": tab,
                "target": target,
                "stack": stack,
            }

    def activate(self, prepared, connection_id, generation):
        with self._db() as db:
            row = db.execute(
                "SELECT revision,pending,pending_until FROM selections WHERE actor=? AND tab=?",
                (self.actor, prepared["tab"]),
            ).fetchone()
            if (
                row is None
                or row["revision"] != prepared["revision"]
                or row["pending"] != prepared["nonce"]
                or row["pending_until"] <= self.clock()
            ):
                raise DashboardTaskError("connection_stale", 409)
            current = {
                "target": prepared["target"],
                "connection_id": connection_id,
                "generation": generation,
            }
            db.execute(
                "UPDATE selections SET revision=revision+1,current=?,stack=?,"
                "pending=NULL,pending_until=NULL,expires=? "
                "WHERE actor=? AND tab=?",
                (
                    json.dumps(current),
                    json.dumps(prepared["stack"]),
                    self.clock() + 7 * 86400,
                    self.actor,
                    prepared["tab"],
                ),
            )
            return len(prepared["stack"])

    def cancel(self, prepared):
        with self._db() as db:
            db.execute(
                "UPDATE selections SET pending=NULL,pending_until=NULL "
                "WHERE actor=? AND tab=? AND pending=?",
                (self.actor, prepared["tab"], prepared["nonce"]),
            )
