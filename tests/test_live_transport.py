"""Fake HTTP/WebSocket peers for the two Live negotiation protocols."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from test_live_config_protocol import fake_auth, setup_for

import talk_live_transport as transport
import talk_realtime as rt
from talk_live_config import LiveConfig

SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


class Socket:
    def __init__(self, *, auto_start=True, auto_close=True, fail_send=False):
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.auto_start = auto_start
        self.auto_close = auto_close
        self.fail_send = fail_send

    def emit(self, event, kind="text"):
        data = event if isinstance(event, (str, bytes)) else json.dumps(event)
        self.queue.put_nowait(SimpleNamespace(type=kind, data=data))

    async def send_json(self, event):
        if self.fail_send:
            auth = fake_auth()
            raise RuntimeError(f"{auth.token} {auth.account_id}")
        self.sent.append(event)
        if event["type"] == "session.start" and self.auto_start:
            self.emit({"type": "session.started", "session": {"id": "live_test"}})
        if event["type"] == "session.close" and self.auto_close:
            self.emit(
                {"type": "session.closed", "reason": "close_requested", "usage": {"seconds": 3}}
            )

    async def receive(self):
        return await self.queue.get()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True


class Client:
    def __init__(self, socket, *, failure=None):
        self.socket = socket
        self.failure = failure
        self.calls = []
        self.closed = False

    def ws_connect(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.failure:
            raise self.failure
        return self.socket

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True


def aiohttp_peer(socket=None, **kwargs):
    socket = socket or Socket()
    client = Client(socket, **kwargs)
    return (
        SimpleNamespace(
            ClientSession=lambda: client,
            WSMsgType=SimpleNamespace(
                TEXT="text",
                BINARY="binary",
                CLOSE="close",
                CLOSED="closed",
                CLOSING="closing",
                ERROR="error",
            ),
        ),
        client,
        socket,
    )


class FakeCall:
    """A negotiated Codex call; the real one is covered in test_live_codex."""

    def __init__(self, answer_sdp=SDP, session_id="rts_codex_test"):
        self.answer_sdp = answer_sdp
        self.session_id = session_id
        self.thread_id = "thread-test"
        self.stopped = 0

    async def stop(self):
        self.stopped += 1


def api_answer(sdp=SDP, session_id="live_test"):
    return httpx.Response(
        201, json={"session": {"id": session_id}, "transport": {"type": "webrtc", "sdp": sdp}}
    )


def forbid_http(_request):
    pytest.fail("subscription negotiation reached an HTTP endpoint")


def test_api_browser_negotiation_uses_only_the_documented_endpoint_and_key():
    async def run():
        config = LiveConfig("api")
        auth = fake_auth("api")
        calls = []

        def respond(request):
            calls.append(request)
            return api_answer()

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            broker = await transport.negotiate_live_browser(
                SDP, setup_for(config), auth, config, http_client=client
            )
        request = calls[0]
        payload = json.loads(request.content)
        assert payload["session"]["delegation"] == {"type": "client"}
        assert "format" not in payload["session"]["audio"]
        assert request.headers["authorization"] == f"Bearer {auth.token}"
        assert str(request.url) == transport.API_SESSIONS_URL
        assert payload["transport"] == {"type": "webrtc", "sdp": SDP}
        assert "chatgpt-account-id" not in request.headers
        assert broker.sideband_url == "wss://api.openai.com/v1/live/sessions/live_test/attach"
        assert broker.codex_call is None
        public = broker.public_response("talk_binding")
        assert public == {"binding_id": "talk_binding", "sdp": SDP}
        assert auth.token not in json.dumps(public)
        assert broker.session_id not in json.dumps(public)

    asyncio.run(run())


def test_subscription_negotiates_through_codex_without_http_or_token(monkeypatch):
    async def run():
        config = LiveConfig()
        call = FakeCall()
        seen = []

        async def negotiate(sdp, setup, settings, *, server=None):
            seen.append((sdp, setup.instructions, settings, server))
            return call

        monkeypatch.setattr(transport, "negotiate_codex_call", negotiate)
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbid_http)) as client:
            broker = await transport.negotiate_live_browser(
                SDP, setup_for(config), fake_auth(), config, http_client=client,
                codex_server="server-sentinel",
            )
        assert seen == [(SDP, "Host policy", config, "server-sentinel")]
        assert broker.codex_call is call
        assert broker.sideband_url is None and broker.request_headers is None
        assert broker.session_id == call.session_id
        public = broker.public_response("talk_binding")
        assert public == {"binding_id": "talk_binding", "sdp": SDP}
        assert call.session_id not in json.dumps(public)
        await broker.close()
        assert call.stopped == 1

    asyncio.run(run())


def test_subscription_invalid_codex_answer_stops_the_call(monkeypatch):
    async def run():
        call = FakeCall(answer_sdp="<html>bad</html>")

        async def negotiate(*_args, **_kwargs):
            return call

        monkeypatch.setattr(transport, "negotiate_codex_call", negotiate)
        with pytest.raises(rt.RealtimeSessionError):
            await transport.negotiate_live_browser(SDP, setup_for(), fake_auth(), LiveConfig())
        assert call.stopped == 1

    asyncio.run(run())


def test_subscription_auth_headers_are_never_built():
    with pytest.raises(rt.RealtimeSessionError, match="negotiates through Codex"):
        transport.auth_headers(fake_auth(), LiveConfig())


@pytest.mark.parametrize("status", [302, 400, 401, 403, 429, 500])
def test_http_failure_does_not_fallback_or_echo_provider_body(status):
    async def run():
        auth = fake_auth("api")
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(
                status, text=f"{auth.token}", headers={"location": "https://untrusted.example/"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(rt.RealtimeSessionError) as exc:
                await transport.negotiate_live_browser(
                    SDP, setup_for(LiveConfig("api")), auth, LiveConfig("api"), http_client=client
                )
        assert len(calls) == 1
        assert auth.token not in str(exc.value)
        assert "No auth fallback" in str(exc.value)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["subscription", "api"])
@pytest.mark.parametrize(
    "sdp",
    ["", "v=0\n", "<html>bad</html>", SDP + "x" * transport.MAX_SDP_BYTES],
    ids=["empty", "no-audio", "html", "oversized"],
)
def test_invalid_sdp_fails_before_any_negotiation(monkeypatch, sdp, mode):
    async def run():
        async def forbidden_codex(*_args, **_kwargs):
            pytest.fail("Invalid SDP reached Codex")

        monkeypatch.setattr(transport, "negotiate_codex_call", forbidden_codex)
        config = LiveConfig(mode)
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbid_http)) as client:
            with pytest.raises(rt.RealtimeSessionError):
                await transport.negotiate_live_browser(
                    sdp, setup_for(config), fake_auth(mode), config, http_client=client
                )

    asyncio.run(run())


def test_oversize_answer_is_bounded():
    async def run():
        config = LiveConfig("api")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(201, text=SDP + "x" * transport.MAX_SDP_BYTES)
            )
        ) as client:
            with pytest.raises(rt.RealtimeSessionError, match="size limit"):
                await transport.negotiate_live_browser(
                    SDP, setup_for(config), fake_auth("api"), config, http_client=client
                )

    asyncio.run(run())


def test_api_rejects_legacy_shape_instead_of_endpoint_aliasing():
    async def run():
        config = LiveConfig("api")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    201,
                    text=SDP,
                    headers={"location": "/v1/live/rtc_test"},
                )
            )
        ) as client:
            with pytest.raises(rt.RealtimeSessionError, match="invalid session response"):
                await transport.negotiate_live_browser(
                    SDP, setup_for(config), fake_auth("api"), config, http_client=client
                )

    asyncio.run(run())


def test_native_api_ws_starts_without_query_or_ephemeral_token_and_waits_for_ack():
    async def run():
        auth = fake_auth("api")
        module, client, socket = aiohttp_peer()
        wire = transport.LiveWebSocketTransport(
            auth=auth, config=LiveConfig("api"), aiohttp_module=module
        )
        await wire.connect({"model": "gpt-live-1"})
        assert client.calls[0][0] == transport.API_WEBSOCKET_URL
        assert client.calls[0][1]["headers"] == {"Authorization": f"Bearer {auth.token}"}
        assert socket.sent == [{"type": "session.start", "session": {"model": "gpt-live-1"}}]
        assert (await anext(wire))["type"] == "session.started"
        socket.emit({"type": "session.output_transcript.delta", "delta": "x"}, "binary")
        assert (await anext(wire))["delta"] == "x"
        socket.emit("bad JSON")
        assert (await anext(wire))["type"] == "error"
        await wire.close()
        assert socket.closed and client.closed

    asyncio.run(run())


def test_ws_startup_failure_closes_contexts_and_redacts_exception():
    async def run():
        auth = fake_auth("api")
        module, client, _ = aiohttp_peer(failure=RuntimeError(f"{auth.token} secret-detail"))
        wire = transport.LiveWebSocketTransport(
            auth=auth, config=LiveConfig("api"), aiohttp_module=module
        )
        with pytest.raises(rt.RealtimeSessionError) as exc:
            await wire.connect({})
        assert client.closed
        assert auth.token not in str(exc.value)
        assert "secret-detail" not in str(exc.value)

    asyncio.run(run())


def test_ws_rejected_startup_does_not_report_ready():
    async def run():
        module, client, socket = aiohttp_peer(Socket(auto_start=False))
        socket.emit({"type": "error", "error": {"code": "invalid_token"}})
        wire = transport.LiveWebSocketTransport(
            auth=fake_auth("api"), config=LiveConfig("api"), aiohttp_module=module
        )
        with pytest.raises(rt.RealtimeSessionError, match="rejected"):
            await wire.connect({})
        assert wire.session_id is None
        assert client.closed and socket.closed

    asyncio.run(run())


def test_ws_start_timeout_closes_its_socket(monkeypatch):
    async def run():
        monkeypatch.setattr(transport, "CONNECT_TIMEOUT_S", 0.01)
        module, client, socket = aiohttp_peer(Socket(auto_start=False))
        wire = transport.LiveWebSocketTransport(
            auth=fake_auth("api"), config=LiveConfig("api"), aiohttp_module=module
        )
        with pytest.raises(TimeoutError):
            await wire.connect({})
        assert client.closed and socket.closed

    asyncio.run(run())


def test_successful_negotiation_cannot_reflect_the_api_key_in_browser_sdp():
    async def run():
        auth = fake_auth("api")
        config = LiveConfig("api")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: api_answer(SDP + f"a=bad:{auth.token}\r\n")
            )
        ) as client:
            with pytest.raises(
                rt.RealtimeSessionError, match="reflected private credentials"
            ) as exc:
                await transport.negotiate_live_browser(
                    SDP, setup_for(config), auth, config, http_client=client
                )
            assert auth.token not in str(exc.value)

    asyncio.run(run())
