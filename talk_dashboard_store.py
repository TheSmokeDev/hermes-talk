"""Bounded durable staging of original dashboard inputs and exact action intents."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager

try:
    from .talk_dashboard_gateway import DashboardTaskError
    from .talk_passive import identifier
except ImportError:  # pragma: no cover - flat plugin load
    from talk_dashboard_gateway import DashboardTaskError
    from talk_passive import identifier

INCOMPLETE_REASONS = frozenset(
    {
        "linkage_ambiguous",
        "input_stage_failed",
        "response_failed",
        "tool_failed",
        "missing_tool_calls",
        "disconnected",
        "cancelled",
    }
)


def bounded_text(value, *, maximum=65536):
    if not isinstance(value, str) or not value.strip():
        raise DashboardTaskError("invalid_event", 400)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise DashboardTaskError("invalid_event", 400) from None
    if size > maximum:
        raise DashboardTaskError("capacity", 413)
    return value


class DashboardStages:
    def __init__(self, outbox, token, *, clock=time.time):
        self.outbox, self.owner, self.clock = outbox, token.owner, clock
        with self._db(token, prune=False) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS dashboard_interactions (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL
                REFERENCES task_event_owners(owner) ON DELETE CASCADE,
                tab_id TEXT NOT NULL, input_id TEXT NOT NULL, record TEXT NOT NULL,
                expires REAL NOT NULL, UNIQUE(owner,tab_id,input_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS dashboard_actions (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                interaction_id TEXT NOT NULL
                REFERENCES dashboard_interactions(id) ON DELETE CASCADE,
                owner TEXT NOT NULL, call_id TEXT NOT NULL, record TEXT NOT NULL,
                UNIQUE(owner,interaction_id,call_id))""")

    @contextmanager
    def _db(self, token, *, prune=True):
        if token.owner != self.owner:
            raise DashboardTaskError("context_denied", 403)
        with self.outbox.fenced(self.owner, token.connection_id, token.generation) as db:
            if prune:
                db.execute("DELETE FROM dashboard_interactions WHERE expires<=?", (self.clock(),))
            yield db

    def _encode(self, value):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise DashboardTaskError("capacity", 413)
        return encoded

    def _capacity(self, db):
        count, size = db.execute(
            "SELECT count(*),coalesce(sum(length(CAST(record AS BLOB))),0) "
            "FROM dashboard_interactions"
        ).fetchone()
        actions, action_bytes = db.execute(
            "SELECT count(*),coalesce(sum(length(CAST(record AS BLOB))),0) FROM dashboard_actions"
        ).fetchone()
        if count > 128 or actions > 512 or size + action_bytes > 4 * 1024 * 1024:
            raise DashboardTaskError("capacity", 409)

    def _load(self, db, token, interaction_id):
        row = db.execute(
            "SELECT record,tab_id FROM dashboard_interactions WHERE id=? AND owner=?",
            (identifier(interaction_id), self.owner.key),
        ).fetchone()
        if row is None or row["tab_id"] != token.connection_id:
            raise DashboardTaskError("interaction_unlinked", 409)
        return json.loads(row["record"])

    def _save(self, db, record):
        expires = self.clock() + 86400
        db.execute(
            "UPDATE dashboard_interactions SET record=?,expires=? WHERE id=? AND owner=?",
            (self._encode(record), expires, record["id"], self.owner.key),
        )
        db.execute(
            "UPDATE task_event_owners SET expires=max(expires,?) WHERE owner=?",
            (expires, self.owner.key),
        )
        self._capacity(db)

    def stage(self, token, input_id, input_type, text):
        identifier(input_id)
        bounded_text(text)
        if input_type not in {"voice", "typed"}:
            raise DashboardTaskError("invalid_event", 400)
        with self._db(token) as db:
            existing = db.execute(
                "SELECT record FROM dashboard_interactions "
                "WHERE owner=? AND tab_id=? AND input_id=?",
                (self.owner.key, token.connection_id, input_id),
            ).fetchone()
            if existing:
                record = json.loads(existing[0])
                if (record["text"], record["input_type"]) != (text, input_type):
                    raise DashboardTaskError("event_conflict", 409)
                return record
            record = {
                "id": uuid.uuid4().hex,
                "input_id": input_id,
                "input_type": input_type,
                "text": text,
                "state": "staged",
                "mode": "undecided",
                "origin_turn_id": uuid.uuid4().hex,
                "event_id": uuid.uuid4().hex,
                "canonical_state": "pending",
                "canonical_message_ids": [],
                "receipt_id": None,
                "responses": {},
                "settled_response": None,
                "attempt_generation": None,
                "created_at": self.clock(),
            }
            db.execute(
                "INSERT INTO dashboard_interactions VALUES (?,?,?,?,?,?)",
                (
                    record["id"],
                    self.owner.key,
                    token.connection_id,
                    input_id,
                    self._encode(record),
                    self.clock() + 86400,
                ),
            )
            self._save(db, record)
            return record

    def get(self, token, interaction_id):
        with self._db(token) as db:
            return self._load(db, token, interaction_id)

    def update(self, token, interaction_id, change):
        with self._db(token) as db:
            record = self._load(db, token, interaction_id)
            change(record)
            self._save(db, record)
            return record

    def event(self, token, body):
        kind = body.get("kind")
        if kind == "input.final":
            return self.stage(token, body.get("input_id"), body.get("input_type"), body.get("text"))
        interaction_id = body.get("interaction_id")
        if kind == "interaction.settle":
            with self._db(token) as db:
                record = self._load(db, token, interaction_id)
                response_id = identifier(body.get("response_id"))
                if record["settled_response"] not in {None, response_id}:
                    raise DashboardTaskError("event_conflict", 409)
                record["settled_response"] = response_id
                actions = [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT record FROM dashboard_actions WHERE owner=? AND interaction_id=?",
                        (self.owner.key, interaction_id),
                    )
                ]
                self._assert_complete(record, actions)
                self._save(db, record)
                return record

        def change(record):
            if kind == "interaction.incomplete":
                if body.get("reason") not in INCOMPLETE_REASONS:
                    raise DashboardTaskError("invalid_event", 400)
                if record["state"] not in {"saved", "execution_linked"}:
                    record.update(state="incomplete", reason=body["reason"])
                return
            response_id = identifier(body.get("response_id"))
            response = record["responses"].get(response_id)
            if kind == "response.started":
                previous = body.get("previous_response_id")
                if previous is not None:
                    identifier(previous)
                    predecessor = record["responses"].get(previous)
                    if (
                        predecessor is None
                        or predecessor["status"] != "completed"
                        or not predecessor["tool_call_ids"]
                    ):
                        raise DashboardTaskError("interaction_unlinked", 409)
                if response:
                    if response["previous_response_id"] != previous:
                        raise DashboardTaskError("event_conflict", 409)
                    return
                if record["settled_response"] is not None or len(record["responses"]) >= 16:
                    raise DashboardTaskError("interaction_incomplete", 409)
                if not previous and record["responses"]:
                    raise DashboardTaskError("interaction_unlinked", 409)
                record["responses"][response_id] = {
                    "response_id": response_id,
                    "previous_response_id": previous,
                    "status": "started",
                    "tool_call_ids": [],
                    "finals": {},
                }
                return
            if response is None:
                raise DashboardTaskError("interaction_unlinked", 409)
            if kind == "response.final":
                item_id = identifier(body.get("output_item_id"))
                text = bounded_text(body.get("text"))
                old = response["finals"].get(item_id)
                if old and old["text"] != text:
                    raise DashboardTaskError("event_conflict", 409)
                if old is None:
                    if record["settled_response"] is not None or len(response["finals"]) >= 8:
                        raise DashboardTaskError("interaction_incomplete", 409)
                    response["finals"][item_id] = {
                        "output_item_id": item_id,
                        "text": text,
                        "event_id": uuid.uuid4().hex,
                        "message_ids": [],
                    }
                return
            if kind == "response.done":
                status, calls = body.get("status"), body.get("tool_call_ids")
                if (
                    status not in {"completed", "cancelled", "failed"}
                    or not isinstance(calls, list)
                    or len(calls) > 16
                ):
                    raise DashboardTaskError("invalid_event", 400)
                calls = [identifier(call) for call in calls]
                if len(set(calls)) != len(calls):
                    raise DashboardTaskError("invalid_event", 400)
                if response["status"] != "started" and (
                    response["status"],
                    response["tool_call_ids"],
                ) != (status, calls):
                    raise DashboardTaskError("event_conflict", 409)
                response.update(status=status, tool_call_ids=calls)
                if status != "completed":
                    record.update(state="incomplete", reason="response_failed")
                return
            raise DashboardTaskError("invalid_event", 400)

        return self.update(token, interaction_id, change)

    def prepare_action(self, token, interaction_id, response_id, call_id, name, arguments, build):
        identifier(call_id)
        with self._db(token) as db:
            record = self._load(db, token, interaction_id)
            response = record["responses"].get(identifier(response_id))
            if response is None or record["settled_response"] is not None:
                raise DashboardTaskError("interaction_unlinked", 409)
            prior = db.execute(
                "SELECT record FROM dashboard_actions "
                "WHERE owner=? AND interaction_id=? AND call_id=?",
                (self.owner.key, interaction_id, call_id),
            ).fetchone()
            if prior:
                action = json.loads(prior[0])
                if (action["name"], action["arguments"]) != (name, arguments):
                    raise DashboardTaskError("event_conflict", 409)
                return action
            if response["status"] != "started" and call_id not in response["tool_call_ids"]:
                raise DashboardTaskError("interaction_unlinked", 409)
            action = {
                "action_id": uuid.uuid4().hex,
                "interaction_id": interaction_id,
                "response_id": response_id,
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "state": "prepared",
                "idempotency_key": uuid.uuid4().hex,
                "api_run_id": None,
                "canonical_message_ids": [],
                "created_at": self.clock(),
            }
            build(record, action)
            run_id = db.execute(
                "INSERT INTO dashboard_actions(id,interaction_id,owner,call_id,record) "
                "VALUES (?,?,?,?,?) RETURNING run_id",
                (
                    action["action_id"],
                    interaction_id,
                    self.owner.key,
                    call_id,
                    self._encode(action),
                ),
            ).fetchone()[0]
            action["run_id"] = run_id
            db.execute(
                "UPDATE dashboard_actions SET record=? WHERE run_id=?",
                (self._encode(action), run_id),
            )
            self._save(db, record)
            return action

    def action(self, token, run_id):
        if type(run_id) is not int or run_id < 1:
            raise DashboardTaskError("invalid_event", 400)
        with self._db(token) as db:
            row = db.execute(
                "SELECT record FROM dashboard_actions WHERE owner=? AND run_id=?",
                (self.owner.key, run_id),
            ).fetchone()
            if row is None:
                raise DashboardTaskError("result_unavailable", 404)
            return json.loads(row[0])

    def update_action(self, token, run_id, **fields):
        with self._db(token) as db:
            row = db.execute(
                "SELECT record FROM dashboard_actions WHERE owner=? AND run_id=?",
                (self.owner.key, run_id),
            ).fetchone()
            if row is None:
                raise DashboardTaskError("result_unavailable", 404)
            action = json.loads(row[0])
            action.update(fields)
            db.execute(
                "UPDATE dashboard_actions SET record=? WHERE owner=? AND run_id=?",
                (self._encode(action), self.owner.key, run_id),
            )
            self._capacity(db)
            return action

    def record_original_receipt(self, original, **fields):
        """Record a response for an existing authorized action after voice disconnect.

        This cannot create an action, alter its owner/request/key, or resurrect deleted
        scope. Presentation still requires the current connection generation.
        """
        allowed = {
            "state",
            "api_run_id",
            "canonical_message_ids",
            "error",
            "last_status",
            "child_session_id",
            "updated_at",
            "output",
        }
        if not set(fields) <= allowed:
            raise DashboardTaskError("invalid_event", 400)
        with self.outbox._db() as db:
            row = db.execute(
                "SELECT record FROM dashboard_actions WHERE owner=? AND id=?",
                (self.owner.key, original["action_id"]),
            ).fetchone()
            if row is None:
                return None
            action = json.loads(row[0])
            if any(
                action.get(key) != original.get(key)
                for key in ("idempotency_key", "request_body", "interaction_id", "call_id")
            ):
                raise DashboardTaskError("event_conflict", 409)
            remote = fields.get("api_run_id")
            if remote and action.get("api_run_id") not in {None, remote}:
                raise DashboardTaskError("event_conflict", 409)
            action.update(fields)
            db.execute(
                "UPDATE dashboard_actions SET record=? WHERE owner=? AND id=?",
                (self._encode(action), self.owner.key, original["action_id"]),
            )
            self._capacity(db)
            return action

    def records(self, token):
        with self._db(token) as db:
            interactions = [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record FROM dashboard_interactions "
                    "WHERE owner=? AND tab_id=? ORDER BY rowid",
                    (self.owner.key, token.connection_id),
                )
            ]
            actions = [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record FROM dashboard_actions WHERE owner=? ORDER BY run_id",
                    (self.owner.key,),
                )
            ]
            return interactions, actions

    def assert_settled(self, token, record):
        _, actions = self.records(token)
        actions = [action for action in actions if action["interaction_id"] == record["id"]]
        return self._assert_complete(record, actions)

    @staticmethod
    def _assert_complete(record, actions):
        responses = record["responses"]
        final = responses.get(record["settled_response"])
        if (
            not final
            or final["status"] != "completed"
            or final["tool_call_ids"]
            or not final["finals"]
        ):
            raise DashboardTaskError("interaction_incomplete", 409)
        if record["state"] == "incomplete":
            raise DashboardTaskError("interaction_incomplete", 409)
        seen, cursor = set(), record["settled_response"]
        while cursor is not None:
            if cursor in seen or cursor not in responses:
                raise DashboardTaskError("interaction_unlinked", 409)
            seen.add(cursor)
            cursor = responses[cursor]["previous_response_id"]
        if seen != set(responses):
            raise DashboardTaskError("interaction_incomplete", 409)
        for response in responses.values():
            actual = {
                action["call_id"]
                for action in actions
                if action["response_id"] == response["response_id"]
            }
            if response["status"] != "completed" or actual != set(response["tool_call_ids"]):
                raise DashboardTaskError("interaction_incomplete", 409)
        if any(action["state"] not in {"accepted", "returned"} for action in actions):
            raise DashboardTaskError("interaction_incomplete", 409)
        return actions
