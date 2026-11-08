"""End-to-end test of the Twilio transport over a real socket.

No Twilio account and no provider: an echo session returns audio as soon as it
receives audio, so the test exercises the whole path — TwiML, WebSocket upgrade,
frame parsing, µ-law decode, resample, session command, output resample, µ-law
encode, 20 ms framing — against a live aiohttp server on an ephemeral port.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

aiohttp = pytest.importorskip("aiohttp")
pytest.importorskip("aiohttp.web")

import talk_realtime as rt  # noqa: E402
import talk_twilio as twilio  # noqa: E402
import talk_twilio_server as server  # noqa: E402


class EchoSession:
    """Returns one short response as soon as it is given caller audio."""

    state = rt.SessionState.CONNECTED

    def __init__(self) -> None:
        self.events: asyncio.Queue[rt.RealtimeEvent] = asyncio.Queue()

    async def connect(self, setup) -> None:
        return None

    async def send(self, commands) -> None:
        for command in commands:
            if isinstance(command, rt.AppendInputAudio):
                # 100 ms of 24 kHz PCM: three Twilio frames once downsampled.
                await self.events.put(rt.OutputAudio(data=b"\x00\x01" * 2400))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.events.get()

    async def close(self) -> None:
        return None


def _setup(caller=None) -> rt.SessionSetup:
    return rt.SessionSetup(model="gemini-3.1-flash-live-preview", voice="Kore", instructions="hi")


async def _exercise() -> None:
    config = twilio.TwilioConfig(
        public_base_url="http://127.0.0.1", auth_token=None, require_signature=False
    )
    app = twilio.create_app(
        config=config,
        session_factory=lambda caller: EchoSession(),
        setup_factory=lambda caller: _setup(),
        aiohttp_module=aiohttp,
    )
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as client:
            response = await client.get(f"{base}/twilio/voice")
            body = await response.text()
            assert response.status == 200
            assert "<Connect><Stream url=" in body

            ws = await client.ws_connect(f"{base}/twilio/stream")
            await ws.send_json({"event": "connected"})
            await ws.send_json({"event": "start", "streamSid": "MZ1", "start": {"callSid": "CA1"}})
            await ws.send_json(
                {
                    "event": "media",
                    "streamSid": "MZ1",
                    "media": {"payload": base64.b64encode(b"\xff" * 160).decode("ascii")},
                }
            )

            first = await asyncio.wait_for(ws.receive_json(), timeout=5)
            assert first["event"] == "media"
            assert first["streamSid"] == "MZ1"
            payload = base64.b64decode(first["media"]["payload"])
            assert len(payload) == twilio.TWILIO_FRAME_BYTES

            await ws.send_json({"event": "stop", "streamSid": "MZ1"})
            await ws.close()
    finally:
        await runner.cleanup()


def test_transport_round_trip_over_a_socket():
    asyncio.run(_exercise())


def test_server_config_requires_public_base_url():
    with pytest.raises(server.ConfigError):
        server.config_from_env({})


def test_server_config_requires_token_unless_allowed():
    with pytest.raises(server.ConfigError):
        server.config_from_env({"TALK_PUBLIC_BASE_URL": "https://voice.example.test"})
    config = server.config_from_env(
        {
            "TALK_PUBLIC_BASE_URL": "https://voice.example.test",
            "TALK_TWILIO_ALLOW_UNSIGNED": "1",
        }
    )
    assert config.require_signature is False


def test_server_builds_a_gemini_session_and_rejects_others():
    with pytest.raises(server.ConfigError):
        server.build_session(None, {"TALK_PROVIDER": "openai"})
    with pytest.raises(server.ConfigError):
        server.build_session(None, {"TALK_PROVIDER": "gemini"})
    session = server.build_session(None, {"TALK_PROVIDER": "gemini", "GEMINI_API_KEY": "fake-key"})
    assert isinstance(session, rt.RealtimeSession)


def test_server_setup_defaults_are_phone_shaped():
    setup = server.build_setup(None, {})
    assert setup.voice == server.DEFAULT_VOICE
    assert "Phone" in setup.instructions or "phone" in setup.instructions


# --- caller policy and demo mode -----------------------------------------


def test_owner_is_always_admitted_and_strangers_are_not(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DEMO_STATE_FILE", tmp_path / "demo.json")
    monkeypatch.setenv("TALK_OWNER_NUMBERS", "+447700900001")
    assert server.caller_policy("+447700900001")
    assert server.caller_policy("447700900001")
    assert not server.caller_policy("+447700900002")
    assert not server.caller_policy(None)


def test_demo_switch_admits_strangers_until_it_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DEMO_STATE_FILE", tmp_path / "demo.json")
    monkeypatch.setenv("TALK_OWNER_NUMBERS", "+447700900001")
    server.set_demo(True, minutes=45)
    assert server.demo_active()
    assert server.caller_policy("+447700900002")
    server.set_demo(False)
    assert not server.demo_active()
    assert not server.caller_policy("+447700900002")


def test_expired_demo_state_refuses_strangers(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DEMO_STATE_FILE", tmp_path / "demo.json")
    server.DEMO_STATE_FILE.write_text(
        '{"enabled": true, "until": "2000-01-01T00:00:00"}', encoding="utf-8"
    )
    assert not server.demo_active()


def test_stranger_gets_public_persona_and_no_memory_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("TALK_OWNER_NUMBERS", "+447700900001")
    monkeypatch.setattr(server, "PUBLIC_PROFILE_FILE", tmp_path / "public.md")
    server.PUBLIC_PROFILE_FILE.write_text("Studies CS at QMUL.", encoding="utf-8")
    setup = server.build_setup("+447700900002", {})
    names = [tool.name for tool in setup.tools]
    assert "lookup_memory" not in names
    assert "take_message" not in names
    assert "handover_to_stranger" in names
    assert "QMUL" in setup.instructions
    assert "public" in setup.instructions.lower()


def test_owner_gets_private_briefing_and_full_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("TALK_OWNER_NUMBERS", "+447700900001")
    monkeypatch.setattr(server, "BRIEFING_FILE", tmp_path / "brief.md")
    server.BRIEFING_FILE.write_text("Defence due 30 November.", encoding="utf-8")
    setup = server.build_setup("+447700900001", {})
    names = [tool.name for tool in setup.tools]
    assert {"lookup_memory", "take_message", "handover_to_stranger"} <= set(names)
    assert "30 November" in setup.instructions


def test_twiml_carries_the_caller_and_can_refuse():
    document = twilio.twiml_stream("wss://x.test/s", {"caller": "+447700900002"})
    assert '<Parameter name="caller" value="+447700900002"/>' in document
    refusal = twilio.twiml_hangup("not now")
    assert "<Say>not now</Say>" in refusal and "<Hangup/>" in refusal


def test_demo_page_shows_state():
    on = server.demo_page({"until": "2026-11-08T18:00:00"}, True)
    off = server.demo_page({"until": None}, False)
    assert "ON - strangers can call" in on
    assert "OFF - only you get through" in off
