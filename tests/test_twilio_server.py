"""End-to-end tests of the experimental Twilio server over a real socket.

No Twilio account and no provider: an echo session returns audio as soon as it
receives audio, so the test exercises the whole path — signed webhook, TwiML,
WebSocket upgrade, stream token, frame parsing, µ-law decode, resample, session
command, output resample, µ-law encode, 20 ms framing — against a live aiohttp
server on an ephemeral port. Every credential here is a fake string.
"""

from __future__ import annotations

import asyncio
import base64
import re
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

aiohttp = pytest.importorskip("aiohttp")
pytest.importorskip("aiohttp.web")

import talk_realtime as rt  # noqa: E402
import talk_twilio as twilio  # noqa: E402
import talk_twilio_server as server  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTH_TOKEN = "fake-twilio-auth-token-7f3a"
CALLER = "+15551230001"


class EchoSession:
    """Returns one short response as soon as it is given caller audio."""

    state = rt.SessionState.CONNECTED
    created: ClassVar[list[EchoSession]] = []

    def __init__(self) -> None:
        self.events: asyncio.Queue[rt.RealtimeEvent] = asyncio.Queue()
        self.closed = False
        EchoSession.created.append(self)

    async def connect(self, setup) -> None:
        return None

    async def send(self, commands) -> None:
        for command in commands:
            if isinstance(command, rt.AppendInputAudio):
                # 100 ms of 24 kHz PCM: five Twilio frames once downsampled.
                await self.events.put(rt.OutputAudio(data=b"\x00\x01" * 2400))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.events.get()

    async def close(self) -> None:
        self.closed = True


def _setup() -> rt.SessionSetup:
    return rt.SessionSetup(model="gemini-3.1-flash-live-preview", voice="Kore", instructions="hi")


class Server:
    """A live app on an ephemeral loopback port plus helpers to call it.

    The public URL is a different origin on purpose, as behind a tunnel: the
    webhook must be signed against the URL Twilio called, not the local one.
    """

    def __init__(self, **options) -> None:
        self.options = options
        self.runner = None
        self.base = ""
        self.config = twilio.TwilioConfig(
            public_base_url="https://voice.example.test", auth_token=AUTH_TOKEN
        )

    async def __aenter__(self) -> Server:
        EchoSession.created.clear()
        app = twilio.create_app(
            config=self.config,
            session_factory=self.options.pop("session_factory", EchoSession),
            setup_factory=_setup,
            aiohttp_module=aiohttp,
            **self.options,
        )
        self.runner = aiohttp.web.AppRunner(app)
        await self.runner.setup()
        site = aiohttp.web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc) -> None:
        await self.runner.cleanup()

    async def webhook(self, client, *, call_sid="CA1", caller=CALLER, sign=True):
        form = {"CallSid": call_sid, "From": caller, "To": "+15550000000"}
        headers = {}
        if sign:
            headers["X-Twilio-Signature"] = twilio.expected_signature(
                AUTH_TOKEN, self.config.voice_url, form
            )
        return await client.post(f"{self.base}/twilio/voice", data=form, headers=headers)


def _token(twiml: str) -> str:
    match = re.search(r'<Parameter name="token" value="([^"]+)"/>', twiml)
    assert match, twiml
    return match.group(1)


def _start(token: str | None, call_sid: str = "CA1") -> dict:
    start: dict = {"callSid": call_sid, "streamSid": "MZ1"}
    if token is not None:
        start["customParameters"] = {"token": token}
    return {"event": "start", "streamSid": "MZ1", "start": start}


def _media() -> dict:
    payload = base64.b64encode(b"\xff" * 160).decode("ascii")
    return {"event": "media", "streamSid": "MZ1", "media": {"payload": payload}}


async def _closes(ws, timeout: float = 5) -> bool:
    """True once the server closes the socket; media still queued is skipped."""

    async def drain() -> bool:
        while True:
            message = await ws.receive()
            if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                return True
            if message.type is aiohttp.WSMsgType.ERROR:
                return False

    return await asyncio.wait_for(drain(), timeout=timeout)


def test_transport_round_trip_over_a_socket():
    async def exercise() -> None:
        async with Server() as srv, aiohttp.ClientSession() as client:
            response = await srv.webhook(client)
            body = await response.text()
            assert response.status == 200
            assert "<Connect><Stream url=" in body
            assert CALLER not in body  # the caller never rides the TwiML

            ws = await client.ws_connect(f"{srv.base}/twilio/stream")
            await ws.send_json({"event": "connected"})
            await ws.send_json(_start(_token(body)))
            await ws.send_json(_media())

            first = await asyncio.wait_for(ws.receive_json(), timeout=5)
            assert first["event"] == "media"
            assert first["streamSid"] == "MZ1"
            payload = base64.b64decode(first["media"]["payload"])
            assert len(payload) == twilio.TWILIO_FRAME_BYTES

            await ws.send_json({"event": "stop", "streamSid": "MZ1"})
            await ws.close()
        assert len(EchoSession.created) == 1
        assert EchoSession.created[0].closed

    asyncio.run(exercise())


def test_webhook_refuses_unsigned_badly_signed_and_get_requests():
    async def exercise() -> None:
        async with Server() as srv, aiohttp.ClientSession() as client:
            assert (await srv.webhook(client, sign=False)).status == 403
            forged = await client.post(
                f"{srv.base}/twilio/voice",
                data={"CallSid": "CA1", "From": CALLER},
                headers={"X-Twilio-Signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAA="},
            )
            assert forged.status == 403
            assert (await client.get(f"{srv.base}/twilio/voice")).status == 405

            # A valid signature over different parameters: the From was changed.
            form = {"CallSid": "CA1", "From": CALLER}
            signature = twilio.expected_signature(AUTH_TOKEN, srv.config.voice_url, form)
            tampered = await client.post(
                f"{srv.base}/twilio/voice",
                data={**form, "From": "+15559990000"},
                headers={"X-Twilio-Signature": signature},
            )
            assert tampered.status == 403

    asyncio.run(exercise())


@pytest.mark.parametrize("token", [None, "made-up-token"])
def test_stream_without_a_webhook_token_gets_no_session(token):
    async def exercise() -> None:
        async with Server() as srv, aiohttp.ClientSession() as client:
            ws = await client.ws_connect(f"{srv.base}/twilio/stream")
            await ws.send_json(_start(token))
            assert await _closes(ws)
        assert EchoSession.created == []

    asyncio.run(exercise())


def test_stream_token_is_single_use_and_tied_to_its_call():
    async def exercise() -> None:
        async with Server(max_calls=5) as srv, aiohttp.ClientSession() as client:
            token = _token(await (await srv.webhook(client)).text())
            wrong_call = await client.ws_connect(f"{srv.base}/twilio/stream")
            await wrong_call.send_json(_start(token, call_sid="CA-other"))
            assert await _closes(wrong_call)

            token = _token(await (await srv.webhook(client, call_sid="CA2")).text())
            first = await client.ws_connect(f"{srv.base}/twilio/stream")
            await first.send_json(_start(token, call_sid="CA2"))
            await first.send_json(_media())
            assert (await asyncio.wait_for(first.receive_json(), timeout=5))["event"] == "media"

            replay = await client.ws_connect(f"{srv.base}/twilio/stream")
            await replay.send_json(_start(token, call_sid="CA2"))
            assert await _closes(replay)
            await first.close()
        assert len(EchoSession.created) == 1

    asyncio.run(exercise())


def test_disallowed_caller_is_rejected_unanswered():
    async def exercise() -> None:
        policy = server.caller_policy(frozenset({"15559990000"}))
        async with Server(caller_policy=policy) as srv, aiohttp.ClientSession() as client:
            body = await (await srv.webhook(client)).text()
            assert '<Reject reason="rejected"/>' in body
            assert "<Connect>" not in body

    asyncio.run(exercise())


def test_a_call_holds_its_slot_until_it_ends():
    async def exercise() -> None:
        async with Server(max_calls=1) as srv, aiohttp.ClientSession() as client:
            token = _token(await (await srv.webhook(client, call_sid="CA1")).text())
            ws = await client.ws_connect(f"{srv.base}/twilio/stream")
            await ws.send_json(_start(token))
            await ws.send_json(_media())
            await asyncio.wait_for(ws.receive_json(), timeout=5)  # the call is live

            busy = await (await srv.webhook(client, call_sid="CA2")).text()
            assert '<Reject reason="busy"/>' in busy

            await ws.send_json({"event": "stop", "streamSid": "MZ1"})
            assert await _closes(ws)
            assert "<Connect>" in await (await srv.webhook(client, call_sid="CA3")).text()

    asyncio.run(exercise())


def test_calls_past_the_limit_are_declined_busy():
    async def exercise() -> None:
        async with Server(max_calls=1) as srv, aiohttp.ClientSession() as client:
            assert "<Connect>" in await (await srv.webhook(client, call_sid="CA1")).text()
            busy = await (await srv.webhook(client, call_sid="CA2")).text()
            assert '<Reject reason="busy"/>' in busy

    asyncio.run(exercise())


class EndingSession(EchoSession):
    """A provider session that dies right after connecting."""

    async def __anext__(self):
        await asyncio.sleep(0.05)
        return rt.SessionTerminated(state=rt.SessionState.FAILED, detail="provider went away")


def test_a_dead_session_hangs_up_instead_of_going_silent():
    async def exercise() -> None:
        async with Server(session_factory=EndingSession) as srv, aiohttp.ClientSession() as client:
            token = _token(await (await srv.webhook(client)).text())
            ws = await client.ws_connect(f"{srv.base}/twilio/stream")
            await ws.send_json(_start(token))
            assert await _closes(ws)
        assert EchoSession.created[0].closed

    asyncio.run(exercise())


class BrokenSession(EchoSession):
    async def connect(self, setup) -> None:
        raise RuntimeError("wss://provider.example/?key=fake-provider-key refused")


def test_a_failed_connect_ends_the_call_without_logging_the_error_text(caplog):
    async def exercise() -> None:
        async with (
            Server(session_factory=BrokenSession, max_calls=1) as srv,
            aiohttp.ClientSession() as client,
        ):
            token = _token(await (await srv.webhook(client)).text())
            ws = await client.ws_connect(f"{srv.base}/twilio/stream")
            await ws.send_json(_start(token))
            assert await _closes(ws)
            # The failed call gave its slot back.
            assert "<Connect>" in await (await srv.webhook(client, call_sid="CA2")).text()

    caplog.set_level("DEBUG")
    asyncio.run(exercise())
    assert "RuntimeError" in caplog.text
    assert "fake-provider-key" not in caplog.text


def test_a_socket_that_never_starts_is_closed(monkeypatch):
    monkeypatch.setattr(twilio, "STREAM_START_TIMEOUT_S", 0.2)

    async def exercise() -> None:
        async with Server() as srv, aiohttp.ClientSession() as client:
            ws = await client.ws_connect(f"{srv.base}/twilio/stream")
            assert await _closes(ws, timeout=3)

    asyncio.run(exercise())


def test_calls_are_cut_at_the_length_limit():
    async def exercise() -> None:
        async with Server(max_call_s=0.3) as srv, aiohttp.ClientSession() as client:
            token = _token(await (await srv.webhook(client)).text())
            ws = await client.ws_connect(f"{srv.base}/twilio/stream")
            await ws.send_json(_start(token))
            assert await _closes(ws, timeout=3)

    asyncio.run(exercise())


def test_serve_starts_on_loopback_and_stops_cleanly():
    config = twilio.TwilioConfig(public_base_url="https://voice.example.test", auth_token="x")

    async def exercise() -> None:
        task = asyncio.create_task(
            twilio.serve(config=config, session_factory=EchoSession, setup_factory=_setup, port=0)
        )
        await asyncio.sleep(0.3)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


def test_serve_refuses_to_bind_off_loopback():
    config = twilio.TwilioConfig(public_base_url="https://voice.example.test", auth_token="x")
    with pytest.raises(ValueError):
        asyncio.run(
            twilio.serve(
                config=config, session_factory=EchoSession, setup_factory=_setup, host="0.0.0.0"
            )
        )


# --- server configuration ------------------------------------------------

BASE_ENV = {
    "TALK_PUBLIC_BASE_URL": "https://voice.example.test",
    "TWILIO_AUTH_TOKEN": AUTH_TOKEN,
    "TALK_TWILIO_ALLOWED_CALLERS": CALLER,
    "GEMINI_API_KEY": "fake-gemini-key-91c2",
}


def test_server_config_requires_public_base_url():
    with pytest.raises(server.ConfigError):
        server.config_from_env({})


def test_server_config_requires_the_auth_token_with_no_unsigned_bypass():
    env = {"TALK_PUBLIC_BASE_URL": "https://voice.example.test", "TALK_TWILIO_ALLOW_UNSIGNED": "1"}
    with pytest.raises(server.ConfigError):
        server.config_from_env(env)


def test_server_config_refuses_plain_http_off_loopback_without_echoing_the_token():
    env = {**BASE_ENV, "TALK_PUBLIC_BASE_URL": "http://voice.example.test"}
    with pytest.raises(server.ConfigError) as caught:
        server.config_from_env(env)
    assert AUTH_TOKEN not in str(caught.value)


def test_allowed_callers_fail_closed():
    with pytest.raises(server.ConfigError):
        server.allowed_callers({})
    with pytest.raises(server.ConfigError):
        server.allowed_callers({"TALK_TWILIO_ALLOWED_CALLERS": "+1555, owner"})
    assert server.allowed_callers({"TALK_TWILIO_ALLOWED_CALLERS": "*"}) is None


def test_caller_policy_compares_digits_and_refuses_withheld_numbers():
    allowed = server.allowed_callers(
        {"TALK_TWILIO_ALLOWED_CALLERS": "+1 (555) 123-0001, +447700900001"}
    )
    policy = server.caller_policy(allowed)
    assert policy("+15551230001")
    assert policy("+447700900001")
    assert not policy("+15551230002")
    assert not policy("anonymous")
    assert not policy("")
    assert not policy(None)
    assert server.caller_policy(None)("anonymous")


def test_server_builds_a_gemini_session_and_rejects_others():
    with pytest.raises(server.ConfigError):
        server.build_session({"TALK_PROVIDER": "openai"})
    with pytest.raises(server.ConfigError):
        server.build_session({"TALK_PROVIDER": "gemini"})
    session = server.build_session({"TALK_PROVIDER": "gemini", "GEMINI_API_KEY": "fake-key"})
    assert isinstance(session, rt.RealtimeSession)


def test_server_setup_defaults_are_phone_shaped():
    setup = server.build_setup({})
    assert setup.voice == server.DEFAULT_VOICE
    assert "AI assistant" in setup.instructions
    assert setup.tools == ()


def test_check_is_marked_experimental_and_prints_no_secrets(monkeypatch, capsys):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    assert server.main(["--check"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("EXPERIMENTAL")
    assert "https://voice.example.test/twilio/voice" in out
    assert "wss://voice.example.test/twilio/stream" in out
    for secret in (AUTH_TOKEN, BASE_ENV["GEMINI_API_KEY"], CALLER, CALLER[1:]):
        assert secret not in out + err


def test_help_is_marked_experimental(capsys):
    with pytest.raises(SystemExit):
        server.main(["--help"])
    assert "EXPERIMENTAL" in capsys.readouterr().out


def test_server_refuses_a_non_loopback_host(monkeypatch, capsys):
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    assert server.main(["--check", "--host", "0.0.0.0"]) == 2
    assert "loopback" in capsys.readouterr().err


# --- isolation from normal Talk ------------------------------------------


def test_no_other_module_references_the_twilio_server():
    """Pressing Talk must not change: nothing outside these two files imports them."""

    sources = [
        *REPO_ROOT.glob("*.py"),
        *(REPO_ROOT / "dashboard").glob("*.py"),
        *(REPO_ROOT / "talk_phone").rglob("*.py"),
        REPO_ROOT / "plugin.yaml",
    ]
    offenders = [
        path.name
        for path in sources
        if not path.name.startswith("talk_twilio")
        and "talk_twilio" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_loading_the_plugin_and_talk_cli_does_not_import_talk_twilio():
    script = f"""
import importlib.util, sys, types
root = {str(REPO_ROOT)!r}
sys.modules["hermes_plugins"] = namespace = types.ModuleType("hermes_plugins")
namespace.__path__ = []
name = "hermes_plugins.hermes_talk"
spec = importlib.util.spec_from_file_location(
    name, root + "/__init__.py", submodule_search_locations=[root]
)
module = importlib.util.module_from_spec(spec)
module.__package__ = name
module.__path__ = [root]
sys.modules[name] = module
spec.loader.exec_module(module)
import talk_cli, talk_host, talk_realtime  # the flat imports tests and pip installs use
loaded = sorted(m for m in sys.modules if m.rsplit(".", 1)[-1].startswith("talk_twilio"))
print("LOADED", loaded)
assert "hermes_plugins.hermes_talk.talk_cli" in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "LOADED []" in result.stdout
