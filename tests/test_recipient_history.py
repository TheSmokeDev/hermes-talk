"""Recipient reads cross authenticated handlers, configured peer transport and owner fences."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
import test_dashboard_tasks as common
from test_recipients import recipient

from talk_dashboard_gateway import DashboardTaskError, RecipientGateway, TaskGateway
from talk_recipients import IDENTITY_FIELDS, RecipientError, mount_recipient_routes

environment = common.environment
NOW = "2026-09-14T12:00:00Z"
NATIVE_HOST = "host:native-fixture"


def native_row(app):
    return {
        **recipient("history-" + app, app, control="none",
                    operations=["select", "history", "status"]),
        "host_id": NATIVE_HOST, "read_only": True,
        "source": {
            "kind": "codex_native_history" if app == "codex_desktop" else "claude_session_history",
            "source_id": "source:" + app, "modified_at": NOW, "read_only": True,
            "path": "C:/private/native.jsonl",
        },
        "hidden": "private-catalog-field",
    }


class NativeHost:
    def __init__(self, fallback):
        self.fallback, self.calls = fallback, []
        self.rows = [native_row(app) for app in ("codex_desktop", "claude_code")]
        self.live = recipient("live-codex", "codex_desktop",
                              operations=["send", "history", "status"])
        self.messages = [
            {"id": "first", "role": "user", "text": "Exact original\n$42", "timestamp": None,
             "truncated": False},
            {"id": "last", "role": "assistant", "text": "Visible reply", "timestamp": NOW,
             "truncated": False},
        ]
        self.supported = True
        self.failure = None
        self.after = None
        self.mutate = None
        self.list_truncated = False

    def __call__(self, request):
        if "/v1/recipient-bridge/" not in request.url.path:
            return self.fallback(request)
        operation = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content)
        self.calls.append((operation, body, request))
        assert request.method == "POST"
        assert request.headers["Authorization"].startswith("Bearer ")
        assert body["session_id"] == "task-a" and body["actor_scope"]
        descriptor = {
            "version": 1, "operations": ["list", "select", "send"],
            "history": {"read_only": True},
        }
        if self.supported:
            descriptor["operations"] += ["catalog", "history", "status"]
        if operation == "probe":
            result = {"recipient_bridge": descriptor}
        elif operation == "list":
            result = {
                "recipients": [self.live], "capabilities": {},
                "truncated": self.list_truncated,
            }
        elif operation == "catalog":
            rows = [row for row in self.rows if body.get("app") in {None, row["app"]}]
            offset = 1 if body.get("cursor") == "catalog-page-two" else 0
            result = {
                "recipients": rows[offset:offset + body["limit"]],
                "sources": [{"app": row["app"], "available": True, "reason": None}
                            for row in rows],
                "observed_at": NOW, "truncated": len(rows) > offset + body["limit"],
                "next_cursor": "catalog-page-two" if len(rows) > offset + body["limit"] else None,
            }
        else:
            target = next((row for row in [*self.rows, self.live]
                           if row["target_token"] == body.get("target_token")), None)
            if target is None:
                return httpx.Response(404, json={"error": "recipient_not_found"})
            identity = {key: target.get(key, NATIVE_HOST) for key in IDENTITY_FIELDS}
            if self.failure and operation in {"history", "status", "select"}:
                return httpx.Response(self.failure[0], json={"error": self.failure[1]})
            if operation == "select":
                result = {**identity, "status": "selected"}
            elif operation == "history":
                result = {
                    **identity, "messages": copy.deepcopy(self.messages),
                    "source": target["source"],
                    "observed_at": NOW, "next_cursor": None, "truncated": False,
                    "system_prompt": "private-envelope-field",
                }
            elif operation == "status":
                result = {
                    **identity, "status": "unknown", "completion_tracking": False,
                    "available": True, "reason": "external_completion_unverified",
                    "observed_at": NOW, "source": target["source"],
                }
            else:
                pytest.fail("Read path attempted control: " + operation)
        result = copy.deepcopy(result)
        if self.mutate:
            self.mutate(operation, result)
        if self.after:
            self.after(operation)
        return httpx.Response(200, json=result)


@pytest.fixture
def bundle(environment):
    manager, request, host, _ = environment
    bound, context = common.join(environment)
    native = NativeHost(host)
    bound.gateway = TaskGateway(replace(
        bound.gateway.transport, _http_transport=httpx.MockTransport(native),
    ), before_request=bound.gateway.before_request)
    routes = {}

    class Router:
        def post(self, path):
            def register(handler):
                routes[path.rsplit("/", 1)[1]] = handler
                return handler
            return register

    def require_auth(request):
        if request.state.principal != "actor-one":
            raise DashboardTaskError("context_denied", 403)

    async def read_body(request):
        return request.payload

    async def task_call(function, request, body):
        return function(request, body)

    mount_recipient_routes(
        Router(), require_auth=require_auth, read_body=read_body,
        task_call=task_call, service=manager.recipients,
    )
    return SimpleNamespace(manager=manager, request=request, native=native, context=context,
                           bound=bound, routes=routes)


def route(bundle, operation, **fields):
    bundle.request.payload = {**bundle.context, **fields}
    response = asyncio.run(bundle.routes[operation](bundle.request))
    assert response.headers["Cache-Control"] == "no-store"
    return json.loads(response.body)


def identity(row):
    return {key: row[key] for key in IDENTITY_FIELDS}


def stored(bundle, app="codex_desktop"):
    return next(row for row in route(bundle, "catalog")["recipients"]
                if row["read_only"] and row["app"] == app)


@pytest.mark.parametrize("app", ["codex_desktop", "claude_code"])
def test_authenticated_catalog_history_status_keep_identity_and_visible_text(bundle, app):
    owner = bundle.bound.token.owner
    catalog = route(bundle, "catalog")
    assert len(catalog["recipients"]) == 3
    assert {row["title"] for row in catalog["recipients"]} == {"Build"}
    row = next(row for row in catalog["recipients"] if row["app"] == app and row["read_only"])
    assert row["host_id"] == owner.host and row["send_agent_message"] == "unavailable"
    assert row["proven_control"] == "none" and row["operations"] == ["select", "history", "status"]
    history = route(bundle, "history", **identity(row))
    assert history["messages"] == bundle.native.messages
    assert identity(history) == identity(row)
    assert set(history["source"]) == {"kind", "source_id", "modified_at", "read_only"}
    status = route(bundle, "status", **identity(row))
    assert status["status"] == "unknown" and status["completion_tracking"] is False
    assert bundle.bound.token.owner == owner
    assert bundle.manager.recipients.snapshot(bundle.bound)["selected"] is None
    assert "private-" not in json.dumps([catalog, history, status])
    assert "C:/" not in json.dumps([catalog, history, status])
    assert all(call[0] in {"list", "probe", "catalog", "history", "status"}
               for call in bundle.native.calls)


def test_read_only_selection_never_inherits_live_permissions_or_changes_voice_owner(bundle):
    row = stored(bundle)
    owner = bundle.bound.attachment.owner
    first = route(bundle, "select", **identity(row), action_id="select-stored")
    assert first["state"] == "selected"
    assert route(bundle, "select", **identity(row), action_id="select-stored") == first
    bundle.native.live.update(
        recipient_id="new-live-codex", task_id=row["task_id"], title=row["title"],
    )
    route(bundle, "catalog")
    response = bundle.manager.recipients.send_agent_message(
        bundle.request, {**bundle.context, "arguments": {"message": "Do not dispatch this"}},
        operation_id="send-stored",
    )
    assert response["reason"] == "recipient_control_unavailable"
    assert bundle.bound.attachment.owner == owner
    assert sum(call[0] == "select" for call in bundle.native.calls) == 1
    assert not any(call[0] in {"send", "inspect"} for call in bundle.native.calls)


def test_unknown_or_stale_selection_never_sends_and_is_not_retried(bundle):
    row = stored(bundle)
    bundle.native.failure = (409, "history_target_expired")
    with pytest.raises(DashboardTaskError) as error:
        route(bundle, "select", **identity(row), action_id="stale-select")
    assert error.value.code == "recipient_history_stale"
    bundle.native.failure = None
    retry = route(bundle, "select", **identity(row), action_id="stale-select")
    assert retry["status"] == "unknown"
    assert bundle.manager.recipients.snapshot(bundle.bound)["selected"] is None
    assert sum(call[0] == "select" for call in bundle.native.calls) == 1


@pytest.mark.parametrize("field,value", [
    ("recipient_id", "foreign"), ("task_id", "foreign"), ("host_id", "foreign"),
    ("app", "claude_code"),
])
def test_foreign_identity_never_reaches_host_read(bundle, field, value):
    row = stored(bundle)
    with pytest.raises(DashboardTaskError):
        route(bundle, "history", **{**identity(row), field: value})
    assert not any(call[0] == "history" for call in bundle.native.calls)


@pytest.mark.parametrize("status,code,expected", [
    (404, "recipient_not_found", "target_missing"),
    (409, "history_target_expired", "recipient_history_stale"),
    (409, "history_snapshot_changed", "recipient_history_stale"),
    (409, "history_cursor_mismatch", "recipient_history_stale"),
    (503, "native_history_unavailable", "recipient_history_unavailable"),
    (403, "recipient_authority_revoked", "context_denied"),
])
def test_host_stale_foreign_and_revoked_tokens_are_not_retried(bundle, status, code, expected):
    row = stored(bundle)
    bundle.native.failure = (status, code)
    with pytest.raises(DashboardTaskError) as error:
        route(bundle, "history", **identity(row), cursor="opaque-history-cursor")
    assert error.value.status == status and error.value.code == expected
    reads = [call for call in bundle.native.calls if call[0] == "history"]
    assert len(reads) == 1 and reads[0][1]["cursor"] == "opaque-history-cursor"


@pytest.mark.parametrize("operation", ["catalog", "history", "status", "select"])
def test_changed_discord_audience_is_checked_after_host_read(bundle, operation):
    row = stored(bundle)
    state = {"valid": True}

    def verify():
        if not state["valid"]:
            raise DashboardTaskError("context_denied", 403)

    bundle.bound.native_surface = SimpleNamespace(
        verify=verify, issuer=SimpleNamespace(transport=bundle.bound.gateway.transport),
        binding={"grant_id": "scoped-grant"},
    )
    def after(current):
        if current == operation:
            state["valid"] = False

    bundle.native.after = after
    fields = {} if operation == "catalog" else identity(row)
    if operation == "select":
        fields["action_id"] = "audience-select"
    with pytest.raises(DashboardTaskError) as error:
        route(bundle, operation, **fields)
    assert error.value.status == 403
    assert all(call[1].get("discord_binding") == {"grant_id": "scoped-grant"}
               for call in bundle.native.calls if call[1].get("discord_binding"))


@pytest.mark.parametrize("field,value", [
    ("host_id", "host:foreign-native"), ("app", "claude_code"), ("task_id", "other-task"),
])
def test_host_response_identity_mismatch_is_rejected_before_projection(bundle, field, value):
    row = stored(bundle)

    def mutate(operation, response):
        if operation == "history":
            response[field] = value

    bundle.native.mutate = mutate
    with pytest.raises(DashboardTaskError) as error:
        route(bundle, "history", **identity(row))
    assert error.value.status == 502


@pytest.mark.parametrize("mutation", [
    {"role": "system"}, {"role": "developer"}, {"role": "tool"},
    {"hidden": True}, {"reasoning": "private"}, {"content": [{"type": "image"}]},
    {"text": "x" * 8001}, {"timestamp": {"private": "data"}},
])
def test_invalid_or_hidden_message_records_cannot_escape(bundle, mutation):
    row = stored(bundle)
    bundle.native.messages[0].update(mutation)
    with pytest.raises(RecipientError) as error:
        route(bundle, "history", **identity(row))
    assert error.value.code == "recipient_response_invalid"


def test_response_and_request_page_bounds_are_enforced(bundle):
    row = stored(bundle)
    for limit in (0, 51, True, 1.5):
        with pytest.raises(DashboardTaskError) as error:
            route(bundle, "history", **identity(row), limit=limit)
        assert error.value.status == 400
    bundle.native.messages = [
        {**bundle.native.messages[0], "id": str(index), "text": "x" * 8000}
        for index in range(5)
    ]
    with pytest.raises(RecipientError):
        route(bundle, "history", **identity(row))
    bundle.native.messages = [{**row, "text": "short"} for row in bundle.native.messages] * 11
    with pytest.raises(RecipientError):
        route(bundle, "history", **identity(row), limit=50)


def test_catalog_truncated_is_true_when_the_live_list_was_cut(bundle):
    """The one flag the UI reads must say incomplete if EITHER side cut the set."""
    bundle.native.list_truncated = True
    reply = route(bundle, "catalog")
    assert reply["live_truncated"] is True
    assert reply["truncated"] is True
    assert reply["next_cursor"] is None  # the stored side was complete


def test_catalog_pagination_preserves_cached_identity_and_opaque_cursor(bundle):
    first = route(bundle, "catalog", limit=1)
    assert first["truncated"] and first["next_cursor"] == "catalog-page-two"
    row = next(row for row in first["recipients"] if row["read_only"])
    second = route(bundle, "catalog", limit=1, cursor=first["next_cursor"])
    assert second["next_cursor"] is None
    assert route(bundle, "history", **identity(row))["messages"]
    assert bundle.native.calls[-1][1]["target_token"] == "private-target-history-codex_desktop"


def test_old_host_capability_keeps_live_rows_and_reports_history_unavailable(bundle):
    bundle.native.supported = False
    result = route(bundle, "catalog")
    assert len(result["recipients"]) == 1 and not result["recipients"][0]["read_only"]
    assert all(source["reason"] == "recipient_history_unsupported" for source in result["sources"])
    assert not any(call[0] == "catalog" for call in bundle.native.calls)


def test_cached_rows_do_not_bypass_authorization_expiry_or_generation(bundle):
    row = stored(bundle)
    service = bundle.manager.recipients
    clock = service.clock()
    service.clock = lambda: clock + 301
    with pytest.raises(DashboardTaskError) as error:
        route(bundle, "history", **identity(row))
    assert error.value.code == "recipient_history_stale"
    bundle.request.state.principal = "revoked"
    with pytest.raises(DashboardTaskError) as error:
        route(bundle, "history", **identity(row))
    assert error.value.status == 403
    bundle.request.state.principal = "actor-one"
    with pytest.raises(DashboardTaskError) as error:
        route(bundle, "history", **identity(row), generation=bundle.context["generation"] + 1)
    assert error.value.code == "connection_stale"
    assert not any(call[0] == "history" for call in bundle.native.calls)


def test_gateway_rejects_unknown_operations_and_keeps_profile_prefix(bundle):
    gateway = RecipientGateway(bundle.bound)
    with pytest.raises(DashboardTaskError) as error:
        gateway._call("../runs")
    assert error.value.status == 400
    bundle.bound.gateway = TaskGateway(replace(
        bundle.bound.gateway.transport, profile="alpha", named_profile=True,
    ))
    gateway.catalog(limit=1)
    assert all(call[2].url.path.startswith("/p/alpha/v1/recipient-bridge/")
               for call in bundle.native.calls)


def test_route_refuses_browser_tokens_and_missing_selection_action_id(bundle):
    row = stored(bundle)
    with pytest.raises(DashboardTaskError) as error:
        route(bundle, "history", **identity(row), target_token="untrusted-token")
    assert error.value.status == 400
    with pytest.raises(DashboardTaskError) as error:
        route(bundle, "select", **identity(row))
    assert error.value.status == 400
