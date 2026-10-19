"""Origin-linked control of existing API runs, independent of voice providers."""

from __future__ import annotations

import json

try:
    from .talk_dashboard_gateway import DashboardTaskError
except ImportError:  # pragma: no cover - flat plugin load
    from talk_dashboard_gateway import DashboardTaskError


STATES = frozenset({"queued", "rejected", "unsupported", "unknown"})


def steering_tool():
    """Only the bound dashboard advertises this receipt-aware API control."""
    return {
        "type": "function",
        "name": "steer_work",
        "description": (
            "Send this original user correction to the same existing running job. Supply "
            "exactly one known task run_id or exact API api_run_id. The server uses the "
            "linked user's original words, never generated correction text. Queued means "
            "backend queue admission only, never delivered or applied. Unknown is "
            "unconfirmed: reuse this action, never start replacement work. Playback stop "
            "or mute controls audio; interrupting the voice response is separate; "
            "stop_work cancels the job. Steering does none of those."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer", "description": "Known task job number."},
                "api_run_id": {"type": "string", "description": "Exact existing API run ID."},
            },
            "additionalProperties": False,
        },
    }


def require_steering(capabilities, *, kind=None, text=None):
    features = capabilities.get("features")
    feature = features.get("run_steering") if isinstance(features, dict) else None
    if (
        not isinstance(feature, dict)
        or type(feature.get("version")) is not int
        or feature["version"] != 1
        or feature.get("supported") is not True
        or feature.get("durable_actions") is not True
        or not isinstance(feature.get("receipt_states"), list)
        or not all(isinstance(item, str) for item in feature["receipt_states"])
        or not STATES.issubset(feature["receipt_states"])
        or type(feature.get("max_input_chars")) is not int
        or not 1 <= feature["max_input_chars"] <= 16000
    ):
        raise DashboardTaskError("steering_unsupported", 409)
    if text is not None and len(text) > feature["max_input_chars"]:
        raise DashboardTaskError("invalid_event", 400)
    if kind is not None:
        origins = feature.get("origin_sources")
        sources = origins.get(kind) if isinstance(origins, dict) else None
        if not isinstance(sources, list) or "passive_receipt" not in sources:
            raise DashboardTaskError("steering_unsupported", 409)
    return feature


def _opaque(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 512
        and not any(ord(char) < 32 for char in value)
    )


def read_target(bound, api_run_id):
    """Fresh owning-session and live-target checks; no title/profile identity inference."""
    run = bound.gateway.run(api_run_id)
    if run.get("session_id") != bound.attachment.owner.session_id:
        raise DashboardTaskError("steering_target_denied", 403)
    target = bound.gateway.steering(api_run_id)
    if (
        target.get("object") != "hermes.run.steering"
        or type(target.get("version")) is not int
        or target["version"] != 1
        or target.get("run_id") != api_run_id
        or type(target.get("supported")) is not bool
    ):
        raise DashboardTaskError("gateway_response_invalid", 502)
    if target["supported"] and (
        target.get("kind") not in ("ordinary", "linked_child")
        or not _opaque(target.get("session_id"))
        or not _opaque(target.get("turn_id"))
        or target.get("status") not in ("running", "waiting_for_approval")
        or (target["kind"] == "ordinary" and target["session_id"] != run["session_id"])
        or (
            target["kind"] == "linked_child" and target["session_id"] != run.get("child_session_id")
        )
    ):
        raise DashboardTaskError("gateway_response_invalid", 502)
    return target


def support_view(bound, api_run_id):
    try:
        require_steering(bound.capabilities)
        target = read_target(bound, api_run_id)
        if target["supported"]:
            require_steering(bound.capabilities, kind=target["kind"])
        return {
            key: target.get(key) for key in ("supported", "reason", "kind", "session_id", "turn_id")
        }
    except DashboardTaskError as exc:
        return {"supported": False, "reason": exc.code}


def receipt_view(action, receipt):
    """Project only versioned, identity-matched evidence. A boolean ACK is insufficient."""
    control = action["control_body"]["control"]
    status, evidence = receipt.get("status"), receipt.get("evidence")
    expected_origin = control["origin"]
    origin = receipt.get("origin")
    origin_matches = (
        isinstance(origin, dict)
        and all(origin.get(key) == value for key, value in expected_origin.items())
        and not set(origin) - {"event_id", "origin_turn_id", "receipt_id"}
    )
    if (
        receipt.get("object") != "hermes.run.steer"
        or type(receipt.get("version")) is not int
        or receipt["version"] != 1
        or receipt.get("run_id") != action["control_api_run_id"]
        or receipt.get("action_id") != action["action_id"]
        or receipt.get("session_id") != control["expected_session_id"]
        or receipt.get("turn_id") != control["expected_turn_id"]
        or not origin_matches
        or not isinstance(status, str)
        or status not in STATES
        or not isinstance(evidence, str)
        or not evidence
        or len(evidence) > 128
        or not all(char.isalnum() or char == "_" for char in evidence)
        or (status == "queued" and evidence != "backend_queue_ack")
        or (status == "unknown" and evidence != "reserved_before_queue")
        or (
            status == "queued"
            and (type(origin.get("receipt_id")) is not int or origin["receipt_id"] < 1)
        )
        or (
            status == "queued"
            and (
                type(receipt.get("parent_message_id")) is not int
                or receipt["parent_message_id"] < 1
                or (
                    action["canonical_message_ids"]
                    and receipt["parent_message_id"] not in action["canonical_message_ids"]
                )
            )
        )
    ):
        raise DashboardTaskError("gateway_response_invalid", 502)
    return {
        "status": status,
        "evidence": evidence,
        "source": "host_receipt",
        "target_run_id": action.get("control_target_run_id"),
        "api_run_id": action["control_api_run_id"],
        "origin_turn_id": control["origin"]["origin_turn_id"],
        "session_id": receipt["session_id"],
        "turn_id": receipt["turn_id"],
        "parent_message_id": receipt.get("parent_message_id"),
        "action_id": action["action_id"],
    }


def _save(bound, action, receipt, *, phase):
    fields = {}
    if receipt.get("status") == "queued" and receipt.get("source") == "host_receipt":
        fields["canonical_message_ids"] = [receipt["parent_message_id"]]
    saved = bound.stages.record_original_receipt(
        action,
        state="returned" if phase == "settled" else "uncertain",
        control_receipt=receipt,
        control_phase=phase,
        **fields,
    )
    if saved is None:
        raise DashboardTaskError("connection_stale", 409)
    return saved


def _observation(action, status, evidence):
    return {
        "status": status,
        "evidence": evidence,
        "source": "client_observation",
        "target_run_id": action.get("control_target_run_id"),
        "api_run_id": action["control_api_run_id"],
        "origin_turn_id": action["control_origin"]["origin_turn_id"],
        "action_id": action["action_id"],
    }


def steer_action(manager, bound, action, *, authorize=None):
    """Reconcile first. Only an explicit current authorized tool call may submit."""
    if action.get("control_phase") == "settled":
        receipt = action.get("control_receipt") or {}
        if receipt.get("status") != "unknown" or receipt.get("source") != "host_receipt":
            return action
        authorize = None  # A durable reservation can be read again, never submitted again.
    try:
        if action.get("control_body") is not None:
            # A receipt read is authoritative; only its exact not-found response permits
            # an explicit same-body retry. Reconnect/state never POST any control.
            receipt = bound.gateway.steer_receipt(action["control_api_run_id"], action["action_id"])
            if receipt is not None:
                return _save(bound, action, receipt_view(action, receipt), phase="settled")
            if authorize is None:
                return action
        elif authorize is None:
            return action
        else:
            bound.capabilities = bound.gateway.capabilities()
            require_steering(bound.capabilities, text=action["control_input"])
            record = bound.stages.get(bound.token, action["interaction_id"])
            origin = manager._link_origin(bound, record)
            target = read_target(bound, action["control_api_run_id"])
            if not target["supported"]:
                if target.get("reason") == "backend_unsupported":
                    raise DashboardTaskError("steering_unsupported", 409)
                return _save(
                    bound,
                    action,
                    _observation(action, "rejected", "run_not_accepting_steer"),
                    phase="settled",
                )
            require_steering(bound.capabilities, kind=target["kind"])
            sources = bound.capabilities["features"]["run_steering"]["origin_sources"]
            if origin is None and (
                target["kind"] != "ordinary" or "pending" not in sources["ordinary"]
            ):
                return _save(
                    bound,
                    action,
                    _observation(action, "unknown", "steering_origin_pending"),
                    phase="pending_origin",
                )
            body = {
                "input": action["control_input"],
                "control": {
                    "version": 1,
                    "action_id": action["action_id"],
                    "expected_session_id": target["session_id"],
                    "expected_turn_id": target["turn_id"],
                    "origin": {
                        **action["control_origin"],
                        **({"receipt_id": origin.revision} if origin else {}),
                    },
                },
            }
            action = bound.stages.freeze_control(
                bound.token, action["run_id"], body, list(origin.message_ids) if origin else []
            )
        # Recheck authority after every potentially blocking read before a side effect.
        authorize()
        action = bound.stages.update_action(
            bound.token, action["run_id"], state="submitting", control_phase="submitting"
        )
        receipt = bound.gateway.steer(action["control_api_run_id"], action["control_body"])
        return _save(bound, action, receipt_view(action, receipt), phase="settled")
    except DashboardTaskError as exc:
        if exc.code in {"connection_stale", "context_denied", "catalog_host_unverified"}:
            raise
        # Any error after the full body exists might hide admission. Never label that
        # failed/applied, generate another key, or fall back to stop/create/unbound text.
        uncertain = action.get("control_body") is not None or exc.retryable
        return _save(
            bound,
            action,
            _observation(
                action,
                "unknown"
                if uncertain
                else ("unsupported" if exc.code == "steering_unsupported" else "rejected"),
                exc.code,
            ),
            phase="unconfirmed" if uncertain else "settled",
        )


def control_output(action):
    receipt = action.get("control_receipt") or _observation(action, "unknown", "pending")
    text = {
        "queued": "Queue admission confirmed; delivery and application are unconfirmed.",
        "unknown": (
            "Control outcome unconfirmed. Keep the original action; do not create replacement work."
        ),
        "rejected": "The existing job refused this correction.",
        "unsupported": "This host or runtime does not support this origin-linked correction.",
    }[receipt["status"]]
    return text + " " + json.dumps(receipt)
