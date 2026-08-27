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
import talk_cli
import talk_core_session
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


def test_bridge_loss_exits_session_cancels_socket_clears_slot_and_notifies(monkeypatch):
    async def scenario():
        _connection, _receiver, _vc, adapter = _wired_host(monkeypatch)
        delivered: list[str] = []

        class _Channel:
            async def send(self, content):
                delivered.append(content)

        adapter._voice_text_channels = {7: 123}
        adapter._client = types.SimpleNamespace(get_channel=lambda channel_id: _Channel())

        class _FailingAudio:
            played_ms = 0

            def __init__(self):
                self._bridge_failure = None
                self.stopped = False

            def start(self):
                pass

            def stop(self):
                self.stopped = True

            def read_input_chunk(self):
                self._bridge_failure = "the Discord voice connection changed"
                raise talk_audio.TalkAudioError(self._bridge_failure)

            def queue_playback(self, _pcm):  # pragma: no cover - no server audio
                raise AssertionError("playback reached a dead bridge")

            def drain_playback(self):
                pass

            def reset_played_ms(self):
                pass

        audio = _FailingAudio()
        monkeypatch.setattr(talk_discord, "DiscordAudio", lambda _guild_id: audio)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("TALK_VOICE", raising=False)
        monkeypatch.setattr(talk_cli.talk_apiserver, "warm_in_background", lambda: None)
        monkeypatch.setattr(
            talk_cli,
            "_mint_session",
            lambda *a, **k: types.SimpleNamespace(client_secret="ephemeral"),
        )

        class _BlockingWebSocket:
            def __init__(self):
                self.receive_started = False
                self.receive_cancelled = False
                self.exited = False
                self._forever = asyncio.Event()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                self.exited = True

            async def send_json(self, _message):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                self.receive_started = True
                try:
                    await self._forever.wait()
                except asyncio.CancelledError:
                    self.receive_cancelled = True
                    raise
                raise StopAsyncIteration  # pragma: no cover - event is never set

        ws = _BlockingWebSocket()

        class _ClientSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                pass

            def ws_connect(self, *_args, **_kwargs):
                return ws

        monkeypatch.setattr(
            talk_cli,
            "_import_aiohttp",
            lambda: types.SimpleNamespace(
                ClientSession=_ClientSession,
                WSMsgType=types.SimpleNamespace(TEXT=object()),
            ),
        )

        reply = talk_discord.start_session(7)
        assert "Starting limited legacy provider-owned voice" in reply
        task = talk_discord._SESSION["task"]
        for _ in range(20):
            if task.done() and not talk_discord._SESSION:
                break
            await asyncio.sleep(0)
        for _ in range(5):  # let the done callback's notification task run
            await asyncio.sleep(0)

        assert task.result() == 1
        assert ws.receive_started
        assert ws.receive_cancelled, "bridge loss left socket receive activity running"
        assert ws.exited
        assert audio.stopped
        assert talk_discord._SESSION == {}, "failed session still owns the slot"
        assert len(delivered) == 1
        assert "voice session failed" in delivered[0].lower()
        assert "connection changed" in delivered[0]

    asyncio.run(scenario())


def test_failure_receipt_falls_back_to_the_adapter_send_path(monkeypatch):
    async def scenario():
        _connection, _receiver, _vc, adapter = _wired_host(monkeypatch)
        delivered: list[tuple[str, str]] = []

        class _BrokenChannel:
            async def send(self, _content):
                raise RuntimeError("stale cached channel")

        adapter._voice_text_channels = {7: 123}
        adapter._client = types.SimpleNamespace(get_channel=lambda channel_id: _BrokenChannel())

        async def adapter_send(chat_id, content):
            delivered.append((chat_id, content))
            return types.SimpleNamespace(success=True)

        adapter.send = adapter_send

        async def fail_from_bridge(audio, **_kwargs):
            audio._bridge_failure = "the Discord voice connection changed"
            return 1

        monkeypatch.setattr(talk_cli, "run_talk_session", fail_from_bridge)
        talk_discord.start_session(7)
        for _ in range(5):
            await asyncio.sleep(0)

        assert delivered and delivered[0][0] == "123"
        assert "connection changed" in delivered[0][1]

    asyncio.run(scenario())


def test_undeliverable_failure_receipt_is_preserved_for_status(monkeypatch):
    async def scenario():
        _connection, _receiver, _vc, adapter = _wired_host(monkeypatch)
        adapter._voice_text_channels = {}  # no linked text room is available

        async def fail_from_bridge(audio, **_kwargs):
            audio._bridge_failure = "the Discord voice credentials changed"
            return 1

        monkeypatch.setattr(talk_cli, "run_talk_session", fail_from_bridge)
        talk_discord.start_session(7)
        for _ in range(5):
            await asyncio.sleep(0)

        assert talk_discord._SESSION == {}
        status = talk_discord.session_status()
        assert "last voice session failed" in status.lower()
        assert "credentials changed" in status

    asyncio.run(scenario())


def test_status_with_no_session():
    assert "No live voice session" in talk_discord.session_status()


def test_legacy_join_receipt_and_status_label_the_limited_lane_and_core_parity(monkeypatch):
    async def scenario():
        _wired_host(monkeypatch)
        gate = asyncio.Event()

        async def keep_open(audio, **_kwargs):
            await gate.wait()
            return 0

        monkeypatch.setattr(talk_cli, "run_talk_session", keep_open)

        receipt = talk_discord.start_session(7)
        status = talk_discord.session_status()

        for text in (receipt, status):
            assert "limited legacy provider-owned" in text.lower()
            assert "/talk core join" in text.lower()
        assert len(receipt) < 240
        gate.set()
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_both_session_call_sites_pass_the_discord_lane(monkeypatch):
    """The lane line is only true if every path names its own room (#64)."""

    async def scenario():
        _wired_host(monkeypatch)
        gate = asyncio.Event()
        seen: list[dict] = []

        async def keep_open(audio, **kwargs):
            seen.append(kwargs)
            await gate.wait()
            return 0

        monkeypatch.setattr(talk_cli, "run_talk_session", keep_open)

        # Legacy lane: no host execution attachment.
        talk_discord.start_session(7)
        for _ in range(5):
            await asyncio.sleep(0)
        assert seen[-1]["lane"] == "discord"
        assert "host_execution_attachment" not in seen[-1]

        # Let the first session finish so the slot frees for the second.
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)

        # Host-execution lane: the attachment passes through with the lane.
        attachment = object()
        talk_discord.start_session(7, host_execution_attachment=attachment)
        for _ in range(5):
            await asyncio.sleep(0)
        assert seen[-1]["lane"] == "discord"
        assert seen[-1]["host_execution_attachment"] is attachment

    asyncio.run(scenario())


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
        self._socket_reader = types.SimpleNamespace(_callbacks=self.callbacks)

    def add_socket_listener(self, cb) -> None:
        self.callbacks.append(cb)

    def remove_socket_listener(self, cb) -> None:
        self.callbacks.remove(cb)  # raises if it was never registered

    def deliver(self, data: bytes) -> None:
        for cb in list(self.callbacks):
            cb(data)


class _FakeReceiver:
    def __init__(self, voice_client=None) -> None:
        self._vc = voice_client
        self._secret_key = b"test-secret"
        self._dave_session = None
        self._buffers: dict = {}
        self._ssrc_to_user: dict[int, int] = {}
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
        connection.secret_key = b"test-secret"
        connection.dave_session = None
        self.playing = None
        self.connected = True

    def is_playing(self) -> bool:
        return self.playing is not None

    def is_connected(self) -> bool:
        return self.connected

    def play(self, source) -> None:
        self.playing = source

    def stop(self) -> None:
        self.playing = None


def _wired_host(monkeypatch):
    connection = _FakeConnection()
    vc = _FakeVoiceClient(connection)
    receiver = _FakeReceiver(vc)
    connection.add_socket_listener(receiver._on_packet)  # as the host does
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


@pytest.mark.parametrize("replacement_point", ["remove", "add"])
def test_stop_never_restores_listener_to_a_replaced_voice_generation(
    monkeypatch, replacement_point
):
    old_connection, old_receiver, _old_vc, adapter = _wired_host(monkeypatch)
    original_listener = old_connection.callbacks[0]
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()
    tapped = old_connection.callbacks[0]

    new_connection = _FakeConnection()
    new_vc = _FakeVoiceClient(new_connection)
    new_receiver = _FakeReceiver(new_vc)
    new_listener = new_receiver._on_packet
    new_connection.add_socket_listener(new_listener)
    published = False

    def publish_replacement():
        nonlocal published
        if published:
            return
        published = True
        adapter._voice_clients[7] = new_vc
        adapter._voice_receivers[7] = new_receiver

    original_remove = old_connection.remove_socket_listener
    original_add = old_connection.add_socket_listener

    def racing_remove(callback):
        original_remove(callback)
        if replacement_point == "remove" and callback is tapped:
            publish_replacement()

    def racing_add(callback):
        original_add(callback)
        if replacement_point == "add" and callback == original_listener:
            publish_replacement()

    monkeypatch.setattr(old_connection, "remove_socket_listener", racing_remove)
    monkeypatch.setattr(old_connection, "add_socket_listener", racing_add)

    bridge.stop()

    assert published
    assert old_receiver._running is True
    assert original_listener not in old_connection.callbacks
    assert adapter._voice_clients[7] is new_vc
    assert adapter._voice_receivers[7] is new_receiver
    assert new_receiver._on_packet == new_listener
    assert new_connection.callbacks == [new_listener]


def test_capture_only_borrows_receive_without_touching_host_playback(monkeypatch):
    connection, receiver, vc, adapter = _wired_host(monkeypatch)
    original_listener = connection.callbacks[0]
    original_play = adapter.play_in_voice_channel
    original_mode = adapter._voice_mode_getter
    host_source = object()
    vc.playing = host_source
    bridge = talk_discord.DiscordAudio(7, capture_only=True)

    bridge.start()

    assert connection.callbacks != [original_listener]
    assert vc.playing is host_source
    assert adapter.play_in_voice_channel == original_play
    assert adapter._voice_mode_getter is original_mode
    assert adapter._voice_mixers == {7: "the-host-mixer"}
    assert bridge._source is None
    connection.deliver(b"\x01\x02" * 8)
    assert bridge.read_input_chunk() is not None

    bridge.stop()
    assert connection.callbacks == [original_listener]
    assert receiver._on_packet == original_listener
    assert vc.playing is host_source


def test_capture_only_playback_fails_closed():
    bridge = talk_discord.DiscordAudio(7, capture_only=True)

    with pytest.raises(talk_audio.TalkAudioError, match="capture-only"):
        bridge.queue_playback(b"\x01\x00")


def test_capture_only_failed_start_restores_listener_once(monkeypatch):
    connection, receiver, vc, adapter = _wired_host(monkeypatch)
    original_listener = connection.callbacks[0]
    host_source = object()
    vc.playing = host_source
    removed: list = []
    original_remove = connection.remove_socket_listener

    def fail_after_removing_original(callback):
        original_remove(callback)
        removed.append(callback)
        if callback == original_listener:
            raise RuntimeError("swap failed after removal")

    monkeypatch.setattr(connection, "remove_socket_listener", fail_after_removing_original)
    bridge = talk_discord.DiscordAudio(7, capture_only=True)

    with pytest.raises(talk_audio.TalkAudioError, match="couldn't listen in"):
        bridge.start()

    assert connection.callbacks == [original_listener]
    assert receiver._on_packet == original_listener
    assert vc.playing is host_source
    assert adapter._voice_mixers == {7: "the-host-mixer"}
    bridge.stop()
    assert connection.callbacks == [original_listener]


@pytest.mark.parametrize("capture_only", [True, False])
def test_failed_swap_fences_inflight_tap_before_rollback_and_restart(monkeypatch, capture_only):
    connection, receiver, vc, _adapter = _wired_host(monkeypatch)
    host_source = object()
    vc.playing = host_source
    original_listener = connection.callbacks[0]
    callback_entered = threading.Event()
    release_callback = threading.Event()
    callback_thread = None

    def blocking_original(data: bytes) -> None:
        callback_entered.set()
        assert release_callback.wait(2), "rollback never released the copied tap"
        original_listener(data)

    connection.callbacks[:] = [blocking_original]
    receiver._on_packet = blocking_original
    receiver._ssrc_to_user[1] = 101
    original_remove = connection.remove_socket_listener

    def fail_swap_and_release_during_rollback(callback):
        nonlocal callback_thread
        if callback is blocking_original:
            original_remove(callback)
            tapped = connection.callbacks[0]
            callback_thread = threading.Thread(target=tapped, args=(b"\x01\x00" * 5,))
            callback_thread.start()
            assert callback_entered.wait(2), "copied tap never entered the host callback"
            raise RuntimeError("swap failed after original removal")
        release_callback.set()
        original_remove(callback)
        callback_thread.join(2)
        assert not callback_thread.is_alive(), "copied tap did not finish during rollback"

    monkeypatch.setattr(connection, "remove_socket_listener", fail_swap_and_release_during_rollback)
    clock = [100.0]
    monkeypatch.setattr(talk_discord.time, "monotonic", lambda: clock[0])
    bridge = talk_discord.DiscordAudio(7, capture_only=capture_only)

    with pytest.raises(talk_audio.TalkAudioError, match="couldn't listen in"):
        bridge.start()

    assert connection.callbacks == [blocking_original]
    assert receiver._on_packet is blocking_original
    assert vc.playing is host_source
    assert bridge._source is None
    assert bridge._inbound.empty()
    assert bridge._capture_remainder == {}
    assert bridge._audio_clock == 0.0
    assert bridge._last_keepalive == 0.0
    assert bridge._last_speaker_key is talk_discord._UNSET

    monkeypatch.setattr(connection, "remove_socket_listener", original_remove)
    clock[0] = 200.0
    bridge.start()
    speaker_events = []
    bridge.set_speaker_notifier(speaker_events.append)

    assert bridge._inbound.empty(), "failed generation PCM survived the restart"
    assert bridge._capture_remainder == {}
    assert bridge._audio_clock == 200.0
    new_pcm = b"\x02\x00" * 8
    connection.deliver(new_pcm)
    packet = bridge._inbound.get_nowait()
    assert packet.pcm == talk_discord.discord_to_session(new_pcm)[0]
    assert packet.speaker["user_id"] == 101
    assert speaker_events == [packet.speaker]
    bridge.stop()


@pytest.mark.parametrize("capture_only", [True, False])
def test_restart_discards_pcm_received_by_host_between_generations(monkeypatch, capture_only):
    connection, receiver, _vc, _adapter = _wired_host(monkeypatch)
    receiver._ssrc_to_user[1] = 101
    bridge = talk_discord.DiscordAudio(7, capture_only=capture_only)
    bridge.start()
    bridge.stop()

    connection.deliver(b"\x01\x00" * 5)
    assert receiver._buffers[1], "host did not preserve between-generation PCM"

    bridge.start()
    speaker_events = []
    bridge.set_speaker_notifier(speaker_events.append)
    new_pcm = b"\x02\x00" * 8
    connection.deliver(new_pcm)

    packet = bridge._inbound.get_nowait()
    assert packet.pcm == talk_discord.discord_to_session(new_pcm)[0]
    assert packet.speaker["user_id"] == 101
    assert speaker_events == [packet.speaker]
    bridge.stop()


def test_capture_only_repeated_stop_restores_listener_exactly_once(monkeypatch):
    connection, receiver, vc, _adapter = _wired_host(monkeypatch)
    original_listener = connection.callbacks[0]
    host_source = object()
    vc.playing = host_source
    bridge = talk_discord.DiscordAudio(7, capture_only=True)
    bridge.start()

    bridge.stop()
    bridge.stop()

    assert connection.callbacks == [original_listener]
    assert receiver._on_packet == original_listener
    assert vc.playing is host_source


def test_capture_only_stop_preserves_a_newer_host_listener(monkeypatch):
    connection, receiver, _vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7, capture_only=True)
    bridge.start()
    tapped = connection.callbacks[0]

    def replacement(_data: bytes) -> None:
        pass

    connection.remove_socket_listener(tapped)
    connection.add_socket_listener(replacement)
    receiver._on_packet = replacement

    bridge.stop()

    assert connection.callbacks == [replacement]
    assert receiver._on_packet is replacement


def test_capture_only_stop_restores_original_when_tap_removal_raises(monkeypatch):
    connection, receiver, _vc, _adapter = _wired_host(monkeypatch)
    original_listener = connection.callbacks[0]
    bridge = talk_discord.DiscordAudio(7, capture_only=True)
    bridge.start()
    stale_tap = connection.callbacks[0]
    connection.callbacks.remove(stale_tap)  # concurrent host teardown won the race

    def fail_removal(_callback):
        stale_tap(b"\x01\x02" * 8)  # copied callback runs after teardown begins
        raise ValueError("listener was already removed")

    monkeypatch.setattr(connection, "remove_socket_listener", fail_removal)

    bridge.stop()

    assert connection.callbacks == [original_listener]
    assert receiver._on_packet == original_listener
    assert bridge.read_input_chunk() is None

    stale_tap(b"\x03\x04" * 8)
    assert bridge.read_input_chunk() is None


def test_stop_does_not_duplicate_original_already_restored_by_host(monkeypatch):
    connection, receiver, _vc, _adapter = _wired_host(monkeypatch)
    original_listener = connection.callbacks[0]
    bridge = talk_discord.DiscordAudio(7, capture_only=True)
    bridge.start()
    stale_tap = connection.callbacks[0]

    def unrelated_listener(_data: bytes) -> None:
        pass

    # Concurrent host recovery wins the registry race but has not yet repaired
    # the receiver attribute observed by stop().
    connection.callbacks[:] = [unrelated_listener, original_listener]

    def already_absent(callback):
        assert callback is stale_tap
        raise ValueError("tap was already removed by host recovery")

    monkeypatch.setattr(connection, "remove_socket_listener", already_absent)

    bridge.stop()

    assert stale_tap not in connection.callbacks
    assert connection.callbacks.count(original_listener) == 1
    assert unrelated_listener in connection.callbacks
    assert receiver._on_packet == original_listener


def test_capture_only_repeated_start_restores_the_original_listener(monkeypatch):
    connection, receiver, _vc, _adapter = _wired_host(monkeypatch)
    original_listener = connection.callbacks[0]
    bridge = talk_discord.DiscordAudio(7, capture_only=True)

    bridge.start()
    first_tap = connection.callbacks[0]
    bridge.start()
    bridge.stop()

    assert connection.callbacks == [original_listener]
    assert receiver._on_packet == original_listener
    assert first_tap not in connection.callbacks


@pytest.mark.parametrize("capture_only", [True, False])
def test_concurrent_starts_share_one_complete_listener_install(monkeypatch, capture_only):
    connection, receiver, _vc, _adapter = _wired_host(monkeypatch)
    original_listener = connection.callbacks[0]
    original_add = connection.add_socket_listener
    add_entered = threading.Event()
    release_add = threading.Event()
    second_returned = threading.Event()
    errors: list[Exception] = []
    tap_adds: list = []

    def blocking_add(callback):
        if callback is not original_listener:
            tap_adds.append(callback)
            if len(tap_adds) == 1:
                add_entered.set()
                assert release_add.wait(2), "test never released the first listener install"
        original_add(callback)

    monkeypatch.setattr(connection, "add_socket_listener", blocking_add)
    bridge = talk_discord.DiscordAudio(7, capture_only=capture_only)

    def run_start(returned=None):
        try:
            bridge.start()
        except Exception as exc:  # noqa: BLE001 - surface worker failure in the test thread
            errors.append(exc)
        finally:
            if returned is not None:
                returned.set()

    first = threading.Thread(target=run_start)
    second = threading.Thread(target=run_start, args=(second_returned,))
    first.start()
    assert add_entered.wait(2), "first start never reached listener installation"
    second.start()
    try:
        assert not second_returned.wait(0.2), "second start escaped an incomplete first start"
    finally:
        release_add.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert len(tap_adds) == 1
    assert connection.callbacks == [tap_adds[0]]
    assert receiver._on_packet is tap_adds[0]

    bridge.stop()
    assert connection.callbacks == [original_listener]
    assert receiver._on_packet == original_listener


@pytest.mark.parametrize("capture_only", [True, False])
def test_stop_waits_for_blocked_start_then_unwinds_the_complete_lifecycle(
    monkeypatch, capture_only
):
    connection, receiver, _vc, _adapter = _wired_host(monkeypatch)
    original_listener = connection.callbacks[0]
    original_add = connection.add_socket_listener
    add_entered = threading.Event()
    release_add = threading.Event()
    stop_called = threading.Event()
    stop_returned = threading.Event()
    errors: list[Exception] = []
    block_next_tap = True

    def blocking_add(callback):
        nonlocal block_next_tap
        if callback is not original_listener and block_next_tap:
            block_next_tap = False
            add_entered.set()
            assert release_add.wait(2), "test never released the blocked listener install"
        original_add(callback)

    monkeypatch.setattr(connection, "add_socket_listener", blocking_add)
    bridge = talk_discord.DiscordAudio(7, capture_only=capture_only)

    def run_start():
        try:
            bridge.start()
        except Exception as exc:  # noqa: BLE001 - surface worker failure in the test thread
            errors.append(exc)

    def run_stop():
        stop_called.set()
        try:
            bridge.stop()
        except Exception as exc:  # noqa: BLE001 - surface worker failure in the test thread
            errors.append(exc)
        finally:
            stop_returned.set()

    start_thread = threading.Thread(target=run_start)
    stop_thread = threading.Thread(target=run_stop)
    start_thread.start()
    assert add_entered.wait(2), "start never reached the blocked listener install"
    stop_thread.start()
    assert stop_called.wait(2)
    try:
        assert not stop_returned.wait(0.2), "stop returned before the earlier start linearized"
    finally:
        release_add.set()
    start_thread.join(2)
    stop_thread.join(2)

    assert not start_thread.is_alive()
    assert not stop_thread.is_alive()
    assert errors == []
    assert connection.callbacks == [original_listener]
    assert receiver._on_packet == original_listener
    assert bridge._bridge is None
    assert bridge._source is None
    assert bridge._original_on_packet is None
    assert bridge._tapped is None
    assert bridge._installed_listener_generation is None
    assert bridge._capture_listener_generation is None
    assert bridge._audio_clock == 0.0

    bridge.start()
    second_tap = connection.callbacks[0]
    assert second_tap is not original_listener
    bridge.stop()
    assert connection.callbacks == [original_listener]
    assert receiver._on_packet == original_listener
    assert second_tap not in connection.callbacks


def test_playback_failure_self_stop_is_reentrant_and_restores_the_host(monkeypatch):
    connection, receiver, vc, adapter = _wired_host(monkeypatch)
    original_listener = connection.callbacks[0]
    original_play = adapter.play_in_voice_channel
    original_mode = adapter._voice_mode_getter

    def fail_play(_source):
        raise RuntimeError("player rejected source")

    monkeypatch.setattr(vc, "play", fail_play)
    bridge = talk_discord.DiscordAudio(7)

    with pytest.raises(talk_audio.TalkAudioError, match="couldn't take over playback"):
        bridge.start()

    assert connection.callbacks == [original_listener]
    assert receiver._on_packet == original_listener
    assert adapter.play_in_voice_channel == original_play
    assert adapter._voice_mode_getter is original_mode
    assert bridge._bridge is None
    assert bridge._source is None
    bridge.stop()
    assert connection.callbacks == [original_listener]


def test_capture_only_bridge_replacement_does_not_stop_either_playback(monkeypatch):
    old_connection, old_receiver, old_vc, adapter = _wired_host(monkeypatch)
    old_source = object()
    old_vc.playing = old_source
    bridge = talk_discord.DiscordAudio(7, capture_only=True)
    bridge.start()

    new_connection = _FakeConnection()
    new_vc = _FakeVoiceClient(new_connection)
    new_source = object()
    new_vc.playing = new_source
    new_receiver = _FakeReceiver(new_vc)
    new_connection.add_socket_listener(new_receiver._on_packet)
    old_receiver._running = False
    adapter._voice_clients[7] = new_vc
    adapter._voice_receivers[7] = new_receiver

    with pytest.raises(talk_audio.TalkAudioError, match="voice connection changed"):
        bridge.read_input_chunk()

    assert old_connection.callbacks == []
    assert old_vc.playing is old_source
    assert new_connection.callbacks == [new_receiver._on_packet]
    assert new_vc.playing is new_source


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


def test_receiver_replacement_fails_closed_on_the_next_old_packet(monkeypatch):
    old_connection, old_receiver, old_vc, adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()

    new_connection = _FakeConnection()
    new_vc = _FakeVoiceClient(new_connection)
    new_receiver = _FakeReceiver(new_vc)
    new_connection.add_socket_listener(new_receiver._on_packet)
    old_receiver._running = False
    adapter._voice_clients[7] = new_vc
    adapter._voice_receivers[7] = new_receiver

    # A final packet can race with the host's replacement. It must detect
    # that this callback is no longer the live bridge, not drain stale PCM.
    old_connection.deliver(b"\x01\x02" * 8)
    with pytest.raises(talk_audio.TalkAudioError, match="voice connection changed"):
        bridge.read_input_chunk()

    assert bridge._bridge is None
    assert old_vc.playing is None
    assert old_connection.callbacks == [], "a dead receiver was re-awakened"
    assert new_connection.callbacks == [new_receiver._on_packet]


def test_receiver_replacement_is_detected_even_when_the_old_socket_goes_silent(monkeypatch):
    old_connection, _old_receiver, _old_vc, adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()

    new_connection = _FakeConnection()
    new_vc = _FakeVoiceClient(new_connection)
    new_receiver = _FakeReceiver(new_vc)
    new_connection.add_socket_listener(new_receiver._on_packet)
    adapter._voice_clients[7] = new_vc
    adapter._voice_receivers[7] = new_receiver

    # A discarded socket normally delivers no final packet, so the session's
    # polling path must independently validate the published bridge identity.
    with pytest.raises(talk_audio.TalkAudioError, match="voice connection changed"):
        bridge.read_input_chunk()
    assert old_connection.callbacks == [], "the obsolete listener was re-registered"


def test_sustained_disconnection_fails_closed_after_a_short_grace(monkeypatch):
    connection, receiver, vc, adapter = _wired_host(monkeypatch)
    original = connection.callbacks[0]
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()

    clock = [1000.0]
    monkeypatch.setattr(talk_discord.time, "monotonic", lambda: clock[0])
    vc.connected = False

    # A single false sample can happen during a reconnect; do not tear down
    # immediately, but do not call the bridge healthy indefinitely either.
    bridge.read_input_chunk()
    clock[0] += 0.9
    bridge.read_input_chunk()
    clock[0] += 0.2
    with pytest.raises(talk_audio.TalkAudioError, match="voice connection disconnected"):
        bridge.read_input_chunk()

    assert bridge._bridge is None
    assert vc.playing is None
    assert connection.callbacks == [original]
    assert receiver._on_packet == original
    assert adapter._voice_mode_getter("c") == "all"


def test_a_verified_reconnect_resets_the_disconnect_grace(monkeypatch):
    _connection, _receiver, vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()

    clock = [2000.0]
    monkeypatch.setattr(talk_discord.time, "monotonic", lambda: clock[0])
    vc.connected = False
    bridge.read_input_chunk()

    clock[0] += 0.8
    vc.connected = True
    bridge.read_input_chunk()

    # A later outage gets its own full grace period; the first outage's
    # timestamp must not poison a successfully verified reconnect.
    clock[0] += 0.4
    vc.connected = False
    bridge.read_input_chunk()
    clock[0] += 0.9
    bridge.read_input_chunk()
    clock[0] += 0.2
    with pytest.raises(talk_audio.TalkAudioError, match="voice connection disconnected"):
        bridge.read_input_chunk()


def test_transport_rekey_fails_closed_instead_of_streaming_false_silence(monkeypatch):
    connection, _receiver, vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()

    # The legacy host receiver copied this value at start. Discord rotates
    # the connection's key on a reconnect, leaving the receiver permanently
    # unable to decrypt while the client can still report connected.
    connection.secret_key = b"rotated-secret"
    with pytest.raises(talk_audio.TalkAudioError, match="voice credentials changed"):
        bridge.read_input_chunk()

    assert bridge._bridge is None
    assert vc.playing is None


def test_dave_session_replacement_fails_closed_before_corrupt_audio_flows(monkeypatch):
    connection, _receiver, vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()

    # DAVE negotiation can replace the live session object while a legacy
    # receiver retains its start-time None/object. Feeding that ciphertext
    # to Opus can sound like speech, so silence-only detection is insufficient.
    connection.dave_session = object()
    with pytest.raises(talk_audio.TalkAudioError, match="voice credentials changed"):
        bridge.read_input_chunk()

    assert bridge._bridge is None
    assert vc.playing is None


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


def test_capture_state_does_not_cross_stop_restart_generation(monkeypatch):
    _connection, receiver, _vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7, capture_only=True)
    old_events: list[dict] = []
    bridge.start()
    bridge.set_speaker_notifier(old_events.append)

    with receiver._lock:
        receiver._ssrc_to_user[11] = 101
        receiver._buffers[11] = bytearray(b"\x01\x00" * 5)
    bridge._drain_receiver(receiver)
    assert not bridge._inbound.empty()
    assert bridge._capture_remainder[11]
    assert len(old_events) == 1

    bridge.stop()
    bridge.start()
    new_events: list[dict] = []
    bridge.set_speaker_notifier(new_events.append)
    bridge._audio_clock = talk_discord.time.monotonic() + 1

    assert bridge.read_input_packet() is None, "old unread PCM crossed the restart"
    assert bridge._capture_remainder == {}

    new_chunk = b"\x04\x00" * 4
    expected_pcm, expected_remainder = talk_discord.discord_to_session(new_chunk)
    with receiver._lock:
        receiver._buffers[11] = bytearray(new_chunk)
    bridge._drain_receiver(receiver)

    packet = bridge.read_input_packet()
    assert packet is not None
    assert packet.pcm == expected_pcm
    assert bridge._capture_remainder[11] == expected_remainder
    assert len(new_events) == 1, "same speaker needs a fresh-generation transition"
    bridge.stop()


def test_drain_resolves_the_discord_speaker_for_each_audio_chunk(monkeypatch):
    _connection, receiver, vc, _adapter = _wired_host(monkeypatch)
    vc.channel = types.SimpleNamespace(
        members=[types.SimpleNamespace(id=101, display_name="Alice")]
    )
    bridge = talk_discord.DiscordAudio(7)
    events: list[dict] = []
    bridge.start()
    bridge.set_speaker_notifier(events.append)

    with receiver._lock:
        receiver._ssrc_to_user[11] = 101
        receiver._buffers[11] = bytearray(b"\x01\x00" * 8)
    bridge._drain_receiver(receiver)

    assert events == [{"ssrc": 11, "user_id": 101, "display_name": "Alice"}]
    assert bridge.read_input_chunk() is not None
    bridge.stop()


class _CoerciveDiscordUserId:
    def __int__(self) -> int:
        return 101


@pytest.mark.parametrize(
    "raw_user_id",
    ["101", "00101", True, 101.0, 101.5, _CoerciveDiscordUserId()],
)
def test_drain_does_not_coerce_receiver_user_id_mappings(monkeypatch, raw_user_id):
    _connection, receiver, vc, _adapter = _wired_host(monkeypatch)
    vc.channel = types.SimpleNamespace(
        members=[types.SimpleNamespace(id=101, display_name="Alice")]
    )
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()

    with receiver._lock:
        receiver._ssrc_to_user[11] = raw_user_id
        receiver._buffers[11] = bytearray(b"\x01\x00" * 8)
    bridge._drain_receiver(receiver)

    packet = bridge.read_input_packet()
    assert packet is not None
    assert packet.speaker == {"ssrc": 11, "user_id": None, "display_name": ""}
    bridge.stop()


def test_same_discord_speaker_chunks_and_ssrc_reorder_do_not_flood(monkeypatch):
    _connection, receiver, vc, _adapter = _wired_host(monkeypatch)
    vc.channel = types.SimpleNamespace(
        members=[types.SimpleNamespace(id=101, display_name="Alice")]
    )
    bridge = talk_discord.DiscordAudio(7)
    events: list[dict] = []
    bridge.start()
    bridge.set_speaker_notifier(events.append)

    for ssrc in (11, 11, 12):
        with receiver._lock:
            receiver._ssrc_to_user[ssrc] = 101
            receiver._buffers[ssrc] = bytearray(b"\x01\x00" * 8)
        bridge._drain_receiver(receiver)

    assert events == [{"ssrc": 11, "user_id": 101, "display_name": "Alice"}]
    bridge.stop()


def test_speaker_mapping_is_resolved_again_after_ssrc_reuse(monkeypatch):
    _connection, receiver, vc, _adapter = _wired_host(monkeypatch)
    vc.channel = types.SimpleNamespace(
        members=[
            types.SimpleNamespace(id=101, display_name="Alice"),
            types.SimpleNamespace(id=202, display_name="Bob"),
        ]
    )
    bridge = talk_discord.DiscordAudio(7)
    events: list[dict] = []
    bridge.start()
    bridge.set_speaker_notifier(events.append)

    for user_id in (101, 202):
        with receiver._lock:
            receiver._ssrc_to_user[11] = user_id
            receiver._buffers[11] = bytearray(b"\x01\x00" * 8)
        bridge._drain_receiver(receiver)

    assert [(event["user_id"], event["display_name"]) for event in events] == [
        (101, "Alice"),
        (202, "Bob"),
    ]
    bridge.stop()


def test_speaker_callback_is_marshaled_from_receiver_thread_to_session_loop(monkeypatch):
    _connection, receiver, vc, _adapter = _wired_host(monkeypatch)
    vc.channel = types.SimpleNamespace(
        members=[types.SimpleNamespace(id=101, display_name="Alice")]
    )
    scheduled: list[tuple] = []
    loop = types.SimpleNamespace(
        call_soon_threadsafe=lambda callback, *args: scheduled.append((callback, args))
    )
    bridge = talk_discord.DiscordAudio(7)
    events: list[dict] = []
    bridge.start()
    bridge._loop = loop
    bridge.set_speaker_notifier(events.append)

    with receiver._lock:
        receiver._ssrc_to_user[11] = 101
        receiver._buffers[11] = bytearray(b"\x01\x00" * 8)
    worker = threading.Thread(target=bridge._drain_receiver, args=(receiver,))
    worker.start()
    worker.join()

    assert events == []
    assert len(scheduled) == 2  # attribution plus the host timer keepalive
    for callback, args in scheduled:
        callback(*args)
    assert events == [{"ssrc": 11, "user_id": 101, "display_name": "Alice"}]
    bridge.stop()


def test_teardown_discards_a_speaker_callback_already_queued_on_the_loop(monkeypatch):
    _connection, receiver, vc, _adapter = _wired_host(monkeypatch)
    vc.channel = types.SimpleNamespace(
        members=[types.SimpleNamespace(id=101, display_name="Alice")]
    )
    scheduled: list[tuple] = []
    bridge = talk_discord.DiscordAudio(7)
    events: list[dict] = []
    bridge.start()
    bridge._loop = types.SimpleNamespace(
        call_soon_threadsafe=lambda callback, *args: scheduled.append((callback, args))
    )
    bridge.set_speaker_notifier(events.append)

    with receiver._lock:
        receiver._ssrc_to_user[11] = 101
        receiver._buffers[11] = bytearray(b"\x01\x00" * 8)
    bridge._drain_receiver(receiver)
    bridge.stop()
    for callback, args in scheduled:
        callback(*args)

    assert events == []


def test_speaker_notifier_reset_discards_old_delivery_and_accepts_current_one():
    bridge = talk_discord.DiscordAudio(7)
    old_events: list[dict] = []
    current_events: list[dict] = []
    speaker = {"ssrc": 11, "user_id": 101, "display_name": "Alice"}

    bridge.set_speaker_notifier(old_events.append)
    old_generation = bridge._speaker_notifier_generation
    old_notifier = bridge._speaker_notifier
    bridge.set_speaker_notifier(None)
    bridge.set_speaker_notifier(current_events.append)

    bridge._deliver_speaker(old_generation, old_notifier, speaker)
    bridge._notify_speaker(speaker)

    assert old_events == []
    assert current_events == [speaker]


def test_failed_start_detaches_a_previously_registered_speaker_notifier(monkeypatch):
    bridge = talk_discord.DiscordAudio(7)
    bridge.set_speaker_notifier(lambda _speaker: None)
    generation = bridge._speaker_notifier_generation
    monkeypatch.setattr(
        talk_discord,
        "resolve_voice_bridge",
        lambda _guild_id: (_ for _ in ()).throw(talk_discord.TalkDiscordError("gone")),
    )

    with pytest.raises(talk_audio.TalkAudioError, match="gone"):
        bridge.start()

    assert bridge._speaker_notifier is None
    assert bridge._speaker_notifier_generation > generation


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


def test_source_satisfies_a_hosts_isinstance_check(monkeypatch):
    # discord.py's VoiceClient.play does `isinstance(source, AudioSource)`
    # and rejects a duck type outright. The first cut shipped duck-typed and
    # died on the first real call with "source must be an AudioSource not
    # _RealtimeSource" — invisible to every offline test, because off-host
    # there is no base class to fail against. This fakes the host's ABC.
    class _HostAudioSource:
        def read(self):  # pragma: no cover - overridden
            raise NotImplementedError

        def is_opus(self):  # pragma: no cover - overridden
            return True

        def cleanup(self):  # pragma: no cover - overridden
            pass

    fake_discord = types.ModuleType("discord")
    fake_discord.AudioSource = _HostAudioSource
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    talk_discord._SOURCE_CLASSES.clear()

    source = talk_discord._new_source(queue.Queue())
    assert isinstance(source, _HostAudioSource), "a host would refuse this source"
    # Our behaviour must still win over the base class's.
    assert source.is_opus() is False
    assert source.read() == talk_discord.SILENCE_FRAME
    talk_discord._SOURCE_CLASSES.clear()


def test_silence_is_synthesized_when_discord_goes_quiet(monkeypatch):
    # Discord stops transmitting when nobody speaks. The session's turn
    # detection measures silence IN THE AUDIO IT RECEIVES, so a bridge that
    # forwards only what arrives never ends the operator's turn — which
    # presents as "it takes forever to answer".
    _connection, _receiver, _vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()

    clock = [1000.0]
    monkeypatch.setattr(talk_discord.time, "monotonic", lambda: clock[0])
    # Already covered for the next frame-duration.
    bridge._audio_clock = clock[0] + 0.02

    # Nothing queued and the stream is covered: nothing owed yet.
    assert bridge.read_input_chunk() is None

    # Once wall time catches up, one frame of silence is owed.
    clock[0] += 0.02
    frame = bridge.read_input_chunk()
    assert frame == talk_discord.SESSION_SILENCE
    assert len(frame) == talk_discord.SESSION_FRAME_BYTES

    # And only one — the clock advanced with it.
    assert bridge.read_input_chunk() is None
    bridge.stop()


def test_real_audio_pays_down_the_clock_instead_of_earning_silence(monkeypatch):
    # A burst of buffered speech must not be followed by a burst of
    # synthesized silence: sending 100ms of audio buys 100ms of the clock.
    _connection, _receiver, _vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()

    clock = [2000.0]
    monkeypatch.setattr(talk_discord.time, "monotonic", lambda: clock[0])
    bridge._audio_clock = clock[0]

    speech = _tone(2400, rate=24_000)  # 100 ms
    bridge._inbound.put_nowait(speech)
    assert bridge.read_input_chunk() == speech

    # 20ms later we are still covered by the audio just sent.
    clock[0] += 0.02
    assert bridge.read_input_chunk() is None
    # Past the end of it, silence resumes.
    clock[0] += 0.10
    assert bridge.read_input_chunk() == talk_discord.SESSION_SILENCE
    bridge.stop()


def test_legacy_raw_bytes_are_explicitly_unresolved_not_synthetic_silence(monkeypatch):
    _connection, _receiver, _vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()
    speech = _tone(480, rate=24_000)
    bridge._inbound.put_nowait(speech)

    packet = bridge.read_input_packet()

    assert packet.pcm == speech
    assert packet.speaker["user_id"] is None
    assert packet.speaker["ssrc"] == 0
    bridge.stop()


def test_no_silence_before_start_or_after_stop(monkeypatch):
    # Synthesizing into a session that does not exist would be noise on a
    # dead socket.
    bridge = talk_discord.DiscordAudio(7)
    assert bridge.read_input_chunk() is None
    _connection, _receiver, _vc, _adapter = _wired_host(monkeypatch)
    bridge.start()
    bridge.stop()
    assert bridge.read_input_chunk() is None


def test_receiver_mapping_and_pcm_are_snapshotted_atomically(monkeypatch):
    _connection, receiver, vc, adapter = _wired_host(monkeypatch)
    vc.channel = types.SimpleNamespace(
        members=[
            types.SimpleNamespace(id=101, display_name="Alice"),
            types.SimpleNamespace(id=202, display_name="Bob"),
        ]
    )
    # The adapter copy is stale. The receiver owns both the decoded buffers
    # and the mapping that identifies them.
    adapter._ssrc_to_user = {11: 101}
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()
    pcm48 = b"\x02\x00" * 8
    with receiver._lock:
        receiver._ssrc_to_user[11] = 202
        receiver._buffers[11] = bytearray(pcm48)
    bridge._drain_receiver(receiver)

    # A remap after drain must not relabel already-queued audio.
    with receiver._lock:
        receiver._ssrc_to_user[11] = 101
    packet = bridge.read_input_packet()

    assert packet.speaker == {"ssrc": 11, "user_id": 202, "display_name": "Bob"}
    assert packet.pcm == talk_discord.discord_to_session(pcm48)[0]
    bridge.stop()


def test_generic_chunk_reader_unwraps_the_atomic_packet(monkeypatch):
    _connection, receiver, _vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()
    pcm48 = b"\x03\x00" * 8
    with receiver._lock:
        receiver._ssrc_to_user[11] = 101
        receiver._buffers[11] = bytearray(pcm48)
    bridge._drain_receiver(receiver)

    assert bridge.read_input_chunk() == talk_discord.discord_to_session(pcm48)[0]
    bridge.stop()


def test_core_session_claims_shared_slot_with_capture_only_audio(monkeypatch):
    created = []
    entered = asyncio.Event()

    class Audio:
        def __init__(self, guild_id, *, capture_only=False):
            created.append((guild_id, capture_only, self))

        def stop(self):
            pass

    async def run_core(factory, *, guild_id, audio, status_callback):
        assert factory is sentinel
        assert guild_id == 7
        assert audio is created[0][2]
        assert callable(status_callback)
        entered.set()
        await asyncio.Event().wait()

    sentinel = object()
    monkeypatch.setattr(
        talk_discord,
        "resolve_voice_bridge",
        lambda guild_id=None: {"guild_id": 7, "adapter": object()},
    )
    monkeypatch.setattr(talk_discord, "DiscordAudio", Audio)
    monkeypatch.setattr(talk_core_session, "run_core_session", run_core)
    monkeypatch.setattr(
        talk_cli,
        "run_talk_session",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("legacy fallback")),
    )

    async def scenario():
        reply = talk_discord.start_core_session(sentinel)
        await asyncio.wait_for(entered.wait(), 0.2)
        assert talk_discord._SESSION["mode"] == "core"
        assert "already" in talk_discord.start_core_session(object()).lower()
        stopped = talk_discord.stop_session()
        await asyncio.sleep(0)
        return reply, stopped

    reply, stopped = asyncio.run(scenario())
    assert created[0][:2] == (7, True)
    assert "starting" in reply.lower() and "core" in reply.lower()
    assert "left" in stopped.lower()


def test_core_status_becomes_listening_and_stale_callback_cannot_change_replacement(monkeypatch):
    callbacks = []

    class Audio:
        def __init__(self, _guild_id, *, capture_only=False):
            assert capture_only

        def stop(self):
            return None

    async def run_core(_factory, *, guild_id, audio, status_callback):
        assert guild_id == 7
        assert audio is not None
        callbacks.append(status_callback)
        await asyncio.Event().wait()

    monkeypatch.setattr(
        talk_discord,
        "resolve_voice_bridge",
        lambda guild_id=None: {"guild_id": 7, "adapter": object()},
    )
    monkeypatch.setattr(talk_discord, "DiscordAudio", Audio)
    monkeypatch.setattr(talk_core_session, "run_core_session", run_core)

    async def scenario():
        talk_discord.start_core_session(object())
        await asyncio.sleep(0)
        assert "starting" in talk_discord.session_status().lower()
        callbacks[0]("listening")
        assert "listening" in talk_discord.session_status().lower()

        first = talk_discord._SESSION["task"]
        talk_discord.stop_session()
        await asyncio.gather(first, return_exceptions=True)

        talk_discord.start_core_session(object())
        await asyncio.sleep(0)
        assert "starting" in talk_discord.session_status().lower()
        callbacks[0]("listening")
        assert "starting" in talk_discord.session_status().lower()
        talk_discord.stop_session()

    asyncio.run(scenario())


def test_core_failure_delivers_a_secret_safe_receipt_without_legacy_fallback(monkeypatch, caplog):
    delivered = []
    secret = "Authorization: Bearer TALK-SECRET-MARKER"

    class Audio:
        def __init__(self, _guild_id, *, capture_only=False):
            assert capture_only

        def stop(self):
            pass

    async def fail_core(*_args, **_kwargs):
        raise RuntimeError(secret)

    async def deliver(adapter, guild_id, receipt):
        delivered.append((adapter, guild_id, receipt))
        return True

    adapter = object()
    monkeypatch.setattr(
        talk_discord,
        "resolve_voice_bridge",
        lambda guild_id=None: {"guild_id": 7, "adapter": adapter},
    )
    monkeypatch.setattr(talk_discord, "DiscordAudio", Audio)
    monkeypatch.setattr(talk_core_session, "run_core_session", fail_core)
    monkeypatch.setattr(talk_discord, "_deliver_failure_receipt", deliver)

    async def scenario():
        talk_discord.start_core_session(object())
        task = talk_discord._SESSION["task"]
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        if talk_discord._NOTIFICATION_TASKS:
            await asyncio.gather(*talk_discord._NOTIFICATION_TASKS)

    asyncio.run(scenario())

    assert delivered and delivered[0][:2] == (adapter, 7)
    receipt = delivered[0][2].lower()
    assert "canonical core voice" in receipt
    assert "runtimeerror" in receipt
    assert "talk join" not in receipt
    public_output = delivered[0][2] + talk_discord.session_status() + caplog.text
    assert "TALK-SECRET-MARKER" not in public_output
    assert secret not in public_output


@pytest.mark.parametrize("failure_point", ["factory", "feed", "audio", "close"])
def test_core_boundaries_never_publish_raw_exception_secrets(monkeypatch, caplog, failure_point):
    delivered = []
    secret = "Authorization: Bearer TALK-BOUNDARY-SECRET"

    class Attachment:
        operator_user_id = 42

        def feed_audio(self, *_args, **_kwargs):
            if failure_point == "feed":
                raise RuntimeError(secret)
            return "accepted"

        async def close(self):
            if failure_point == "close":
                raise RuntimeError(secret)

    class Factory:
        async def open(self, *_args, **_kwargs):
            if failure_point == "factory":
                raise RuntimeError(secret)
            return Attachment()

    class Audio:
        def __init__(self, _guild_id, *, capture_only=False):
            assert capture_only
            self.reads = 0

        def start(self):
            if failure_point == "audio":
                raise RuntimeError(secret)

        def read_input_packet(self):
            self.reads += 1
            if failure_point == "feed" and self.reads == 1:
                return talk_discord.InputAudioPacket(speaker={"user_id": 42}, pcm=b"\x01\x00")
            raise asyncio.CancelledError

        def stop(self):
            return None

    async def deliver(_adapter, _guild_id, receipt):
        delivered.append(receipt)
        return False

    monkeypatch.setattr(
        talk_discord,
        "resolve_voice_bridge",
        lambda guild_id=None: {"guild_id": 7, "adapter": object()},
    )
    monkeypatch.setattr(talk_discord, "DiscordAudio", Audio)
    monkeypatch.setattr(talk_core_session, "build_core_setup", lambda: object())
    monkeypatch.setattr(talk_discord, "_deliver_failure_receipt", deliver)

    async def scenario():
        talk_discord.start_core_session(Factory())
        task = talk_discord._SESSION["task"]
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        if talk_discord._NOTIFICATION_TASKS:
            await asyncio.gather(*talk_discord._NOTIFICATION_TASKS)

    asyncio.run(scenario())

    assert len(delivered) == 1
    public_output = delivered[0] + talk_discord.session_status() + caplog.text
    assert "RuntimeError" in public_output
    assert "TALK-BOUNDARY-SECRET" not in public_output
    assert secret not in public_output


def test_stale_core_completion_cannot_publish_over_a_live_replacement(monkeypatch):
    delivered = []
    replacement_entered = asyncio.Event()
    calls = 0

    class Audio:
        def __init__(self, _guild_id, *, capture_only=False):
            assert capture_only

        def stop(self):
            return None

    async def run_core(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            asyncio.get_running_loop().call_soon(talk_discord.start_core_session, object())
            raise RuntimeError("identical failure")
        replacement_entered.set()
        await asyncio.Event().wait()

    async def deliver(*args):
        delivered.append(args)
        return True

    monkeypatch.setattr(
        talk_discord,
        "resolve_voice_bridge",
        lambda guild_id=None: {"guild_id": 7, "adapter": object()},
    )
    monkeypatch.setattr(talk_discord, "DiscordAudio", Audio)
    monkeypatch.setattr(talk_core_session, "run_core_session", run_core)
    monkeypatch.setattr(talk_discord, "_deliver_failure_receipt", deliver)

    async def scenario():
        talk_discord.start_core_session(object())
        first = talk_discord._SESSION["task"]
        await asyncio.wait_for(replacement_entered.wait(), 0.2)
        replacement = talk_discord._SESSION["task"]
        await asyncio.sleep(0)
        assert first.done()
        assert replacement is not first and not replacement.done()
        assert talk_discord._LAST_FAILURE is None
        assert delivered == []
        replacement.cancel()
        await asyncio.gather(replacement, return_exceptions=True)

    asyncio.run(scenario())


def test_old_delayed_receipt_cannot_clear_identical_new_generation_failure(monkeypatch):
    release_first = asyncio.Event()
    first_delivery_entered = asyncio.Event()
    deliveries = 0

    class Audio:
        def __init__(self, _guild_id, *, capture_only=False):
            assert capture_only

        def stop(self):
            return None

    async def fail_core(*_args, **_kwargs):
        raise RuntimeError("identical failure")

    async def deliver(*_args):
        nonlocal deliveries
        deliveries += 1
        if deliveries == 1:
            first_delivery_entered.set()
            await release_first.wait()
            return True
        return False

    monkeypatch.setattr(
        talk_discord,
        "resolve_voice_bridge",
        lambda guild_id=None: {"guild_id": 7, "adapter": object()},
    )
    monkeypatch.setattr(talk_discord, "DiscordAudio", Audio)
    monkeypatch.setattr(talk_core_session, "run_core_session", fail_core)
    monkeypatch.setattr(talk_discord, "_deliver_failure_receipt", deliver)

    async def scenario():
        talk_discord.start_core_session(object())
        await asyncio.wait_for(first_delivery_entered.wait(), 0.2)
        talk_discord.start_core_session(object())
        second = talk_discord._SESSION["task"]
        await asyncio.gather(second, return_exceptions=True)
        await asyncio.sleep(0)
        expected = talk_discord._LAST_FAILURE
        assert expected is not None
        release_first.set()
        await asyncio.sleep(0)
        assert expected == talk_discord._LAST_FAILURE
        if talk_discord._NOTIFICATION_TASKS:
            await asyncio.gather(*talk_discord._NOTIFICATION_TASKS)

    asyncio.run(scenario())
