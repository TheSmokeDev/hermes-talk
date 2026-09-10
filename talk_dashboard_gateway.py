"""Configured, fixed-route gateway transport for bound dashboard task operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import quote, urlsplit

import httpx

try:
    from .talk_passive import HistoryError, HistoryTransport, identifier, session_id
except ImportError:  # pragma: no cover - flat plugin load
    from talk_passive import HistoryError, HistoryTransport, identifier, session_id


class DashboardTaskError(Exception):
    MESSAGES: ClassVar[dict[str, str]] = {
        "context_unavailable": "This host lacks verified dashboard task context support.",
        "context_denied": "This request has no authorized dashboard task identity.",
        "catalog_host_unverified": "The task catalog and gateway store could not be matched.",
        "unverified_remote_host": "Remote task continuity needs an explicit verified host binding.",
        "child_dispatch_unsupported": "This gateway lacks durable linked-child dispatch support.",
        "approval_reader_unsupported": "This gateway lacks a verified current-approval reader.",
        "gateway_unavailable": "The gateway is unavailable; the original intent remains pending.",
        "gateway_response_invalid": "The gateway returned an unsupported response.",
        "gateway_refused": "The gateway refused this operation.",
        "invalid_event": "This task event is incomplete or invalid.",
        "event_conflict": "An existing event identity has different content.",
        "connection_stale": "This connection is no longer current; reconnect to the original task.",
        "interaction_unlinked": "The response is not linked to a durably staged original input.",
        "interaction_incomplete": "This interaction has unfinished response or tool events.",
        "capacity": "The bounded pending task store is full.",
        "result_unavailable": "That job's result is not currently available.",
        "unsupported_tool": "This tool is unavailable in canonical task mode.",
        "busy": "The original task is busy; persistence remains pending.",
        "target_missing": "That authorized target is missing or its catalog entry expired.",
        "target_already_selected": "That target is already selected; use Rejoin to reconnect.",
        "target_ambiguous": "More than one authorized target matches; choose an exact target.",
        "target_route_changed": "That target's configured route or canonical store changed.",
        "target_auth_unavailable": "The configured target has no usable scoped credential.",
        "target_auth_denied": "The configured target refused authentication.",
        "target_offline": "The configured target is offline; no local substitute was selected.",
        "target_unsupported": "The configured target lacks the required continuity capability.",
        "selection_store_unavailable": "The local selection state could not be read safely.",
        "selection_busy": "A target change is already being prepared for this tab.",
        "return_empty": "There is no previous authorized target to return to.",
    }

    def __init__(self, code: str, status: int = 409, *, retryable=False):
        self.code = code if code in self.MESSAGES else "gateway_refused"
        self.status = status
        self.retryable = retryable
        super().__init__(self.MESSAGES[self.code])

    def detail(self):
        return {"code": self.code, "message": str(self)}


@dataclass(frozen=True, slots=True)
class TaskGateway:
    transport: HistoryTransport

    def _request(self, method, suffix, *, body=None, key=None, max_bytes=2 * 1024 * 1024):
        """Suffix is chosen only by the fixed methods below, never by a browser/model."""
        prefix = f"/p/{self.transport.profile}" if self.transport.named_profile else ""
        headers = {"Authorization": "Bearer " + self.transport.credential}
        if key is not None:
            headers["Idempotency-Key"] = identifier(key)
        try:
            with (
                httpx.Client(
                    timeout=3,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport._http_transport,
                ) as client,
                client.stream(
                    method,
                    self.transport.base_url.rstrip("/") + prefix + suffix,
                    json=body,
                    headers=headers,
                ) as response,
            ):
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    if len(raw) + len(chunk) > max_bytes:
                        raise DashboardTaskError("gateway_response_invalid", 502, retryable=True)
                    raw.extend(chunk)
                status = response.status_code
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            if status not in {200, 202}:
                code = data.get("error")
                if isinstance(code, dict):
                    code = code.get("code")
                if status in {401, 403}:
                    raise DashboardTaskError("context_denied", 403)
                raise DashboardTaskError(
                    "busy" if code == "busy" else "gateway_refused",
                    status,
                    retryable=code in {"busy", "store_unavailable"},
                )
            return data
        except (httpx.HTTPError, OSError):
            raise DashboardTaskError("gateway_unavailable", 503, retryable=True) from None
        except (ValueError, TypeError, UnicodeError):
            raise DashboardTaskError("gateway_response_invalid", 502, retryable=True) from None

    def capabilities(self):
        return self._request("GET", "/v1/capabilities", max_bytes=256 * 1024)

    def require_local(self):
        if urlsplit(self.transport.base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise DashboardTaskError("unverified_remote_host", 503)

    def require_child(self, capabilities):
        features = capabilities.get("features", {})
        if not isinstance(features, dict):
            raise DashboardTaskError("child_dispatch_unsupported", 503)
        child = features.get("linked_child_dispatch", {})
        durable = features.get("runs_idempotency", {})
        origins = child.get("origin_sources", []) if isinstance(child, dict) else []
        if (
            not isinstance(child, dict)
            or type(child.get("version")) is not int
            or child["version"] != 1
            or child.get("supported") is not True
            or child.get("separate_child_goal") is not True
            or not isinstance(origins, list)
            or not all(isinstance(item, str) for item in origins)
            or not {"fresh", "passive_receipt"}.issubset(origins)
            or not isinstance(durable, dict)
            or durable.get("durable") is not True
        ):
            raise DashboardTaskError("child_dispatch_unsupported", 503)

    def session(self, selected_session):
        try:
            value = self._request(
                "GET", "/api/sessions/" + quote(session_id(selected_session), safe="")
            )
        except DashboardTaskError as exc:
            if exc.status == 404:
                raise DashboardTaskError("target_missing", 404) from None
            raise
        data = value.get("session")
        if not isinstance(data, dict) or data.get("id") != selected_session:
            raise DashboardTaskError("gateway_response_invalid", 502)
        return data

    def sessions(self, *, bot=False):
        query = "?title=Bot%20Chat&include_hidden=1&limit=2" if bot else "?limit=20"
        response = self._request("GET", "/api/sessions" + query, max_bytes=256 * 1024)
        rows = response.get("data")
        if not isinstance(rows, list) or len(rows) > 200:
            raise DashboardTaskError("gateway_response_invalid", 502)
        if any(
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or self.transport.credential in row["id"]
            for row in rows
        ):
            raise DashboardTaskError("gateway_response_invalid", 502)
        return rows

    def dispatch(self, body, *, idempotency_key):
        if set(body) != {"input", "session_id", "origin", "child"}:
            raise DashboardTaskError("invalid_event", 400)
        response = self._request("POST", "/v1/runs", body=body, key=idempotency_key)
        try:
            identifier(response.get("run_id"))
        except HistoryError:
            raise DashboardTaskError("gateway_response_invalid", 502, retryable=True) from None
        return response

    def run(self, run_id):
        data = self._request("GET", "/v1/runs/" + identifier(run_id))
        if data.get("run_id") != run_id:
            raise DashboardTaskError("gateway_response_invalid", 502)
        return data

    def stop(self, run_id):
        return self._request("POST", "/v1/runs/" + identifier(run_id) + "/stop", body={})

    def approve(self, run_id, request_id, choice):
        if choice not in {"once", "session", "deny"}:
            raise DashboardTaskError("invalid_event", 400)
        return self._request(
            "POST",
            "/v1/runs/" + identifier(run_id) + "/approval",
            body={"request_id": identifier(request_id), "choice": choice},
        )

    def approvals(self, run_id):
        try:
            data = self._request("GET", "/v1/runs/" + identifier(run_id) + "/approval")
        except DashboardTaskError as exc:
            if exc.status in {404, 405}:
                raise DashboardTaskError("approval_reader_unsupported", 503) from None
            raise
        if (
            data.get("object") != "hermes.run.approvals"
            or data.get("run_id") != run_id
            or not isinstance(data.get("approvals"), list)
            or len(data["approvals"]) > 64
        ):
            raise DashboardTaskError("gateway_response_invalid", 502)
        projected = []
        for pending in data["approvals"]:
            if not isinstance(pending, dict):
                raise DashboardTaskError("gateway_response_invalid", 502)
            try:
                request_id = identifier(pending.get("request_id"))
            except HistoryError:
                raise DashboardTaskError("gateway_response_invalid", 502) from None
            offered = pending.get("choices")
            if not isinstance(offered, list):
                offered = ["once", "deny"]
                if pending.get("allow_session") is True and not pending.get("smart_denied", False):
                    offered.insert(1, "session")
            projected.append(
                {
                    "request_id": request_id,
                    "description": str(pending.get("description") or "Pending host approval")[:500],
                    "choices": [
                        choice for choice in ("once", "session", "deny") if choice in offered
                    ],
                }
            )
        return {**data, "approvals": projected}
