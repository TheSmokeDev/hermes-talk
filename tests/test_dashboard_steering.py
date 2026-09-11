"""Same-job controls over the real coordinator/outbox and bounded fixture gateway."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
import test_dashboard_tasks as common
from test_dashboard_tasks import Host, child, event, input_event, join

from talk_dashboard_gateway import DashboardTaskError, TaskGateway


class SteeringHost(Host):
    def __init__(self):
        super().__init__()
        self.controls, self.deliveries = {}, []
        self.steering_supported = True
        self.ordinary_origin = False
        self.kind = "linked_child"
        self.turn = "host:turn:1"
        self.before_steer = None
        self.before_target = None
        self.drop = None
        self.read_unavailable = False
        self.receipt_override = None

    def __call__(self, request):
        path = request.url.path
        if path.endswith("/steer"):
            remote = path.split("/")[-2]
            if remote not in self.jobs:
                return self.response(404, {"error": "not_found"})
            job = self.jobs[remote]
            action_id = request.url.params.get("action_id")
            if request.method == "GET":
                if action_id is not None:
                    if self.read_unavailable:
                        return self.response(503, {"error": "store_unavailable"})
                    receipt = self.controls.get((remote, action_id))
                    return (
                        self.response(200, receipt[1])
                        if receipt
                        else self.response(404, {"error": "steer_receipt_not_found"})
                    )
                if self.before_target:
                    self.before_target()
                return self.response(
                    200,
                    {
                        "object": "hermes.run.steering",
                        "version": 1,
                        "run_id": remote,
                        "status": job["status"],
                        "supported": job["status"] == "running",
                        "reason": None,
                        "kind": self.kind,
                        "session_id": job["child_session_id"]
                        if self.kind == "linked_child"
                        else job["session_id"],
                        "turn_id": self.turn,
                    },
                )
            body = json.loads(request.content)
            self.requests.append(("POST", path, body, None))
            if self.before_steer:
                self.before_steer()
            if self.drop == "before":
                self.drop = None
                raise httpx.ReadError("request did not arrive")
            control = body["control"]
            key = (remote, control["action_id"])
            if key in self.controls:
                assert self.controls[key][0] == body
                return self.response(200, self.controls[key][1])
            origin = control["origin"]
            source = self.receipts.get(("default", origin["event_id"]))
            evidence = "backend_queue_ack"
            status = "queued"
            parent = source["receipt"]["message_ids"][0] if source else None
            if not source:
                status, evidence = "rejected", "origin_unavailable"
            elif job["status"] != "running":
                status, evidence = "rejected", "run_not_accepting_steer"
            elif control["expected_turn_id"] != self.turn:
                status, evidence = "rejected", "stale_target"
            else:
                assert source["original"] == (
                    job["session_id"],
                    origin["origin_turn_id"],
                    [{"role": "user", "content": body["input"]}],
                )
                assert origin["receipt_id"] == source["receipt"]["revision"]
                self.deliveries.append(body)
            receipt = {
                "object": "hermes.run.steer",
                "version": 1,
                "run_id": remote,
                "action_id": control["action_id"],
                "session_id": control["expected_session_id"],
                "turn_id": control["expected_turn_id"],
                "origin": origin,
                "parent_message_id": parent,
                "status": status,
                "evidence": evidence,
                "created_at": 123.0,
            }
            if self.drop == "unknown":
                receipt.update(status="unknown", evidence="reserved_before_queue")
            self.controls[key] = (body, receipt)
            if self.drop in {"after", "unknown"}:
                self.drop = None
                raise httpx.ReadError("lost control acknowledgement")
            return self.response(200, self.receipt_override or receipt)
        response = super().__call__(request)
        if path.endswith("/v1/capabilities"):
            data = response.json()
            data["features"]["run_steering"] = {
                "version": 1,
                "supported": self.steering_supported,
                "durable_actions": True,
                "receipt_states": ["queued", "rejected", "unsupported", "unknown"],
                "origin_sources": {
                    "linked_child": ["passive_receipt"],
                    "ordinary": ["passive_receipt"] if self.ordinary_origin else [],
                },
                "max_input_chars": 16000,
            }
            return self.response(200, data)
        return response


@pytest.fixture
def environment(tmp_path):
    manager, request, _, root = common.environment.__wrapped__(tmp_path)
    host = SteeringHost()
    transport = manager.transport_factory
    manager.transport_factory = lambda context: replace(
        transport(context), _http_transport=httpx.MockTransport(host)
    )
    return manager, request, host, root


def prepare(environment):
    bound, context = join(environment)
    work = child(environment, context, input_event(environment, context))
    original = input_event(
        environment, context, input_id="correction", text="Use exactly $42.\nPlease."
    )
    event(
        environment,
        context,
        "response.started",
        interaction_id=original["interaction_id"],
        response_id="control-response",
    )
    body = {
        **context,
        "interaction_id": original["interaction_id"],
        "response_id": "control-response",
        "call_id": "control-call",
        "name": "steer_work",
        "arguments": {"run_id": work["action"]["run_id"]},
    }
    return bound, context, body


def test_two_exact_origins_steer_same_job_once_and_history_is_not_rewritten(environment):
    manager, request, host, _ = environment
    bound, context, body = prepare(environment)
    before = list(host.rows[("default", "task-a")])
    result = manager.tool(request, body)
    receipt = result["action"]["control"]
    assert receipt["status"] == "queued" and receipt["source"] == "host_receipt"
    assert receipt["parent_message_id"] == before[-1]["id"]
    assert host.deliveries[0]["input"] == "Use exactly $42.\nPlease."
    assert manager.tool(request, body)["action"] == result["action"]
    second = input_event(
        environment, context, input_id="second", text="Also preserve the audience."
    )
    event(
        environment,
        context,
        "response.started",
        interaction_id=second["interaction_id"],
        response_id="second-response",
    )
    second_body = {
        **body,
        "interaction_id": second["interaction_id"],
        "response_id": "second-response",
        "call_id": "second-control",
    }
    second_result = manager.tool(request, second_body)
    assert second_result["action"]["control"]["api_run_id"] == receipt["api_run_id"]
    assert len(host.jobs) == 1 and len(host.deliveries) == 2
    assert host.rows[("default", "task-a")][:-1] == before
    state = manager.state(request, context)
    assert state["jobs"][0]["steering"]["supported"] is True
    assert len(state["jobs"]) == 1
    assert bound.stages.action(bound.token, result["action"]["run_id"])["api_run_id"] is None


@pytest.mark.parametrize("loss", ["before", "after", "unknown"])
def test_response_loss_reconnect_is_read_only_and_explicit_retry_is_exact(environment, loss):
    manager, request, host, _ = environment
    bound, context, body = prepare(environment)
    host.drop = loss
    result = manager.tool(request, body)
    assert result["action"]["control"]["status"] == "unknown"
    frozen = bound.stages.action(bound.token, result["action"]["run_id"])["control_body"]
    posts = len([row for row in host.requests if row[0] == "POST" and row[1].endswith("/steer")])
    manager.close(request, context)
    new_bound, new_context = join(environment)
    manager.state(request, new_context)
    assert (
        len([row for row in host.requests if row[0] == "POST" and row[1].endswith("/steer")])
        == posts
    )
    result = manager.tool(request, {**body, **new_context})
    assert result["action"]["control"]["status"] == ("unknown" if loss == "unknown" else "queued")
    assert len(host.deliveries) == 1
    assert (
        new_bound.stages.action(new_bound.token, result["action"]["run_id"])["control_body"]
        == frozen
    )


def test_unavailable_reconciliation_never_resubmits(environment):
    manager, request, host, _ = environment
    _, _, body = prepare(environment)
    host.drop = "before"
    manager.tool(request, body)
    host.read_unavailable = True
    assert manager.tool(request, body)["action"]["control"]["status"] == "unknown"
    assert not host.deliveries
    assert len([r for r in host.requests if r[0] == "POST" and r[1].endswith("/steer")]) == 1


def test_host_unknown_can_reconcile_later_settlement_without_resubmission(environment):
    manager, request, host, _ = environment
    _, context, body = prepare(environment)
    host.drop = "unknown"
    manager.tool(request, body)
    assert manager.tool(request, body)["action"]["control"]["source"] == "host_receipt"
    stored = next(iter(host.controls.values()))[1]
    stored.update(status="queued", evidence="backend_queue_ack")
    result = manager.state(request, context)
    assert result["interactions"][-1]["actions"][0]["control"]["status"] == "queued"
    assert len([r for r in host.requests if r[0] == "POST" and r[1].endswith("/steer")]) == 1


def test_configured_peer_uses_same_profile_transport_for_existing_job_control(environment):
    manager, request, host, _ = environment
    owner = manager.resolve_context(request, "default")
    peer = replace(
        manager.transport_factory(owner), base_url="https://peer.example", named_profile=True
    )
    record = {"peer_id": "saved-peer", "target_id": "opaque-target", "host_label": "Saved peer"}
    resolved = SimpleNamespace(context=owner, transport=peer, record=record)
    manager.target_resolver = lambda req, target: resolved
    manager.selection_guard = lambda req, bound: None
    bound = manager.join(
        request,
        {"session_id": "task-a", "profile": "default", "tab_id": "peer-tab"},
        resolved=resolved,
    )
    context = {"connection_id": bound.connection_id, "generation": bound.generation}
    work = child(environment, context, input_event(environment, context))
    correction = input_event(
        environment, context, input_id="peer-correction", text="Peer correction"
    )
    event(
        environment,
        context,
        "response.started",
        interaction_id=correction["interaction_id"],
        response_id="peer-response",
    )
    result = manager.tool(
        request,
        {
            **context,
            "interaction_id": correction["interaction_id"],
            "response_id": "peer-response",
            "call_id": "peer-control",
            "name": "steer_work",
            "arguments": {"run_id": work["action"]["run_id"]},
        },
    )
    assert result["action"]["control"]["status"] == "queued"
    assert any(row[1] == "/p/default/v1/runs/remote-1/steer" for row in host.requests)


@pytest.mark.parametrize(
    "case", ["unsupported", "ordinary", "foreign", "stale", "stopped", "deleted"]
)
def test_refusal_never_creates_replacement_or_stops_job(environment, case):
    manager, request, host, _ = environment
    _, _, body = prepare(environment)
    if case == "unsupported":
        host.steering_supported = False
    elif case == "ordinary":
        host.kind = "ordinary"
    elif case == "foreign":
        body["arguments"] = {"api_run_id": "remote-1"}
        host.jobs["remote-1"]["session_id"] = "task-b"
    elif case == "stale":
        host.before_steer = lambda: setattr(host, "turn", "host:turn:2")
    elif case == "stopped":
        host.before_steer = lambda: host.jobs["remote-1"].update(status="cancelled")
    else:
        host.before_steer = host.receipts.clear
    result = manager.tool(request, body)
    assert result["action"]["control"]["status"] in {"rejected", "unsupported"}
    assert len(host.jobs) == 1 and not host.deliveries
    assert not any(row[1].endswith("/stop") for row in host.requests)


def test_ordinary_origin_capability_is_additive_and_exact_api_run_is_owner_checked(environment):
    manager, request, host, _ = environment
    _, _, body = prepare(environment)
    host.kind, host.ordinary_origin = "ordinary", True
    body["arguments"] = {"api_run_id": "remote-1"}
    result = manager.tool(request, body)
    assert result["action"]["control"]["status"] == "queued"
    assert result["action"]["control"]["session_id"] == "task-a"


def test_unsupported_runtime_is_explicitly_unavailable(environment, monkeypatch):
    manager, request, host, _ = environment
    bound, _, body = prepare(environment)
    original = bound.gateway.steering
    monkeypatch.setattr(
        TaskGateway,
        "steering",
        lambda self, remote: {
            **original(remote),
            "supported": False,
            "reason": "backend_unsupported",
        },
    )
    result = manager.tool(request, body)
    assert result["action"]["control"]["status"] == "unsupported"
    assert not host.deliveries


def test_pending_original_requires_receipt_and_reconnect_does_not_send_new_control(environment):
    manager, request, host, _ = environment
    _, context = join(environment)
    work = child(environment, context, input_event(environment, context))
    host.busy_commit = True
    correction = input_event(environment, context, input_id="pending", text="Pending correction")
    assert correction["canonical_state"] == "pending"
    event(
        environment,
        context,
        "response.started",
        interaction_id=correction["interaction_id"],
        response_id="pending-response",
    )
    body = {
        **context,
        "interaction_id": correction["interaction_id"],
        "response_id": "pending-response",
        "call_id": "pending-control",
        "name": "steer_work",
        "arguments": {"run_id": work["action"]["run_id"]},
    }
    result = manager.tool(request, body)
    assert result["action"]["control"]["evidence"] == "steering_origin_pending"
    manager.close(request, context)
    _, new_context = join(environment)
    assert not host.deliveries
    assert manager.tool(request, {**body, **new_context})["action"]["control"]["status"] == "queued"
    assert (
        len(
            [
                row
                for row in host.rows[("default", "task-a")]
                if row["content"] == "Pending correction"
            ]
        )
        == 1
    )


def test_late_receipt_persists_for_original_owner_but_old_presentation_fails(environment):
    manager, request, host, _ = environment
    bound, context, body = prepare(environment)
    host.before_steer = lambda: manager.close(request, context)
    with pytest.raises(DashboardTaskError, match="no longer current"):
        manager.tool(request, body)
    host.before_steer = None
    new_bound, new_context = join(environment)
    state = manager.state(request, new_context)
    control = state["interactions"][-1]["actions"][0]["control"]
    assert control["status"] == "queued" and len(host.deliveries) == 1
    assert new_bound.attachment.owner == bound.attachment.owner


def test_revoked_actor_during_target_read_blocks_post(environment):
    manager, request, host, _ = environment
    _, _, body = prepare(environment)
    host.before_target = lambda: setattr(request.state, "principal", "actor-two")
    with pytest.raises(DashboardTaskError):
        manager.tool(request, body)
    assert not host.deliveries


def test_changed_tool_fingerprint_and_generated_correction_are_refused(environment):
    manager, request, host, _ = environment
    _, _, body = prepare(environment)
    manager.tool(request, body)
    with pytest.raises(DashboardTaskError):
        manager.tool(request, {**body, "arguments": {"api_run_id": "remote-1"}})
    with pytest.raises(DashboardTaskError):
        manager.tool(request, {**body, "arguments": {"run_id": 1, "input": "Invented correction"}})
    assert len(host.deliveries) == 1


def test_boolean_ack_and_foreign_receipt_never_claim_queue_admission(environment):
    manager, request, host, _ = environment
    _, _, body = prepare(environment)
    host.receipt_override = {"steered": True}
    result = manager.tool(request, body)
    assert result["action"]["control"]["status"] == "unknown"
    assert "unconfirmed" in result["output"]


def test_fixed_gateway_only_exact_receipt_not_found_allows_retry(environment):
    bound, _ = join(environment)
    for error in ("not_found", {"code": "steer_receipt_not_found"}):
        transport = replace(
            bound.gateway.transport,
            _http_transport=httpx.MockTransport(
                lambda request, error=error: httpx.Response(404, json={"error": error})
            ),
        )
        with pytest.raises(DashboardTaskError):
            TaskGateway(transport).steer_receipt("remote-1", "action-one")
