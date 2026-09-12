"""Recipient projection for already accepted canonical Talk Codex worker jobs.

Listing derives identity from Talk dispatch/origin receipts, never Codex history.
The caller authenticates the bound request. Selection/status recheck the exact
host run. Steering is mapped before canonical action preparation; this module
never dispatches work, posts messages, or creates another control ledger.
"""

from __future__ import annotations

import json

try:
    from .talk_dashboard_gateway import DashboardTaskError
    from .talk_dashboard_store import bounded_text
    from .talk_passive import digest
    from .talk_run_control import read_target, require_steering
except ImportError:  # pragma: no cover - flat plugin load
    from talk_dashboard_gateway import DashboardTaskError
    from talk_dashboard_store import bounded_text
    from talk_passive import digest
    from talk_run_control import read_target, require_steering

APP = "codex_worker"
IDENTITY_FIELDS = ("recipient_id", "app", "task_id", "target_token")


def _id(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 512
        and not any(ord(char) < 32 for char in value)
    )


class TalkWorkerRecipients:
    def __init__(self, bound):
        self.bound = bound

    def _jobs(self):
        token = self.bound.token
        with self.bound.stages._db(token, write=False) as db:
            rows = db.execute(
                "SELECT a.record,i.record FROM dashboard_actions a "
                "JOIN dashboard_interactions i ON i.id=a.interaction_id "
                "WHERE a.owner=? AND i.owner=? ORDER BY a.run_id DESC",
                (token.owner.key, token.owner.key),
            ).fetchall()
        jobs = []
        for encoded, original in rows:
            action, record = json.loads(encoded), json.loads(original)
            request = action.get("request_body") or {}
            child, origin = request.get("child") or {}, request.get("origin") or {}
            messages = record.get("canonical_message_ids")
            if (
                action.get("name") != "delegate_task"
                or action.get("state") != "accepted"
                or not _id(action.get("api_run_id"))
                or not _id(action.get("action_id"))
                or type(action.get("run_id")) is not int
                or action["run_id"] < 1
                or child.get("worker") != "hermes-talk-codex"
                or child.get("correlation_id") != action["action_id"]
                or request.get("session_id") != token.owner.session_id
                or record.get("canonical_state") != "saved"
                or not isinstance(messages, list)
                or not messages
                or any(type(value) is not int or value < 1 for value in messages)
                or type(record.get("receipt_id")) is not int
                or record["receipt_id"] < 1
                or origin.get("event_id") != record.get("event_id")
                or origin.get("origin_turn_id") != record.get("origin_turn_id")
                or (
                    origin.get("receipt_id") is not None
                    and origin["receipt_id"] != record["receipt_id"]
                )
            ):
                continue
            jobs.append((action, record))
        return jobs

    def _record(self, action, original):
        owner = self.bound.token.owner
        proof = digest(
            [
                owner.key,
                action["action_id"],
                action["api_run_id"],
                action["idempotency_key"],
                original["receipt_id"],
                original["canonical_message_ids"],
            ]
        )
        title = " ".join(str(action.get("goal") or "Existing background task").split())[:360]
        return {
            "recipient_id": "talk-worker-" + digest([owner.key, action["action_id"]]),
            "host_id": owner.host,
            "app": APP,
            "task_id": action["api_run_id"],
            "title": f"Codex worker #{action['run_id']}: {title}",
            "target_token": "talk-worker-" + proof,
            "proven_control": "worker",
            "operations": ["status", "steer_work"],
            "run_id": action["run_id"],
            "status": action.get("last_status", "accepted"),
            "status_source": "canonical_dispatch_receipt",
        }

    def list_recipients(self, app=None):
        rows = [self._record(*job) for job in self._jobs()] if app in {None, APP} else []
        return {
            "host_id": self.bound.token.owner.host,
            "recipients": rows[:256],
            "truncated": len(rows) > 256,
            "capabilities": {},
        }

    def _resolve(self, target, *, current=True):
        if not isinstance(target, dict) or target.get("app") != APP:
            raise DashboardTaskError("target_missing", 404)
        match = next(
            (
                (action, original)
                for action, original in self._jobs()
                if self._record(action, original)["recipient_id"] == target.get("recipient_id")
            ),
            None,
        )
        if match is None:
            raise DashboardTaskError("target_missing", 404)
        action, original = match
        projected = self._record(action, original)
        if any(target.get(key) != projected[key] for key in IDENTITY_FIELDS):
            raise DashboardTaskError("context_denied", 403)
        if "host_id" in target and target["host_id"] != projected["host_id"]:
            raise DashboardTaskError("context_denied", 403)
        run = None
        if current:
            run = self.bound.gateway.run(action["api_run_id"])
            if (
                run.get("session_id") != self.bound.token.owner.session_id
                or type(run.get("parent_message_id")) is not int
                or run.get("parent_message_id") not in original["canonical_message_ids"]
                or (
                    action.get("child_session_id") is not None
                    and action["child_session_id"] != run.get("child_session_id")
                )
            ):
                raise DashboardTaskError("steering_target_denied", 403)
        return projected, action, run

    @staticmethod
    def _identity(record):
        return {key: record[key] for key in ("host_id", "recipient_id", "app", "task_id")}

    def select(self, target):
        record, _, _ = self._resolve(target)
        return {**self._identity(record), "status": "selected"}

    def status(self, target):
        record, _, run = self._resolve(target)
        return {
            **self._identity(record),
            "status": run.get("status", "unknown"),
            "run_id": record["run_id"],
            "child_session_id": run.get("child_session_id"),
            "status_source": "host_run",
            "updated_at": run.get("updated_at"),
        }

    def steering_body(self, target, body):
        """Route a fresh canonical call through DashboardTasks.tool, before prepare_action.

        The generated message argument is not a new origin. steer_work uses the
        linked original operator input and freezes the existing host turn itself.
        """
        if (
            not isinstance(body, dict)
            or body.get("name") != "send_agent_message"
            or not isinstance(body.get("arguments"), dict)
            or set(body["arguments"]) - {"message", "app"}
            or body["arguments"].get("app", APP) != APP
        ):
            raise DashboardTaskError("invalid_event", 400)
        bounded_text(body["arguments"].get("message"), maximum=16000)
        record, action, _ = self._resolve(target)
        self.bound.stages.get(self.bound.token, body.get("interaction_id"))
        self.bound.capabilities = self.bound.gateway.capabilities()
        target_state = read_target(self.bound, action["api_run_id"])
        if not target_state["supported"]:
            raise DashboardTaskError("steering_unsupported", 409)
        if target_state["kind"] != "linked_child":
            raise DashboardTaskError("steering_target_denied", 403)
        require_steering(self.bound.capabilities, kind=target_state["kind"])
        return {**body, "name": "steer_work", "arguments": {"run_id": record["run_id"]}}


class WorkerRecipientBackend:
    """Compose the worker projection with the existing host UI backend."""

    def __init__(self, bound, fallback):
        self.workers, self.fallback = TalkWorkerRecipients(bound), fallback

    def list_recipients(self, app=None):
        workers = self.workers.list_recipients(app)
        if app == APP:
            return workers
        remote = self.fallback.list_recipients(app=app)
        if remote.get("host_id") != workers["host_id"]:
            raise DashboardTaskError("context_denied", 403)
        rows = remote.get("recipients")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DashboardTaskError("gateway_response_invalid", 502)
        # Only this canonical projection can label a record as an owned Talk worker.
        combined = [row for row in rows if row.get("app") != APP] + workers["recipients"]
        return {
            **remote,
            "recipients": combined[:256],
            "truncated": bool(
                remote.get("truncated") or workers["truncated"] or len(combined) > 256
            ),
        }

    def select(self, target):
        return (self.workers if target.get("app") == APP else self.fallback).select(target)

    def status(self, target):
        return self.workers.status(target)

    def steering_body(self, target, body):
        return self.workers.steering_body(target, body)

    def send(self, operation_id, target, message, *, commit_token=None):
        if target.get("app") == APP:
            raise DashboardTaskError("steering_origin_pending", 409)
        return self.fallback.send(operation_id, target, message, commit_token=commit_token)

    def reconcile(self, operation_id, target):
        if target.get("app") == APP:
            raise DashboardTaskError("steering_origin_pending", 409)
        return self.fallback.reconcile(operation_id, target)

    def inspect(self, target, *, capture=True):
        if target.get("app") == APP:
            raise DashboardTaskError("unsupported_tool", 400)
        return self.fallback.inspect(target, capture=capture)
