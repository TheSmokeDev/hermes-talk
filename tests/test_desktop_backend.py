"""Owned Desktop authentication and exact stored-conversation discovery."""

from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from test_dashboard_api import FakeRequest, api
from test_target_switching import fleet as fleet

from talk_dashboard_gateway import DashboardTaskError


def test_owned_desktop_bridge_requires_constant_time_token_and_host_identity(monkeypatch):
    monkeypatch.setenv(api.DASHBOARD_TOKEN_ENV, "external-dashboard-fixture")
    monkeypatch.setenv(api.DESKTOP_TOKEN_ENV, "owned-child-fixture")
    request = FakeRequest(headers={api.DESKTOP_TOKEN_HEADER: "owned-child-fixture"})
    verified, comparisons = [], []
    compare = api.hmac.compare_digest

    def verify(candidate):
        verified.append(candidate)
        return SimpleNamespace(principal_id="verified-host-operator")

    def checked(left, right):
        comparisons.append((left, right))
        return compare(left, right)

    monkeypatch.setattr(api.talk_dashboard_tasks, "resolve_context", verify)
    monkeypatch.setattr(api.hmac, "compare_digest", checked)
    api.require_dashboard_auth(request)
    assert verified == [request]
    assert (b"owned-child-fixture", b"owned-child-fixture") in comparisons


@pytest.mark.parametrize("failure", [
    "remote", "missing_peer", "wrong", "unset", "host_denied", "host_denied_unconfigured",
])
def test_desktop_bridge_never_replaces_external_or_host_auth(monkeypatch, failure):
    monkeypatch.setenv(api.DASHBOARD_TOKEN_ENV, "external-dashboard-fixture")
    monkeypatch.setenv(api.DESKTOP_TOKEN_ENV, "owned-child-fixture")
    if failure == "unset":
        monkeypatch.delenv(api.DESKTOP_TOKEN_ENV)
    elif failure == "host_denied_unconfigured":
        monkeypatch.delenv(api.DASHBOARD_TOKEN_ENV)
    host = {"remote": "203.0.113.2", "missing_peer": None}.get(failure, "127.0.0.1")
    token = "wrong-child-fixture" if failure == "wrong" else "owned-child-fixture"
    verified = []

    def deny(request):
        verified.append(request)
        raise DashboardTaskError("context_denied", 403)

    monkeypatch.setattr(api.talk_dashboard_tasks, "resolve_context", deny)
    with pytest.raises(api.HTTPException) as error:
        api.require_dashboard_auth(FakeRequest(
            host=host, headers={api.DESKTOP_TOKEN_HEADER: token},
        ))
    assert error.value.status_code == 401
    assert len(verified) == (1 if failure.startswith("host_denied") else 0)
    assert token not in str(error.value.detail)
    monkeypatch.setenv(api.DASHBOARD_TOKEN_ENV, "external-dashboard-fixture")
    api.require_dashboard_auth(FakeRequest(
        host="203.0.113.2", headers={api.DASHBOARD_TOKEN_HEADER: "external-dashboard-fixture"},
    ))


def test_exact_desktop_session_outside_recent_page_is_authorized_cached_and_revalidated(fleet):
    host = fleet.hosts["local"]
    exact_id = "stored-desktop-session"
    host.titles[("alpha", exact_id)] = "fixture-gateway-key older conversation"
    host.rows[("alpha", exact_id)] = []
    factory = fleet.catalog.local_factory

    def paginated(request):
        response = host(request)
        if request.url.path.endswith("/api/sessions"):
            data = response.json()
            data["data"] = [row for row in data["data"] if row["id"] != exact_id]
            return httpx.Response(200, json=data)
        return response

    fleet.catalog.local_factory = lambda context: replace(
        factory(context), _http_transport=httpx.MockTransport(paginated),
    )
    body = {"peer_id": "local", "profile": "alpha", "session_id": exact_id, "tab_id": "desk"}
    response = fleet.catalog.catalog(fleet.request, body)
    matches = [row for row in response["targets"] if row["session_id"] == exact_id]
    assert len(matches) == 1
    selected = matches[0]
    assert selected["profile"] == "alpha" and selected["peer_id"] == "local"
    assert selected["label"] == "[redacted] older conversation"
    cached = fleet.catalog.state(fleet.request).target(selected["target_id"])
    assert cached["session_id"] == exact_id
    assert any(r.url.path == f"/p/alpha/api/sessions/{exact_id}" for r in host.requests_all)
    assert all(r.method == "GET" for r in host.requests_all)
    foreign = SimpleNamespace(state=SimpleNamespace(principal="another-operator"))
    with pytest.raises(DashboardTaskError, match="missing"):
        fleet.catalog.materialize(foreign, selected["target_id"])
    host.store_id = "replacement-store"
    with pytest.raises(DashboardTaskError, match="matched"):
        fleet.catalog.materialize(fleet.request, selected["target_id"])


@pytest.mark.parametrize("failure", [
    "missing", "profile", "response_id", "denied", "profile_fallback",
])
def test_exact_desktop_target_failure_never_returns_an_alternative(fleet, failure):
    body = {"peer_id": "local", "profile": "default", "session_id": "task-a"}
    if failure == "missing":
        body["session_id"] = "not-stored"
    elif failure == "profile":
        body["profile"] = "unregistered"
    elif failure == "denied":
        body["profile"] = "alpha"
        fleet.denied.add("alpha")
    elif failure == "profile_fallback":
        body["profile"] = "alpha"
        resolver = fleet.manager.resolve_context
        fleet.manager.resolve_context = lambda request, profile=None: resolver(request, "default")
    else:
        factory = fleet.catalog.local_factory
        host = fleet.hosts["local"]

        def wrong_id(request):
            response = host(request)
            if request.url.path == "/api/sessions/task-a":
                return httpx.Response(200, json={"session": {"id": "task-b"}})
            return response

        fleet.catalog.local_factory = lambda context: replace(
            factory(context), _http_transport=httpx.MockTransport(wrong_id),
        )
    with pytest.raises(DashboardTaskError) as error:
        fleet.catalog.catalog(fleet.request, body)
    assert error.value.code == {
        "missing": "target_missing", "profile": "target_missing",
        "response_id": "gateway_response_invalid", "denied": "context_denied",
        "profile_fallback": "context_denied",
    }[failure]


@pytest.mark.parametrize("change", [
    {"session_id": "../task-a"}, {"session_id": "task-a\n"}, {"session_id": None},
    {"session_id": "a" * 257}, {"session_id": "task-a/other"}, {"profile": None},
    {"peer_id": None}, {"peer_id": "east"},
])
def test_exact_desktop_target_requires_strict_id_and_explicit_local_profile(fleet, change):
    body = {"peer_id": "local", "profile": "default", "session_id": "task-a", **change}
    with pytest.raises(DashboardTaskError) as error:
        fleet.catalog.catalog(fleet.request, body)
    assert error.value.code == "invalid_event" and error.value.status == 400
    assert not any(host.requests_all for host in fleet.hosts.values())
