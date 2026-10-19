"""Canonical worker recipients use the existing exact-origin steering path."""

from __future__ import annotations

import copy
from dataclasses import replace

import httpx
import pytest
import test_dashboard_tasks as common
from test_dashboard_steering import SteeringHost
from test_recipients import Backend

from talk_dashboard_gateway import DashboardTaskError
from talk_recipients import RecipientService
from talk_worker_recipients import TalkWorkerRecipients, WorkerRecipientBackend


class WorkerHost(SteeringHost):
    def __call__(self, request):
        response = super().__call__(request)
        if request.url.path.endswith("/v1/capabilities"):
            data = response.json()
            data["features"]["linked_child_dispatch"]["external_workers"] = {
                "version": 1,
                "names": ["hermes-talk-codex"],
            }
            return self.response(200, data)
        return response


@pytest.fixture
def environment(tmp_path):
    manager, request, _, root = common.environment.__wrapped__(tmp_path)
    host = WorkerHost()
    factory = manager.transport_factory
    manager.transport_factory = lambda context: replace(
        factory(context), _http_transport=httpx.MockTransport(host)
    )
    return manager, request, host, root


def worker(environment, bound, context, *, suffix="one", kind="codex"):
    manager, request, _, _ = environment
    original = common.input_event(environment, context, input_id="worker-input-" + suffix)
    common.event(
        environment,
        context,
        "response.started",
        interaction_id=original["interaction_id"],
        response_id="worker-response-" + suffix,
    )
    response = manager.tool(
        request,
        {
            **context,
            "interaction_id": original["interaction_id"],
            "response_id": "worker-response-" + suffix,
            "call_id": "worker-call-" + suffix,
            "name": "delegate_task",
            "arguments": {"task": "Existing project work", "worker": kind},
        },
    )
    return bound.stages.action(bound.token, response["action"]["run_id"])


def target(row):
    return {key: row[key] for key in ("recipient_id", "app", "task_id", "target_token")}


def setup(environment):
    bound, context = common.join(environment)
    action = worker(environment, bound, context)
    projection = TalkWorkerRecipients(bound)
    row = projection.list_recipients("codex_worker")["recipients"][0]
    return bound, context, action, projection, row


def correction(environment, context):
    original = common.input_event(
        environment,
        context,
        input_id="correction",
        text="Use exactly $42.\nPreserve the existing job.",
    )
    common.event(
        environment,
        context,
        "response.started",
        interaction_id=original["interaction_id"],
        response_id="send-response",
    )
    return {
        **context,
        "interaction_id": original["interaction_id"],
        "response_id": "send-response",
        "call_id": "send-call",
        "name": "send_agent_message",
        "arguments": {
            "message": "Generated wording is not original authority.",
            "app": "codex_worker",
        },
    }


def test_list_projects_only_accepted_owned_codex_workers_without_new_dispatch(environment):
    _, _, host, _ = environment
    bound, context, action, projection, row = setup(environment)
    worker(environment, bound, context, suffix="hermes", kind="hermes")
    requests = len(host.requests)
    envelope = projection.list_recipients()
    assert len(envelope["recipients"]) == 1
    assert len(host.requests) == requests and len(host.jobs) == 2
    assert row["app"] == "codex_worker" and row["task_id"] == action["api_run_id"]
    assert row["proven_control"] == "worker" and row["operations"] == ["status", "steer_work"]
    assert row["title"].startswith("Codex worker #")
    assert projection.list_recipients("codex_desktop")["recipients"] == []


@pytest.mark.parametrize("mutation", ["worker", "correlation", "origin", "receipt", "uncertain"])
def test_missing_or_conflicting_dispatch_proof_never_becomes_a_recipient(environment, mutation):
    bound, _, action, projection, _ = setup(environment)
    body = copy.deepcopy(action["request_body"])
    fields = {"request_body": body}
    if mutation == "worker":
        body["child"]["worker"] = "hermes"
    elif mutation == "correlation":
        body["child"]["correlation_id"] = "different-action"
    elif mutation == "origin":
        body["origin"]["event_id"] = "different-origin"
    elif mutation == "receipt":
        body["origin"]["receipt_id"] += 1
    else:
        fields["state"] = "uncertain"
    bound.stages.update_action(bound.token, action["run_id"], **fields)
    assert projection.list_recipients()["recipients"] == []


def test_select_and_status_recheck_exact_host_run_and_parent_receipt(environment):
    _, _, host, _ = environment
    _, _, action, projection, row = setup(environment)
    assert projection.select(target(row))["status"] == "selected"
    host.jobs[action["api_run_id"]]["status"] = "completed"
    observed = projection.status(target(row))
    assert observed["status"] == "completed" and observed["status_source"] == "host_run"
    host.jobs[action["api_run_id"]]["parent_message_id"] = 9999
    with pytest.raises(DashboardTaskError) as error:
        projection.select(target(row))
    assert error.value.status == 403


def test_foreign_owner_and_modified_target_token_cannot_select_worker(environment):
    _, _, _, projection, row = setup(environment)
    with pytest.raises(DashboardTaskError) as error:
        projection.select({**target(row), "target_token": "made-up-proof"})
    assert error.value.status == 403
    other, _ = common.join(environment, session="task-b", tab="other-tab")
    with pytest.raises(DashboardTaskError) as error:
        TalkWorkerRecipients(other).select(target(row))
    assert error.value.status == 404


def test_canonical_worker_steering_uses_original_words_and_never_starts_another_job(environment):
    manager, request, host, _ = environment
    _, context, action, projection, row = setup(environment)
    body = correction(environment, context)
    mapped = projection.steering_body(target(row), body)
    assert mapped["name"] == "steer_work" and mapped["arguments"] == {"run_id": action["run_id"]}
    assert not host.deliveries and len(host.jobs) == 1
    first = manager.tool(request, mapped)
    repeat = manager.tool(request, mapped)
    assert first["action"]["control"] == repeat["action"]["control"]
    assert first["action"]["control"]["status"] == "queued"
    assert len(host.jobs) == len(host.deliveries) == 1
    assert host.deliveries[0]["input"] == "Use exactly $42.\nPreserve the existing job."
    assert first["action"]["control"]["api_run_id"] == action["api_run_id"]


def test_worker_control_lost_receipt_reconciles_same_action(environment):
    manager, request, host, _ = environment
    _, context, _, projection, row = setup(environment)
    mapped = projection.steering_body(target(row), correction(environment, context))
    host.drop = "after"
    first = manager.tool(request, mapped)
    assert first["action"]["control"]["status"] == "unknown"
    second = manager.tool(request, mapped)
    assert second["action"]["control"]["status"] == "queued"
    assert second["action"]["action_id"] == first["action"]["action_id"]
    assert len(host.deliveries) == len(host.jobs) == 1


def test_completed_worker_and_revoked_request_do_not_dispatch_controls(environment):
    manager, request, host, _ = environment
    _, context, action, projection, row = setup(environment)
    body = correction(environment, context)
    host.jobs[action["api_run_id"]]["status"] = "completed"
    with pytest.raises(DashboardTaskError, match=r"does not support|cannot steer"):
        projection.steering_body(target(row), body)
    host.jobs[action["api_run_id"]]["status"] = "running"
    mapped = projection.steering_body(target(row), body)
    request.state.principal = "different-operator"
    with pytest.raises(DashboardTaskError) as error:
        manager.tool(request, mapped)
    assert error.value.status == 403 and not host.deliveries


def test_backend_composition_keeps_desktop_recipients_and_service_worker_selection(environment):
    manager, request, host, _ = environment
    bound, context, _, _, row = setup(environment)
    fallback = Backend(bound)
    backend = WorkerRecipientBackend(bound, fallback)
    rows = backend.list_recipients()["recipients"]
    assert {item["app"] for item in rows} == {"codex_worker", "codex_desktop", "claude_code"}
    assert "worker-a" not in {item["recipient_id"] for item in rows}
    assert backend.list_recipients("codex_worker")["recipients"] == [row]
    service = RecipientService(manager, backend_factory=lambda _: backend)
    selected = service.select_recipient(
        request,
        {
            **context,
            "arguments": {"reference": row["recipient_id"], "app": "codex_worker"},
        },
        operation_id="select-existing-worker",
    )
    assert selected["recipient"]["app"] == "codex_worker" and selected["state"] == "selected"
    assert bound.attachment.owner.session_id == "task-a" and len(host.jobs) == 1
    with pytest.raises(DashboardTaskError):
        backend.send("send-worker", target(row), "No unlinked control")
    with pytest.raises(DashboardTaskError):
        backend.inspect(target(row))
    assert not any(call[0] in {"prepare", "commit"} for call in fallback.calls)


def test_worker_route_refuses_parent_runtime_substitution_and_malformed_arguments(environment):
    _, _, host, _ = environment
    _, context, _, projection, row = setup(environment)
    body = correction(environment, context)
    with pytest.raises(DashboardTaskError) as error:
        projection.steering_body(target(row), {**body, "arguments": {"message": ""}})
    assert error.value.status == 400
    host.kind = "ordinary"
    with pytest.raises(DashboardTaskError) as error:
        projection.steering_body(target(row), body)
    assert error.value.status == 403 and not host.deliveries


def test_manager_worker_recipient_call_reconciles_once_and_cannot_follow_selection(environment):
    manager, request, host, _ = environment
    bound, context = common.join(environment)
    original_job = worker(environment, bound, context, suffix="original")
    other_job = worker(environment, bound, context, suffix="other")
    body = correction(environment, context)

    def recipient_call(name, arguments, call_id):
        result = manager.tool(
            request, {**body, "name": name, "arguments": arguments, "call_id": call_id}
        )
        return result["action"]["recipient_receipt"]

    catalog = recipient_call("list_recipients", {"app": "codex_worker"}, "list-workers")
    rows = {row["task_id"]: row for row in catalog["recipients"]}
    assert set(rows) == {original_job["api_run_id"], other_job["api_run_id"]}
    selected = recipient_call(
        "select_recipient",
        {"reference": rows[original_job["api_run_id"]]["recipient_id"], "app": "codex_worker"},
        "select-original-worker",
    )
    assert selected["state"] == "selected"
    assert selected["recipient"]["send_agent_message"] == "direct"

    host.drop = "after"
    first = manager.tool(request, body)
    assert first["action"]["control"]["status"] == "unknown"
    reconciled = manager.tool(request, body)
    assert reconciled["action"]["action_id"] == first["action"]["action_id"]
    assert reconciled["action"]["control"]["status"] == "queued"
    assert reconciled["action"]["control"]["api_run_id"] == original_job["api_run_id"]
    assert manager.tool(request, body)["action"] == reconciled["action"]
    assert len(host.controls) == len(host.deliveries) == 1
    assert host.deliveries[0]["input"] == "Use exactly $42.\nPreserve the existing job."

    switched = recipient_call(
        "select_recipient",
        {"reference": rows[other_job["api_run_id"]]["recipient_id"], "app": "codex_worker"},
        "select-other-worker",
    )
    assert switched["recipient"]["task_id"] == other_job["api_run_id"]
    with pytest.raises(DashboardTaskError) as error:
        manager.tool(request, body)
    assert error.value.code == "event_conflict" and error.value.status == 409
    assert set(host.jobs) == {original_job["api_run_id"], other_job["api_run_id"]}
    assert set(host.controls) == {(original_job["api_run_id"], first["action"]["action_id"])}
    assert len(host.deliveries) == 1
    assert (
        len([row for row in host.requests if row[0] == "POST" and row[1].endswith("/steer")]) == 1
    )
