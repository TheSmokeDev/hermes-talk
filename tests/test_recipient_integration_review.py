"""Recipient result recovery crosses the real canonical action boundary."""

import pytest
import test_dashboard_tasks as dashboard_fixtures
from test_recipients import Backend

from talk_dashboard_gateway import DashboardTaskError
from talk_recipients import RecipientService

environment = dashboard_fixtures.environment


def settled_uncertain_send(environment, *, dropped_phase):
    manager, request, _, _ = environment
    bound, context = dashboard_fixtures.join(environment)
    backend = Backend(bound)
    manager.recipients = RecipientService(manager, backend_factory=lambda _: backend)
    manager.recipients.select_recipient(
        request, {**context, "arguments": {"reference": "desktop-a"}},
        operation_id="select-one", bound=bound,
    )
    setattr(backend, "drop_" + dropped_phase, True)
    interaction = dashboard_fixtures.input_event(environment, context)
    interaction_id = interaction["interaction_id"]
    dashboard_fixtures.event(
        environment, context, "response.started", interaction_id=interaction_id,
        response_id="send-response",
    )
    response = manager.tool(request, {
        **context, "interaction_id": interaction_id, "response_id": "send-response",
        "call_id": "send-call", "name": "send_agent_message",
        "arguments": {"message": "Continue the original job."},
    })
    assert response["action"]["recipient_receipt"]["status"] == "unknown"
    dashboard_fixtures.event(
        environment, context, "response.done", interaction_id=interaction_id,
        response_id="send-response", status="completed", tool_call_ids=["send-call"],
    )
    settled = dashboard_fixtures.finish(
        environment, context, interaction, response="final-response",
        previous="send-response", text="Delivery is uncertain.",
    )
    assert settled["state"] == "saved"
    setattr(backend, "drop_" + dropped_phase, False)
    return manager, request, backend, context, response["action"]


@pytest.mark.parametrize("reconnect", [False, True])
def test_settled_unknown_delivery_result_reconciles_original_operation(environment, reconnect):
    manager, request, backend, context, action = settled_uncertain_send(
        environment, dropped_phase="commit",
    )
    if reconnect:
        _, context = dashboard_fixtures.join(environment)
    result = manager.result(request, {**context, "run_id": action["run_id"]})
    assert result["status"] == "posted"
    assert result["receipt"]["operation_id"] == action["action_id"]
    assert result["receipt"]["recipient"]["recipient_id"] == "desktop-a"
    assert [row[0] for row in backend.calls].count("prepare") == 1
    assert [row[0] for row in backend.calls].count("commit") == 1
    assert any(row[:2] == ("reconcile", action["action_id"]) for row in backend.calls)
    again = manager.result(request, {**context, "run_id": action["run_id"]})
    assert again["status"] == "posted"
    assert [row[0] for row in backend.calls].count("commit") == 1


def test_result_reconciliation_never_commits_a_queued_prepare(environment):
    manager, request, backend, context, action = settled_uncertain_send(
        environment, dropped_phase="prepare",
    )
    for _ in range(2):
        result = manager.result(request, {**context, "run_id": action["run_id"]})
        assert result["status"] == "queued"
        assert result["receipt"]["operation_id"] == action["action_id"]
    assert [row[0] for row in backend.calls].count("prepare") == 1
    assert not any(row[0] == "commit" for row in backend.calls)


def test_reconciliation_rechecks_authority_before_returning_receipt(environment, monkeypatch):
    manager, request, backend, context, action = settled_uncertain_send(
        environment, dropped_phase="commit",
    )
    reconcile = backend.reconcile

    def revoke_after_reconcile(operation_id, target):
        receipt = reconcile(operation_id, target)
        request.state.principal = "different-actor"
        return receipt

    monkeypatch.setattr(backend, "reconcile", revoke_after_reconcile)
    with pytest.raises(DashboardTaskError, match="authorized"):
        manager.result(request, {**context, "run_id": action["run_id"]})
    assert [row[0] for row in backend.calls].count("commit") == 1


@pytest.mark.parametrize("with_app", [False, True])
def test_unknown_send_stays_fenced_after_same_task_token_refresh(environment, with_app):
    manager, request, backend, context, _ = settled_uncertain_send(
        environment, dropped_phase="commit",
    )
    backend.rows[0]["target_token"] = "refreshed-proof-for-the-same-task"
    manager.recipients.select_recipient(
        request, {**context, "arguments": {"reference": "desktop-a"}},
        operation_id="reselect-same-task",
    )
    arguments = {"message": "Continue the original job."}
    if with_app:
        arguments["app"] = "codex_desktop"
    with pytest.raises(DashboardTaskError) as error:
        manager.recipients.send_agent_message(
            request, {**context, "arguments": arguments}, operation_id="new-send-attempt",
        )
    assert error.value.code == "recipient_reconciliation_required"
    assert [row[0] for row in backend.calls].count("commit") == 1
