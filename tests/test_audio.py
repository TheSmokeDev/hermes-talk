"""Audio — the queue and barge-in logic, with no device and no sounddevice.

Only the pure half is exercised here: everything below is what runs between
the PortAudio callbacks, which is where a barge-in is won or lost. Opening a
real device is a canary step, not a CI step.
"""

from __future__ import annotations

import struct
import sys
import threading
import time

import pytest

import talk_audio


class _Buffer:
    """Stands in for the writable block PortAudio hands the output callback."""

    def __init__(self, size: int):
        self.data = bytearray(size)

    def __setitem__(self, key, value):
        self.data[key] = value


def test_lazy_import_failure_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)

    with pytest.raises(talk_audio.TalkAudioError, match=r"hermes-talk\[audio\]"):
        talk_audio.import_sounddevice()
    assert talk_audio.audio_available() is False

def test_audio_availability_only_requires_sounddevice(monkeypatch):
    monkeypatch.setattr(talk_audio, "import_sounddevice", lambda: object())

    assert talk_audio.audio_available() is True


def test_device_override_parses_index_or_name():
    assert talk_audio._device(None) is None
    assert talk_audio._device("3") == 3
    assert talk_audio._device("Speakers (Realtek)") == "Speakers (Realtek)"


class _Stream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


class _SoundDevice:
    def __init__(self):
        self.input_stream = None
        self.output_stream = None

    def RawInputStream(self, **kwargs):
        self.input_stream = _Stream(**kwargs)
        return self.input_stream

    def RawOutputStream(self, **kwargs):
        self.output_stream = _Stream(**kwargs)
        return self.output_stream


class _CompletedProcess:
    def __init__(self, stdout: str):
        self.stdout = stdout


def test_linux_default_audio_uses_pulse_webrtc_echo_cancellation(monkeypatch):
    sd = _SoundDevice()
    commands = []
    monkeypatch.setattr(talk_audio, "import_sounddevice", lambda: sd)
    monkeypatch.setattr(talk_audio.sys, "platform", "linux")
    monkeypatch.setattr(talk_audio.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(talk_audio.talk_config, "audio_input_device", lambda: None)
    monkeypatch.setattr(talk_audio.talk_config, "audio_output_device", lambda: None)

    def run(command, **_kwargs):
        commands.append(command)
        return _CompletedProcess("42\n")

    monkeypatch.setattr(talk_audio.subprocess, "run", run)
    audio = talk_audio.DuplexAudio()

    audio.start()

    assert commands[0][1:3] == ["load-module", "module-echo-cancel"]
    assert sd.input_stream.kwargs["device"] == "pulse"
    assert sd.output_stream.kwargs["device"] == "pulse"
    assert talk_audio.os.environ["PULSE_SOURCE"].startswith("hermes_talk_aec_")
    assert talk_audio.os.environ["PULSE_SINK"].startswith("hermes_talk_aec_sink_")

    audio.stop()

    assert commands[-1] == ["/usr/bin/pactl", "unload-module", "42"]

def test_pulse_device_open_failure_retries_original_devices(monkeypatch):
    class FailingPulseSoundDevice(_SoundDevice):
        def RawInputStream(self, **kwargs):
            if kwargs["device"] == "pulse":
                raise RuntimeError("Pulse route is unavailable")
            return super().RawInputStream(**kwargs)

    sd = FailingPulseSoundDevice()
    commands = []
    monkeypatch.setattr(talk_audio, "import_sounddevice", lambda: sd)
    monkeypatch.setattr(talk_audio.sys, "platform", "linux")
    monkeypatch.setattr(talk_audio.shutil, "which", lambda _command: "/usr/bin/pactl")
    monkeypatch.setattr(talk_audio.talk_config, "audio_input_device", lambda: None)
    monkeypatch.setattr(talk_audio.talk_config, "audio_output_device", lambda: None)

    def run(command, **_kwargs):
        commands.append(command)
        return _CompletedProcess("42\n")

    monkeypatch.setattr(talk_audio.subprocess, "run", run)
    audio = talk_audio.DuplexAudio()

    audio.start()

    assert sd.input_stream.kwargs["device"] is None
    assert sd.output_stream.kwargs["device"] is None
    assert commands[-1] == ["/usr/bin/pactl", "unload-module", "42"]
    assert audio._pulse_webrtc.active is False
    audio.stop()


def test_pulse_echo_cancelled_input_is_never_amplitude_gated(monkeypatch):
    sd = _SoundDevice()
    monkeypatch.setattr(talk_audio, "import_sounddevice", lambda: sd)
    monkeypatch.setattr(talk_audio.sys, "platform", "linux")
    monkeypatch.setattr(talk_audio.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(talk_audio.talk_config, "audio_input_device", lambda: None)
    monkeypatch.setattr(talk_audio.talk_config, "audio_output_device", lambda: None)
    monkeypatch.setattr(
        talk_audio.subprocess,
        "run",
        lambda *_args, **_kwargs: _CompletedProcess("42\n"),
    )
    audio = talk_audio.DuplexAudio()
    audio.start()
    playback = _pcm16(20_000, 2_400)
    audio.queue_playback(playback)
    audio._output_callback(_Buffer(len(playback)), 2_400, None, None)
    quiet_speech = _pcm16(2_000, 2_400)

    audio._input_callback(quiet_speech, 2_400, None, None)

    assert audio.read_input_chunk() == quiet_speech
    audio.stop()


def test_explicit_devices_skip_pulse_echo_module(monkeypatch):
    sd = _SoundDevice()
    monkeypatch.setattr(talk_audio, "import_sounddevice", lambda: sd)
    monkeypatch.setattr(talk_audio.talk_config, "audio_input_device", lambda: "microphone")
    monkeypatch.setattr(talk_audio.talk_config, "audio_output_device", lambda: "speaker")
    monkeypatch.setattr(
        talk_audio.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("explicit devices must not load a PulseAudio module"),
    )
    audio = talk_audio.DuplexAudio()

    audio.start()

    assert sd.input_stream.kwargs["device"] == "microphone"
    assert sd.output_stream.kwargs["device"] == "speaker"
    audio.stop()


def test_audio_start_serializes_pulse_environment_through_stream_open(monkeypatch):
    monkeypatch.setattr(talk_audio, "import_sounddevice", lambda: object())
    monkeypatch.setattr(talk_audio.sys, "platform", "linux")
    monkeypatch.setattr(talk_audio.shutil, "which", lambda _command: "/usr/bin/pactl")
    monkeypatch.setattr(talk_audio.talk_config, "audio_input_device", lambda: None)
    monkeypatch.setattr(talk_audio.talk_config, "audio_output_device", lambda: None)
    module_ids = iter(("41", "42"))
    monkeypatch.setattr(
        talk_audio.subprocess,
        "run",
        lambda command, **_kwargs: _CompletedProcess(next(module_ids))
        if command[1] == "load-module"
        else _CompletedProcess(""),
    )
    active_opens = 0
    maximum_active_opens = 0
    lock = threading.Lock()

    def open_streams(audio, _sd, _input_device, _output_device):
        nonlocal active_opens, maximum_active_opens
        with lock:
            active_opens += 1
            maximum_active_opens = max(maximum_active_opens, active_opens)
            assert talk_audio.os.environ["PULSE_SOURCE"] == audio._pulse_webrtc._source_name
        time.sleep(0.02)
        with lock:
            active_opens -= 1

    monkeypatch.setattr(talk_audio.DuplexAudio, "_open_streams", open_streams)
    sessions = [talk_audio.DuplexAudio(), talk_audio.DuplexAudio()]
    workers = [threading.Thread(target=session.start) for session in sessions]

    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert maximum_active_opens == 1
    for session in sessions:
        session.stop()


def test_input_chunks_round_trip():
    audio = talk_audio.DuplexAudio()
    assert audio.read_input_chunk() is None

    audio._input_callback(b"\x01\x02", 1, None, None)

    assert audio.read_input_chunk() == b"\x01\x02"
    assert audio.read_input_chunk() is None

def _pcm16(amplitude: int, frames: int = 32) -> bytes:
    return struct.pack(f"<{frames}h", *([amplitude] * frames))



def test_playback_echo_below_omp_barge_in_threshold_is_not_uploaded():
    audio = talk_audio.DuplexAudio()
    playback = _pcm16(20_000, 2_400)
    audio.queue_playback(playback)
    audio._output_callback(_Buffer(len(playback)), 2_400, None, None)

    audio._input_callback(_pcm16(5_000, 2_400), 2_400, None, None)

    assert audio.read_input_chunk() is None

def test_fallback_gate_does_not_allow_vad_to_override_omp_threshold():
    audio = talk_audio.DuplexAudio()
    playback = _pcm16(20_000, 2_400)
    audio.queue_playback(playback)
    audio._output_callback(_Buffer(len(playback)), 2_400, None, None)
    moderate_speech = _pcm16(5_000, 2_400)

    audio._input_callback(moderate_speech, 2_400, None, None)

    assert audio.read_input_chunk() is None


def test_voice_above_omp_barge_in_threshold_interrupts_playback():
    audio = talk_audio.DuplexAudio()
    playback = _pcm16(20_000, 2_400)
    audio.queue_playback(playback)
    audio._output_callback(_Buffer(len(playback)), 2_400, None, None)
    barge_in = _pcm16(30_000, 2_400)

    audio._input_callback(barge_in, 2_400, None, None)

    assert audio.read_input_chunk() == barge_in


def test_loud_input_above_omp_threshold_is_uploaded():
    audio = talk_audio.DuplexAudio()
    playback = _pcm16(20_000, 2_400)
    audio.queue_playback(playback)
    audio._output_callback(_Buffer(len(playback)), 2_400, None, None)
    loud_input = _pcm16(30_000, 2_400)

    audio._input_callback(loud_input, 2_400, None, None)

    assert audio.read_input_chunk() == loud_input


def test_microphone_audio_passes_when_playback_is_silent():
    audio = talk_audio.DuplexAudio()
    silence = _Buffer(64)
    audio._output_callback(silence, 32, None, None)
    microphone = _pcm16(2_000)

    audio._input_callback(microphone, 32, None, None)

    assert audio.read_input_chunk() == microphone


def test_full_input_queue_drops_instead_of_blocking():
    audio = talk_audio.DuplexAudio()
    for _ in range(talk_audio.MAX_INPUT_BLOCKS + 10):
        audio._input_callback(b"\x00\x00", 1, None, None)

    assert audio._input.qsize() == talk_audio.MAX_INPUT_BLOCKS


def test_playback_spans_queue_chunk_boundaries():
    audio = talk_audio.DuplexAudio()
    audio.queue_playback(b"\x01\x02\x03\x04")
    audio.queue_playback(b"\x05\x06")

    out = _Buffer(6)
    audio._output_callback(out, 3, None, None)

    assert bytes(out.data) == b"\x01\x02\x03\x04\x05\x06"
    assert audio.played_ms == int(3 * 1000 / talk_audio.SAMPLE_RATE)


def test_underrun_is_padded_with_silence_and_not_counted():
    audio = talk_audio.DuplexAudio()
    audio.queue_playback(b"\x01\x02")

    out = _Buffer(8)
    audio._output_callback(out, 4, None, None)

    assert bytes(out.data) == b"\x01\x02\x00\x00\x00\x00\x00\x00"
    # Silence the operator never asked for must not inflate the truncate point.
    assert audio.played_ms == int(1 * 1000 / talk_audio.SAMPLE_RATE)


def test_drain_playback_discards_queue_and_residual():
    audio = talk_audio.DuplexAudio()
    audio.queue_playback(b"\x01\x02\x03\x04\x05\x06")
    audio._output_callback(_Buffer(2), 1, None, None)
    assert audio._residual  # a partial block is still buffered

    audio.drain_playback()

    out = _Buffer(4)
    audio._output_callback(out, 2, None, None)
    assert bytes(out.data) == b"\x00\x00\x00\x00"


def test_drain_returns_and_resets_the_atomic_heard_boundary():
    audio = talk_audio.DuplexAudio()
    audio.queue_playback(b"\x00\x00" * 2_400, item_id="item-1")
    audio._output_callback(_Buffer(4_800), 2_400, None, None)

    assert audio.played_ms == 100
    assert audio.drain_playback() == ("item-1", 100)
    assert audio.played_ms == 0


def test_callback_crossing_items_attributes_boundary_to_new_item():
    audio = talk_audio.DuplexAudio()
    audio.queue_playback(b"\x01\x00" * 2_400, item_id="old")
    audio.queue_playback(b"\x02\x00" * 1_200, item_id="new")

    audio._output_callback(_Buffer(7_200), 3_600, None, None)

    assert audio.drain_playback() == ("new", 50)


def test_empty_playback_is_ignored():
    audio = talk_audio.DuplexAudio()
    audio.queue_playback(b"")
    assert audio._playback.qsize() == 0


def test_full_playback_queue_drops_instead_of_blocking():
    audio = talk_audio.DuplexAudio()
    for _ in range(talk_audio.MAX_PLAYBACK_BLOCKS + 10):
        audio.queue_playback(b"\x00\x00")

    assert audio._playback.qsize() == talk_audio.MAX_PLAYBACK_BLOCKS


def test_stop_is_safe_before_start_and_twice():
    audio = talk_audio.DuplexAudio()
    audio.stop()
    audio.stop()


def test_pulse_stop_preserves_environment_when_never_started_or_load_failed(monkeypatch):
    monkeypatch.setenv("PULSE_SOURCE", "external-source")
    monkeypatch.setenv("PULSE_SINK", "external-sink")
    route = talk_audio._PulseWebRtcAudio()
    route.stop()
    route.stop()
    assert talk_audio.os.environ["PULSE_SOURCE"] == "external-source"
    assert talk_audio.os.environ["PULSE_SINK"] == "external-sink"

    monkeypatch.setattr(talk_audio.sys, "platform", "linux")
    monkeypatch.setattr(talk_audio.shutil, "which", lambda _command: "/usr/bin/pactl")
    monkeypatch.setattr(
        talk_audio.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("load failed")),
    )
    assert route.start(None, None) == (None, None)
    route.stop()
    assert talk_audio.os.environ["PULSE_SOURCE"] == "external-source"
    assert talk_audio.os.environ["PULSE_SINK"] == "external-sink"


@pytest.mark.parametrize("stop_order", [(0, 1), (1, 0)])
def test_concurrent_pulse_routes_restore_environment_in_either_order(
    monkeypatch, stop_order
):
    monkeypatch.setenv("PULSE_SOURCE", "original-source")
    monkeypatch.setenv("PULSE_SINK", "original-sink")
    monkeypatch.setattr(talk_audio.sys, "platform", "linux")
    monkeypatch.setattr(talk_audio.shutil, "which", lambda _command: "/usr/bin/pactl")
    module_ids = iter(("41\n", "42\n"))
    monkeypatch.setattr(
        talk_audio.subprocess,
        "run",
        lambda command, **_kwargs: _CompletedProcess(next(module_ids))
        if command[1] == "load-module"
        else _CompletedProcess(""),
    )
    routes = [talk_audio._PulseWebRtcAudio(), talk_audio._PulseWebRtcAudio()]
    routes[0].start(None, None)
    first_source = routes[0]._source_name
    routes[1].start(None, None)
    second_source = routes[1]._source_name
    assert first_source != second_source

    routes[stop_order[0]].stop()
    survivor = routes[stop_order[1]]
    assert talk_audio.os.environ["PULSE_SOURCE"] == survivor._source_name
    routes[stop_order[1]].stop()

    assert talk_audio.os.environ["PULSE_SOURCE"] == "original-source"
    assert talk_audio.os.environ["PULSE_SINK"] == "original-sink"
