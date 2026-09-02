"""Audio — the queue and barge-in logic, with no device and no sounddevice.

Only the pure half is exercised here: everything below is what runs between
the PortAudio callbacks, which is where a barge-in is won or lost. Opening a
real device is a canary step, not a CI step.
"""

from __future__ import annotations

import struct
import sys

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


def test_device_override_parses_index_or_name():
    assert talk_audio._device(None) is None
    assert talk_audio._device("3") == 3
    assert talk_audio._device("Speakers (Realtek)") == "Speakers (Realtek)"


def test_input_chunks_round_trip():
    audio = talk_audio.DuplexAudio()
    assert audio.read_input_chunk() is None

    audio._input_callback(b"\x01\x02", 1, None, None)

    assert audio.read_input_chunk() == b"\x01\x02"
    assert audio.read_input_chunk() is None

def _pcm16(amplitude: int, frames: int = 32) -> bytes:
    return struct.pack(f"<{frames}h", *([amplitude] * frames))

class _SpeechDetector:
    def __init__(self, speech: bool):
        self.speech = speech
        self.calls = 0

    def is_speech(self, _frame: bytes, sample_rate: int) -> bool:
        assert sample_rate == 8_000
        self.calls += 1
        return self.speech


def test_playback_echo_below_omp_barge_in_threshold_is_not_uploaded():
    audio = talk_audio.DuplexAudio()
    playback = _pcm16(20_000)
    audio.queue_playback(playback)
    audio._output_callback(_Buffer(len(playback)), 32, None, None)

    audio._input_callback(_pcm16(5_000), 32, None, None)

    assert audio.read_input_chunk() is None


def test_voice_above_omp_barge_in_threshold_interrupts_playback():
    detector = _SpeechDetector(True)
    audio = talk_audio.DuplexAudio(speech_detector=detector)
    playback = _pcm16(20_000, 2_400)
    audio.queue_playback(playback)
    audio._output_callback(_Buffer(len(playback)), 2_400, None, None)
    barge_in = _pcm16(30_000, 2_400)

    audio._input_callback(barge_in, 2_400, None, None)

    assert audio.read_input_chunk() == barge_in
    assert detector.calls > 0


def test_loud_room_noise_does_not_interrupt_playback():
    detector = _SpeechDetector(False)
    audio = talk_audio.DuplexAudio(speech_detector=detector)
    playback = _pcm16(20_000, 2_400)
    audio.queue_playback(playback)
    audio._output_callback(_Buffer(len(playback)), 2_400, None, None)

    audio._input_callback(_pcm16(30_000, 2_400), 2_400, None, None)

    assert detector.calls > 0
    assert audio.read_input_chunk() is None


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


def test_played_ms_survives_a_drain_until_reset():
    audio = talk_audio.DuplexAudio()
    audio.queue_playback(b"\x00\x00" * 2_400)
    audio._output_callback(_Buffer(4_800), 2_400, None, None)

    assert audio.played_ms == 100
    # The truncate has to be measured AFTER the drain, so the counter cannot
    # be cleared by the drain itself.
    audio.drain_playback()
    assert audio.played_ms == 100

    audio.reset_played_ms()
    assert audio.played_ms == 0


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
