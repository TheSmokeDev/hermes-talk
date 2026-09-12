"""Bounded durable staging of original dashboard inputs and exact action intents."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager

try:
    from .talk_dashboard_gateway import DashboardTaskError
    from .talk_passive import digest, identifier
except ImportError:  # pragma: no cover - flat plugin load
    from talk_dashboard_gateway import DashboardTaskError
    from talk_passive import digest, identifier

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

        def initialize(db):
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
            db.execute(
                "CREATE INDEX IF NOT EXISTS dashboard_interactions_expiry "
                "ON dashboard_interactions(expires)"
            )

        outbox.ensure_schema("dashboard_stages_v1", initialize)
        self._last_prune = float("-inf")

    @contextmanager
    def _db(self, token, *, prune=False, write=True):
        if token.owner != self.owner:
            raise DashboardTaskError("context_denied", 403)
        with self.outbox.fenced(
            self.owner, token.connection_id, token.generation, write=write
        ) as db:
            if (prune or write) and self.clock() - self._last_prune >= 60:
                # Keep unresolved decisions/actions for reconciliation, even across long calls.
                db.execute(
                    "DELETE FROM dashboard_interactions WHERE expires<=? "
                    "AND json_extract(record,'$.live_decision.state') IS NULL "
                    "AND NOT EXISTS (SELECT 1 FROM dashboard_actions a "
                    "WHERE a.interaction_id=dashboard_interactions.id "
                    "AND json_extract(a.record,'$.state') NOT IN ('returned','failed'))",
                    (self.clock(),),
                )
                self._last_prune = self.clock()
            yield db

    def _encode(self, value):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > 1024 * 1024:
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
        if count > 4096 or actions > 16384 or size + action_bytes > 64 * 1024 * 1024:
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
        if input_type not in {"voice", "typed"}:
            raise DashboardTaskError("invalid_event", 400)
        return self._stage(token, input_id, input_type, text)

    @staticmethod
    def live_input_id(source_window):
        fragments = [
            {
                key: value
                for key, value in row.items()
                if not (key == "finality" and value == ("turn" if row["final"] else "delta"))
            }
            for row in source_window["fragments"]
        ]
        return "live-" + digest([source_window["provider_session_id"], fragments])

    def stage_live(self, token, source_window, *, db=None):
        fragments = source_window["fragments"]
        text = "".join(item["text"] for item in fragments)
        input_id = self.live_input_id(source_window)
        complete = (
            len(fragments) == 1
            and fragments[0].get("finality", "turn" if fragments[0]["final"] else "delta") == "turn"
        )
        return self._stage(
            token,
            input_id,
            ("typed" if fragments[0].get("modality") == "typed" else "voice")
            if complete
            else "voice_window",
            text,
            source_window=source_window,
            db=db,
        )

    def _stage(self, token, input_id, input_type, text, *, source_window=None, db=None):
        identifier(input_id)
        bounded_text(text, maximum=60000)
        if db is not None:
            return self._stage_record(db, token, input_id, input_type, text, source_window)
        with self._db(token) as db:
            return self._stage_record(db, token, input_id, input_type, text, source_window)

    def _stage_record(self, db, token, input_id, input_type, text, source_window):
        existing = db.execute(
            "SELECT record FROM dashboard_interactions WHERE owner=? AND tab_id=? AND input_id=?",
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
        if source_window is not None:
            # Capture identity survives a new audio connection. Only canonical input receipts
            # are reusable; decisions, response chains, and action authority stay tab-owned.
            previous = db.execute(
                "SELECT record FROM dashboard_interactions WHERE owner=? AND input_id=? "
                "ORDER BY rowid LIMIT 1",
                (self.owner.key, input_id),
            ).fetchone()
            if previous:
                original = json.loads(previous[0])
                if (original["text"], original["input_type"]) != (text, input_type):
                    raise DashboardTaskError("event_conflict", 409)
                for key in (
                    "origin_turn_id",
                    "event_id",
                    "canonical_state",
                    "canonical_message_ids",
                    "receipt_id",
                ):
                    record[key] = original[key]
            else:
                record["origin_turn_id"] = digest([self.owner.key, input_id, "live-origin"])
                record["event_id"] = digest([self.owner.key, input_id, "live-event"])
            record["source_window"] = source_window
            record["canonical_text"] = (
                text
                if input_type in {"voice", "typed"}
                else "[Captured Live transcript fragments; this is not a finalized utterance.]\n"
                + text
            )
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
        with self._db(token, write=False) as db:
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

    def claim_live_decision(self, token, interaction_id, *, db=None):
        if db is None:
            with self._db(token) as db:
                return self.claim_live_decision(token, interaction_id, db=db)
        record = self._load(db, token, interaction_id)
        if "source_window" not in record:
            raise DashboardTaskError("interaction_unlinked", 409)
        if record.get("live_decision") is not None:
            return record, False
        record["live_decision"] = {"state": "reasoning"}
        self._save(db, record)
        return record, True

    def live_action(self, token, interaction_id):
        with self._db(token, write=False) as db:
            self._load(db, token, interaction_id)
            row = db.execute(
                "SELECT record FROM dashboard_actions WHERE owner=? AND interaction_id=? "
                "AND call_id=?",
                (self.owner.key, interaction_id, "hermes-action-" + interaction_id),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def complete_live_decision(self, token, interaction_id, decision):
        def change(record):
            current = record.get("live_decision")
            if current is None or current["state"] != "reasoning":
                raise DashboardTaskError("event_conflict", 409)
            response_id = "hermes-decision-" + interaction_id
            call_id = "hermes-action-" + interaction_id
            record["live_decision"] = {"state": "completed", **decision}
            record["responses"][response_id] = {
                "response_id": response_id,
                "source": "hermes_plugin_llm",
                "previous_response_id": None,
                "status": "completed",
                "tool_call_ids": [call_id] if decision["name"] else [],
                "finals": {},
            }

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

    def set_update_preference(self, token, run_id, events):
        """Commit the setting and its retry receipt together, in transaction order."""
        if events._owner != self.owner or events._outbox is not self.outbox:
            raise DashboardTaskError("context_denied", 403)
        with self._db(token) as db:
            row = db.execute(
                "SELECT record FROM dashboard_actions WHERE owner=? AND run_id=?",
                (self.owner.key, run_id),
            ).fetchone()
            if row is None:
                raise DashboardTaskError("interaction_unlinked", 409)
            action = json.loads(row[0])
            if action["name"] != "set_update_preference" or set(action["arguments"]) != {"mode"}:
                raise DashboardTaskError("invalid_event", 400)
            if action["state"] == "returned":
                return action
            result = events._set_update_preference(db, action["arguments"]["mode"])
            action.update(state="returned", output=json.dumps(result))
            db.execute(
                "UPDATE dashboard_actions SET record=? WHERE owner=? AND run_id=?",
                (self._encode(action), self.owner.key, run_id),
            )
            self._capacity(db)
            return action

    def freeze_control(self, token, run_id, body, message_ids):
        """First full control body wins; concurrent copies must use that exact target/body."""
        with self._db(token) as db:
            row = db.execute(
                "SELECT record FROM dashboard_actions WHERE owner=? AND run_id=?",
                (self.owner.key, run_id),
            ).fetchone()
            if row is None:
                raise DashboardTaskError("interaction_unlinked", 409)
            action = json.loads(row[0])
            if action["name"] != "steer_work":
                raise DashboardTaskError("invalid_event", 400)
            if action.get("control_body") is None:
                action["control_body"] = body
                action["canonical_message_ids"] = message_ids
                db.execute(
                    "UPDATE dashboard_actions SET record=? WHERE owner=? AND run_id=?",
                    (self._encode(action), self.owner.key, run_id),
                )
                self._capacity(db)
            return action

    def claim_approval(self, token, run_id):
        """One caller may select/submit an approval; retries never acquire new authority."""
        with self._db(token) as db:
            row = db.execute(
                "SELECT record FROM dashboard_actions WHERE owner=? AND run_id=?",
                (self.owner.key, run_id),
            ).fetchone()
            if row is None:
                raise DashboardTaskError("interaction_unlinked", 409)
            action = json.loads(row[0])
            if action["name"] != "resolve_approval":
                raise DashboardTaskError("invalid_event", 400)
            claimed = action["state"] == "prepared"
            if claimed:
                action["state"] = "approval_checking"
                db.execute(
                    "UPDATE dashboard_actions SET record=? WHERE owner=? AND run_id=?",
                    (self._encode(action), self.owner.key, run_id),
                )
            return action, claimed

    def freeze_approval(self, token, run_id, api_run_id, request_id, choice):
        with self._db(token) as db:
            row = db.execute(
                "SELECT record FROM dashboard_actions WHERE owner=? AND run_id=?",
                (self.owner.key, run_id),
            ).fetchone()
            if row is None:
                raise DashboardTaskError("interaction_unlinked", 409)
            action = json.loads(row[0])
            if action["name"] != "resolve_approval" or action["state"] != "approval_checking":
                raise DashboardTaskError("event_conflict", 409)
            action.update(
                state="approval_submitting",
                request_body={"request_id": identifier(request_id), "choice": choice},
                approval_api_run_id=identifier(api_run_id),
            )
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
            "control_receipt",
            "control_phase",
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
                for key in (
                    "idempotency_key",
                    "request_body",
                    "interaction_id",
                    "call_id",
                    "control_body",
                    "control_api_run_id",
                    "approval_api_run_id",
                    "name",
                )
            ):
                raise DashboardTaskError("event_conflict", 409)
            remote = fields.get("api_run_id")
            if remote and action.get("api_run_id") not in {None, remote}:
                raise DashboardTaskError("event_conflict", 409)
            if action.get("control_phase") == "settled" and fields.get("control_receipt"):
                # A late timeout cannot downgrade an already verified receipt. Host unknown
                # stays read-only, but a later authoritative settlement can resolve it.
                previous = action["control_receipt"]
                if (
                    previous.get("status") != "unknown"
                    or previous.get("source") != "host_receipt"
                    or fields["control_receipt"].get("source") != "host_receipt"
                ):
                    return action
            action.update(fields)
            db.execute(
                "UPDATE dashboard_actions SET record=? WHERE owner=? AND id=?",
                (self._encode(action), self.owner.key, original["action_id"]),
            )
            self._capacity(db)
            return action

    def records(self, token):
        with self._db(token, write=False) as db:
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
