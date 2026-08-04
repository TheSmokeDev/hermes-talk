"""Discord voice as an audio device.

What is being proved: the bridge wears :class:`talk_audio.DuplexAudio`'s
exact surface (so the session above it cannot tell which room it is in),
the 48k-stereo/24k-mono conversions are lossless enough to round-trip a
tone, and every reach into the host's internals degrades to a spoken
refusal instead of a traceback.
"""

from __future__ import annotations

import array
import math
import queue
import sys
import types

import pytest

import talk_audio
import talk_discord


@pytest.fixture(autouse=True)
def _clean():
    talk_discord.reset_for_tests()
    yield
    talk_discord.reset_for_tests()


def _tone(samples: int, *, rate: int, freq: float = 440.0, stereo: bool = False) -> bytes:
    out = array.array("h")
    for i in range(samples):
        value = int(12000 * math.sin(2 * math.pi * freq * i / rate))
        out.append(value)
        if stereo:
            out.append(value)
    return out.tobytes()


# -- the interface contract ---------------------------------------------------


def test_bridge_wears_the_audio_device_surface():
    # The session calls exactly these seven. If DuplexAudio grows an eighth,
    # this test is what tells us the Discord room needs it too.
    surface = (
        "start",
        "stop",
        "read_input_chunk",
        "queue_playback",
        "drain_playback",
        "played_ms",
        "reset_played_ms",
    )
    bridge = talk_discord.DiscordAudio()
    for name in surface:
        assert hasattr(talk_audio.DuplexAudio, name), f"DuplexAudio lost {name}"
        assert hasattr(bridge, name), f"DiscordAudio is missing {name}"


# -- rate conversion ----------------------------------------------------------


def test_downsample_halves_the_sample_count_and_collapses_channels():
    # 4 stereo frames (8 samples) of 48k -> 2 mono samples of 24k.
    pcm = _tone(4, rate=48_000, stereo=True)
    out = talk_discord.discord_to_session(pcm)
    assert len(out) == len(pcm) // 4


def test_upsample_doubles_and_duplicates_to_stereo():
    pcm = _tone(10, rate=24_000)
    out, carry = talk_discord.session_to_discord(pcm)
    assert len(out) == len(pcm) * 4
    assert carry is not None


def test_a_tone_survives_the_round_trip():
    # Not bit-exact — the box filter and the interpolator both smooth — but
    # the signal must still be the same tone at the same amplitude, not
    # noise and not silence.
    original = _tone(480, rate=24_000, freq=440.0)
    up, _ = talk_discord.session_to_discord(original)
    back = talk_discord.discord_to_session(up)
    assert len(back) == len(original)

    src = array.array("h")
    src.frombytes(original)
    dst = array.array("h")
    dst.frombytes(back)
    src_rms = math.sqrt(sum(s * s for s in src) / len(src))
    dst_rms = math.sqrt(sum(s * s for s in dst) / len(dst))
    assert dst_rms > src_rms * 0.8, "round trip lost the signal"
    assert dst_rms < src_rms * 1.2, "round trip added energy"


def test_carry_makes_chunk_boundaries_continuous():
    # Feeding two halves with the carry threaded must match feeding the
    # whole thing — otherwise every chunk boundary is a click.
    whole = _tone(64, rate=24_000)
    joined, _ = talk_discord.session_to_discord(whole)
    first, carry = talk_discord.session_to_discord(whole[:64])
    second, _ = talk_discord.session_to_discord(whole[64:], carry=carry)
    assert first + second == joined


def test_conversions_survive_empty_and_odd_input():
    assert talk_discord.discord_to_session(b"") == b""
    assert talk_discord.discord_to_session(b"\x01") == b""
    assert talk_discord.session_to_discord(b"") == (b"", None)
    out, carry = talk_discord.session_to_discord(b"\x01")
    assert out == b"" and carry is None


# -- the host lookup ----------------------------------------------------------


def _fake_host(monkeypatch, *, clients=None, receivers=None, runner=True, adapter=True):
    """Install a fake gateway/adapter the way the host exposes them."""

    platform = types.SimpleNamespace(DISCORD="discord")
    monkeypatch.setitem(sys.modules, "models", types.SimpleNamespace(Platform=platform))

    fake_adapter = types.SimpleNamespace(
        _voice_clients=clients if clients is not None else {},
        _voice_receivers=receivers if receivers is not None else {},
    )
    fake_runner = types.SimpleNamespace(
        adapters={platform.DISCORD: fake_adapter} if adapter else {}
    )
    module = types.ModuleType("gateway.run")
    module._gateway_runner_ref = (lambda: fake_runner) if runner else (lambda: None)
    package = types.ModuleType("gateway")
    package.run = module
    monkeypatch.setitem(sys.modules, "gateway", package)
    monkeypatch.setitem(sys.modules, "gateway.run", module)
    return fake_adapter


def test_no_host_refuses_in_one_sentence(monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway", None)
    monkeypatch.setitem(sys.modules, "gateway.run", None)
    with pytest.raises(talk_discord.TalkDiscordError, match="no Hermes gateway"):
        talk_discord.resolve_voice_bridge()


def test_gateway_not_running_refuses(monkeypatch):
    _fake_host(monkeypatch, runner=False)
    with pytest.raises(talk_discord.TalkDiscordError, match="isn't running"):
        talk_discord.resolve_voice_bridge()


def test_discord_adapter_absent_refuses(monkeypatch):
    _fake_host(monkeypatch, adapter=False)
    with pytest.raises(talk_discord.TalkDiscordError, match="isn't loaded"):
        talk_discord.resolve_voice_bridge()


def test_unknown_internals_refuse_rather_than_crash(monkeypatch):
    adapter = _fake_host(monkeypatch)
    del adapter._voice_clients  # a host that renamed its internals
    with pytest.raises(talk_discord.TalkDiscordError, match="shape I know"):
        talk_discord.resolve_voice_bridge()


def test_not_in_a_channel_says_so(monkeypatch):
    _fake_host(monkeypatch, clients={})
    with pytest.raises(talk_discord.TalkDiscordError, match="not in a voice channel"):
        talk_discord.resolve_voice_bridge()


def test_single_channel_is_resolved_without_naming_it(monkeypatch):
    vc = object()
    _fake_host(monkeypatch, clients={42: vc}, receivers={42: object()})
    bridge = talk_discord.resolve_voice_bridge()
    assert bridge["guild_id"] == 42
    assert bridge["voice_client"] is vc


def test_two_channels_demand_a_choice(monkeypatch):
    _fake_host(monkeypatch, clients={1: object(), 2: object()})
    with pytest.raises(talk_discord.TalkDiscordError, match="more than one"):
        talk_discord.resolve_voice_bridge()


# -- the outbound source ------------------------------------------------------


def test_source_never_returns_empty_on_underrun():
    # An empty read retires the host's player thread and the call goes mute
    # for good, with no error anywhere. Silence is the only safe underrun.
    source = talk_discord._RealtimeSource(queue.Queue())
    frame = source.read()
    assert frame == talk_discord.SILENCE_FRAME
    assert len(frame) == talk_discord.DISCORD_FRAME_BYTES


def test_source_reassembles_across_chunk_boundaries():
    frames: queue.Queue = queue.Queue()
    # Two chunks that do not align to the 20ms frame size.
    frames.put(b"\x01\x02" * 1000)
    frames.put(b"\x03\x04" * 1000)
    source = talk_discord._RealtimeSource(frames)
    first = source.read()
    assert len(first) == talk_discord.DISCORD_FRAME_BYTES
    assert source.frames_served == 1


def test_source_is_not_opus():
    assert talk_discord._RealtimeSource(queue.Queue()).is_opus() is False


# -- session lifecycle --------------------------------------------------------


def test_status_with_no_session():
    assert "No live voice session" in talk_discord.session_status()


def test_stop_with_no_session_says_so():
    assert "not in a voice session" in talk_discord.stop_session()


def test_start_outside_a_loop_points_at_the_terminal():
    reply = talk_discord.start_session()
    assert "hermes talk" in reply
