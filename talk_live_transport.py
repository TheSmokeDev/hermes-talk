"""Server-owned Live negotiation and JSON transports.

API mode uses the documented /v1/live/sessions protocol with the user's key.
Subscription mode negotiates through the Codex CLI's app-server
(``talk_live_codex``); Talk never holds the subscription token.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import Mapping
from contextlib import AsyncExitStack

import httpx

try:
    from . import talk_realtime as rt
    from .talk_live_codex import CodexLiveTransport, negotiate_codex_call
    from .talk_live_config import LiveConfig, validate_live_auth
    from .talk_live_protocol import build_live_session
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_realtime as rt
    from talk_live_codex import CodexLiveTransport, negotiate_codex_call
    from talk_live_config import LiveConfig, validate_live_auth
    from talk_live_protocol import build_live_session

API_SESSIONS_URL = "https://api.openai.com/v1/live/sessions"
API_WEBSOCKET_URL = "wss://api.openai.com/v1/live/sessions"
CONNECT_TIMEOUT_S = 30.0
MAX_SDP_BYTES = 256 * 1024
MAX_WIRE_BYTES = 2 * 1024 * 1024
MAX_STARTUP_EVENTS = 128
_ID = re.compile(r"^[A-Za-z0-9_-]{1,512}$")


def validate_sdp(sdp: str) -> str:
    if (
        not isinstance(sdp, str)
        or len(sdp.encode("utf-8")) > MAX_SDP_BYTES
        or "\x00" in sdp
        or not re.match(r"^v=0\r?\n", sdp)
        or not re.search(r"(?:^|\n)m=audio ", sdp)
    ):
        raise rt.RealtimeSessionError("GPT-Live requires a bounded audio SDP offer/answer")
    return sdp


def auth_headers(auth, config: LiveConfig) -> dict[str, str]:
    """Headers for API mode only; the subscription lane never sees a token."""

    validate_live_auth(auth, config)
    if config.auth_mode != "api":
        raise rt.RealtimeSessionError("Subscription GPT-Live negotiates through Codex")
    return {"Authorization": f"Bearer {auth.token}"}


def _session_id(value) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise rt.RealtimeSessionError("GPT-Live returned an invalid session identifier")
    return value


async def _post_json(client, url: str, headers: dict, payload: dict) -> tuple[bytes, dict]:
    try:
        async with client.stream(
            "POST",
            url,
            headers=headers,
            json=payload,
            follow_redirects=False,
        ) as response:
            if response.status_code not in {200, 201}:
                raise rt.RealtimeSessionError(
                    f"GPT-Live session creation failed (HTTP {response.status_code}); "
                    "check access for the selected auth mode. No auth fallback was attempted."
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_SDP_BYTES:
                    raise rt.RealtimeSessionError(
                        "GPT-Live session response exceeded the size limit"
                    )
                body.extend(chunk)
            return bytes(body), dict(response.headers)
    except rt.RealtimeSessionError:
        raise
    except Exception:  # noqa: BLE001 - transport errors can contain credentials
        # Transport exceptions may contain bearer headers or account identifiers.
        raise rt.RealtimeSessionError("GPT-Live session negotiation failed") from None


class LiveWebSocketTransport:
    def __init__(self, *, auth, config: LiveConfig, aiohttp_module=None):
        self.auth = auth
        self.config = config
        self._aiohttp = aiohttp_module
        self._stack = AsyncExitStack()
        self._ws = None
        self._buffer = deque()
        self.session_id = None
        self.finalized = False
        self.final_usage = None
        self._closed = False

    async def open(self, *, url: str, headers: dict) -> None:
        if self._aiohttp is None:
            import aiohttp

            self._aiohttp = aiohttp
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S):
                client = await self._stack.enter_async_context(self._aiohttp.ClientSession())
                self._ws = await self._stack.enter_async_context(
                    client.ws_connect(
                        url,
                        headers=headers,
                        max_msg_size=MAX_WIRE_BYTES,
                    )
                )
        except asyncio.CancelledError:
            await self._stack.aclose()
            self._closed = True
            raise
        except Exception:  # noqa: BLE001 - transport errors can contain credentials
            await self._stack.aclose()
            self._closed = True
            raise rt.RealtimeSessionError("GPT-Live WebSocket connection failed") from None

    async def connect(self, session: dict) -> None:
        if self.config.auth_mode != "api":
            raise rt.RealtimeSessionError("Subscription native sessions require WebRTC")
        await self.open(url=API_WEBSOCKET_URL, headers=auth_headers(self.auth, self.config))
        try:
            await self.send_json({"type": "session.start", "session": session})
            async with asyncio.timeout(CONNECT_TIMEOUT_S):
                while True:
                    event = await self._receive()
                    if event.get("type") == "error":
                        raise rt.RealtimeSessionError("GPT-Live rejected session startup")
                    self._buffer.append(event)
                    if event.get("type") == "session.started":
                        self.session_id = _session_id(event.get("session", {}).get("id"))
                        break
                    if event.get("type") == "session.closed":
                        raise rt.RealtimeSessionError("GPT-Live closed during startup")
                    if len(self._buffer) >= MAX_STARTUP_EVENTS:
                        raise rt.RealtimeSessionError("GPT-Live startup event limit exceeded")
        except BaseException:
            await self.close()
            raise

    async def send_json(self, event: dict) -> None:
        if self._ws is None or self._closed:
            raise rt.RealtimeSessionError("GPT-Live transport is not connected")
        await self._ws.send_json(event)

    async def _receive(self) -> dict:
        while True:
            message = await self._ws.receive()
            kinds = self._aiohttp.WSMsgType
            if message.type in (kinds.TEXT, kinds.BINARY):
                try:
                    event = json.loads(message.data)
                except (ValueError, UnicodeError):
                    return {"type": "error", "error": {"code": "malformed_frame"}}
                if not isinstance(event, dict):
                    return {"type": "error", "error": {"code": "malformed_frame"}}
                if event.get("type") == "session.closed":
                    self.finalized = True
                    usage = event.get("usage")
                    self.final_usage = dict(usage) if isinstance(usage, Mapping) else None
                return event
            if message.type in (kinds.CLOSE, kinds.CLOSED, kinds.CLOSING):
                raise EOFError("GPT-Live disconnected before session finalization")
            if message.type == kinds.ERROR:
                raise rt.RealtimeSessionError("GPT-Live WebSocket failed")

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._buffer:
            return self._buffer.popleft()
        if self._closed:
            raise StopAsyncIteration
        return await self._receive()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._stack.aclose()


class LiveBrowserSession:
    """Server-only negotiated call. Serialize only public_response(), never this object."""

    def __init__(
        self,
        *,
        answer_sdp,
        session_id,
        sideband_url,
        auth,
        config,
        setup,
        aiohttp_module=None,
        request_headers=None,
        codex_call=None,
    ):
        self.answer_sdp = answer_sdp
        self.session_id = session_id
        self.sideband_url = sideband_url
        self.request_headers = request_headers
        self.codex_call = codex_call
        self._codex_transport = None
        self.auth = auth
        self.config = config
        self.setup = setup
        self._aiohttp = aiohttp_module
        self._session = None
        self._opening = False
        self._opening_task = None
        self._closed = False

    def public_response(self, binding_id: str) -> dict:
        return {"binding_id": _session_id(binding_id), "sdp": self.answer_sdp}

    def attach_outbound(self, outbound) -> None:
        """Route client events to the browser's data channel (subscription calls only)."""

        if self._codex_transport is not None:
            self._codex_transport.attach_outbound(outbound)

    async def open_session(self):
        if self._closed:
            raise rt.RealtimeSessionError("GPT-Live browser binding is closed")
        if self._session is not None:
            return self._session
        if self._opening:
            raise rt.RealtimeSessionError("GPT-Live browser sideband is already opening")
        self._opening = True
        self._opening_task = asyncio.current_task()
        try:
            from .talk_live_realtime import LiveRealtimeSession
        except ImportError:  # pragma: no cover - flat-module fallback
            from talk_live_realtime import LiveRealtimeSession
        if self.codex_call is not None:
            transport = self._codex_transport = CodexLiveTransport(self.codex_call)
        else:
            transport = LiveWebSocketTransport(
                auth=self.auth,
                config=self.config,
                aiohttp_module=self._aiohttp,
            )
        try:
            if self.codex_call is None:
                await transport.open(
                    url=self.sideband_url,
                    headers=self.request_headers or auth_headers(self.auth, self.config),
                )
            transport.session_id = self.session_id
            session = LiveRealtimeSession(auth=self.auth, config=self.config)
            session.attach_transport(transport, self.setup, session_id=self.session_id, media=False)
            self._session = session
            return session
        except BaseException:
            await transport.close()
            raise
        finally:
            self._opening = False
            self._opening_task = None

    async def close(self):
        self._closed = True
        if self._opening_task is not None and self._opening_task is not asyncio.current_task():
            pending = self._opening_task
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if self._session is not None:
            await self._session.close()
        elif self.codex_call is not None:
            await self.codex_call.stop()


async def negotiate_live_browser(
    sdp: str,
    setup: rt.SessionSetup,
    auth,
    config: LiveConfig,
    *,
    http_client=None,
    aiohttp_module=None,
    codex_server=None,
) -> LiveBrowserSession:
    validate_live_auth(auth, config)
    validate_sdp(sdp)
    if config.auth_mode == "subscription":
        build_live_session(setup, config, webrtc=True)
        call = await negotiate_codex_call(sdp, setup, config, server=codex_server)
        try:
            validate_sdp(call.answer_sdp)
        except BaseException:
            await call.stop()
            raise
        return LiveBrowserSession(
            answer_sdp=call.answer_sdp,
            session_id=_session_id(call.session_id),
            sideband_url=None,
            auth=auth,
            config=config,
            setup=setup,
            codex_call=call,
        )
    session = build_live_session(setup, config, webrtc=True)
    payload = {"session": session, "transport": {"type": "webrtc", "sdp": sdp}}
    headers = auth_headers(auth, config)
    if http_client is None:
        async with httpx.AsyncClient(timeout=CONNECT_TIMEOUT_S) as client:
            body, _response_headers = await _post_json(client, API_SESSIONS_URL, headers, payload)
    else:
        body, _response_headers = await _post_json(http_client, API_SESSIONS_URL, headers, payload)
    try:
        result = json.loads(body)
        session_id = _session_id(result["session"]["id"])
        if result["transport"]["type"] != "webrtc":
            raise ValueError("wrong transport")
        answer = result["transport"]["sdp"]
    except (KeyError, ValueError, TypeError) as exc:
        raise rt.RealtimeSessionError("GPT-Live returned an invalid session response") from exc
    validate_sdp(answer)
    if auth.token and auth.token in answer:
        raise rt.RealtimeSessionError("GPT-Live SDP response reflected private credentials")
    return LiveBrowserSession(
        answer_sdp=answer,
        session_id=session_id,
        sideband_url=f"{API_WEBSOCKET_URL}/{session_id}/attach",
        auth=auth,
        config=config,
        setup=setup,
        aiohttp_module=aiohttp_module,
        request_headers=headers,
    )


__all__ = [
    "API_SESSIONS_URL",
    "API_WEBSOCKET_URL",
    "CONNECT_TIMEOUT_S",
    "LiveBrowserSession",
    "LiveWebSocketTransport",
    "auth_headers",
    "negotiate_live_browser",
    "validate_sdp",
]
