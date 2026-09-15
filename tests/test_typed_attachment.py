"""Typed attachment reaches canonical asynchronous input without a voice session."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from test_live_coordinator import Decision
from test_native_surface_binding import Issuer
from test_target_switching import fleet as fleet
from test_target_switching import target

import talk_audio
from talk_dashboard_gateway import DashboardTaskError
from talk_native_surface import prepare_surface


@pytest.fixture
def typed(fleet, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("typed_route_fixture", source)
    api = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = api
    spec.loader.exec_module(api)
    monkeypatch.setattr(api, "TASKS", fleet.manager)
    monkeypatch.setattr(api, "TARGETS", fleet.selection)
    monkeypatch.setattr(api.talk_dashboard_tasks, "resolve_context", fleet.manager.resolve_context)
    monkeypatch.setenv("TALK_DASHBOARD_TOKEN", "typed-dashboard-token")
    monkeypatch.setenv("HERMES_DESKTOP_TALK_TOKEN", "typed-desktop-token")

    def forbidden(*args, **kwargs):
        pytest.fail("Typed attachment must not resolve voice configuration, auth, or transport")

    for module, name in (
        (api, "_resolve_voice_mode"),
        (api, "_mint"),
        (api.talk_auth, "resolve_auth"),
        (api.talk_live_config, "resolve_live_config"),
        (api.talk_live_config, "resolve_live_auth"),
        (api.LIVE_SESSIONS, "create"),
        (talk_audio.DuplexAudio, "start"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    decision = Decision()
    coordinator = api.LIVE_SESSIONS.coordinator
    coordinator.manager, coordinator.targets = fleet.manager, fleet.selection
    coordinator.tools, coordinator.decide = api._session_tools, decision
    paths = {
        "/native/attach": api.native_task_attach,
        "/targets": api.task_targets,
        "/state": api.task_state,
        "/close": api.task_close,
        **{
            "/live/" + handler.__name__.removeprefix("live_"): handler
            for handler in api.LIVE_ROUTE_HANDLERS
            if handler.__name__ in {"live_typed", "live_operation"}
        },
    }

    def endpoint(handler):
        async def call(request):
            request.state.principal = request.headers.get("x-actor", "actor-one")
            try:
                return JSONResponse(await handler(request))
            except api.HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

        return call

    app = Starlette(routes=[
        Route(path, endpoint(handler), methods=["GET" if path.endswith("operation") else "POST"])
        for path, handler in paths.items()
    ])
    return SimpleNamespace(api=api, fleet=fleet, app=app, decision=decision,
                           coordinator=coordinator, forbidden=forbidden)


def client_for(typed, surface="dashboard"):
    header = ({"x-hermes-desktop-talk-token": "typed-desktop-token"} if surface == "desktop"
              else {"x-talk-token": "typed-dashboard-token"})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=typed.app),
                             base_url="http://hermes.test", headers=header)


async def attach(client, *, surface="dashboard", selected=None, **fields):
    if selected is None:
        catalog = await client.post("/targets", json={})
        assert catalog.status_code == 200, catalog.text
        selected = next(row["target_id"] for row in catalog.json()["targets"]
                        if row["session_id"] == "task-a")
    return await client.post("/native/attach", json={
        "input_mode": "typed", "surface": surface, "target_id": selected,
        **({} if "connection_id" in fields else {"tab_id": "typed-tab"}), **fields,
    })


def context(response):
    assert response.status_code == 200, response.text
    return {key: response.json()["task"][key] for key in ("connection_id", "generation")}


@pytest.mark.parametrize("surface", ["desktop", "dashboard"])
def test_authenticated_attach_and_async_input_share_canonical_receipts(typed, surface):
    async def run():
        async with client_for(typed, surface) as client:
            response = await attach(client, surface=surface)
            owner = context(response)
            descriptor = response.json()
            assert descriptor["input_mode"] == "typed"
            assert descriptor["surface_context"] == {"surface": surface}
            assert descriptor["voice_state"] == "not_connected"
            assert not {"clientSecret", "authSource", "model", "instructions", "tools"} & (
                descriptor.keys()
            )
            assert descriptor["selection"]["state"] == "activated"
            assert descriptor["task"]["history"]["messages"][0]["content"] == "Earlier typed task"
            decision = typed.decision
            decision.started, decision.release = asyncio.Event(), asyncio.Event()
            body = {**owner, "provider_session_id": "typed-capture", "input_id": "input-one",
                    "text": "  Inspect this exact original request.\nKeep this spacing.  ",
                    "admission": "async"}
            try:
                accepted = await client.post("/live/typed", json=body)
                assert accepted.status_code == 200, accepted.text
                receipt = accepted.json()
                assert receipt["pending"] is True and receipt["operation_id"]
                await asyncio.wait_for(decision.started.wait(), 5)
                repeat = await client.post("/live/typed", json=body)
                assert repeat.json()["operation_id"] == receipt["operation_id"]
                pending = await client.get("/live/operation", params={
                    **owner, "operation_id": receipt["operation_id"],
                })
                assert pending.status_code == 200 and pending.json()["pending"] is True
            finally:
                decision.release.set()
            await asyncio.wait_for(asyncio.gather(*typed.coordinator._tasks.values()), 10)
            result = await client.get("/live/operation", params={
                **owner, "operation_id": receipt["operation_id"],
            })
            assert result.status_code == 200 and result.json()["pending"] is False
            assert result.json()["result"]["action"]["state"] == "accepted"
            host = typed.fleet.hosts["local"]
            assert len(host.jobs) == len(decision.calls) == 1
            assert [row["content"] for row in host.rows[("default", "task-a")]].count(
                body["text"]
            ) == 1
            assert not typed.api.LIVE_SESSIONS.bindings
            closed = await client.post("/close", json=owner)
            assert closed.status_code == 200 and len(host.jobs) == 1
            assert not any(path.endswith("/stop") for _, path, _, _ in host.requests)

    asyncio.run(run())


@pytest.mark.parametrize("headers", [{}, {"x-talk-token": "wrong"},
                                    {"x-hermes-desktop-talk-token": "wrong"}])
def test_attach_authentication_precedes_json_and_target_preparation(typed, headers):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=typed.app),
                                     base_url="http://hermes.test") as client:
            response = await client.post("/native/attach", content="{broken", headers=headers)
            assert response.status_code == 401
            assert not any(host.requests_all for host in typed.fleet.hosts.values())

    asyncio.run(run())


@pytest.mark.parametrize("fields,status", [
    ({"input_mode": "invalid"}, 400),
    ({"input_mode": []}, 400),
    ({"surface": "unknown"}, 403),
    ({"surface": []}, 403),
    ({"surface_token": "untrusted-proof"}, 403),
])
def test_invalid_typed_attach_retires_candidate_and_selection_reservation(typed, fields, status):
    async def run():
        async with client_for(typed) as client:
            result = await attach(client, **fields)
            assert result.status_code == status, result.text
            assert not typed.fleet.manager._bindings
            assert not typed.fleet.hosts["local"].attachments
            assert typed.fleet.catalog.state(typed.fleet.request).snapshot("typed-tab")[
                "current"
            ] is None
            assert (await attach(client)).status_code == 200

    asyncio.run(run())


def test_typed_reconnect_fences_old_generation_actor_and_input_identity(typed):
    async def run():
        async with client_for(typed) as client:
            old = context(await attach(client))
            fresh = context(await attach(client, **old))
            assert fresh != old
            for owner, headers, status in ((old, {}, 409),
                                           (fresh, {"x-actor": "foreign-actor"}, 409)):
                result = await client.post("/live/typed", headers=headers, json={
                    **owner, "provider_session_id": "capture", "input_id": "late",
                    "text": "Do not dispatch", "admission": "async",
                })
                assert result.status_code == status, result.text
            assert not typed.decision.calls and not typed.fleet.hosts["local"].jobs

    asyncio.run(run())


@pytest.mark.parametrize("text,status", [("", 400), ("   ", 400), ("é" * 8001, 413)],
                         ids=["empty", "whitespace", "utf8-byte-limit"])
def test_typed_input_keeps_existing_utf8_bound_and_nonempty_contract(typed, text, status):
    async def run():
        async with client_for(typed) as client:
            owner = context(await attach(client))
            response = await client.post("/live/typed", json={
                **owner, "provider_session_id": "capture", "input_id": "invalid",
                "text": text, "admission": "async",
            })
            assert response.status_code == status, response.text
            assert not typed.decision.calls and not typed.fleet.hosts["local"].jobs

    asyncio.run(run())


def test_failed_attach_preserves_previous_owner(typed, monkeypatch):
    async def run():
        async with client_for(typed) as client:
            old = context(await attach(client))
            selected = target(typed.fleet, "task-b")

            def denied(*args, **kwargs):
                raise DashboardTaskError("context_denied", 403)

            with monkeypatch.context() as patch:
                patch.setattr(typed.fleet.selection, "activate", denied)
                result = await attach(client, selected=selected, **old)
            assert result.status_code == 403
            assert typed.fleet.manager.binding(typed.fleet.request, old)
            assert not typed.fleet.hosts["local"].jobs
            assert (await attach(client, selected=selected, **old)).status_code == 200

    asyncio.run(run())


def test_disconnected_typed_attach_cancels_before_activation(typed):
    async def run():
        selected = target(typed.fleet)
        request = SimpleNamespace(
            state=typed.fleet.request.state,
            headers={"x-talk-token": "typed-dashboard-token"},
        )

        async def body():
            return {"target_id": selected, "tab_id": "typed-tab",
                    "input_mode": "typed", "surface": "dashboard"}

        async def disconnected():
            return True

        request.json, request.is_disconnected = body, disconnected
        with pytest.raises(typed.api.HTTPException) as caught:
            await typed.api.native_task_attach(request)
        assert caught.value.status_code == 409
        assert not typed.fleet.manager._bindings
        assert not typed.fleet.hosts["local"].attachments

    asyncio.run(run())


def test_cancelled_surface_preparation_discards_only_unactivated_candidate(typed, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return prepare_surface(*args, **kwargs)

    async def run():
        async with client_for(typed) as client:
            with monkeypatch.context() as patch:
                patch.setattr(typed.api.talk_native_surface, "prepare_surface", delayed)
                pending = asyncio.create_task(attach(client))
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    pending.cancel()
                    await asyncio.sleep(0)
                finally:
                    release.set()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            assert not typed.fleet.manager._bindings
            assert not typed.fleet.hosts["local"].attachments
            assert (await attach(client)).status_code == 200

    asyncio.run(run())


def test_cancelled_activation_keeps_durable_selection(typed, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    activate = typed.fleet.selection.activate

    def delayed(*args, **kwargs):
        result = activate(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    async def run():
        async with client_for(typed) as client:
            monkeypatch.setattr(typed.fleet.selection, "activate", delayed)
            pending = asyncio.create_task(attach(client))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                pending.cancel()
                await asyncio.sleep(0)
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            current = typed.fleet.catalog.state(typed.fleet.request).snapshot("typed-tab")[
                "current"
            ]
            owner = {key: current[key] for key in ("connection_id", "generation")}
            assert typed.fleet.manager.binding(typed.fleet.request, owner)
            assert not typed.fleet.hosts["local"].jobs

    asyncio.run(run())


def test_typed_discord_cannot_drop_audience_authorization(typed, monkeypatch):
    issuer = Issuer()

    def surface(*args, **kwargs):
        return prepare_surface(*args, **kwargs, issuer_factory=lambda _: issuer)

    monkeypatch.setattr(typed.api.talk_native_surface, "prepare_surface", surface)

    async def run():
        async with client_for(typed) as client:
            old = context(await attach(client, surface="discord", surface_token="room-proof",
                                       anchor_session_id="room-session"))
            downgrade = await attach(client, **old)
            assert downgrade.status_code == 403
            issuer.denied = True
            result = await client.post("/live/typed", json={
                **old, "provider_session_id": "capture", "input_id": "denied",
                "text": "Do not dispatch", "admission": "async",
            })
            assert result.status_code in {403, 409}
            assert not typed.decision.calls and not typed.fleet.hosts["local"].jobs

    asyncio.run(run())
