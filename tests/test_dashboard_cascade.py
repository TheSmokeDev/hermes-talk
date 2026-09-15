"""Dashboard cascade relay — the browser lane's custom voice, server-side only.

The tab holds the provider socket (WebRTC) and relays the model's TEXT deltas
to ``POST /cascade-tts``; the route runs them through the same CascadeVoice
the terminal and Discord lanes use and streams PCM24k back. What is proved
here, offline: the gate never fails open, the ElevenLabs key never leaves the
process, only an explicit ``done`` completes an answer (an aborted stream is
a barge-in, not a flush), a malformed line cancels rather than half-speaks,
and the mint flips the session to text output only in cascade mode.
"""

from __future__ import annotations

import asyncio
import base64
import json
import types

import aiohttp
import pytest
from test_dashboard_api import api, serialized

import talk_cascade_voice
import talk_config

#: A credential shaped like the real thing, so a leak is greppable rather than
#: a subtle substring match.
FAKE_KEY = "fake-elevenlabs-key-for-tests"
FAKE_VOICE_ID = "fakeVoiceId0001"


@pytest.fixture(autouse=True)
def _cascade_env(monkeypatch, tmp_path):
    """Cascade configured, dashboard token unset, real operator keys scrubbed."""

    monkeypatch.delenv(api.DASHBOARD_TOKEN_ENV, raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("TALK_PROVIDER", raising=False)
    monkeypatch.setenv("TALK_VOICE_MODE", "cascade")
    monkeypatch.setenv("TALK_ELEVENLABS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TALK_ELEVENLABS_VOICE_ID", FAKE_VOICE_ID)
    monkeypatch.setenv("TALK_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class StreamRequest:
    """A request whose body is a scripted NDJSON byte stream.

    ``hold`` gates the abort point: with it set, the stream pauses after
    ``chunks`` until the test releases it, then yields ``tail`` (empty for a
    plain EOF). Without it a fake stream would EOF instantly — the abort would
    beat the TTS dial and the test would prove nothing about mid-stream
    cancellation.
    """

    def __init__(
        self,
        chunks: list[bytes],
        *,
        tail: list[bytes] | None = None,
        hold=None,
        headers=None,
        host="127.0.0.1",
    ) -> None:
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.client = _FakeClient(host) if host is not None else None
        self._chunks = list(chunks)
        self._tail = list(tail or [])
        self._hold = hold

    async def stream(self):
        for chunk in self._chunks:
            yield chunk
        if self._hold is not None:
            await self._hold.wait()
        for chunk in self._tail:
            yield chunk


class JsonRequest:
    """The mint/status shape: headers, peer, and an awaitable JSON body."""

    def __init__(self, body=None, *, headers=None, host="127.0.0.1") -> None:
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.client = _FakeClient(host)
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


def ndjson(*lines: dict) -> list[bytes]:
    """One chunk per line — the shape the browser's writer produces."""

    return [(json.dumps(line) + "\n").encode() for line in lines]


class _FakeTtsWs:
    """A scripted ElevenLabs stream-input socket: records sends, yields frames."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True

    def feed(self, frame: dict) -> None:
        message = types.SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(frame))
        self.incoming.put_nowait(message)

    def feed_audio(self, pcm: bytes) -> None:
        self.feed({"audio": base64.b64encode(pcm).decode("ascii")})

    def feed_final(self) -> None:
        self.feed({"isFinal": True})


class _FakeTtsModule:
    """The two aiohttp members the cascade touches, with scripted sockets."""

    WSMsgType = aiohttp.WSMsgType

    def __init__(self) -> None:
        self.dials: list[dict] = []
        self.sockets: list[_FakeTtsWs] = []

    def ClientSession(self):
        module = self

        class _Session:
            async def ws_connect(self, url, headers=None, timeout=None):
                ws = _FakeTtsWs()
                module.dials.append({"url": url, "headers": headers})
                module.sockets.append(ws)
                return ws

            async def close(self) -> None:
                pass

        return _Session()


@pytest.fixture
def tts(monkeypatch):
    """Patch the cascade's ElevenLabs dial; return the scripted-module recorder."""

    module = _FakeTtsModule()
    monkeypatch.setattr(talk_cascade_voice, "_import_aiohttp", lambda: module)
    return module


async def _wait_for(condition, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0)


async def _collect(response) -> bytes:
    out = b""
    async for chunk in response.body_iterator:
        out += chunk
    return out


def _run(coro):
    return asyncio.run(coro)


# -- the gate -----------------------------------------------------------------


def test_cascade_route_refuses_a_remote_peer_without_a_token():
    with pytest.raises(api.HTTPException) as excinfo:
        _run(api.cascade_tts(StreamRequest([], host="203.0.113.9")))

    assert excinfo.value.status_code == 403


def test_cascade_route_demands_the_configured_token(monkeypatch):
    monkeypatch.setenv(api.DASHBOARD_TOKEN_ENV, "s3cret")

    with pytest.raises(api.HTTPException) as excinfo:
        _run(api.cascade_tts(StreamRequest([])))

    assert excinfo.value.status_code == 401


# -- fail-closed config -------------------------------------------------------


def test_cascade_route_refuses_when_the_server_is_not_in_cascade_mode(monkeypatch):
    monkeypatch.delenv("TALK_VOICE_MODE", raising=False)

    with pytest.raises(api.HTTPException) as excinfo:
        _run(api.cascade_tts(StreamRequest([], headers={}, host="127.0.0.1")))

    assert excinfo.value.status_code == 409
    assert "TALK_VOICE_MODE" in str(excinfo.value.detail)


def test_cascade_route_refuses_a_broken_knob_before_dialing(monkeypatch, tts):
    monkeypatch.delenv("TALK_ELEVENLABS_VOICE_ID", raising=False)

    with pytest.raises(api.HTTPException) as excinfo:
        _run(api.cascade_tts(StreamRequest(ndjson({"delta": "hi"}))))

    assert excinfo.value.status_code == 400
    assert "TALK_ELEVENLABS_VOICE_ID" in str(excinfo.value.detail)
    assert tts.dials == []  # refused before a secret or socket was spent


def test_cascade_route_refuses_a_non_openai_provider(monkeypatch, tts):
    monkeypatch.setenv("TALK_PROVIDER", "grok")

    with pytest.raises(api.HTTPException) as excinfo:
        _run(api.cascade_tts(StreamRequest(ndjson({"delta": "hi"}))))

    assert excinfo.value.status_code == 400
    assert "grok" in str(excinfo.value.detail)
    assert tts.dials == []


# -- the stream lifecycle -----------------------------------------------------


def test_deltas_stream_through_the_cascade_and_pcm_comes_back(tts):
    pcm_one = b"\x01\x02" * 480
    pcm_two = b"\x03\x04" * 480

    async def scenario():
        body = ndjson(
            {"delta": "First sentence. "},
            {"delta": "Second one. "},
            {"done": "First sentence. Second one."},
        )
        response = await api.cascade_tts(StreamRequest(body))
        assert response.media_type == api.CASCADE_PCM_MEDIA_TYPE

        async def script_upstream():
            await _wait_for(lambda: len(tts.sockets) == 1)
            ws = tts.sockets[0]
            await _wait_for(lambda: len(ws.sent) == 4)  # BOS, two chunks, EOS
            ws.feed_audio(pcm_one)
            ws.feed_audio(pcm_two)
            ws.feed_final()

        upstream = asyncio.create_task(script_upstream())
        collected = await _collect(response)
        await upstream

        ws = tts.sockets[0]
        assert [m.get("text") for m in ws.sent] == [
            " ",
            "First sentence.",
            "Second one.",
            "",
        ]
        # The key rode the xi-api-key header only — never the URL, and never
        # the response: the browser sees PCM bytes and nothing else.
        assert tts.dials[0]["headers"] == {"xi-api-key": FAKE_KEY}
        assert FAKE_KEY not in tts.dials[0]["url"]
        assert FAKE_KEY.encode() not in collected
        assert collected == pcm_one + pcm_two

    _run(scenario())


def test_an_aborted_stream_cancels_the_tts_instead_of_flushing(tts):
    """EOF without `done` is a barge-in: the interrupted answer dies quietly."""

    async def scenario():
        hold = asyncio.Event()
        body = ndjson({"delta": "An answer the operator talks over. "})
        response = await api.cascade_tts(StreamRequest(body, hold=hold))
        collected = bytearray()

        async def drain():
            async for chunk in response.body_iterator:
                collected.extend(chunk)

        drainer = asyncio.create_task(drain())
        await _wait_for(lambda: len(tts.sockets) == 1)
        ws = tts.sockets[0]
        await _wait_for(lambda: len(ws.sent) == 2)  # BOS + chunk, never EOS
        pcm = b"\x00\x01" * 240
        ws.feed_audio(pcm)
        await _wait_for(lambda: bytes(collected) == pcm)

        hold.set()  # the barge-in: the request stream ends with no `done`
        await asyncio.wait_for(drainer, 2)

        assert ws.closed, "the aborted response's TTS stream was left open"
        assert len(ws.sent) == 2, "EOS went out for an interrupted answer"
        # Audio emitted before the abort point was already on the wire; the
        # rest of the answer never left the TTS socket.
        assert bytes(collected) == pcm

    _run(scenario())


def test_a_malformed_line_cancels_the_answer_and_logs_one_receipt(tts, caplog):
    async def scenario():
        hold = asyncio.Event()
        body = ndjson({"delta": "Half an answer. "})
        tail = [
            b"this is not json\n",
            b'{"delta": "tail that must never be spoken. "}\n',
            b'{"done": "Half an answer."}\n',
        ]
        with caplog.at_level("WARNING", logger="hermes_dashboard_plugin_hermes_talk"):
            response = await api.cascade_tts(StreamRequest(body, tail=tail, hold=hold))
            collected = bytearray()

            async def drain():
                async for chunk in response.body_iterator:
                    collected.extend(chunk)

            drainer = asyncio.create_task(drain())
            await _wait_for(lambda: len(tts.sockets) == 1)
            ws = tts.sockets[0]
            await _wait_for(lambda: len(ws.sent) == 2)
            pcm = b"\x01\x00" * 240
            ws.feed_audio(pcm)
            await _wait_for(lambda: bytes(collected) == pcm)

            hold.set()  # now the garbage line arrives
            await asyncio.wait_for(drainer, 2)

            assert ws.closed
            assert len(ws.sent) == 2, "the tail after the malformed line was spoken"
            assert bytes(collected) == pcm  # audio up to the break, then end

    _run(scenario())
    assert any("malformed" in record.getMessage() for record in caplog.records)


def test_an_oversized_line_is_malformed_input(tts, caplog):
    body = [b'{"delta": "' + b"x" * (api.CASCADE_MAX_LINE_BYTES + 16) + b'"}']

    async def scenario():
        with caplog.at_level("WARNING", logger="hermes_dashboard_plugin_hermes_talk"):
            response = await api.cascade_tts(StreamRequest(body))
            return await _collect(response)

    collected = _run(scenario())

    assert collected == b""
    assert tts.dials == []  # nothing speakable ever reached the cascade


def test_a_whitespace_only_answer_closes_without_dialing(tts):
    async def scenario():
        body = ndjson({"done": "   "})
        response = await api.cascade_tts(StreamRequest(body))
        return await _collect(response)

    assert _run(scenario()) == b""
    assert tts.dials == []


def test_a_tts_failure_ends_the_stream_without_an_http_error(tts):
    """Upstream TTS trouble degrades the answer to text-only — never a 5xx."""

    async def scenario():
        body = ndjson({"delta": "This answer loses its voice. "}, {"done": "x"})
        response = await api.cascade_tts(StreamRequest(body))

        async def watch():
            await _wait_for(lambda: len(tts.sockets) == 1)
            tts.sockets[0].feed({"error": "upstream said no"})

        watcher = asyncio.create_task(watch())
        collected = await _collect(response)
        await watcher
        return collected

    assert _run(scenario()) == b""  # zero audio, clean end — text stays on screen


# -- the mint's cascade shape ---------------------------------------------------


@pytest.fixture
def minted(monkeypatch):
    """Replace the ONE network call, and record the session it was handed."""

    seen: dict = {}

    def fake_post(auth_token: str, session: dict) -> dict:
        seen["auth_token"] = auth_token
        seen["session"] = session
        return {"value": "ek_dashboard_cascade_test", "expires_at": 1_700_000_000}

    import talk_wire

    monkeypatch.setattr(talk_wire, "post_client_secret", fake_post)
    return seen


def test_cascade_mint_requests_text_output_and_names_the_mode(minted):
    body = _run(api.create_session(JsonRequest()))

    assert body["ok"] is True
    assert body["voiceMode"] == "cascade"
    assert body["voice"] == ""  # no provider voice exists in text-output mode
    session = minted["session"]
    assert session["output_modalities"] == ["text"]
    assert "output" not in session["audio"]
    # Listening is untouched: input audio, VAD, and transcription all stay.
    assert session["audio"]["input"]["turn_detection"]["type"] == "server_vad"
    # The cascade key was resolved but never left the process.
    assert FAKE_KEY not in serialized(body)
    assert FAKE_KEY not in serialized(session)


def test_cascade_mint_ignores_the_provider_voice_param(minted):
    """The select is disabled in cascade mode; a stale voice must not 400."""

    body = _run(api.create_session(JsonRequest({"voice": "gilbert"})))

    assert body["ok"] is True
    assert body["voiceMode"] == "cascade"


def test_cascade_mint_refuses_broken_config_before_spending_a_secret(minted, monkeypatch):
    monkeypatch.delenv("TALK_ELEVENLABS_API_KEY", raising=False)

    with pytest.raises(api.HTTPException) as excinfo:
        _run(api.create_session(JsonRequest()))

    assert excinfo.value.status_code == 400
    assert "TALK_ELEVENLABS_API_KEY" in str(excinfo.value.detail)
    assert minted == {}  # no ephemeral secret was spent on a refused config


def test_native_mint_is_untouched_by_the_cascade_knob(minted, monkeypatch):
    monkeypatch.delenv("TALK_VOICE_MODE", raising=False)

    body = _run(api.create_session(JsonRequest()))

    assert body["voiceMode"] == "native"
    assert "output_modalities" not in minted["session"]
    assert minted["session"]["audio"]["output"] == {"voice": talk_config.DEFAULT_TALK_VOICE}


def test_status_reports_the_voice_mode():
    body = _run(api.talk_status(JsonRequest()))
    assert body["voiceMode"] == "cascade"
    assert FAKE_KEY not in serialized(body)


def test_status_defaults_to_native(monkeypatch):
    monkeypatch.delenv("TALK_VOICE_MODE", raising=False)

    body = _run(api.talk_status(JsonRequest()))
    assert body["voiceMode"] == "native"


# -- the ASGI branch the relay actually runs on -------------------------------
#
# These drive the response object directly rather than through Starlette's
# TestClient. That is deliberate and load-bearing: TestClient omits
# `spec_version` from the scope entirely, so it takes the SAME broken branch
# (`scope.get("asgi", {}).get("spec_version", "2.0")` -> below 2.4) and a test
# written through it would HANG rather than fail. Every assertion here is
# pinned to the scope VALUE, never to a version string, so a future uvicorn
# that advertises 2.4 leaves these tests meaningful instead of vacuous.


class _RecordingChannel:
    """An ASGI ``send``/``receive`` pair that records who called what.

    ``receive`` never returns: it is the disconnect listener's first await,
    and a relay that touches it has taken the body message the relay's own
    ``request.stream()`` was waiting for.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.receive_calls = 0

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def receive(self) -> dict:
        self.receive_calls += 1
        await asyncio.Event().wait()  # the deadlock, made explicit
        raise AssertionError("unreachable")

    def body(self) -> bytes:
        return b"".join(
            m.get("body", b"") for m in self.sent if m["type"] == "http.response.body"
        )


def _http_scope(spec_version: str) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "method": "POST",
        "path": "/cascade-tts",
        "headers": [],
    }


async def _one_chunk():
    yield b"pcm-bytes"


def test_relay_streams_on_a_server_advertising_asgi_below_2_4():
    """The branch uvicorn actually selects: spec_version 2.3 over HTTP.

    Starlette's own StreamingResponse races a disconnect listener against
    the body generator here, and the listener calls receive() FIRST —
    swallowing the browser's http.request messages, so the relay waits
    forever for text that already arrived. RelayResponse owns the body
    channel and must never touch receive().
    """

    channel = _RecordingChannel()
    response = api.RelayResponse(_one_chunk(), media_type=api.CASCADE_PCM_MEDIA_TYPE)

    _run(
        asyncio.wait_for(
            response(_http_scope("2.3"), channel.receive, channel.send), timeout=2.0
        )
    )

    assert channel.receive_calls == 0, "the relay must own the request body channel"
    assert channel.body() == b"pcm-bytes"
    assert channel.sent[0]["type"] == "http.response.start"
    assert channel.sent[0]["status"] == 200
    assert channel.sent[-1] == {
        "type": "http.response.body",
        "body": b"",
        "more_body": False,
    }


class _AsgiChannel:
    """A real single-consumer ASGI channel: each message is delivered ONCE.

    This is the whole bug in one object. The browser's upload arrives as
    ``http.request`` messages on the same channel the disconnect listener
    reads, and whichever coroutine calls ``receive()`` first takes them. A
    fake that hands the body to everyone who asks cannot reproduce this.
    """

    def __init__(self, *bodies: bytes) -> None:
        self.messages = [
            {"type": "http.request", "body": body, "more_body": True}
            for body in bodies
        ]
        self.messages.append({"type": "http.request", "body": b"", "more_body": False})
        self.messages.append({"type": "http.disconnect"})
        self.sent: list[dict] = []
        self.receive_calls = 0

    async def receive(self) -> dict:
        self.receive_calls += 1
        if self.messages:
            return self.messages.pop(0)
        await asyncio.Event().wait()  # drained: whoever waits here is stuck
        raise AssertionError("unreachable")

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    def body(self) -> bytes:
        return b"".join(
            m.get("body", b"") for m in self.sent if m["type"] == "http.response.body"
        )


async def _drive_relay(response_cls, tts, pcm: bytes) -> bytes:
    """Run the real relay body through ``response_cls`` over a real channel."""

    starlette_requests = pytest.importorskip("starlette.requests")

    upload = b"".join(
        ndjson({"delta": "Hello there. "}, {"done": "Hello there."})
    )
    channel = _AsgiChannel(upload)
    scope = _http_scope("2.3")
    request = starlette_requests.Request(scope, channel.receive)

    config = api._resolve_cascade_relay_config()
    response = response_cls(
        api._cascade_pcm_stream(request, config),
        media_type=api.CASCADE_PCM_MEDIA_TYPE,
    )

    async def script_upstream():
        await _wait_for(lambda: len(tts.sockets) == 1, timeout=1.0)
        ws = tts.sockets[0]
        await _wait_for(lambda: len(ws.sent) >= 3, timeout=1.0)
        ws.feed_audio(pcm)
        ws.feed_final()

    upstream = asyncio.create_task(script_upstream())
    try:
        await asyncio.wait_for(response(scope, channel.receive, channel.send), 3.0)
    finally:
        upstream.cancel()
        await asyncio.gather(upstream, return_exceptions=True)
    return channel.body()


def test_the_stock_streaming_response_loses_the_upload_on_the_same_scope(tts):
    """Fail-without-the-fix, pinned to the scope VALUE, not a version string.

    Starlette's disconnect listener calls receive() first and takes the
    browser's `http.request` messages, so the relay's own request.stream()
    only ever sees `http.disconnect` and the answer's text never reaches the
    cascade. The reported symptom exactly: HTTP 200, zero bytes of PCM, and
    a ClientDisconnect once the browser gives up.

    If a future starlette stops racing here, THIS test fails and the
    subclass can be reconsidered — the fixed one beside it stays green
    either way, which is why the assertion is on the scope value rather
    than on a starlette or uvicorn version.
    """

    starlette_responses = pytest.importorskip("starlette.responses")

    body = _run(
        _drive_relay(starlette_responses.StreamingResponse, tts, b"\x07\x08" * 480)
    )

    assert body == b"", "the stock response is expected to lose the upload"
    assert not tts.sockets, "no text reached the cascade, so it never dialed"


def test_the_relay_response_delivers_the_pcm_on_the_same_scope(tts):
    """The same channel, the same scope, the same relay — with the fix."""

    pcm = b"\x07\x08" * 480

    body = _run(_drive_relay(api.RelayResponse, tts, pcm))

    assert body == pcm
    assert len(tts.sockets) == 1
    assert [m.get("text") for m in tts.sockets[0].sent] == [" ", "Hello there.", ""]


def test_relay_streams_identically_when_the_server_advertises_2_4():
    """A server that advertises 2.4 gets byte-identical behavior."""

    channel = _RecordingChannel()
    response = api.RelayResponse(_one_chunk(), media_type=api.CASCADE_PCM_MEDIA_TYPE)

    _run(
        asyncio.wait_for(
            response(_http_scope("2.4"), channel.receive, channel.send), timeout=2.0
        )
    )

    assert channel.receive_calls == 0
    assert channel.body() == b"pcm-bytes"


def test_the_route_returns_a_relay_response_not_a_bare_streaming_response():
    """The whole fix is which class the route hands back."""

    response = _run(api.cascade_tts(StreamRequest([])))
    assert isinstance(response, api.RelayResponse)
    assert response.media_type == api.CASCADE_PCM_MEDIA_TYPE


def test_a_client_that_vanishes_mid_upload_is_an_abort_not_an_error(tts, caplog):
    """ClientDisconnect is handled like a stream that ends without `done`.

    A browser that goes away mid-answer (barge-in, tab closed) must cancel
    the TTS rather than raise out of the feeder — the operator hears silence,
    the log stays clean, and no half-answer keeps speaking into a closed tab.
    """

    class _DisconnectingRequest(StreamRequest):
        async def stream(self):
            for chunk in self._chunks:
                yield chunk
            raise api.ClientDisconnect()

    request = _DisconnectingRequest(ndjson({"delta": "Hello there. "}))

    async def scenario():
        response = await api.cascade_tts(request)
        return await _collect(response)

    with caplog.at_level("WARNING"):
        out = _run(scenario())

    assert out == b""  # aborted before any PCM was completed
    assert "malformed" not in caplog.text
    assert "unrecognized" not in caplog.text
