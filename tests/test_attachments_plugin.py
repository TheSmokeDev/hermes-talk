"""The plugin upload adapter and attachment admission, over the real ASGI routes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_text_input
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from test_live_coordinator import Decision
from test_text_input import SeamHost, attach, build_fleet, client_for, code, context

import talk_audio
import talk_text_input
from talk_dashboard_gateway import INPUT_ATTACHMENT_PATH

GATEWAY_KEY = "fixture-gateway-key"
HOST_BASE = "http://127.0.0.1:8642"
FILE_BYTES = b"an operator's own report, byte for byte"
OTHER_BYTES = b"a second file that is not the first"


def published(limits, *, supported=True, child=True):
    return {
        "version": 1, "supported": supported,
        "endpoint": {"method": "POST", "path": INPUT_ATTACHMENT_PATH},
        "dispatch_field": "child.attachments",
        "reference_fields": ["attachment_id", "sha256"],
        "limits": dict(limits),
        "retention": {"unbound_ttl_seconds": 86400, "bound_attachments_retained": True},
        "delivery": {
            "hermes_child": {"available": child, "terminal_backend": "local",
                             "reason": "" if child else "terminal_backend_delivery_unverified"},
            "external_task_workers": {"available": False,
                                      "reason": "worker_attachment_delivery_unsupported"},
            "existing_app_recipients": {
                "available": False, "reason": "recipient_attachment_delivery_unsupported"},
        },
        "inspection_requires_tool_use": True,
    }


class AttachmentHost(SeamHost):
    """The fleet host plus the canonical input-attachment ingress it publishes."""

    def __init__(self, store_id):
        super().__init__(store_id)
        self.attachments_supported = True
        self.child_delivery = True
        self.attachment_limits = {
            "max_files_per_input": 8,
            "max_file_bytes": 10 * 1024 * 1024,
            "max_input_bytes": 20 * 1024 * 1024,
        }
        self.stored, self.uploads = {}, []
        self.attachment_refusal = None

    def __call__(self, request):
        path = request.url.path
        if path.startswith("/p/"):
            path = "/" + path.split("/", 3)[3]
        if path == INPUT_ATTACHMENT_PATH:
            return self.ingest(request)
        response = super().__call__(request)
        if path == "/v1/capabilities":
            data = response.json()
            data["features"]["input_attachments"] = published(
                self.attachment_limits,
                supported=self.attachments_supported,
                child=self.child_delivery,
            )
            return self.response(200, data)
        return response

    def ingest(self, request):
        assert request.headers["Authorization"] == "Bearer " + GATEWAY_KEY
        body = json.loads(request.content)
        self.uploads.append(body)
        if self.attachment_refusal is not None:
            return self.response(*self.attachment_refusal)
        assert set(body) == {
            "session_id", "upload_id", "filename", "content_type", "content_base64",
        }
        data = base64.b64decode(body["content_base64"], validate=True)
        if len(data) > self.attachment_limits["max_file_bytes"]:
            return self.response(413, {"error": "attachment_size_limit"})
        previous = self.stored.get(body["upload_id"])
        if previous is not None:
            assert previous["bytes"] == len(data)
            return self.response(200, previous)
        receipt = {
            "attachment_id": "att_"
            + hashlib.sha256(body["upload_id"].encode()).hexdigest()[:32],
            "filename": body["filename"],
            # Detected from the bytes, never the declared label.
            "content_type": "application/octet-stream",
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "state": "stored",
        }
        self.stored[body["upload_id"]] = receipt
        return self.response(200, receipt)


@pytest.fixture
def seam(tmp_path, monkeypatch):
    """The real mounted routes over a host that publishes the attachment feature."""
    monkeypatch.setattr(test_text_input, "SeamHost", AttachmentHost)
    source = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("attachment_route_fixture", source)
    api = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = api
    spec.loader.exec_module(api)
    fleet = build_fleet(api.TASKS, tmp_path)
    monkeypatch.setattr(api, "TARGETS", fleet.selection)
    monkeypatch.setattr(api.talk_dashboard_tasks, "resolve_context", fleet.manager.resolve_context)
    monkeypatch.setenv("TALK_DASHBOARD_TOKEN", "typed-dashboard-token")
    monkeypatch.setenv("HERMES_DESKTOP_TALK_TOKEN", "typed-desktop-token")

    def forbidden(*args, **kwargs):
        pytest.fail("Attachment upload must not resolve voice configuration, auth, or transport")

    for module, name in (
        (api, "_mint"),
        (api.talk_auth, "resolve_auth"),
        (api.LIVE_SESSIONS, "create"),
        (talk_audio.DuplexAudio, "start"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    coordinator = api.LIVE_SESSIONS.coordinator
    coordinator.targets, coordinator.tools, coordinator.decide = (
        fleet.selection, api._session_tools, Decision(),
    )
    named = {handler.__name__: handler for handler in (
        *api.TEXT_INPUT_ROUTE_HANDLERS, *api.ATTACHMENT_ROUTE_HANDLERS,
    )}
    paths = {
        "/text/input": ("POST", named["text_input"]),
        "/attachments/upload": ("POST", named["attachment_upload"]),
        "/native/attach": ("POST", api.native_task_attach),
        "/targets": ("POST", api.task_targets),
        "/state": ("POST", api.task_state),
        "/status": ("GET", api.talk_status),
    }

    def endpoint(handler):
        async def call(request):
            request.state.principal = request.headers.get("x-actor", "actor-one")
            try:
                result = await handler(request)
            except api.HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            return result if isinstance(result, Response) else JSONResponse(result)

        return call

    app = Starlette(routes=[
        Route(path, endpoint(handler), methods=[method])
        for path, (method, handler) in paths.items()
    ])
    return SimpleNamespace(api=api, fleet=fleet, app=app, host=fleet.hosts["local"])


async def upload(client, owner, input_id, data=FILE_BYTES, *, filename="report.txt"):
    return await client.post("/attachments/upload", json={
        **owner, "input_id": input_id, "filename": filename,
        "content_type": "text/plain",
        "bytes_base64": base64.b64encode(data).decode("ascii"),
    })


async def start(client, owner, input_id, references, *, text="Read the attached report"):
    return await client.post("/text/input", json={
        **owner, "input_id": input_id, "text": text, "operation": "start_worker",
        "attachments": references,
    })


def reference(response):
    body = response.json()
    return {"attachment_id": body["attachment_id"], "sha256": body["sha256"]}


def child_bodies(host):
    return [body["child"] for _, body in host.posts("/v1/runs")]


def test_upload_returns_an_opaque_reference_and_no_host_detail(seam):
    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            reply = await upload(client, owner, "draft-one")
            assert reply.status_code == 200, reply.text
            assert reply.headers["cache-control"] == "no-store"
            body = reply.json()
            assert set(body) == {
                "ok", "input_id", "attachment_id", "filename", "content_type", "bytes", "sha256",
            }
            assert body["ok"] is True and body["input_id"] == "draft-one"
            assert body["sha256"] == hashlib.sha256(FILE_BYTES).hexdigest()
            assert body["bytes"] == len(FILE_BYTES)
            assert body["attachment_id"].startswith("att_")
            # Never the credential, the resolved host route, or the profile scope.
            assert GATEWAY_KEY not in reply.text and HOST_BASE not in reply.text
            assert "state" not in body and "path" not in body
            assert len(seam.host.uploads) == 1
            sent = seam.host.uploads[0]
            assert sent["session_id"] == "task-a" and len(sent["upload_id"]) == 64
            # The browser never chooses the upload identity; the server derives it.
            assert sent["upload_id"] not in json.dumps({**owner, "input_id": "draft-one"})

    asyncio.run(run())


def test_the_same_file_twice_replays_one_stored_receipt(seam):
    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            first = await upload(client, owner, "draft-one")
            repeat = await upload(client, owner, "draft-one")
            assert first.status_code == repeat.status_code == 200, repeat.text
            assert first.json() == repeat.json()
            # The pin answers the retry; the host is never asked a second time.
            assert len(seam.host.uploads) == 1

    asyncio.run(run())


def test_upload_is_refused_when_the_host_does_not_advertise_support(seam):
    """Negative proof: an unsupported host never receives the operator's bytes."""

    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            seam.host.attachments_supported = False
            reply = await upload(client, owner, "draft-one")
            assert reply.status_code == 409, reply.text
            assert code(reply) == "attachments_unsupported"
            assert seam.host.uploads == []
            assert not any(
                path.endswith(INPUT_ATTACHMENT_PATH) for _, path, _, _ in seam.host.requests
            )

    asyncio.run(run())


def test_bounds_are_enforced_from_the_hosts_published_limits(seam):
    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            seam.host.attachment_limits = {
                "max_files_per_input": 2, "max_file_bytes": 64, "max_input_bytes": 80,
            }
            first = await upload(client, owner, "draft-one", FILE_BYTES)
            assert first.status_code == 200, first.text
            # 39 + 35 = 74 bytes fits; the third file would pass the file bound and
            # break the per-input bound the host published.
            second = await upload(client, owner, "draft-one", OTHER_BYTES, filename="b.txt")
            assert second.status_code == 200, second.text
            third = await upload(client, owner, "draft-one", b"one more", filename="c.txt")
            assert third.status_code == 413 and code(third) == "attachment_size_limit"
            oversize = await upload(client, owner, "draft-two", b"x" * 65, filename="d.txt")
            assert oversize.status_code == 413 and code(oversize) == "attachment_size_limit"
            assert len(seam.host.uploads) == 2

    asyncio.run(run())


def test_a_reference_from_another_input_is_refused_before_any_dispatch(seam):
    """Negative proof: a cross-input reference never reaches /v1/runs."""

    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            stored = reference(await upload(client, owner, "draft-one"))
            reply = await start(client, owner, "draft-two", [stored])
            assert reply.status_code == 409, reply.text
            assert code(reply) == "attachment_reference_unknown"
            assert seam.host.posts("/v1/runs") == []

    asyncio.run(run())


def test_a_new_generation_does_not_inherit_an_earlier_pin(seam):
    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            stored = reference(await upload(client, owner, "draft-one"))
            rebound = context(await attach(client))
            assert rebound != owner
            reply = await start(client, rebound, "draft-one", [stored])
            assert reply.status_code == 409 and code(reply) == "attachment_reference_unknown"
            assert seam.host.posts("/v1/runs") == []

    asyncio.run(run())


def test_a_forged_reference_is_refused(seam):
    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            stored = reference(await upload(client, owner, "draft-one"))
            forged = {**stored, "sha256": hashlib.sha256(OTHER_BYTES).hexdigest()}
            reply = await start(client, owner, "draft-one", [forged])
            assert reply.status_code == 409 and code(reply) == "attachment_reference_unknown"
            assert seam.host.posts("/v1/runs") == []

    asyncio.run(run())


@pytest.mark.parametrize("fields", [
    {"operation": "message"},
    {"operation": "steer", "run_id": 1},
    {"operation": "cancel", "run_id": 1, "action_id": "action-one"},
    {"operation": "approval", "run_id": 1, "action_id": "a", "request_id": "r", "choice": "once"},
])
def test_attachments_are_refused_on_every_operation_but_start_worker(seam, fields):
    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            stored = reference(await upload(client, owner, "draft-one"))
            before = len(seam.host.requests_all)
            reply = await client.post("/text/input", json={
                **owner, "input_id": "draft-one", "text": "Take this file",
                "attachments": [stored], **fields,
            })
            assert reply.status_code == 400, reply.text
            assert code(reply) == "attachment_operation_unsupported"
            assert len(seam.host.requests_all) == before

    asyncio.run(run())


def test_start_worker_carries_child_attachments_and_dispatches_once(seam):
    """Negative proof: the repeated input_id replays its receipt without a second run."""

    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            stored = reference(await upload(client, owner, "draft-one"))
            reply = await start(client, owner, "draft-one", [stored])
            assert reply.status_code == 200, reply.text
            assert reply.json()["state"] == "accepted"
            children = child_bodies(seam.host)
            assert len(children) == 1
            assert children[0]["attachments"] == [stored]
            assert "worker" not in children[0]
            repeat = await start(client, owner, "draft-one", [stored])
            assert repeat.status_code == 200, repeat.text
            assert repeat.json()["run_id"] == reply.json()["run_id"]
            assert len(seam.host.posts("/v1/runs")) == 1
            # A reordered set is the same set: identity is the reference, not the order.
            second = reference(await upload(client, owner, "draft-one", OTHER_BYTES,
                                            filename="second.txt"))
            changed = await start(client, owner, "draft-one", [second, stored])
            assert changed.status_code == 409 and code(changed) == "event_conflict"
            assert len(seam.host.posts("/v1/runs")) == 1

    asyncio.run(run())


def test_a_start_worker_without_attachments_is_unchanged(seam):
    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            reply = await start(client, owner, "plain-one", [], text="Start the worker")
            assert reply.status_code == 200 and reply.json()["state"] == "accepted"
            children = child_bodies(seam.host)
            assert children[0]["worker"] == "hermes-talk-codex"
            assert "attachments" not in children[0]

    asyncio.run(run())


def test_a_host_that_cannot_deliver_to_a_child_refuses_the_dispatch(seam):
    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            stored = reference(await upload(client, owner, "draft-one"))
            seam.host.child_delivery = False
            reply = await start(client, owner, "draft-one", [stored])
            assert reply.status_code == 400, reply.text
            assert code(reply) == "attachment_operation_unsupported"
            assert seam.host.posts("/v1/runs") == []

    asyncio.run(run())


def test_a_host_refusal_reaches_the_operator_as_a_fixed_code(seam):
    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            seam.host.attachment_refusal = (400, {"error": "invalid_attachment_filename"})
            reply = await upload(client, owner, "draft-one")
            assert reply.status_code == 400, reply.text
            assert code(reply) == "attachment_rejected"
            assert GATEWAY_KEY not in reply.text and HOST_BASE not in reply.text
            seam.host.attachment_refusal = (413, {"error": "attachment_size_limit"})
            large = await upload(client, owner, "draft-two")
            assert large.status_code == 413 and code(large) == "attachment_size_limit"

    asyncio.run(run())


def test_malformed_uploads_refuse_before_binding_or_host_io(seam):
    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            before = len(seam.host.requests_all)
            for body in (
                {"input_id": "draft-one", "filename": "a.txt", "content_type": "text/plain",
                 "bytes_base64": "!!not base64!!"},
                {"input_id": "bad id", "filename": "a.txt", "content_type": "text/plain",
                 "bytes_base64": "YQ=="},
                {"input_id": "draft-one", "filename": "", "content_type": "text/plain",
                 "bytes_base64": "YQ=="},
                {"input_id": "draft-one", "filename": "a.txt", "content_type": "not-a-type",
                 "bytes_base64": "YQ=="},
                {"input_id": "draft-one", "filename": "a.txt", "content_type": "text/plain",
                 "bytes_base64": "YQ==", "session_id": "task-b"},
            ):
                reply = await client.post("/attachments/upload", json={**owner, **body})
                assert reply.status_code == 400, reply.text
                assert code(reply) == "invalid_event"
            assert len(seam.host.requests_all) == before

    asyncio.run(run())


def test_the_upload_route_refuses_a_stale_connection(seam):
    async def run():
        async with client_for(seam) as client:
            owner = context(await attach(client))
            reply = await upload(client, {**owner, "generation": owner["generation"] + 1},
                                 "draft-one")
            assert reply.status_code == 409 and code(reply) == "connection_stale"
            assert seam.host.uploads == []

    asyncio.run(run())


def test_the_descriptor_follows_the_live_host_capability(seam):
    async def run():
        async with client_for(seam) as client:
            supported = await client.get("/status")
            assert supported.status_code == 200, supported.text
            assert supported.json()["textInput"] == {
                "version": 1, "attachments": True,
                "operations": ["message", "start_worker", "steer", "cancel", "approval"],
            }
            seam.host.attachments_supported = False
            withdrawn = await client.get("/status")
            assert withdrawn.json()["textInput"]["attachments"] is False

    asyncio.run(run())


def test_a_malformed_capability_document_is_not_support():
    """Rule 2: the descriptor answers from the physical document, not a claim in it."""

    limits = {"max_files_per_input": 8, "max_file_bytes": 1, "max_input_bytes": 2}
    assert talk_text_input.descriptor(
        {"features": {"input_attachments": published(limits)}})["attachments"] is True
    for broken in (
        None,
        {},
        {"features": {}},
        {"features": {"input_attachments": {**published(limits), "supported": "yes"}}},
        {"features": {"input_attachments": {**published(limits), "version": 2}}},
        {"features": {"input_attachments": {
            **published(limits), "endpoint": {"method": "POST", "path": "/v1/elsewhere"}}}},
        {"features": {"input_attachments": {**published(limits), "limits": {}}}},
        {"features": {"input_attachments": {
            **published(limits), "dispatch_field": "child.files"}}},
    ):
        assert talk_text_input.descriptor(broken)["attachments"] is False
