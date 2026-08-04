"""Discord voice as an audio device.

What is being proved: the bridge wears :class:`talk_audio.DuplexAudio`'s
exact surface (so the session above it cannot tell which room it is in),
the 48k-stereo/24k-mono conversions are lossless enough to round-trip a
tone, and every reach into the host's internals degrades to a spoken
refusal instead of a traceback.
"""

from __future__ import annotations

import array
import asyncio
import math
import queue
import sys
import threading
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
    out, remainder = talk_discord.discord_to_session(pcm)
    assert len(out) == len(pcm) // 4
    assert remainder == b""


def test_downsample_carries_a_partial_group_instead_of_dropping_it():
    # A dropped remainder transposes L/R for every later sample — the
    # channels stay swapped for the rest of the stream.
    whole = _tone(8, rate=48_000, stereo=True)
    joined, tail = talk_discord.discord_to_session(whole)
    assert tail == b""
    # Split mid-group (5 samples in, not a multiple of 4).
    first, carry = talk_discord.discord_to_session(whole[:10])
    assert carry, "a partial group must be carried, not dropped"
    second, tail2 = talk_discord.discord_to_session(whole[10:], carry=carry)
    assert first + second == joined
    assert tail2 == b""


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
    back, _ = talk_discord.discord_to_session(up)
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
    assert talk_discord.discord_to_session(b"") == (b"", b"")
    odd_out, odd_carry = talk_discord.discord_to_session(b"\x01")
    assert odd_out == b"" and odd_carry == b"\x01"
    assert talk_discord.session_to_discord(b"") == (b"", None)
    out, carry = talk_discord.session_to_discord(b"\x01")
    assert out == b"" and carry is None


# -- the host lookup ----------------------------------------------------------


def _fake_host(monkeypatch, *, clients=None, receivers=None, runner=True, adapter=True):
    """Install a fake gateway/adapter the way the host exposes them."""

    # The host keeps Platform in gateway.config — NOT in `models`, which is a
    # different project's convention and does not exist here. Pinning the
    # wrong path made every lookup fail and refuse on a healthy gateway.
    platform = types.SimpleNamespace(DISCORD="discord")
    gateway_config = types.ModuleType("gateway.config")
    gateway_config.Platform = platform
    monkeypatch.setitem(sys.modules, "gateway.config", gateway_config)
    monkeypatch.setitem(sys.modules, "models", None)

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


# -- the host handoff (what offline tests missed the first time) --------------


class _FakeConnection:
    """discord.py stores the CALLBACK OBJECT and calls what it stored."""

    def __init__(self) -> None:
        self.callbacks: list = []

    def add_socket_listener(self, cb) -> None:
        self.callbacks.append(cb)

    def remove_socket_listener(self, cb) -> None:
        self.callbacks.remove(cb)  # raises if it was never registered

    def deliver(self, data: bytes) -> None:
        for cb in list(self.callbacks):
            cb(data)


class _FakeReceiver:
    def __init__(self) -> None:
        self._buffers: dict = {}
        self._lock = threading.Lock()
        self._running = True  # the host clears this in its own teardown
        self.seen: list = []

    def _on_packet(self, data: bytes) -> None:
        self.seen.append(data)
        with self._lock:
            self._buffers.setdefault(1, bytearray()).extend(data)


class _FakeVoiceClient:
    def __init__(self, connection) -> None:
        self._connection = connection
        self.playing = None

    def is_playing(self) -> bool:
        return self.playing is not None

    def play(self, source) -> None:
        self.playing = source

    def stop(self) -> None:
        self.playing = None


def _wired_host(monkeypatch):
    connection = _FakeConnection()
    receiver = _FakeReceiver()
    connection.add_socket_listener(receiver._on_packet)  # as the host does
    vc = _FakeVoiceClient(connection)
    adapter = _fake_host(monkeypatch, clients={7: vc}, receivers={7: receiver})
    adapter._reset_voice_timeout = lambda gid: adapter.__dict__.setdefault(
        "timer_resets", []
    ).append(gid)
    adapter._voice_mode_getter = lambda chat_id: "all"
    adapter._voice_mixers = {7: "the-host-mixer"}

    async def _real_play(guild_id, path):  # the host's speech chokepoint
        adapter.__dict__.setdefault("host_played", []).append(path)
        return True

    adapter.play_in_voice_channel = _real_play
    return connection, receiver, vc, adapter


def test_tap_is_registered_not_just_assigned(monkeypatch):
    # THE bug that made the first cut unable to work: discord.py appends the
    # bound method OBJECT to its listener list and calls what it stored, so
    # rebinding the attribute alone taps nothing and the call hears silence
    # with no error anywhere.
    connection, receiver, _vc, _adapter = _wired_host(monkeypatch)
    original = connection.callbacks[0]  # the exact object the host registered
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()

    assert original not in connection.callbacks, "old listener still wired"
    assert len(connection.callbacks) == 1, "tap must replace, not stack"

    connection.deliver(b"" * 8)
    assert receiver.seen, "the host's own handler must still run"
    assert bridge.read_input_chunk() is not None, "the tap never fired"

    bridge.stop()
    assert connection.callbacks == [original], "original not restored"


def test_stop_restores_the_host_exactly(monkeypatch):
    connection, receiver, _vc, adapter = _wired_host(monkeypatch)
    original = connection.callbacks[0]
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()
    assert adapter._voice_mixers == {}, "host mixer must be parked while we own audio"
    assert adapter._voice_mode_getter("c") == "off", "host speech must be parked"

    bridge.stop()
    assert connection.callbacks == [original]
    # Equality, not identity: Python mints a fresh bound-method object on
    # every attribute access, so `a.m is a.m` is False even untouched.
    assert receiver._on_packet == original
    assert adapter._voice_mode_getter("c") == "all", "mode getter not handed back"
    # The mixer is deliberately NOT handed back: stopping playback makes
    # discord.py close it permanently, and a closed mixer still reports
    # speech_active — so returning it would stall every later host reply
    # for the full playback timeout and drop it.
    assert adapter._voice_mixers == {}, "a closed mixer must not be handed back"


def test_capture_rearms_the_hosts_inactivity_timer(monkeypatch):
    # The host auto-leaves the channel when its silence gate sees nothing —
    # and we drain the very buffers that gate measures. Without re-arming,
    # it pulls the bot out mid-conversation.
    connection, _receiver, _vc, adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge._loop = types.SimpleNamespace(
        call_soon_threadsafe=lambda fn, *a: fn(*a)  # run inline for the test
    )
    bridge.start()
    bridge._loop = types.SimpleNamespace(call_soon_threadsafe=lambda fn, *a: fn(*a))
    connection.deliver(b"" * 8)
    assert adapter.__dict__.get("timer_resets") == [7]
    bridge.stop()


def test_played_ms_counts_what_was_heard_not_what_was_queued(monkeypatch):
    # The model streams far faster than realtime. Counting at queue time
    # tells a barge-in truncate the operator heard everything generated.
    _connection, _receiver, vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()

    bridge.queue_playback(_tone(2400, rate=24_000))  # 100ms of audio, queued
    assert bridge.played_ms == 0, "nothing has been heard yet"

    vc.playing.read()  # the host's player pulls exactly one 20ms frame
    assert bridge.played_ms == 20
    bridge.stop()


def test_underrun_never_retires_the_player(monkeypatch):
    _connection, _receiver, vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()
    frame = vc.playing.read()  # nothing queued
    assert frame == talk_discord.SILENCE_FRAME
    assert frame != b""
    bridge.stop()


def test_host_speech_is_parked_at_the_real_chokepoint(monkeypatch):
    # Parking the adapter's voice-mode getter does NOT suppress replies —
    # that getter is a read-only view onto the runner's dict, consulted only
    # by the inactivity timer. Every route the host speaks through funnels
    # into play_in_voice_channel, and it blocks on our continuous source
    # for the full playback timeout before force-stopping us.
    _connection, _receiver, _vc, adapter = _wired_host(monkeypatch)
    original_play = adapter.play_in_voice_channel
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()

    spoke = asyncio.run(adapter.play_in_voice_channel(7, "reply.mp3"))
    assert spoke is False, "host speech must be refused while we own the channel"
    assert "host_played" not in adapter.__dict__, "host still reached the speaker"

    bridge.stop()
    assert adapter.play_in_voice_channel == original_play, "chokepoint not handed back"
    assert asyncio.run(adapter.play_in_voice_channel(7, "after.mp3")) is True


def test_a_failed_swap_never_leaves_the_host_deaf(monkeypatch):
    # If the swap half-completes, the original listener must go back — a
    # host with no receive callback is deaf for the life of the connection
    # with no path back.
    connection, _receiver, _vc, _adapter = _wired_host(monkeypatch)
    original = connection.callbacks[0]

    def explode(cb):
        raise RuntimeError("listener registry rejected us")

    monkeypatch.setattr(connection, "add_socket_listener", explode)
    bridge = talk_discord.DiscordAudio(7)
    with pytest.raises(talk_audio.TalkAudioError, match="couldn't listen in"):
        bridge.start()
    assert connection.callbacks == [original], "host left without a receive callback"


def test_stop_does_not_rewake_a_torn_down_receiver(monkeypatch):
    # If the host tore the receiver down first, re-registering an inert
    # callback keeps discord.py's reader thread awake forever.
    connection, receiver, _vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()
    receiver._running = False  # the host's own teardown ran
    bridge.stop()
    assert connection.callbacks == [], "re-registered a listener for a dead receiver"


def test_capture_remainders_do_not_bleed_between_speakers(monkeypatch):
    # One speaker's partial sample group prepended to another's audio would
    # shift it and transpose L/R for that chunk.
    _connection, receiver, _vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()
    with receiver._lock:
        receiver._buffers[1] = bytearray(bytes([1, 0]) * 5)  # 5 samples: 1 group + 1 left
        receiver._buffers[2] = bytearray(bytes([2, 0]) * 5)
    bridge._drain_receiver(receiver)
    assert set(bridge._capture_remainder) == {1, 2}
    assert bridge._capture_remainder[1] != bridge._capture_remainder[2]
    bridge.stop()


def test_adapter_is_found_when_only_the_key_name_matches(monkeypatch):
    # Last-resort lookup: a host that moves or renames the Platform enum
    # must still resolve, because "I could not import your enum" is
    # indistinguishable from "Discord is not running" to an operator.
    monkeypatch.setitem(sys.modules, "gateway.config", None)
    monkeypatch.setitem(sys.modules, "models", None)
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", None)

    class _PlatformKey:  # an enum-like key: hashable, carries .value
        value = "discord"

    fake_adapter = types.SimpleNamespace(
        _voice_clients={5: object()}, _voice_receivers={5: object()}
    )
    runner = types.SimpleNamespace(adapters={_PlatformKey(): fake_adapter})
    module = types.ModuleType("gateway.run")
    module._gateway_runner_ref = lambda: runner
    package = types.ModuleType("gateway")
    package.run = module
    monkeypatch.setitem(sys.modules, "gateway", package)
    monkeypatch.setitem(sys.modules, "gateway.run", module)

    bridge = talk_discord.resolve_voice_bridge()
    assert bridge["adapter"] is fake_adapter
