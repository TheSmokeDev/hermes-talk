"""Twilio Media Streams transport tests — codec, resampler, protocol, bridge.

No network: the bridge tests drive a scripted session against an in-memory
sender, mirroring how the Gemini adapter tests use a scripted socket. Every
token here is a fake short string.
"""

from __future__ import annotations

import array
import asyncio
import base64
import math
import struct

import pytest

import talk_realtime as rt
import talk_twilio as twilio


def _tone(rate: int, hz: float, samples: int, amplitude: int = 12000) -> bytes:
    values = array.array(
        "h",
        (int(amplitude * math.sin(2 * math.pi * hz * index / rate)) for index in range(samples)),
    )
    return values.tobytes()


def _samples(pcm: bytes) -> list[int]:
    values = array.array("h")
    values.frombytes(pcm)
    return list(values)


class ScriptedSession:
    """Minimal RealtimeSession stand-in."""

    state = rt.SessionState.CONNECTED

    def __init__(self, events):
        self.events = list(events)
        self.connected_with = None
        self.commands: list[rt.RealtimeCommand] = []
        self.closed = False

    async def connect(self, setup):
        self.connected_with = setup

    async def send(self, commands):
        self.commands.extend(commands)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)

    async def close(self):
        self.closed = True


def _setup() -> rt.SessionSetup:
    return rt.SessionSetup(model="gemini-3.1-flash-live-preview", voice="Kore", instructions="hi")


# --- codec ---------------------------------------------------------------


def test_ulaw_silence_decodes_to_zero():
    assert twilio.ulaw_to_pcm16(b"\xff") == struct.pack("<h", 0)


def test_ulaw_round_trip_stays_close():
    original = _samples(_tone(8000, 440.0, 800))
    encoded = twilio.pcm16_to_ulaw(_tone(8000, 440.0, 800))
    decoded = _samples(twilio.ulaw_to_pcm16(encoded))
    assert len(decoded) == len(original)
    # µ-law is logarithmic: allow a few percent of full scale.
    worst = max(abs(a - b) for a, b in zip(original, decoded, strict=True))
    assert worst < 1200


def test_pcm16_to_ulaw_rejects_odd_length():
    with pytest.raises(ValueError):
        twilio.pcm16_to_ulaw(b"\x00\x01\x02")


def test_empty_payloads_are_no_ops():
    assert twilio.ulaw_to_pcm16(b"") == b""
    assert twilio.pcm16_to_ulaw(b"") == b""


# --- resampler -----------------------------------------------------------


def test_resampler_doubles_length_for_8k_to_16k():
    resampler = twilio.PcmResampler(8000, 16000)
    out = resampler.process(_tone(8000, 300.0, 160))
    assert 310 <= len(_samples(out)) <= 330


def test_resampler_thirds_length_for_24k_to_8k():
    resampler = twilio.PcmResampler(24000, 8000)
    out = resampler.process(_tone(24000, 300.0, 480))
    assert 150 <= len(_samples(out)) <= 170


def test_resampler_carries_state_between_frames():
    resampler = twilio.PcmResampler(8000, 16000)
    first = _samples(resampler.process(_tone(8000, 300.0, 160)))
    second = _samples(resampler.process(_tone(8000, 300.0, 160)))
    assert first and second
    # No reset means the second frame continues at full amplitude.
    assert max(abs(value) for value in second) > 10000


def test_resampler_rejects_bad_rates():
    with pytest.raises(ValueError):
        twilio.PcmResampler(0, 8000)


# --- framing -------------------------------------------------------------


def test_frame_ulaw_pads_the_tail():
    frames = twilio.frame_ulaw(b"\x00" * 200)
    assert [len(frame) for frame in frames] == [160, 160]
    assert frames[-1].endswith(b"\xff")


def test_frame_ulaw_exact_multiple_is_not_padded():
    frames = twilio.frame_ulaw(b"\x00" * 320)
    assert [len(frame) for frame in frames] == [160, 160]


# --- protocol ------------------------------------------------------------


def test_parse_connected_and_start():
    assert twilio.parse_message('{"event": "connected"}').event == "connected"
    frame = twilio.parse_message(
        {
            "event": "start",
            "streamSid": "MZ123",
            "start": {"callSid": "CA123", "streamSid": "MZ123"},
        }
    )
    assert frame.stream_sid == "MZ123"
    assert frame.call_sid == "CA123"
    assert frame.parameters == {}


def test_parse_start_surfaces_custom_parameters():
    frame = twilio.parse_message(
        {
            "event": "start",
            "streamSid": "MZ1",
            "start": {"callSid": "CA1", "customParameters": {"token": "abc"}},
        }
    )
    assert frame.parameters == {"token": "abc"}


def test_parse_media_decodes_base64():
    payload = b"\xff" * 160
    frame = twilio.parse_message(
        {
            "event": "media",
            "streamSid": "MZ123",
            "media": {"payload": base64.b64encode(payload).decode("ascii")},
        }
    )
    assert frame.payload == payload


def test_parse_media_requires_stream_sid_and_payload():
    with pytest.raises(ValueError):
        twilio.parse_message({"event": "media", "media": {"payload": "//8="}})
    with pytest.raises(ValueError):
        twilio.parse_message({"event": "media", "streamSid": "MZ123", "media": {}})


def test_parse_rejects_unknown_events_and_bad_json():
    with pytest.raises(ValueError):
        twilio.parse_message({"event": "teleport"})
    with pytest.raises(ValueError):
        twilio.parse_message("{not json}")


def test_parse_rejects_invalid_base64():
    with pytest.raises(ValueError):
        twilio.parse_message({"event": "media", "streamSid": "MZ1", "media": {"payload": "!!!!"}})


def test_parse_rejects_oversized_payload():
    with pytest.raises(ValueError):
        twilio.parse_message(
            {"event": "media", "streamSid": "MZ1", "media": {"payload": "A" * 20000}}
        )


# --- twiml ---------------------------------------------------------------


def test_twiml_stream_escapes_and_wraps():
    document = twilio.twiml_stream("wss://example.test/twilio/stream")
    assert document.startswith("<?xml")
    assert '<Connect><Stream url="wss://example.test/twilio/stream"/></Connect>' in document


def test_twiml_stream_carries_escaped_parameters():
    document = twilio.twiml_stream("wss://example.test/s", {"token": 'a"<&', "empty": ""})
    assert '<Parameter name="token" value="a&quot;&lt;&amp;"/>' in document
    assert "empty" not in document


def test_twiml_reject_declines_without_answering():
    assert '<Response><Reject reason="rejected"/></Response>' in twilio.twiml_reject()
    assert '<Reject reason="busy"/>' in twilio.twiml_reject("busy")
    with pytest.raises(ValueError):
        twilio.twiml_reject("<Say>")


def test_twiml_stream_rejects_non_websocket_url():
    with pytest.raises(ValueError):
        twilio.twiml_stream("https://example.test/twilio/stream")


# --- signature -----------------------------------------------------------


def test_signature_matches_twilios_documented_vector():
    signature = twilio.expected_signature(
        "12345",
        "https://mycompany.com/myapp.php?foo=1&bar=2",
        {
            "CallSid": "CA1234567890ABCDE",
            "Caller": "+12349013030",
            "Digits": "1234",
            "From": "+12349013030",
            "To": "+18005551212",
        },
    )
    assert signature == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="


def test_validate_signature_rejects_wrong_or_missing():
    params = {"CallSid": "CA1", "From": "+15551234567"}
    url = "https://example.test/twilio/voice"
    good = twilio.expected_signature("token", url, params)
    assert twilio.validate_signature("token", url, params, good)
    assert not twilio.validate_signature("token", url, params, "nope")
    assert not twilio.validate_signature("token", url, params, None)
    assert not twilio.validate_signature("other", url, params, good)
    assert not twilio.validate_signature("token", url, params, "nön-ascii")


def test_config_derives_urls_and_requires_token():
    config = twilio.TwilioConfig(public_base_url="https://voice.example.test/", auth_token="token")
    assert config.voice_url == "https://voice.example.test/twilio/voice"
    assert config.stream_url == "wss://voice.example.test/twilio/stream"
    with pytest.raises(ValueError):
        twilio.TwilioConfig(public_base_url="https://voice.example.test")
    assert "token" not in repr(config).replace("auth_token", "")


@pytest.mark.parametrize(
    "url",
    [
        "http://voice.example.test",  # plain http off loopback
        "ftp://voice.example.test",
        "https://",
        "https://voice.example.test/?x=1",
        "https://voice.example.test/#frag",
        "",
    ],
)
def test_config_refuses_plain_or_malformed_public_urls(url):
    with pytest.raises(ValueError):
        twilio.TwilioConfig(public_base_url=url, auth_token="token")


def test_config_normalises_an_uppercase_scheme_for_the_stream():
    config = twilio.TwilioConfig(public_base_url="HTTPS://Voice.example.test/", auth_token="t")
    assert config.stream_url == "wss://Voice.example.test/twilio/stream"
    assert "<Connect>" in twilio.twiml_stream(config.stream_url)


def test_config_allows_plain_http_on_loopback_only():
    config = twilio.TwilioConfig(public_base_url="http://127.0.0.1:8767", auth_token="token")
    assert config.stream_url == "ws://127.0.0.1:8767/twilio/stream"


# --- stream tokens -------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_stream_token_is_single_use_and_bound_to_its_call():
    tokens = twilio.StreamTokens()
    token = tokens.issue("CA1")
    assert len(tokens) == 1
    assert tokens.redeem(token, "CA1") is True
    assert tokens.redeem(token, "CA1") is False  # reused
    other = tokens.issue("CA2")
    assert tokens.redeem(other, "CA1") is False  # another call's token
    assert tokens.redeem(other, "CA2") is False  # and it is spent now
    assert tokens.redeem(None, "CA1") is False
    assert tokens.redeem("guess", "CA1") is False
    assert len(tokens) == 0


def test_stream_token_expires():
    clock = FakeClock()
    tokens = twilio.StreamTokens(ttl_s=60, clock=clock)
    stale = tokens.issue("CA1")
    clock.now += 61
    assert tokens.redeem(stale, "CA1") is False
    tokens.issue("CA2")
    clock.now += 61
    assert len(tokens) == 0  # expired ones stop counting as calls in progress


# --- bridge --------------------------------------------------------------


def test_bridge_forwards_inbound_audio_as_pcm16():
    session = ScriptedSession([])
    frames: list[bytes] = []

    async def send_media(payload: bytes) -> None:
        frames.append(payload)

    async def run() -> None:
        bridge = twilio.TwilioCallBridge(session, send_media=send_media)
        await bridge.feed_media(b"\xff" * 160)

    asyncio.run(run())
    assert len(session.commands) == 1
    command = session.commands[0]
    assert isinstance(command, rt.AppendInputAudio)
    # 20 ms at the session's 24 kHz: 480 samples. (The Gemini facade expects
    # 24 kHz and downsamples to 16 kHz itself; feeding it 16 kHz would reach
    # Gemini sped up by half.)
    assert len(command.data) == 480 * 2


def test_bridge_sends_output_audio_in_twilio_frames():
    session = ScriptedSession([rt.OutputAudio(data=_tone(24000, 300.0, 2400))])
    frames: list[bytes] = []

    async def send_media(payload: bytes) -> None:
        frames.append(payload)

    async def run() -> None:
        bridge = twilio.TwilioCallBridge(session, send_media=send_media)
        await bridge.run()

    asyncio.run(run())
    assert frames
    assert all(len(frame) == twilio.TWILIO_FRAME_BYTES for frame in frames)


@pytest.mark.parametrize("event", [rt.OutputInterrupted(), rt.SpeechStarted()])
def test_bridge_clears_playback_on_interruption(event):
    session = ScriptedSession([event])
    cleared = []

    async def send_media(payload: bytes) -> None:
        raise AssertionError("no audio expected")

    async def clear() -> None:
        cleared.append(True)

    async def run() -> None:
        bridge = twilio.TwilioCallBridge(session, send_media=send_media, clear_playback=clear)
        await bridge.run()

    asyncio.run(run())
    assert cleared == [True]


@pytest.mark.parametrize(
    "terminal",
    [
        rt.SessionTerminated(state=rt.SessionState.FAILED, detail="gone"),
        rt.ProviderFailure(detail="dead socket", terminal=True),
    ],
)
def test_bridge_run_returns_when_the_session_ends(terminal):
    after = rt.OutputAudio(data=_tone(24000, 300.0, 2400))
    session = ScriptedSession([terminal, after])
    frames: list[bytes] = []

    async def send_media(payload: bytes) -> None:
        frames.append(payload)

    async def run() -> None:
        await twilio.TwilioCallBridge(session, send_media=send_media).run()

    asyncio.run(run())
    assert frames == []
    assert session.events == [after]  # stopped at the terminal event


def test_bridge_keeps_going_after_a_non_terminal_failure():
    session = ScriptedSession(
        [rt.ProviderFailure(detail="one bad frame"), rt.OutputAudio(data=_tone(24000, 300.0, 480))]
    )
    frames: list[bytes] = []

    async def send_media(payload: bytes) -> None:
        frames.append(payload)

    async def run() -> None:
        await twilio.TwilioCallBridge(session, send_media=send_media).run()

    asyncio.run(run())
    assert frames


def test_bridge_ignores_inbound_after_close():
    session = ScriptedSession([])
    seen = []

    async def send_media(payload: bytes) -> None:
        seen.append(payload)

    async def run() -> None:
        bridge = twilio.TwilioCallBridge(session, send_media=send_media)
        await bridge.close()
        await bridge.feed_media(b"\xff" * 160)

    asyncio.run(run())
    assert session.closed is True
    assert session.commands == []
