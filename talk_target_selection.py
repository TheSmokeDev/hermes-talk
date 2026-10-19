"""Prepare/mint/activate target switching; model tools propose, never activate."""

from __future__ import annotations

import json
from dataclasses import dataclass

try:
    from .talk_dashboard_gateway import DashboardTaskError
    from .talk_passive import HistoryError, identifier
    from .talk_target_catalog import TargetCatalog
except ImportError:  # pragma: no cover - flat plugin load
    from talk_dashboard_gateway import DashboardTaskError
    from talk_passive import HistoryError, identifier
    from talk_target_catalog import TargetCatalog

SELECTION_TOOLS = frozenset({"list_targets", "switch_target", "return_to_previous"})


def selection_tools():
    scope = {"peer_id": {"type": "string"}, "profile": {"type": "string"}}
    return [
        {
            "type": "function",
            "name": "list_targets",
            "description": "List authorized tasks and Bots. Remote profiles must be explicit.",
            "parameters": {"type": "object", "properties": scope, "additionalProperties": False},
        },
        {
            "type": "function",
            "name": "switch_target",
            "description": (
                "Request a task or Bot switch by exact name or target ID. "
                "Ambiguity needs a choice. "
                "Activation happens after this tool batch; never claim it already happened."
            ),
            "parameters": {
                "type": "object",
                "properties": {"reference": {"type": "string"}, **scope},
                "required": ["reference"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "return_to_previous",
            "description": (
                "Request return to the previous task after this tool batch; "
                "old jobs keep their owners."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


@dataclass(slots=True)
class PreparedSelection:
    state: object
    reservation: dict
    bound: object


class TargetSelection:
    def __init__(self, manager, *, catalog=None):
        self.manager = manager
        self.catalog = catalog or TargetCatalog(manager)
        manager.target_resolver = self.catalog.materialize
        manager.selection_guard = self.guard

    def guard(self, request, bound):
        tab = self.catalog.state(request).locate(bound.connection_id, bound.generation)
        if tab != bound.browser_tab:
            raise DashboardTaskError("connection_stale", 409)

    def prepare(self, request, body, *, initial=False, reconnect=False):
        if not isinstance(body, dict):
            raise DashboardTaskError("invalid_event", 400)
        allowed = (
            {"target_id", "tab_id", "page_reference"}
            if initial
            else {
                "target_id",
                "reference",
                "back",
                "peer_id",
                "profile",
                "page_reference",
                "connection_id",
                "generation",
            }
        )
        if set(body) - allowed:
            raise DashboardTaskError("invalid_event", 400)
        state = self.catalog.state(request)
        expected = None
        if initial:
            tab = identifier(body.get("tab_id"))
        else:
            if type(body.get("generation")) is not int:
                raise DashboardTaskError("invalid_event", 400)
            expected = (identifier(body.get("connection_id")), body["generation"])
            tab = state.locate(*expected)
        back = body.get("back") is True
        if sum([bool(body.get("target_id")), bool(body.get("reference")), back]) != 1:
            raise DashboardTaskError("invalid_event", 400)
        target = None
        if body.get("target_id"):
            if not isinstance(body["target_id"], str):
                raise DashboardTaskError("invalid_event", 400)
            target = state.target(body["target_id"])
        elif not back:
            match = self.catalog.resolve(
                request,
                body.get("reference"),
                peer_id=body.get("peer_id"),
                profile=body.get("profile"),
            )
            if match["state"] != "resolved":
                return {"ok": False, **match}
            target = match["target"]
        if not initial and not back and not reconnect:
            current = state.snapshot(tab)["current"]
            if current and current["target"]["target_id"] == target["target_id"]:
                raise DashboardTaskError("target_already_selected", 409)
        prepared = state.reserve(tab, target, back=back, expected=expected)
        try:
            resolved = self.catalog.materialize(request, prepared["target"])
            bound = self.manager.join(
                request,
                {
                    "session_id": resolved.record["session_id"],
                    "profile": resolved.context.profile_name,
                    "tab_id": tab,
                    "page_reference": body.get("page_reference"),
                },
                resolved=resolved,
                activate=False,
            )
            return PreparedSelection(state, prepared, bound)
        except BaseException:
            state.cancel(prepared)
            raise

    def activate(self, request, prepared):
        # Revalidate actor, credentials, profile and physical store after the external mint.
        resolved = self.catalog.materialize(request, prepared.reservation["target"])
        if (
            resolved.context != prepared.bound.context
            or resolved.transport.owner(resolved.record["session_id"])
            != prepared.bound.attachment.owner
        ):
            raise DashboardTaskError("context_denied", 403)
        self.manager.activate(
            prepared.bound,
            commit=lambda: prepared.state.activate(
                prepared.reservation, prepared.bound.connection_id, prepared.bound.generation
            ),
        )
        return {
            "state": "activated",
            "return_depth": prepared.bound.return_depth,
            **self.catalog.public(prepared.reservation["target"]),
        }

    def cancel(self, prepared):
        self.manager.discard(prepared.bound)
        prepared.state.cancel(prepared.reservation)

    def tool(self, request, body):
        bound = self.manager.binding(request, body, write=True)
        name, arguments = body.get("name"), body.get("arguments")
        if (
            bound.target_record is None
            or name not in SELECTION_TOOLS
            or not isinstance(arguments, dict)
        ):
            raise DashboardTaskError("unsupported_tool", 400)
        allowed = set() if name == "return_to_previous" else {"peer_id", "profile"}
        if name == "switch_target":
            allowed.add("reference")
        if set(arguments) - allowed:
            raise DashboardTaskError("invalid_event", 400)

        def build(record, action):
            if name != "list_targets" and record["mode"] != "execution":
                record["mode"] = "control"

        action = bound.stages.prepare_action(
            bound.token,
            body.get("interaction_id"),
            body.get("response_id"),
            body.get("call_id"),
            name,
            arguments,
            build,
        )
        result = action.get("selection_result")
        if result is None:
            try:
                if name == "list_targets":
                    result = self.catalog.catalog(request, arguments)
                elif name == "return_to_previous":
                    if not self.catalog.state(request).snapshot(bound.browser_tab)["stack"]:
                        raise DashboardTaskError("return_empty", 409)
                    result = {"ok": True, "selection": {"back": True}}
                else:
                    match = self.catalog.resolve(
                        request,
                        arguments.get("reference"),
                        peer_id=arguments.get("peer_id"),
                        profile=arguments.get("profile"),
                    )
                    result = (
                        {"ok": True, "selection": self.catalog.public(match["target"])}
                        if match["state"] == "resolved"
                        else {"ok": False, **match}
                    )
            except (DashboardTaskError, HistoryError) as exc:
                result = {"ok": False, "state": "refused", "error": exc.code}
            action = bound.stages.update_action(
                bound.token, action["run_id"], state="returned", selection_result=result
            )
        self.manager.binding(request, body)
        return {
            **result,
            "output": json.dumps(result, ensure_ascii=False)[:12000],
            "action": self.manager._action_view(action),
        }
