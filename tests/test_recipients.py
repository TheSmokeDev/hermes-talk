"""Recipient routing uses actual owner fences and durable stores; UI I/O is controlled."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace

import pytest
import test_dashboard_tasks as dashboard_fixtures

from talk_dashboard_gateway import DashboardTaskError
from talk_recipient_store import RecipientStore
from talk_recipients import RecipientError, RecipientService, recipient_tools

environment = dashboard_fixtures.environment
join = dashboard_fixtures.join


def recipient(recipient_id, app, *, title="Build", control="ui_bridge", operations=None):
    return {
        "recipient_id": recipient_id,
        "app": app,
        "task_id": recipient_id + "-task",
        "title": title,
        "target_token": "private-target-" + recipient_id,
        "proven_control": control,
        "operations": ["send", "reconcile", "inspect"] if operations is None else operations,
    }


class Backend:
    def __init__(self, bound):
        self.host_id = bound.token.owner.host
        self.rows = [
            recipient("desktop-a", "codex_desktop"),
            recipient("claude-a", "claude_code"),
            recipient("worker-a", "codex_worker", title="Owned worker", control="worker"),
        ]
        self.calls, self.journal = [], {}
        self.after_list = self.after_prepare = self.after_commit = None
        self.after_inspect = None
        self.drop_prepare = self.drop_commit = False
        self.final_status = "posted"
        self.computer = {"mode": "delegated", "tool": "delegate_task", "verified": True}

    def identity(self, target):
        row = next(row for row in self.rows if row["recipient_id"] == target["recipient_id"])
        assert {key: row[key] for key in target} == target
        return {
            "host_id": self.host_id,
            **{
                key: row[key]
                for key in (
                    "recipient_id",
                    "app",
                    "task_id",
                )
            },
        }

    def list_recipients(self, app=None):
        self.calls.append(("list", app))
        if self.after_list:
            self.after_list()
        return {
            "host_id": self.host_id,
            "recipients": copy.deepcopy(self.rows),
            "capabilities": {"computer_use": self.computer},
        }

    def select(self, target):
        self.calls.append(("select", target["recipient_id"]))
        return {**self.identity(target), "status": "selected"}

    def send(self, operation_id, target, message, *, commit_token=None):
        phase = "commit" if commit_token else "prepare"
        self.calls.append((phase, operation_id, target["recipient_id"], message))
        identity = {**self.identity(target), "operation_id": operation_id}
        if not commit_token:
            assert operation_id not in self.journal
            receipt = {
                **identity,
                "status": "queued",
                "commit_token": "private-commit-" + operation_id,
            }
            self.journal[operation_id] = receipt
            if self.after_prepare:
                self.after_prepare()
            if self.drop_prepare:
                raise TimeoutError
            return copy.deepcopy(receipt)
        assert self.journal[operation_id]["commit_token"] == commit_token
        self.journal[operation_id] = {**identity, "status": self.final_status}
        if self.after_commit:
            self.after_commit()
        if self.drop_commit:
            raise TimeoutError
        return copy.deepcopy(self.journal[operation_id])

    def reconcile(self, operation_id, target):
        self.calls.append(("reconcile", operation_id, target["recipient_id"]))
        self.identity(target)
        return copy.deepcopy(self.journal[operation_id])

    def inspect(self, target, *, capture=False):
        self.calls.append(("inspect", target["recipient_id"], capture))
        assert capture is True
        receipt = {
            **self.identity(target),
            "status": "completed",
            "artifact_id": "capture-one",
            "captured_at": "2026-09-12T12:00:00Z",
        }
        receipt["artifact"] = {
            **{
                key: receipt[key]
                for key in ("artifact_id", "captured_at", "recipient_id", "task_id")
            },
            "path": "fixture-capture.png",
            "mime_type": "image/png",
            "sha256": "a" * 64,
            "window": {
                "pid": 123,
                "window_id": "window-1",
                "process_started": 100,
                "exe": "fixture.exe",
            },
        }
        if self.after_inspect:
            self.after_inspect()
        return receipt


@pytest.fixture
def bundle(environment):
    manager, request, host, _ = environment
    bound, context = join(environment)
    backend = Backend(bound)
    service = RecipientService(manager, backend_factory=lambda _: backend)
    return service, backend, bound, context, request, host


def call(bundle, name, arguments=None, *, operation_id="operation-one"):
    service, _, bound, context, request, _ = bundle
    arguments = arguments or {}
    body = {**context, "name": name, "arguments": arguments}
    action = {"action_id": operation_id, "run_id": 1, "name": name, "arguments": arguments}
    return service.tool(request, body, action=action, bound=bound)


def select(bundle, reference="desktop-a", *, operation_id="select-one", app=None):
    arguments = {"reference": reference}
    if app:
        arguments["app"] = app
    return call(bundle, "select_recipient", arguments, operation_id=operation_id)


def send(bundle, *, operation_id="send-one", message="Continue the original work."):
    return call(bundle, "send_agent_message", {"message": message}, operation_id=operation_id)


def test_lists_both_existing_apps_and_worker_without_exposing_control_tokens(bundle):
    result = call(bundle, "list_recipients")
    assert {row["app"] for row in result["recipients"]} == {
        "codex_desktop",
        "claude_code",
        "codex_worker",
    }
    assert len({row["recipient_id"] for row in result["recipients"]}) == 3
    assert "private-target" not in json.dumps(result)
    assert result["capabilities"]["computer_use"] == {
        "mode": "delegated",
        "tool": "delegate_task",
        "verified": True,
        "reason": "host_verified",
    }
    assert bundle[1].calls == [("list", None)]


def test_duplicate_names_need_named_choice_and_explicit_app_resolves(bundle):
    result = select(bundle, "Build")
    assert result["state"] == "ambiguous"
    assert {row["app"] for row in result["choices"]} == {"codex_desktop", "claude_code"}
    assert not any(item[0] == "select" for item in bundle[1].calls)
    selected = select(bundle, "Build", app="claude_code", operation_id="select-claude")
    assert selected["recipient"]["recipient_id"] == "claude-a"


def test_missing_selection_does_not_choose_only_matching_app_or_create_worker(bundle):
    first = send(bundle)
    assert first["state"] == "selection_required"
    assert len(first["choices"]) == 3
    select(bundle)
    assert send(bundle) == first
    assert not any(item[0] in {"prepare", "commit"} for item in bundle[1].calls)


def test_missing_named_recipient_clears_addressing_without_substitution(bundle):
    select(bundle)
    result = select(bundle, "Unlisted Codex task", operation_id="select-missing")
    assert result["state"] == "missing"
    assert bundle[0].snapshot(bundle[2])["selected"] is None
    assert send(bundle)["state"] == "selection_required"


@pytest.mark.parametrize(
    "target,app",
    [("desktop-a", "codex_desktop"), ("claude-a", "claude_code"), ("worker-a", "codex_worker")],
)
def test_selection_keeps_voice_owner_and_delivers_to_exact_recipient(bundle, target, app):
    voice_owner = bundle[2].attachment.owner
    select(bundle, target)
    result = send(bundle)
    assert result["status"] == "posted"
    assert result["recipient"]["app"] == app
    assert "completion are unconfirmed" in result["output"]
    assert bundle[2].attachment.owner == voice_owner
    assert [item[2] for item in bundle[1].calls if item[0] in {"prepare", "commit"}] == [
        target,
        target,
    ]
    assert "private-commit" not in json.dumps(result)


@pytest.mark.parametrize(
    "control,live_owner,app,allowed",
    [
        ("none", False, "codex_desktop", False),
        ("app_server", False, "codex_desktop", False),
        ("app_server", True, "codex_desktop", True),
        ("worker", True, "codex_desktop", False),
        ("ui_bridge", True, "codex_worker", False),
        ("app_server", True, "claude_code", False),
    ],
)
def test_history_never_grants_app_server_control_or_cross_app_worker_substitution(
    bundle,
    control,
    live_owner,
    app,
    allowed,
):
    row = bundle[1].rows[0]
    row.update(proven_control=control, live_owner=live_owner, app=app)
    select(bundle)
    result = send(bundle)
    assert (result["status"] == "posted") is allowed
    assert any(item[0] == "prepare" for item in bundle[1].calls) is allowed


def test_lost_prepare_reconciles_before_commit_and_keeps_original_target(bundle):
    select(bundle)
    bundle[1].drop_prepare = True
    assert send(bundle)["status"] == "unknown"
    select(bundle, "claude-a", operation_id="select-other")
    old = bundle[0]
    bundle = (RecipientService(old.manager, backend_factory=old.backend_factory), *bundle[1:])
    result = send(bundle)
    assert result["status"] == "posted"
    assert result["recipient"]["recipient_id"] == "desktop-a"
    phases = [item[0] for item in bundle[1].calls if item[0] in {"prepare", "reconcile", "commit"}]
    assert phases == ["prepare", "reconcile", "commit"]
    assert bundle[0].snapshot(bundle[2])["selected"]["recipient_id"] == "claude-a"


def test_lost_commit_reconciles_without_second_enter(bundle):
    select(bundle)
    bundle[1].drop_commit = True
    assert send(bundle)["status"] == "unknown"
    assert send(bundle)["status"] == "posted"
    assert [item[0] for item in bundle[1].calls].count("commit") == 1


def test_unknown_reconciliation_never_resends(bundle):
    select(bundle)
    bundle[1].drop_commit = True
    send(bundle)
    bundle[1].journal["send-one"]["status"] = "unknown"
    assert send(bundle)["status"] == "unknown"
    assert send(bundle)["status"] == "unknown"
    assert [item[0] for item in bundle[1].calls].count("prepare") == 1
    assert [item[0] for item in bundle[1].calls].count("commit") == 1


def test_stale_authority_after_preparation_prevents_enter_and_private_output(bundle):
    select(bundle)
    original = bundle[4].state.principal
    bundle[1].after_prepare = lambda: setattr(bundle[4].state, "principal", "other-actor")
    with pytest.raises(DashboardTaskError, match="authorized"):
        send(bundle)
    assert not any(item[0] == "commit" for item in bundle[1].calls)
    bundle[4].state.principal = original
    bundle[1].after_prepare = None
    assert send(bundle)["status"] == "posted"
    assert [item[0] for item in bundle[1].calls].count("prepare") == 1


def test_revoked_binding_after_commit_records_receipt_without_releasing_output(bundle):
    select(bundle)
    original = bundle[4].state.principal
    bundle[1].after_commit = lambda: setattr(bundle[4].state, "principal", "other-actor")
    with pytest.raises(DashboardTaskError):
        send(bundle)
    record = RecipientStore(bundle[2]).operation(
        "send-one",
        "send_agent_message",
        {
            "message": "Continue the original work.",
        },
    )
    assert record["status"] == "posted"
    bundle[4].state.principal = original
    bundle[1].after_commit = None
    assert send(bundle)["status"] == "posted"
    assert [item[0] for item in bundle[1].calls].count("commit") == 1


def test_same_operation_cannot_change_message(bundle):
    select(bundle)
    send(bundle)
    with pytest.raises(DashboardTaskError, match="different content"):
        send(bundle, message="A different instruction")


def test_body_operation_id_is_never_used_and_action_arguments_must_match(bundle):
    service, _, bound, context, request, _ = bundle
    body = {
        **context,
        "name": "send_agent_message",
        "arguments": {"message": "Hello"},
        "operation_id": "model-invented",
    }
    with pytest.raises(DashboardTaskError):
        service.tool(
            request,
            body,
            action={
                "action_id": "real",
                "name": "send_agent_message",
                "arguments": {"message": "Tampered"},
            },
            bound=bound,
        )
    select(bundle)
    result = service.tool(
        request,
        body,
        action={"action_id": "real", "name": "send_agent_message", "arguments": body["arguments"]},
        bound=bound,
    )
    assert result["operation_id"] == "real"


def test_old_selection_retry_does_not_retarget_after_later_selection(bundle):
    select(bundle)
    select(bundle, "claude-a", operation_id="select-other")
    select(bundle)
    assert bundle[0].snapshot(bundle[2])["selected"]["recipient_id"] == "claude-a"


def test_foreign_host_catalog_and_receipt_are_rejected(bundle):
    bundle[1].host_id = "different-host"
    with pytest.raises(RecipientError, match="different execution host"):
        call(bundle, "list_recipients")


def test_selection_is_scoped_to_selected_execution_host(environment):
    manager, request, _, _ = environment
    first, context = join(environment)
    backend = Backend(first)
    service = RecipientService(manager, backend_factory=lambda _: backend)
    bundle = (service, backend, first, context, request, None)
    select(bundle)
    original_factory = manager.transport_factory
    manager.transport_factory = lambda context: replace(
        original_factory(context), base_url="http://127.0.0.1:9642"
    )
    second, second_context = join(environment, tab="tab-other")
    other = Backend(second)
    service.backend_factory = lambda _: other
    second_bundle = (service, other, second, second_context, request, None)
    assert first.token.owner.host != second.token.owner.host
    assert service.snapshot(second)["selected"] is None
    assert send(second_bundle)["state"] == "selection_required"
    assert not any(item[0] == "prepare" for item in other.calls)


def test_snapshot_and_capabilities_do_not_capture_or_read_ui(bundle):
    select(bundle)
    before = list(bundle[1].calls)
    assert bundle[0].snapshot(bundle[2])["selected"]["title"] == "Build"
    assert bundle[0].capabilities(bundle[2])["computer_use"]["mode"] == "delegated"
    assert before == bundle[1].calls
    bundle[0].clock = lambda: 10**12
    assert bundle[0].capabilities(bundle[2])["computer_use"]["mode"] == "unknown"


@pytest.mark.parametrize(
    "capability",
    [
        {"mode": "direct", "verified": False},
        {"mode": "delegated", "verified": True},
        {"mode": "delegated", "verified": True, "tool": "invented"},
    ],
)
def test_unproven_host_capability_is_unknown(bundle, capability):
    bundle[1].computer = capability
    result = call(bundle, "list_recipients")
    assert result["capabilities"]["computer_use"]["mode"] == "unknown"


def test_sensitive_catalog_output_is_denied_after_binding_revoked(bundle):
    bundle[1].after_list = lambda: setattr(bundle[4].state, "principal", "other-actor")
    with pytest.raises(DashboardTaskError):
        call(bundle, "list_recipients")


def test_inspect_only_selection_captures_once_on_explicit_tool(bundle):
    bundle[1].rows[0].update(proven_control="none", operations=["inspect"])
    select(bundle)
    assert not any(item[0] == "inspect" for item in bundle[1].calls)
    result = call(bundle, "inspect_screen", operation_id="capture-one")
    assert result["status"] == "completed"
    assert result["capture"]["artifact_id"] == "capture-one"
    assert call(bundle, "inspect_screen", operation_id="capture-one") == result
    assert [item[0] for item in bundle[1].calls].count("inspect") == 1
    assert send(bundle)["status"] == "failed"


def test_capture_receipt_is_hidden_after_authority_revoked(bundle):
    select(bundle)
    bundle[1].after_inspect = lambda: setattr(bundle[4].state, "principal", "other-actor")
    with pytest.raises(DashboardTaskError):
        call(bundle, "inspect_screen", operation_id="capture-one")


def test_capture_requires_matching_artifact_not_an_assistant_claim(bundle, monkeypatch):
    select(bundle)
    monkeypatch.setattr(
        bundle[1],
        "inspect",
        lambda target, capture: {
            **bundle[1].identity(target),
            "status": "completed",
            "text": "I can see your screen.",
        },
    )
    with pytest.raises(RecipientError):
        call(bundle, "inspect_screen", operation_id="capture-one")


def test_concurrent_same_id_can_only_prepare_and_commit_once(bundle):
    select(bundle)
    entered, release = threading.Event(), threading.Event()
    bundle[1].after_prepare = lambda: (entered.set(), release.wait(5))
    results = []
    thread = threading.Thread(target=lambda: results.append(send(bundle)))
    thread.start()
    assert entered.wait(5)
    try:
        retry = send(bundle)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert retry["status"] == "posted"
    assert results[0]["status"] == "posted"
    assert [item[0] for item in bundle[1].calls].count("prepare") == 1
    assert [item[0] for item in bundle[1].calls].count("commit") == 1


def test_tool_schemas_never_offer_worker_creation_or_model_supplied_operation_ids():
    schemas = recipient_tools()
    assert {tool["name"] for tool in schemas} == {
        "list_recipients",
        "select_recipient",
        "send_agent_message",
        "inspect_screen",
    }
    assert all("operation_id" not in tool["parameters"]["properties"] for tool in schemas)
    assert all("worker" not in tool["parameters"]["properties"] for tool in schemas)


def test_explicit_codex_request_never_goes_to_selected_claude(bundle):
    select(bundle, "claude-a")
    arguments = {"message": "Continue the original job.", "app": "codex_desktop"}
    result = call(bundle, "send_agent_message", arguments, operation_id="codex-send")
    assert result["state"] == "recipient_app_mismatch"
    assert {row["app"] for row in result["choices"]} == {"codex_desktop"}
    assert not any(item[0] == "prepare" for item in bundle[1].calls)
    select(bundle, "desktop-a", operation_id="select-codex")
    assert call(bundle, "send_agent_message", arguments, operation_id="codex-send") == result


def test_verified_inspection_capability_names_actual_tool(bundle):
    bundle[1].computer = {
        "mode": "delegated",
        "tool": "inspect_screen",
        "verified": True,
        "reason": "verified_capture",
    }
    result = call(bundle, "list_recipients")
    assert result["capabilities"]["computer_use"] == bundle[1].computer


def test_inspection_only_window_does_not_claim_computer_use_unavailable(bundle):
    bundle[1].computer = {"mode": "unavailable", "verified": True}
    for row in bundle[1].rows:
        row.update(proven_control="none", operations=["inspect"])
    result = call(bundle, "list_recipients")
    assert result["capabilities"]["computer_use"] == {
        "mode": "delegated",
        "tool": "inspect_screen",
        "verified": True,
        "reason": "verified_recipient_inspection",
    }


@pytest.mark.parametrize(
    "status", ["queued", "posted", "accepted", "completed", "failed", "unknown"]
)
def test_only_factual_host_delivery_status_is_reported(bundle, monkeypatch, status):
    select(bundle)
    backend = bundle[1]
    monkeypatch.setattr(
        backend,
        "send",
        lambda op, target, message: {
            **backend.identity(target),
            "operation_id": op,
            "status": status,
        },
    )
    result = send(bundle)
    assert result["status"] == status
    assert result["ok"] is (status in {"posted", "accepted", "completed"})


def test_mismatched_delivery_identity_remains_unknown_without_releasing_receipt(
    bundle, monkeypatch
):
    select(bundle)
    backend = bundle[1]
    monkeypatch.setattr(
        backend,
        "send",
        lambda op, target, message: {
            **backend.identity(target),
            "operation_id": op,
            "recipient_id": "claude-a",
            "status": "posted",
        },
    )
    with pytest.raises(RecipientError):
        send(bundle)
    assert (
        RecipientStore(bundle[2]).operation(
            "send-one",
            "send_agent_message",
            {
                "message": "Continue the original work.",
            },
        )["status"]
        == "unknown"
    )
