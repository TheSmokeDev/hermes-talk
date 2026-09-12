"""Deterministic attachment/poll ordering over the real native task routes."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import httpx
import pytest
from test_native_controller import Audio, Session
from test_native_controller import native as native
from test_target_switching import fleet as fleet

import talk_cli
from talk_native_api import NativeTaskAPI, NativeTaskError


def test_terminal_reconnect_holds_old_poll_until_attachment_receipt(native, monkeypatch):
    async def scenario():
        server_rotated, release_attach, poll_attempted = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        stale_requests, selected = [], asyncio.Queue()
        inner = httpx.ASGITransport(app=native.app)
        gate_attach = False

        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                path = request.url.path.rsplit("/", 1)[-1]
                if path == "state" and server_rotated.is_set() and not release_attach.is_set():
                    stale_requests.append(request)
                response = await inner.handle_async_request(request)
                if path == "attach" and gate_attach:
                    assert response.is_success
                    server_rotated.set()
                    await release_attach.wait()
                return response

        class API(NativeTaskAPI):
            async def request(self, path, body=None, **kwargs):
                if path == "/state" and server_rotated.is_set() and not release_attach.is_set():
                    poll_attempted.set()
                return await super().request(path, body, **kwargs)

        client = httpx.AsyncClient(transport=Transport())
        api = API("http://127.0.0.1", talk_token=os.environ["TALK_DASHBOARD_TOKEN"], client=client)
        monkeypatch.setenv("TALK_VOICE_MODE", "native")
        monkeypatch.setenv("TALK_TURN_DETECTION", "provider_native")
        monkeypatch.delenv("TALK_SEMANTIC_EAGERNESS", raising=False)
        monkeypatch.setattr(
            talk_cli,
            "resolve_provider_lane",
            lambda: SimpleNamespace(
                provider="openai",
                model="fixture",
                voice="fixture",
                auth=object(),
            ),
        )
        running = asyncio.create_task(
            talk_cli.run_native_talk_session(
                audio=Audio(),
                session_factory=lambda _: Session(),
                api=api,
                task={"target_id": "task-a"},
                on_controller=selected.put_nowait,
            )
        )
        switch = None
        try:
            first = await asyncio.wait_for(selected.get(), 5)
            gate_attach = True
            switch = asyncio.create_task(first.command("/reconnect"))
            await asyncio.wait_for(server_rotated.wait(), 5)
            await asyncio.sleep(1.05)
            # Selection pauses the runner before capture flush and attachment.
            # Its scheduled refresh must not admit any old-generation poll.
            assert not poll_attempted.is_set() and not stale_requests
            assert not api.closed and not running.done()
            release_attach.set()
            assert await asyncio.wait_for(switch, 5) == {"selected": True}
            final = await asyncio.wait_for(selected.get(), 5)
            assert final.context != first.context and first.closed
            assert not final.session.sent
            await final.session.close()
            assert await asyncio.wait_for(running, 5) == 0
            assert not any(host.jobs for host in native.fleet.hosts.values())
        finally:
            release_attach.set()
            if switch is not None:
                await asyncio.gather(switch, return_exceptions=True)
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await client.aclose()

    asyncio.run(scenario())


def test_queued_action_keeps_original_context_and_never_moves_to_new_task():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        paths = []

        async def handle(request):
            paths.append(request.url.path.rsplit("/", 1)[-1])
            if paths[-1] == "attach":
                entered.set()
                await release.wait()
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "task": {"connection_id": "new", "generation": 2},
                        "instructions": "Approved context",
                        "tools": [],
                    },
                )
            return httpx.Response(409, json={"error": "old_binding"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            api = NativeTaskAPI("http://127.0.0.1", client=client)
            original = {"connection_id": "old", "generation": 1}
            api.context = dict(original)
            attaching = asyncio.create_task(api.attach(target_id="new-target"))
            await entered.wait()
            queued = asyncio.create_task(
                api.request(
                    "/tool",
                    {"name": "resolve_approval"},
                    expected_context=original,
                )
            )
            await asyncio.sleep(0)
            assert paths == ["attach"]
            release.set()
            await attaching
            with pytest.raises(NativeTaskError, match="older connection"):
                await queued
            assert paths == ["attach"]

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403, 409])
def test_current_connection_refusals_remain_errors(status):
    async def scenario():
        async def handle(request):
            return httpx.Response(status, json={"error": "authorization_lost"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            api = NativeTaskAPI("http://127.0.0.1", client=client)
            original = {"connection_id": "current", "generation": 1}
            api.context = dict(original)
            with pytest.raises(NativeTaskError) as caught:
                await api.request("/state", expected_context=original)
            assert caught.value.status == status
            assert api.context == original

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [503, 403, 409])
def test_runner_recovers_transient_state_error_and_closes_on_authority_error(
    native, monkeypatch, status
):
    async def scenario():
        failed, recovered, selected = asyncio.Event(), asyncio.Event(), asyncio.Queue()
        inner = httpx.ASGITransport(app=native.app)
        state_calls = 0

        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                nonlocal state_calls
                if request.url.path.endswith("/state"):
                    state_calls += 1
                    if state_calls == 2:
                        failed.set()
                        return httpx.Response(status, json={"error": "fixture"})
                response = await inner.handle_async_request(request)
                if state_calls >= 3 and request.url.path.endswith("/state"):
                    recovered.set()
                return response

        async with httpx.AsyncClient(transport=Transport()) as client:
            api = NativeTaskAPI(
                "http://127.0.0.1", talk_token=os.environ["TALK_DASHBOARD_TOKEN"], client=client
            )
            monkeypatch.setenv("TALK_VOICE_MODE", "native")
            monkeypatch.setenv("TALK_TURN_DETECTION", "provider_native")
            monkeypatch.delenv("TALK_SEMANTIC_EAGERNESS", raising=False)
            monkeypatch.setattr(
                talk_cli,
                "resolve_provider_lane",
                lambda: SimpleNamespace(
                    provider="openai",
                    model="fixture",
                    voice="fixture",
                    auth=object(),
                ),
            )
            audio = Audio()
            running = asyncio.create_task(
                talk_cli.run_native_talk_session(
                    audio=audio,
                    session_factory=lambda _: Session(),
                    api=api,
                    task={"target_id": "task-a"},
                    on_controller=selected.put_nowait,
                )
            )
            try:
                first = await asyncio.wait_for(selected.get(), 5)
                await asyncio.wait_for(failed.wait(), 5)
                await asyncio.sleep(0)
                if status == 503:
                    assert first.service_paused and not running.done()
                    before = list(first.session.sent)
                    await first.send_audio(b"\x01\x00")
                    assert first.session.sent == before
                    await asyncio.wait_for(recovered.wait(), 5)
                    await asyncio.sleep(0)
                    assert not first.service_paused and not running.done()
                    await first.session.close()
                    assert await asyncio.wait_for(running, 5) == 0
                else:
                    assert await asyncio.wait_for(running, 5) == 1
                    assert not recovered.is_set()
                assert audio.stopped and api.closed
            finally:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


def test_selection_flushes_old_capture_before_attachment_and_pauses_media(native, monkeypatch):
    async def scenario():
        flushing, release, selected = asyncio.Event(), asyncio.Event(), asyncio.Queue()
        inner = httpx.ASGITransport(app=native.app)
        attachments = 0

        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                nonlocal attachments
                if request.url.path.endswith("/attach"):
                    attachments += 1
                return await inner.handle_async_request(request)

        async with httpx.AsyncClient(transport=Transport()) as client:
            api = NativeTaskAPI(
                "http://127.0.0.1", talk_token=os.environ["TALK_DASHBOARD_TOKEN"], client=client
            )
            monkeypatch.setenv("TALK_VOICE_MODE", "native")
            monkeypatch.setenv("TALK_TURN_DETECTION", "provider_native")
            monkeypatch.delenv("TALK_SEMANTIC_EAGERNESS", raising=False)
            monkeypatch.setattr(
                talk_cli,
                "resolve_provider_lane",
                lambda: SimpleNamespace(
                    provider="openai",
                    model="fixture",
                    voice="fixture",
                    auth=object(),
                ),
            )
            running = asyncio.create_task(
                talk_cli.run_native_talk_session(
                    audio=Audio(),
                    session_factory=lambda _: Session(),
                    api=api,
                    task={"target_id": "task-a"},
                    on_controller=selected.put_nowait,
                )
            )
            switch = None
            try:
                first = await asyncio.wait_for(selected.get(), 5)

                async def flush(*, retry):
                    assert retry
                    flushing.set()
                    await release.wait()

                first.flush_captures = flush
                switch = asyncio.create_task(first.command("/reconnect"))
                await asyncio.wait_for(flushing.wait(), 5)
                assert first.service_paused and attachments == 1
                before = list(first.session.sent)
                await first.send_audio(b"\x01\x00")
                assert first.session.sent == before
                release.set()
                assert await asyncio.wait_for(switch, 5) == {"selected": True}
                final = await asyncio.wait_for(selected.get(), 5)
                assert attachments == 2 and first.closed
                await final.session.close()
                assert await asyncio.wait_for(running, 5) == 0
            finally:
                release.set()
                if switch is not None:
                    await asyncio.gather(switch, return_exceptions=True)
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())
