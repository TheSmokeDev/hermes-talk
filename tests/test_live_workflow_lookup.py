"""Spoken catalog lookups keep the selected task's receipts and gateway profile."""

from __future__ import annotations

import asyncio
import json

import pytest
from test_live_coordinator import Decision
from test_target_switching import CatalogHost, activate, target
from test_target_switching import fleet as base_fleet

from talk_live_coordinator import LiveCoordinator


@pytest.fixture
def fleet(tmp_path):
    return base_fleet.__wrapped__(tmp_path)


@pytest.mark.parametrize("catalog", ["available", "unadvertised", "partial", "malformed", "secret"])
def test_spoken_lookup_uses_only_the_selected_peers_profile(fleet, monkeypatch, catalog):
    observed = []
    serve = CatalogHost.__call__
    paths = {"skills": "/v1/skills", "toolsets": "/v1/toolsets"}

    def gateway(host, request):
        path = request.url.path
        if path.endswith(tuple(paths.values())):
            observed.append((host.store_id, path))
            assert request.method == "GET"
            assert request.headers["Authorization"] == "Bearer fixture-gateway-key"
            if path.endswith("/skills") and catalog == "partial":
                return host.response(503, {"error": "unavailable"})
            if catalog == "malformed":
                return host.response(200, {"unsupported": []})
            data = ([{"name": "needle-workflow", "description": "Useful skill"}]
                    if path.endswith("/skills") else [{"name": "needle-app", "enabled": False}])
            if catalog == "secret":
                data[0]["description"] = "fixture-gateway-key"
            return host.response(200, {"object": "list", "data": data})
        response = serve(host, request)
        if path.endswith("/v1/capabilities"):
            endpoints = {key: {"method": "GET", "path": route} for key, route in paths.items()}
            if catalog == "unadvertised":
                endpoints = {"skills": {"method": "GET", "path": "https://wrong.invalid/catalog"}}
            return host.response(200, {**json.loads(response.content), "endpoints": endpoints})
        return response

    monkeypatch.setattr(CatalogHost, "__call__", gateway)
    bound, context, _ = activate(fleet, target(fleet, "bot-alpha", profile="alpha", peer="east"))
    decide = Decision()
    coordinator = LiveCoordinator(fleet.manager, None, lambda _: [{"name": "talk_capabilities"}],
                                  decide=decide)
    body = {**context, "provider_session_id": "voice-1", "delegation_id": "lookup-1",
            "offset_ms": 2000, "fragments": [
                {"event_id": "one", "text": "Hey, slash", "start_ms": 100, "end_ms": 500},
                {"event_id": "two", "text": " needle.", "start_ms": 500, "end_ms": 1000},
                {"event_id": "assistant", "role": "assistant", "text": "start a job"},
                {"event_id": "later", "text": " start a job", "start_ms": 3000, "end_ms": 3500},
            ]}
    first = asyncio.run(coordinator.delegation(fleet.request, body))
    replay = asyncio.run(coordinator.delegation(
        fleet.request, {**body, "delegation_id": "lookup-2"}
    ))
    assert replay == first
    assert not decide.calls and not any(host.jobs for host in fleet.hosts.values())
    assert first["action"]["name"] == "talk_capabilities" and first["action"]["state"] == "returned"
    result = json.loads(first["output"])
    if catalog in {"available", "partial"}:
        assert result["tools_resolved"] is False
        assert "profile" in result["source"]
        names = {match["name"] for match in result["matches"]}
        expected = {"needle-workflow", "needle-app"} if catalog == "available" else {"needle-app"}
        assert names == expected
        if catalog == "partial":
            assert "skills unavailable" in result["source"]
    else:
        assert result["available"] is False and "unavailable" in result["detail"]
    assert observed == ([] if catalog == "unadvertised" else [
        ("east-store", "/p/alpha/v1/skills"), ("east-store", "/p/alpha/v1/toolsets"),
    ])
    records, actions = bound.stages.records(bound.token)
    assert len(records) == len(actions) == 1
    assert records[0]["text"] == "Hey, slash needle."
    assert actions[0]["output"] == first["output"]
    assert "fixture-gateway-key" not in first["output"]


@pytest.mark.parametrize("text,expected", [
    ("Hey slash!", "Workflow lookup is unavailable"),
    ("Which Hermes plugins are installed?", "Workflow lookup is unavailable"),
    ("/jira", "Workflow lookup is unavailable"),
    ("Hey Hermes, run slash Jira", "supports discovery only"),
    ("I heard someone say hey slash Jira.", "ordinary conversation"),
])
def test_typed_lookup_respects_task_tool_availability_and_position(fleet, text, expected):
    _, context, _ = activate(fleet, target(fleet))
    calls = []

    async def decide(**kwargs):
        calls.append(kwargs)
        return {"name": "", "arguments": {}, "message": "ordinary conversation"}

    coordinator = LiveCoordinator(fleet.manager, None, lambda _: [], decide=decide)
    reply = asyncio.run(coordinator.typed(fleet.request, {
        **context, "provider_session_id": "voice", "input_id": "typed", "text": text,
    }))
    assert expected in reply["output"]
    assert len(calls) == int(expected == "ordinary conversation")
