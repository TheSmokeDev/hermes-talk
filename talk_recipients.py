"""Existing recipients addressed from an independently owned Hermes voice task.

Only the authenticated host backend enumerates targets and proves control.
Recipient titles are display data; neither titles nor stored Codex history
establish an app-server owner. This service never creates a task or a worker.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import ClassVar

try:
    from starlette.requests import Request
    from starlette.responses import JSONResponse
except ImportError:  # pragma: no cover - plugin import without the dashboard extra
    Request = object
    JSONResponse = None

try:
    from .talk_dashboard_gateway import DashboardTaskError
    from .talk_dashboard_store import bounded_text
    from .talk_passive import identifier
    from .talk_recipient_store import RecipientStore
except ImportError:  # pragma: no cover - flat plugin load
    from talk_dashboard_gateway import DashboardTaskError
    from talk_dashboard_store import bounded_text
    from talk_passive import identifier
    from talk_recipient_store import RecipientStore

RECIPIENT_TOOLS = frozenset(
    {
        "list_recipients",
        "select_recipient",
        "send_agent_message",
        "inspect_screen",
        "catalog_recipients",
        "read_recipient_history",
        "recipient_status",
    }
)
RECIPIENT_APPS = ("codex_desktop", "claude_code", "codex_worker", "hermes_task")
DELIVERY_STATUSES = frozenset({"queued", "posted", "accepted", "completed", "failed", "unknown"})
CAPABILITY_MODES = frozenset({"direct", "delegated", "unavailable", "unknown"})
IDENTITY_FIELDS = ("recipient_id", "app", "task_id", "host_id")
HISTORY_APPS = ("codex_desktop", "claude_code")
RECIPIENT_ERRORS = {
    "recipient_backend_unavailable": "The selected host has no verified recipient bridge.",
    "recipient_response_invalid": "The selected host returned an invalid recipient receipt.",
    "recipient_host_mismatch": "That recipient belongs to a different execution host.",
    "recipient_selection_mismatch": (
        "The addressed recipient is not the currently selected recipient. "
        "Select it again before sending."
    ),
    "recipient_control_unavailable": (
        "The selected recipient is read-only or has no verified send control."
    ),
}


class RecipientError(DashboardTaskError):
    MESSAGES: ClassVar[dict[str, str]] = {**DashboardTaskError.MESSAGES, **RECIPIENT_ERRORS}


def recipient_tools():
    app = {"type": "string", "enum": list(RECIPIENT_APPS)}
    identity = {key: {"type": "string"} for key in IDENTITY_FIELDS}
    identity["app"] = app
    page = {
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "cursor": {"type": "string"},
    }
    definitions = [
        (
            "list_recipients",
            "List existing tasks in Codex desktop, Claude Code and owned workers "
            "on the selected execution host, with their actual control capabilities.",
            {"app": app},
            [],
        ),
        (
            "select_recipient",
            "Address one existing recipient by exact title or recipient ID. "
            "Duplicate names require a named choice. This does not switch the Hermes voice task "
            "and cannot create a new worker or substitute another app.",
            {"reference": {"type": "string"}, **identity},
            [],
        ),
        (
            "send_agent_message",
            "Send instructions to the explicitly selected existing recipient. "
            "Use the factual delivery receipt: posted is not acceptance or completion. "
            "An uncertain delivery must be reconciled with its original operation, never resent.",
            {"message": {"type": "string"}, "app": app},
            ["message"],
        ),
        (
            "inspect_screen",
            "Ask the selected execution host to capture the explicitly selected "
            "recipient window using its verified computer-use capability. This is an explicit "
            "screen inspection request. Never claim to see a screen without its capture receipt.",
            {},
            [],
        ),
        (
            "catalog_recipients",
            "List live recipients and separate read-only native conversations. "
            "History permission never grants send or control. Titles are labels only.",
            {"app": {"type": "string", "enum": list(HISTORY_APPS)}, **page},
            [],
        ),
        (
            "read_recipient_history",
            "Read bounded visible native text for the exact catalog identity. "
            "This never resumes, sends to, or navigates an app.",
            {**identity, **page},
            list(IDENTITY_FIELDS),
        ),
        (
            "recipient_status",
            "Read the exact recipient status. External completion remains unknown; "
            "readable history does not prove completion.",
            identity,
            list(IDENTITY_FIELDS),
        ),
    ]
    return [
        {
            "type": "function",
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }
        for name, description, properties, required in definitions
    ]


def _text(value, maximum=512):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise RecipientError("recipient_response_invalid", 502)
    if any(ord(char) < 32 for char in value):
        raise RecipientError("recipient_response_invalid", 502)
    return value


def _target(record):
    return {
        **{key: record[key] for key in ("recipient_id", "app", "task_id", "target_token")},
        **({"native_host_id": record["native_host_id"]} if "native_host_id" in record else {}),
    }


def _can_send(record):
    if record.get("read_only") is True:
        return False
    control, app = record["proven_control"], record["app"]
    if app == "codex_worker" and control == "worker" and "steer_work" in record["operations"]:
        return True
    if not {"send", "send_agent_message", "turn/steer"}.intersection(record["operations"]):
        return False
    if control == "ui_bridge":
        return app in {"codex_desktop", "claude_code"}
    if control == "app_server":
        return app == "codex_desktop" and record.get("live_owner") is True
    if control == "peer_ipc":
        return app == "claude_code" and record.get("live_owner") is True
    if control == "worker":
        return app == "codex_worker"
    return control == "host_task" and app == "hermes_task"


def _public(record):
    if record is None:
        return None
    return {
        **{
            key: record[key]
            for key in (
                "recipient_id",
                "host_id",
                "app",
                "task_id",
                "title",
                "proven_control",
                "operations",
            )
        },
        "send_agent_message": "direct" if _can_send(record) else "unavailable",
        "read_only": record.get("read_only", False),
        **({"source": record["source"]} if "source" in record else {}),
    }


def _source(value, app):
    kind = {"codex_desktop": "codex_native_history", "claude_code": "claude_session_history"}
    if (
        not isinstance(value, dict)
        or value.get("kind") != kind.get(app)
        or value.get("read_only") is not True
    ):
        raise RecipientError("recipient_response_invalid", 502)
    source_id = _text(value.get("source_id"))
    if "/" in source_id or "\\" in source_id:
        raise RecipientError("recipient_response_invalid", 502)
    return {
        "kind": value["kind"], "source_id": source_id,
        "modified_at": _text(value.get("modified_at"), 128), "read_only": True,
    }


def _page(envelope):
    if not isinstance(envelope, dict) or type(envelope.get("truncated")) is not bool:
        raise RecipientError("recipient_response_invalid", 502)
    cursor = envelope.get("next_cursor")
    return {
        "next_cursor": _text(cursor, 4096) if cursor is not None else None,
        "truncated": envelope["truncated"],
        "observed_at": _text(envelope.get("observed_at"), 128),
    }


class RecipientService:
    def __init__(self, manager, *, backend_factory=None, clock=time.time):
        self.manager = manager
        self.backend_factory = backend_factory or getattr(manager, "recipient_backend", None)
        self.clock = clock
        self._cache = OrderedDict()
        self._cache_lock = threading.RLock()

    @staticmethod
    def _cache_key(bound):
        return (bound.token.owner.key, bound.token.connection_id, bound.token.generation)

    def _cache_rows(self, bound, rows):
        with self._cache_lock:
            key = self._cache_key(bound)
            records = self._cache.get(key, {})
            records = {
                identity: value for identity, value in records.items() if value[0] > self.clock()
            }
            for row in rows:
                previous = records.get(row["recipient_id"])
                if previous and any(
                    previous[1].get(field) != row.get(field)
                    for field in (*IDENTITY_FIELDS, "read_only", "native_host_id")
                ):
                    raise RecipientError("recipient_response_invalid", 502)
                records[row["recipient_id"]] = (self.clock() + 300, row)
            if len(records) > 512:
                raise DashboardTaskError("capacity", 409)
            self._cache[key] = records
            self._cache.move_to_end(key)
            while len(self._cache) > 32:
                self._cache.popitem(last=False)

    def _cached_target(self, bound, arguments):
        if arguments["host_id"] != bound.token.owner.host:
            raise RecipientError("recipient_host_mismatch", 409)
        with self._cache_lock:
            record = self._cache.get(self._cache_key(bound), {}).get(arguments["recipient_id"])
            if record is None:
                raise DashboardTaskError("target_missing", 404)
            expires, target = record
            if any(arguments.get(key) != target[key] for key in IDENTITY_FIELDS):
                raise DashboardTaskError("target_missing", 404)
            if expires <= self.clock():
                raise DashboardTaskError("recipient_history_stale", 409)
            return target

    def _binding(self, request, body, bound=None):
        current = self.manager.binding(request, body, write=True)
        if bound is not None and current is not bound:
            raise DashboardTaskError("connection_stale", 409)
        return current

    def _backend(self, bound):
        if self.backend_factory is None:
            raise RecipientError("recipient_backend_unavailable", 503)
        return self.backend_factory(bound)

    def _store(self, bound):
        return RecipientStore(bound, clock=self.clock)

    def snapshot(self, bound):
        """Stored metadata only; the caller must already have authenticated the binding."""
        value = self._store(bound).snapshot()
        return {"host_id": bound.token.owner.host, "selected": _public(value["selected"])}

    def capabilities(self, bound):
        value = self._store(bound).snapshot()
        fresh = value["observed_at"] is not None and self.clock() - value["observed_at"] <= 60
        return {
            "host_id": bound.token.owner.host,
            "observed_at": value["observed_at"],
            "computer_use": value["capabilities"].get("computer_use", self._unknown_capability())
            if fresh
            else self._unknown_capability(),
        }

    @staticmethod
    def _unknown_capability():
        return {"mode": "unknown", "verified": False, "reason": "capability_unverified"}

    def _catalog(self, request, body, bound, backend, *, app=None):
        self._binding(request, body, bound)
        envelope = backend.list_recipients(app=app)
        self._binding(request, body, bound)
        if not isinstance(envelope, dict) or envelope.get("host_id") != bound.token.owner.host:
            raise RecipientError("recipient_host_mismatch", 409)
        rows = envelope.get("recipients")
        if not isinstance(rows, list) or len(rows) > 256:
            raise RecipientError("recipient_response_invalid", 502)
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise RecipientError("recipient_response_invalid", 502)
            host = row.get("host_id", envelope["host_id"])
            if host != envelope["host_id"]:
                raise RecipientError("recipient_host_mismatch", 409)
            record = {
                key: _text(row.get(key), 4096 if key == "target_token" else 512)
                for key in ("recipient_id", "app", "task_id", "title", "target_token")
            }
            control = row.get("proven_control", "none")
            operations = row.get("operations", [])
            if (
                record["app"] not in RECIPIENT_APPS
                or control
                not in {
                    "none",
                    "ui_bridge",
                    "app_server",
                    "peer_ipc",
                    "worker",
                    "host_task",
                }
                or not isinstance(operations, list)
                or len(operations) > 16
                or any(not isinstance(op, str) or len(op) > 64 for op in operations)
            ):
                raise RecipientError("recipient_response_invalid", 502)
            if record["recipient_id"] in seen:
                raise RecipientError("recipient_response_invalid", 502)
            seen.add(record["recipient_id"])
            record.update(
                host_id=host,
                proven_control=control,
                operations=operations,
                live_owner=row.get("live_owner") is True,
                read_only=row.get("read_only") is True,
            )
            if app is None or record["app"] == app:
                result.append(record)
        caps = envelope.get("capabilities", {})
        computer = caps.get("computer_use", {}) if isinstance(caps, dict) else {}
        capability = self._unknown_capability()
        if isinstance(computer, dict) and computer.get("verified") is True:
            mode, tool = computer.get("mode"), computer.get("tool")
            if mode in CAPABILITY_MODES and (
                mode != "delegated" or tool in {"delegate_task", "inspect_screen"}
            ):
                capability = {
                    "mode": mode,
                    "verified": True,
                    "reason": _text(computer.get("reason", "host_verified"), 256),
                }
                if tool in {"delegate_task", "inspect_screen", "computer_use"}:
                    capability["tool"] = tool
        if capability["mode"] == "unavailable" and any(
            {"inspect", "inspect_screen"}.intersection(row["operations"]) for row in result
        ):
            capability = {
                "mode": "delegated",
                "tool": "inspect_screen",
                "verified": True,
                "reason": "verified_recipient_inspection",
            }
        self._store(bound).cache_capabilities({"computer_use": capability})
        self._cache_rows(bound, result)
        return result, envelope.get("truncated") is True

    @staticmethod
    def _arguments(name, arguments):
        allowed = {
            "list_recipients": {"app"},
            "select_recipient": {"reference", *IDENTITY_FIELDS},
            "send_agent_message": {"message", "app", *IDENTITY_FIELDS},
            "inspect_screen": set(),
            "catalog_recipients": {"app", "limit", "cursor"},
            "read_recipient_history": {*IDENTITY_FIELDS, "limit", "cursor"},
            "recipient_status": set(IDENTITY_FIELDS),
        }
        if (
            name not in allowed
            or not isinstance(arguments, dict)
            or set(arguments) - allowed[name]
            or ("app" in arguments and arguments["app"] not in RECIPIENT_APPS)
        ):
            raise DashboardTaskError("invalid_event", 400)
        if name == "select_recipient":
            if "reference" in arguments:
                if set(arguments) - {"reference", "app"}:
                    raise DashboardTaskError("invalid_event", 400)
                bounded_text(arguments.get("reference"), maximum=512)
            elif set(arguments) != set(IDENTITY_FIELDS):
                raise DashboardTaskError("invalid_event", 400)
        if name in {"read_recipient_history", "recipient_status"} or (
            name == "select_recipient" and "reference" not in arguments
        ):
            for key in IDENTITY_FIELDS:
                bounded_text(arguments.get(key), maximum=512)
        if name in {"catalog_recipients", "read_recipient_history"}:
            limit = arguments.get("limit", 20)
            if type(limit) is not int or not 1 <= limit <= 50:
                raise DashboardTaskError("invalid_event", 400)
            if "cursor" in arguments:
                bounded_text(arguments["cursor"], maximum=4096)
            if arguments.get("app") not in (None, *HISTORY_APPS):
                raise DashboardTaskError("invalid_event", 400)
        if name == "send_agent_message":
            bounded_text(arguments.get("message"), maximum=16000)
            if set(arguments) & (set(IDENTITY_FIELDS) - {"app"}):
                # A captured send names its recipient exactly, or not at all.
                if not set(IDENTITY_FIELDS) <= set(arguments):
                    raise DashboardTaskError("invalid_event", 400)
                for key in IDENTITY_FIELDS:
                    bounded_text(arguments[key], maximum=512)

    @staticmethod
    def identity_of(arguments):
        """The exact recipient a captured send names, or None for a selection-bound send."""
        if isinstance(arguments, dict) and set(IDENTITY_FIELDS) <= set(arguments):
            return {key: arguments[key] for key in IDENTITY_FIELDS}
        return None

    @staticmethod
    def require_identity(target, expected):
        """A later selection cannot retarget a captured send: refuse before anything binds."""
        if target is None or any(target.get(key) != expected[key] for key in IDENTITY_FIELDS):
            raise RecipientError("recipient_selection_mismatch", 409)

    def tool(self, request, body, *, action, bound=None):
        """Action is the server's prepared canonical action, never a provider argument."""
        bound = self._binding(request, body, bound)
        name, arguments = body.get("name"), body.get("arguments")
        self._arguments(name, arguments)
        if (
            not isinstance(action, dict)
            or action.get("name") != name
            or action.get("arguments") != arguments
        ):
            raise DashboardTaskError("event_conflict", 409)
        operation_id = identifier(action.get("action_id"))
        if name == "list_recipients":
            return self.list_recipients(request, body, bound=bound)
        if name in {"catalog_recipients", "read_recipient_history", "recipient_status"}:
            return getattr(self, name)(request, body, bound=bound)
        if name == "select_recipient":
            return self.select_recipient(request, body, operation_id=operation_id, bound=bound)
        if name == "inspect_screen":
            return self.inspect_screen(request, body, operation_id=operation_id, bound=bound)
        return self.send_agent_message(request, body, operation_id=operation_id, bound=bound)

    def list_recipients(self, request, body, *, bound=None):
        bound = self._binding(request, body, bound)
        arguments = body.get("arguments", {})
        self._arguments("list_recipients", arguments)
        rows, truncated = self._catalog(
            request, body, bound, self._backend(bound), app=arguments.get("app"),
        )
        return {
            "ok": True,
            "state": "listed",
            "host_id": bound.token.owner.host,
            "recipients": [_public(row) for row in rows],
            "truncated": truncated,
            "selected": self.snapshot(bound)["selected"],
            "capabilities": self.capabilities(bound),
            "output": "Existing recipients: " + self._choices_text(rows),
        }

    def catalog_recipients(self, request, body, *, bound=None):
        bound = self._binding(request, body, bound)
        arguments = body.get("arguments", {})
        self._arguments("catalog_recipients", arguments)
        backend = self._backend(bound)
        live, live_truncated = self._catalog(
            request, body, bound, backend, app=arguments.get("app")
        )
        self._binding(request, body, bound)
        try:
            envelope = backend.catalog(**arguments)
        except DashboardTaskError as error:
            self._binding(request, body, bound)
            if error.code != "recipient_history_unsupported":
                raise
            envelope = {
                "host_id": bound.token.owner.host, "recipients": [],
                "sources": [
                    {"app": app, "available": False, "reason": error.code}
                    for app in HISTORY_APPS if arguments.get("app") in {None, app}
                ],
                "next_cursor": None, "truncated": False,
                "observed_at": datetime.now(UTC).isoformat(),
            }
        self._binding(request, body, bound)
        if not isinstance(envelope, dict) or envelope.get("host_id") != bound.token.owner.host:
            raise RecipientError("recipient_host_mismatch", 409)
        pagination = _page(envelope)
        rows, sources = envelope.get("recipients"), envelope.get("sources")
        if (
            not isinstance(rows, list) or len(rows) > arguments.get("limit", 20)
            or not isinstance(sources, list) or len(sources) > 2
        ):
            raise RecipientError("recipient_response_invalid", 502)
        projected_sources, source_apps = [], set()
        for source in sources:
            if (
                not isinstance(source, dict) or source.get("app") not in HISTORY_APPS
                or source["app"] in source_apps
                or type(source.get("available")) is not bool
                or arguments.get("app") not in {None, source["app"]}
            ):
                raise RecipientError("recipient_response_invalid", 502)
            source_apps.add(source["app"])
            projected_sources.append({
                "app": source["app"], "available": source["available"],
                "reason": (_text(source["reason"], 256)
                           if source.get("reason") is not None else None),
            })
        stored, seen = [], {row["recipient_id"] for row in live}
        for row in rows:
            if not isinstance(row, dict):
                raise RecipientError("recipient_response_invalid", 502)
            record = {
                key: _text(row.get(key), 4096 if key == "target_token" else 512)
                for key in (*IDENTITY_FIELDS, "title", "target_token")
            }
            if record["host_id"] != bound.token.owner.host:
                raise RecipientError("recipient_host_mismatch", 409)
            if (
                record["app"] not in HISTORY_APPS
                or arguments.get("app") not in {None, record["app"]}
                or row.get("read_only") is not True
                or row.get("proven_control") != "none"
                or row.get("operations") != ["select", "history", "status"]
                or record["recipient_id"] in seen
                or not any(
                    source["app"] == record["app"] and source["available"] for source in sources
                )
            ):
                raise RecipientError("recipient_response_invalid", 502)
            seen.add(record["recipient_id"])
            record.update(
                read_only=True, proven_control="none", operations=["select", "history", "status"],
                source=_source(row.get("source"), record["app"]),
            )
            if "native_host_id" in row:
                record["native_host_id"] = _text(row["native_host_id"])
            stored.append(record)
        self._cache_rows(bound, stored)
        self._binding(request, body, bound)
        return {
            "ok": True, "state": "listed", "host_id": bound.token.owner.host,
            "recipients": [_public(row) for row in live + stored],
            "sources": projected_sources, **pagination,
            # The one flag the UI reads: the set is incomplete if EITHER side cut it.
            "truncated": bool(pagination.get("truncated")) or bool(live_truncated),
            "live_truncated": live_truncated,
            "selected": self.snapshot(bound)["selected"],
            "output": "Existing recipients: " + self._choices_text(live + stored),
        }

    def read_recipient_history(self, request, body, *, bound=None):
        bound = self._binding(request, body, bound)
        arguments = body.get("arguments", {})
        self._arguments("read_recipient_history", arguments)
        target = self._cached_target(bound, arguments)
        if "history" not in target["operations"]:
            raise DashboardTaskError("recipient_history_unsupported", 503)
        self._binding(request, body, bound)
        receipt = self._backend(bound).history(
            _target(target), limit=arguments.get("limit", 20), cursor=arguments.get("cursor"),
        )
        self._binding(request, body, bound)
        self._identity(receipt, target)
        source = _source(receipt.get("source"), target["app"])
        if "source" in target and source["source_id"] != target["source"]["source_id"]:
            raise DashboardTaskError("recipient_history_stale", 409)
        pagination = _page(receipt)
        rows = receipt.get("messages")
        if not isinstance(rows, list) or len(rows) > arguments.get("limit", 20):
            raise RecipientError("recipient_response_invalid", 502)
        messages, characters, seen = [], 0, set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != {"id", "role", "text", "timestamp", "truncated"}
                or row.get("role") not in {"user", "assistant"}
                or not isinstance(row.get("text"), str) or not row["text"]
                or len(row["text"]) > 8000 or type(row.get("truncated")) is not bool
            ):
                raise RecipientError("recipient_response_invalid", 502)
            message_id = _text(row["id"])
            characters += len(row["text"])
            if message_id in seen or characters > 32000:
                raise RecipientError("recipient_response_invalid", 502)
            seen.add(message_id)
            messages.append({
                "id": message_id, "role": row["role"], "text": row["text"],
                "timestamp": _text(row["timestamp"], 128) if row["timestamp"] is not None else None,
                "truncated": row["truncated"],
            })
        self._binding(request, body, bound)
        return {
            "ok": True, **{key: target[key] for key in IDENTITY_FIELDS},
            "source": source, "messages": messages, **pagination,
            "output": "\n".join(f"{row['role']}: {row['text']}" for row in messages),
        }

    def recipient_status(self, request, body, *, bound=None):
        bound = self._binding(request, body, bound)
        arguments = body.get("arguments", {})
        self._arguments("recipient_status", arguments)
        target = self._cached_target(bound, arguments)
        if "status" not in target["operations"]:
            raise DashboardTaskError("recipient_history_unsupported", 503)
        self._binding(request, body, bound)
        receipt = self._backend(bound).status(_target(target))
        self._binding(request, body, bound)
        self._identity(receipt, target)
        identity = {key: target[key] for key in IDENTITY_FIELDS}
        if target["app"] == "codex_worker":
            if receipt.get("status") not in DELIVERY_STATUSES | {"running", "cancelled"}:
                raise RecipientError("recipient_response_invalid", 502)
            return {
                "ok": True, **identity, "status": receipt["status"],
                "status_source": "host_run", "output": "Worker status: " + receipt["status"],
            }
        if (
            receipt.get("status") != "unknown" or receipt.get("completion_tracking") is not False
            or type(receipt.get("available")) is not bool
            or (receipt["available"] and receipt.get("reason") != "external_completion_unverified")
        ):
            raise RecipientError("recipient_response_invalid", 502)
        source = _source(receipt.get("source"), target["app"]) if receipt["available"] else None
        if source and "source" in target and source["source_id"] != target["source"]["source_id"]:
            raise DashboardTaskError("recipient_history_stale", 409)
        self._binding(request, body, bound)
        return {
            "ok": True, **identity, "status": "unknown", "completion_tracking": False,
            "available": receipt["available"], "reason": _text(receipt.get("reason"), 256),
            "observed_at": _text(receipt.get("observed_at"), 128),
            **({"source": source} if source else {}),
            "output": "External task completion is unverified.",
        }

    def select_exact(self, request, body, *, operation_id, bound=None):
        arguments = body.get("arguments", {})
        if set(arguments) != set(IDENTITY_FIELDS):
            raise DashboardTaskError("invalid_event", 400)
        return self.select_recipient(request, body, operation_id=operation_id, bound=bound)

    @staticmethod
    def _choices_text(rows):
        return (
            "; ".join(f"{row['title']} ({row['app']}, {row['recipient_id']})" for row in rows)
            or "none are currently verified on this host."
        )

    def _choice_result(self, status, rows, operation_id):
        return {
            "ok": False,
            "status": "failed",
            "state": status,
            "operation_id": operation_id,
            "choices": [_public(row) for row in rows],
            "output": "Choose an existing recipient: " + self._choices_text(rows),
        }

    def select_recipient(self, request, body, *, operation_id, bound=None):
        bound = self._binding(request, body, bound)
        arguments = body.get("arguments", {})
        self._arguments("select_recipient", arguments)
        store, backend = self._store(bound), self._backend(bound)
        previous = store.operation(operation_id, "select_recipient", arguments)
        if previous:
            return self._return(request, body, bound, previous)
        if "reference" in arguments:
            rows, _ = self._catalog(request, body, bound, backend, app=arguments.get("app"))
            reference = arguments["reference"].strip().casefold()
            matches = [
                row for row in rows
                if reference in {row["recipient_id"].casefold(), row["title"].casefold()}
            ]
        else:
            rows = matches = [self._cached_target(bound, arguments)]
        target = matches[0] if len(matches) == 1 else None
        record, created = store.prepare(operation_id, "select_recipient", arguments, target=target)
        if not created:
            return self._return(request, body, bound, record)
        if target is None:
            result = self._choice_result(
                "ambiguous" if matches else "missing", matches or rows, operation_id
            )
            return self._return(request, body, bound, store.finish(record, result))
        self._binding(request, body, bound)
        record, claimed = store.claim(record)
        if not claimed:
            return self._return(request, body, bound, record)
        try:
            proof = backend.select(_target(target))
            self._binding(request, body, bound)
            self._identity(proof, target)
            if proof.get("status") != "selected":
                raise RecipientError("recipient_response_invalid", 502)
            result = {
                "ok": True,
                "status": "completed",
                "state": "selected",
                "operation_id": operation_id,
                "recipient": _public(target),
                "output": f"Addressing {target['title']} ({target['app']}). "
                "The Hermes voice task is unchanged.",
            }
            record = store.finish(record, result, selected=target)
        except DashboardTaskError:
            raise
        except (OSError, TimeoutError):
            record = store.finish(record, self._delivery_result(record, "unknown"))
        return self._return(request, body, bound, record)


    @staticmethod
    def _identity(receipt, target, operation_id=None):
        if (
            not isinstance(receipt, dict)
            or any(
                receipt.get(key) != target[key]
                for key in ("host_id", "recipient_id", "app", "task_id")
            )
            or (operation_id is not None and receipt.get("operation_id") != operation_id)
        ):
            raise RecipientError("recipient_response_invalid", 502)

    def _delivery_result(self, record, status, *, reason=None):
        target = record["target"]
        title = f"{target['title']} ({target['app']})" if target else "a recipient"
        descriptions = {
            "queued": f"The message to {title} is queued; delivery is not confirmed.",
            "posted": (
                f"The message was posted to {title}. Acceptance and completion are unconfirmed."
            ),
            "accepted": f"{title} accepted the message; completion is unconfirmed.",
            "completed": f"{title} reported completion for this operation.",
            "failed": f"The message to {title} failed.",
            "unknown": (
                f"Delivery to {title} is uncertain. Reconcile this operation; do not resend it."
            ),
        }
        return {
            "ok": status in {"posted", "accepted", "completed"},
            "status": status,
            "operation_id": record["operation_id"],
            "recipient": _public(target),
            "output": descriptions[status],
            **({"reason": reason} if reason else {}),
        }

    def _return(self, request, body, bound, record):
        self._binding(request, body, bound)
        if record["result"] is not None:
            return record["result"]
        if record["name"] in {"inspect_screen", "select_recipient"}:
            return {
                "ok": False,
                "status": "unknown",
                "operation_id": record["operation_id"],
                "recipient": _public(record["target"]),
                "output": "The recipient operation is unconfirmed; no message was sent.",
            }
        return self._delivery_result(record, record["status"])

    def _send_receipt(self, store, record, receipt):
        self._identity(receipt, record["target"], record["operation_id"])
        status = receipt.get("status")
        if status not in DELIVERY_STATUSES:
            raise RecipientError("recipient_response_invalid", 502)
        commit_token = receipt.get("commit_token")
        if commit_token is not None:
            if status != "queued":
                raise RecipientError("recipient_response_invalid", 502)
            commit_token = _text(commit_token, 4096)
        return store.finish(
            record, self._delivery_result(record, status), commit_token=commit_token
        )

    def send_agent_message(self, request, body, *, operation_id, bound=None):
        bound = self._binding(request, body, bound)
        arguments = body.get("arguments", {})
        self._arguments("send_agent_message", arguments)
        store, backend = self._store(bound), self._backend(bound)
        expected = self.identity_of(arguments)
        record, _ = store.prepare(
            operation_id, "send_agent_message", arguments,
            guard=(lambda target: self.require_identity(target, expected)) if expected else None,
        )
        target = record["target"]
        if target is None:
            if record["result"] is None:
                rows, _ = self._catalog(request, body, bound, backend)
                record = store.finish(
                    record, self._choice_result("selection_required", rows, operation_id)
                )
            return self._return(request, body, bound, record)
        if target["host_id"] != bound.token.owner.host:
            raise RecipientError("recipient_host_mismatch", 409)
        if arguments.get("app") is not None and target["app"] != arguments["app"]:
            if record["result"] is None:
                rows, _ = self._catalog(request, body, bound, backend, app=arguments["app"])
                result = self._choice_result("recipient_app_mismatch", rows, operation_id)
                record = store.finish(record, result)
            return self._return(request, body, bound, record)
        if not _can_send(target):
            record = store.finish(
                record,
                self._delivery_result(
                    record,
                    "failed",
                    reason="recipient_control_unavailable",
                ),
            )
            return self._return(request, body, bound, record)
        if record["result"] and record["status"] in {"failed", "completed"}:
            return self._return(request, body, bound, record)
        self._binding(request, body, bound)
        record, claimed = store.claim(record)
        try:
            self._binding(request, body, bound)
            receipt = (
                backend.send(operation_id, _target(target), arguments["message"])
                if claimed
                else backend.reconcile(operation_id, _target(target))
            )
            record = self._send_receipt(store, record, receipt)
            if record["status"] == "queued" and record["commit_token"]:
                self._binding(request, body, bound)
                record, commit = store.claim_commit(record)
                if commit:
                    self._binding(request, body, bound)
                    receipt = backend.send(
                        operation_id,
                        _target(target),
                        arguments["message"],
                        commit_token=record["commit_token"],
                    )
                    record = self._send_receipt(store, record, receipt)
        except DashboardTaskError as exc:
            store.finish(record, self._delivery_result(record, "unknown"))
            if not exc.retryable:
                raise
            record = store.operation(operation_id, "send_agent_message", arguments)
        except (OSError, TimeoutError):
            record = store.finish(record, self._delivery_result(record, "unknown"))
        return self._return(request, body, bound, record)

    def reconcile(self, request, body, *, action, bound=None):
        """Observe a prior delivery using its frozen identity; never submit or commit input."""
        bound = self._binding(request, body, bound)
        store = self._store(bound)
        record = store.operation(action["action_id"], action["name"], action["arguments"])
        if record is None:
            return action.get("recipient_receipt")
        if (action["name"] != "send_agent_message" or not record["target"]
                or not record["attempted"] or record["status"] in {"failed", "completed"}):
            return self._return(request, body, bound, record)
        self._binding(request, body, bound)
        try:
            receipt = self._backend(bound).reconcile(
                record["operation_id"], _target(record["target"]),
            )
            record = self._send_receipt(store, record, receipt)
        except DashboardTaskError as exc:
            if not exc.retryable:
                raise
        except (OSError, TimeoutError):
            pass
        return self._return(request, body, bound, record)

    def inspect_screen(self, request, body, *, operation_id, bound=None):
        bound = self._binding(request, body, bound)
        arguments = body.get("arguments", {})
        self._arguments("inspect_screen", arguments)
        store, backend = self._store(bound), self._backend(bound)
        record, _ = store.prepare(operation_id, "inspect_screen", arguments)
        target = record["target"]
        if record["result"] is not None:
            return self._return(request, body, bound, record)
        if target is None:
            rows, _ = self._catalog(request, body, bound, backend)
            record = store.finish(
                record, self._choice_result("selection_required", rows, operation_id)
            )
            return self._return(request, body, bound, record)
        if target["host_id"] != bound.token.owner.host:
            raise RecipientError("recipient_host_mismatch", 409)
        if not {"inspect", "inspect_screen"}.intersection(target["operations"]):
            result = {
                "ok": False,
                "status": "failed",
                "operation_id": operation_id,
                "recipient": _public(target),
                "reason": "recipient_inspection_unavailable",
                "output": "This host cannot verify a capture of the selected recipient window.",
            }
            return self._return(request, body, bound, store.finish(record, result))
        self._binding(request, body, bound)
        record, claimed = store.claim(record)
        if not claimed:
            return self._return(request, body, bound, record)
        try:
            self._binding(request, body, bound)
            receipt = backend.inspect(_target(target), capture=True)
            self._identity(receipt, target)
            artifact = receipt.get("artifact")
            if not isinstance(artifact, dict) or receipt.get("status") != "completed":
                raise RecipientError("recipient_response_invalid", 502)
            artifact_id = _text(receipt.get("artifact_id"))
            captured_at = _text(receipt.get("captured_at"), 128)
            if (
                artifact.get("artifact_id") != artifact_id
                or artifact.get("captured_at") != captured_at
                or artifact.get("recipient_id") != target["recipient_id"]
                or artifact.get("task_id") != target["task_id"]
                or artifact.get("mime_type") not in {"image/png", "image/jpeg"}
            ):
                raise RecipientError("recipient_response_invalid", 502)
            capture = {
                "artifact_id": artifact_id,
                "captured_at": captured_at,
                "path": _text(artifact.get("path"), 4096),
                "mime_type": artifact["mime_type"],
                "sha256": _text(artifact.get("sha256"), 64),
            }
            result = {
                "ok": True,
                "status": "completed",
                "operation_id": operation_id,
                "recipient": _public(target),
                "capture": capture,
                "output": f"Captured the selected {target['app']} window for {target['title']}.",
            }
            record = store.finish(record, result)
        except DashboardTaskError:
            raise
        except (OSError, TimeoutError):
            record = store.finish(
                record,
                {
                    "ok": False,
                    "status": "unknown",
                    "operation_id": operation_id,
                    "recipient": _public(target),
                    "output": "The screen capture is unconfirmed.",
                },
            )
        return self._return(request, body, bound, record)

def mount_recipient_routes(router, *, require_auth, read_body, task_call, service):
    def invoke(operation, request, body):
        fields = {
            "catalog": {"app", "limit", "cursor"},
            "history": {*IDENTITY_FIELDS, "limit", "cursor"},
            "status": set(IDENTITY_FIELDS),
            "select": {*IDENTITY_FIELDS, "action_id"},
        }
        if (not isinstance(body, dict)
                or set(body) - {"connection_id", "generation"} - fields[operation]):
            raise DashboardTaskError("invalid_event", 400)
        arguments = {key: value for key, value in body.items() if key in fields[operation]}
        if operation == "select":
            operation_id = identifier(bounded_text(arguments.pop("action_id", None), maximum=160))
            return service.select_exact(
                request, {**body, "arguments": arguments}, operation_id=operation_id,
            )
        name = {
            "catalog": "catalog_recipients", "history": "read_recipient_history",
            "status": "recipient_status",
        }[operation]
        return getattr(service, name)(request, {**body, "arguments": arguments})

    async def respond(operation, request, body):
        result = await task_call(
            lambda request, body: invoke(operation, request, body), request, body,
        )
        if JSONResponse is None:  # pragma: no cover - no dashboard response implementation
            raise DashboardTaskError("context_unavailable", 503)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/recipients/catalog")
    async def recipient_catalog(request: Request):
        require_auth(request)
        body = await read_body(request)
        return await respond("catalog", request, body)

    @router.post("/recipients/history")
    async def recipient_history(request: Request):
        require_auth(request)
        body = await read_body(request)
        return await respond("history", request, body)

    @router.post("/recipients/status")
    async def recipient_status(request: Request):
        require_auth(request)
        body = await read_body(request)
        return await respond("status", request, body)

    @router.post("/recipients/select")
    async def recipient_select(request: Request):
        require_auth(request)
        body = await read_body(request)
        return await respond("select", request, body)

    return recipient_catalog, recipient_history, recipient_status, recipient_select

