"""Explicit text input over the bound canonical task: one route, five operations.

``POST /text/input`` carries the operator's exact words plus one named operation.
Every operation binds the current connection generation, pre-authorizes against
physical state (the selection row, the owner-scoped action row, the live gateway
run), persists the original words once, records an operator-explicit decision
row, and only then dispatches through the existing ``DashboardTasks.tool`` path.
Nothing here creates a coordinator, a store, or a routing decision. A host state
of unknown/failed/rejected/unsupported reaches the reply verbatim and nothing
retries on its own.
"""

from __future__ import annotations

import asyncio

try:
    from starlette.requests import Request
    from starlette.responses import JSONResponse
except ImportError:  # pragma: no cover - plugin import without the dashboard extra
    Request = object
    JSONResponse = None

try:
    from . import talk_input_attachments
    from .talk_dashboard_gateway import DashboardTaskError
    from .talk_dashboard_store import bounded_text
    from .talk_dashboard_tasks import CHILD_TOOLS, codex_worker_available
    from .talk_passive import HistoryError, identifier
    from .talk_recipients import DELIVERY_STATUSES, IDENTITY_FIELDS, RecipientError
    from .talk_run_control import control_output
    from .talk_task_sources import TaskEventError
except ImportError:  # pragma: no cover - flat plugin load
    import talk_input_attachments
    from talk_dashboard_gateway import DashboardTaskError
    from talk_dashboard_store import bounded_text
    from talk_dashboard_tasks import CHILD_TOOLS, codex_worker_available
    from talk_passive import HistoryError, identifier
    from talk_recipients import DELIVERY_STATUSES, IDENTITY_FIELDS, RecipientError
    from talk_run_control import control_output
    from talk_task_sources import TaskEventError

OPERATIONS = ("message", "start_worker", "steer", "cancel", "approval")
COMMON_FIELDS = frozenset(
    {"connection_id", "generation", "input_id", "text", "operation", "attachments"}
)
REQUIRED_FIELDS = {
    "message": (),
    "start_worker": (),
    "steer": ("run_id",),
    "cancel": ("run_id", "action_id"),
    "approval": ("run_id", "action_id", "request_id", "choice"),
}
OPTIONAL_FIELDS = {
    "message": ("recipient",),
    "start_worker": ("worker",),
    "steer": (),
    "cancel": (),
    "approval": (),
}
CHOICES = frozenset({"once", "session", "deny"})
WORKERS = frozenset({"hermes", "codex"})
TERMINAL_RUNS = frozenset({"completed", "failed", "cancelled", "lost"})
SETTLED_ACTIONS = frozenset({"returned", "accepted", "failed"})
OPEN_DELIVERIES = frozenset({"queued", "unknown"})
COORDINATOR_STATES = {
    "admitted": "queued",
    "deciding": "queued",
    "dispatching": "queued",
    "completed": "completed",
    "uncertain": "unknown",
    "failed": "failed",
}


#: The only operation whose dispatch reaches a Hermes child, which is the only delivery
#: the host advertises. Everything else refuses attachments rather than dropping them.
ATTACHMENT_OPERATIONS = frozenset({"start_worker"})


def descriptor(capabilities=None):
    """The published input descriptor. ``attachments`` is live, never a constant.

    The caller supplies the host capability document it just read; ``None`` (no host
    read was possible) advertises false, which is what the panel's disabled-with-reason
    path already handles.
    """
    return {
        "version": 1,
        "operations": list(OPERATIONS),
        "attachments": talk_input_attachments.supported(capabilities),
    }


def _identifier(value):
    try:
        return identifier(value)
    except HistoryError:
        raise DashboardTaskError("invalid_event", 400) from None


def validate(body):
    """Pure shape validation: no binding, no store read, no host I/O."""
    if not isinstance(body, dict):
        raise DashboardTaskError("invalid_event", 400)
    operation = body.get("operation")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise DashboardTaskError("invalid_event", 400)
    required, optional = REQUIRED_FIELDS[operation], OPTIONAL_FIELDS[operation]
    if set(body) - COMMON_FIELDS - set(required) - set(optional) or any(
        key not in body for key in required
    ):
        raise DashboardTaskError("invalid_event", 400)
    fields = {
        "operation": operation,
        "input_id": _identifier(body.get("input_id")),
        "text": bounded_text(body.get("text"), maximum=16000),
    }
    attachments = body.get("attachments", [])
    if attachments:
        # Shape first, then reachability: a malformed list is malformed on every
        # operation, and only a well-formed reference earns the operation refusal.
        references = talk_input_attachments.normalize_references(attachments)
        if operation not in ATTACHMENT_OPERATIONS:
            raise DashboardTaskError("attachment_operation_unsupported", 400)
        fields["attachments"] = references
    elif attachments != []:
        raise DashboardTaskError("invalid_event", 400)
    if "recipient" in body:
        recipient = body["recipient"]
        if not isinstance(recipient, dict) or set(recipient) != set(IDENTITY_FIELDS):
            raise DashboardTaskError("invalid_event", 400)
        fields["recipient"] = {
            key: bounded_text(recipient[key], maximum=512) for key in IDENTITY_FIELDS
        }
    if "worker" in body:
        worker = body["worker"]
        if not isinstance(worker, str) or worker not in WORKERS:
            raise DashboardTaskError("invalid_event", 400)
        fields["worker"] = worker
    if "run_id" in body:
        run_id = body["run_id"]
        if type(run_id) is not int or run_id < 1:
            raise DashboardTaskError("invalid_event", 400)
        fields["run_id"] = run_id
    for key in ("action_id", "request_id"):
        if key in body:
            fields[key] = _identifier(body[key])
    if "choice" in body:
        choice = body["choice"]
        if not isinstance(choice, str) or choice not in CHOICES:
            raise DashboardTaskError("invalid_event", 400)
        fields["choice"] = choice
    return fields


def _selected_recipient(tasks, bound, wanted):
    """The store's CURRENT selection is the target; the body only names what it expects."""
    selected = tasks.recipients.snapshot(bound)["selected"]
    if selected is None or any(selected.get(key) != wanted[key] for key in IDENTITY_FIELDS):
        raise RecipientError("recipient_selection_mismatch", 409)
    if selected.get("read_only") is True or selected.get("send_agent_message") != "direct":
        raise RecipientError("recipient_control_unavailable", 409)
    return selected


def _authorize_run(tasks, bound, fields):
    """Owner-scoped action row, exact card identity, then the live run's owning session."""
    operation, run_id = fields["operation"], fields["run_id"]
    action = bound.stages.action(bound.token, run_id)
    if action["name"] not in CHILD_TOOLS or not action.get("api_run_id"):
        raise DashboardTaskError("steering_target_denied", 409)
    if operation != "steer" and action["action_id"] != fields["action_id"]:
        raise DashboardTaskError("event_conflict", 409)
    run = bound.gateway.run(action["api_run_id"])
    tasks._result_owner(bound, action, run)
    if operation == "steer":
        return "steer_work", {"run_id": run_id}, run
    if operation == "cancel":
        return "stop_work", {"run_id": run_id}, run
    return "resolve_approval", {
        "run_id": run_id, "choice": fields["choice"], "approval_id": fields["request_id"],
    }, run


def _settled(operation, prior, name, arguments, selected):
    """A repeated input_id returns its stored action instead of dispatching again.

    The stored action was looked up by this request's own identity (interaction
    and call id). It is reused when it is settled and is the same request: the
    same name and arguments, or the ``steer_work`` that ``tool()`` rewrote a
    Codex-worker message into, provided its control target is still the selected
    worker. Everything else re-enters ``tool()``, whose own fences hold: a
    different request conflicts in ``prepare_action``, an open bridge delivery
    (queued/unknown) is reconciled and never resent, a settled stop is replayed
    from its stored reply and a torn one is only observed, never posted again.
    """
    if prior["state"] not in SETTLED_ACTIONS:
        return False
    if prior["name"] == name:
        if prior["arguments"] != arguments:
            return False
    elif operation == "message" and prior["name"] == "steer_work":
        if selected is None or prior.get("control_api_run_id") != selected.get("task_id"):
            return False
    else:
        return False
    if operation == "message" and prior["name"] == "send_agent_message":
        return (prior.get("recipient_receipt") or {}).get("status") not in OPEN_DELIVERIES
    return True


def _control_status(action):
    return (action.get("control_receipt") or {}).get("status", "unknown")


def _reply(operation, fields, action, selected):
    """Honest per-operation state from the stored action; never upgraded."""
    reply = {"ok": True, "input_id": fields["input_id"], "operation": operation}
    if operation == "message":
        if action["name"] == "steer_work":
            # A selected Codex worker is steered with the original words (tool() rewrite).
            reply.update(
                state=_control_status(action), recipient=selected, output=control_output(action),
            )
            return reply
        receipt = action.get("recipient_receipt") or {}
        recipient = receipt.get("recipient")
        if not isinstance(recipient, dict) or any(
            recipient.get(key) != selected[key] for key in IDENTITY_FIELDS
        ):
            raise RecipientError("recipient_selection_mismatch", 409)
        status = receipt.get("status")
        reply.update(
            state=status if status in DELIVERY_STATUSES else "unknown", recipient=recipient,
        )
        if receipt.get("output"):
            reply["output"] = receipt["output"]
        return reply
    reply["run_id"] = action["run_id"] if operation == "start_worker" else fields["run_id"]
    state = action["state"]
    if operation == "steer":
        reply.update(state=_control_status(action), output=control_output(action))
    elif operation == "cancel":
        reply["state"] = {"returned": "posted", "failed": "failed"}.get(state, "unknown")
    elif operation == "approval":
        if state == "returned":
            reply["state"] = "unknown" if action.get("error") else "accepted"
        else:
            reply["state"] = "failed" if state == "failed" else "unknown"
        if action.get("output"):
            reply["output"] = action["output"]
    else:
        reply["state"] = {"accepted": "accepted", "failed": "failed"}.get(state, "unknown")
    return reply


def dispatch(tasks, request, body, fields):
    """Steps 2-7 of the seam, on a worker thread: bind, pre-authorize, persist, dispatch."""
    operation = fields["operation"]
    bound = tasks.binding(request, body, write=True)
    context = {"connection_id": bound.connection_id, "generation": bound.generation}
    selected = run = None
    if operation == "message":
        selected = _selected_recipient(tasks, bound, fields["recipient"])
        # The verified identity travels with the send: the store binds to it or refuses.
        name = "send_agent_message"
        arguments = {"message": fields["text"], **fields["recipient"]}
    elif operation == "start_worker":
        references = fields.get("attachments")
        worker = fields.get("worker", "hermes" if references else "codex")
        bound.capabilities = tasks.capabilities(bound, refresh=True)
        if worker == "codex" and not codex_worker_available(bound.capabilities):
            raise DashboardTaskError("child_dispatch_unsupported", 409)
        name, arguments = "delegate_task", {"task": fields["text"], "worker": worker}
        if references:
            # Refuse before anything is posted: the host must still advertise the
            # feature, and every reference must be pinned to THIS input under THIS
            # binding. ``tool()`` re-checks the pins at the dispatch chokepoint.
            talk_input_attachments.require_feature(bound.capabilities)
            if worker != "hermes":
                raise DashboardTaskError("attachment_operation_unsupported", 400)
            arguments["attachments"] = tasks.verified_attachments(
                bound, fields["input_id"], references
            )
    else:
        name, arguments, run = _authorize_run(tasks, bound, fields)
    record = bound.stages.stage(bound.token, fields["input_id"], "typed", fields["text"])
    record = tasks._persist_original(bound, record)
    response_id, call_id = "text-input-" + record["id"], "text-action-" + record["id"]

    def decide(row):
        row["responses"].setdefault(response_id, {
            "response_id": response_id,
            "source": "operator_explicit",
            "previous_response_id": None,
            "status": "completed",
            "tool_call_ids": [call_id],
            "finals": {},
        })

    bound.stages.update(bound.token, record["id"], decide)
    prior = bound.stages.live_action(bound.token, record["id"], call_id=call_id)
    if prior is not None and _settled(operation, prior, name, arguments, selected):
        action = prior
    else:
        unposted = prior is None or prior["state"] == "prepared"
        if operation == "cancel" and unposted and run.get("status") in TERMINAL_RUNS:
            # No stop has been posted for this input: a finished run is refused without one.
            # A stop already in flight (submitting) is observed by tool() instead.
            raise DashboardTaskError("gateway_refused", 409)
        result = tasks.tool(request, {
            **context, "interaction_id": record["id"], "response_id": response_id,
            "call_id": call_id, "name": name, "arguments": arguments,
        })
        action = bound.stages.action(bound.token, result["action"]["run_id"])
    tasks.binding(request, body)
    return _reply(operation, fields, action, selected)


def mount_text_input_routes(
    router, *, require_auth, read_body, task_call, tasks, coordinator, http_exception,
):
    async def invoke(operation, request, body):
        try:
            return await operation
        except (DashboardTaskError, HistoryError, TaskEventError) as error:

            def raise_domain_error(_request, _body, failure=error):
                raise failure

            return await task_call(raise_domain_error, request, body)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - upstream auth/session details are server-only
            raise http_exception(
                status_code=502,
                detail={
                    "code": "live_unavailable",
                    "message": (
                        "GPT-Live could not complete this operation. "
                        "Inspect the task before retrying."
                    ),
                },
            ) from None

    async def owner_message(request, body, fields):
        """The pinned task's own typed path: the coordinator stages, decides and executes."""
        view = await coordinator.typed(request, {
            "connection_id": body.get("connection_id"),
            "generation": body.get("generation"),
            "provider_session_id": "text-input:" + str(body.get("connection_id")),
            "input_id": fields["input_id"],
            "text": fields["text"],
            "admission": "async",
        })
        reply = {
            "ok": True, "input_id": fields["input_id"], "operation": "message",
            "state": COORDINATOR_STATES.get(view.get("state"), "unknown"),
        }
        if view.get("operation_id"):
            reply["operation_id"] = view["operation_id"]
        if view.get("output"):
            reply["output"] = view["output"]
        return reply

    @router.post("/text/input")
    async def text_input(request: Request):
        require_auth(request)
        body = await read_body(request)
        fields = await task_call(lambda _request, value: validate(value), request, body)
        if fields["operation"] == "message" and "recipient" not in fields:
            result = await invoke(owner_message(request, body, fields), request, body)
        else:
            result = await task_call(
                lambda current, value: dispatch(tasks, current, value, fields), request, body,
            )
        if JSONResponse is None:  # pragma: no cover - no dashboard response implementation
            raise http_exception(
                status_code=503, detail=DashboardTaskError("context_unavailable", 503).detail(),
            )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return (text_input,)
