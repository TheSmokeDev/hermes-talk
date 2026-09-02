"""Duplex terminal audio — pcm16 mono 24 kHz in and out.

sounddevice is imported lazily and never at module scope: the plugin has to
import cleanly on a headless box with no PortAudio, and the audio extra is
optional (``pip install "hermes-talk[audio]"``). Same pattern Hermes uses in
``tools/voice_mode.py``.

The PortAudio callbacks run on their own thread and touch nothing but plain
queues — no asyncio, no locks held across a device call. The async session
polls :meth:`DuplexAudio.read_input_chunk` and pushes with
:meth:`DuplexAudio.queue_playback`.

:attr:`DuplexAudio.played_ms` counts audio actually handed to the speaker,
which is what a barge-in truncate has to be measured in: the server has
already generated far more than the operator heard.
"""

from __future__ import annotations

import contextlib
import math
import os
import queue
import shutil
import subprocess
import sys
import threading

try:
    from . import talk_config
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_config

SAMPLE_RATE = 24_000
CHANNELS = 1
SAMPLE_WIDTH = 2
BLOCKSIZE = 2_400  # 100 ms at 24 kHz
FRAME_BYTES = SAMPLE_WIDTH * CHANNELS

#: Bounded so a stalled reader cannot grow the process without limit. Input is
#: the tighter of the two: stale microphone audio is worse than dropped audio.
MAX_INPUT_BLOCKS = 50
MAX_PLAYBACK_BLOCKS = 200

# Match OMP's live controller: while model audio is playing, microphone blocks
# below the acoustic echo floor are local playback leakage, not barge-in.
OUTPUT_ACTIVE_LEVEL = 0.015
MIN_BARGE_IN_LEVEL = 0.04
OUTPUT_ECHO_RATIO = 0.65
VAD_SAMPLE_RATE = 8_000
VAD_FRAME_SAMPLES = 240  # 30 ms
VAD_FRAME_BYTES = VAD_FRAME_SAMPLES * SAMPLE_WIDTH
VAD_SPEECH_FRAMES = 2  # 60 ms filters transient room noise, stays under OMP's 150 ms target

_INSTALL_HINT = (
    'audio support is not installed — run: pip install "hermes-talk[audio]" '
    "(needs PortAudio; on Debian/Ubuntu also: apt install libportaudio2)"
)


class TalkAudioError(Exception):
    """Audio devices are unusable."""


def import_sounddevice():
    """Lazy-import sounddevice with an actionable failure."""

    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:
        raise TalkAudioError(f"{_INSTALL_HINT} ({exc})") from exc
    return sd

def import_webrtcvad():
    """Lazy-import the speech classifier shipped by the audio extra."""

    try:
        import webrtcvad
    except (ImportError, OSError) as exc:
        raise TalkAudioError(f"{_INSTALL_HINT} ({exc})") from exc
    return webrtcvad


def audio_available() -> bool:
    """True when a duplex session could actually open devices."""

    try:
        import_sounddevice()
    except TalkAudioError:
        return False
    return True


def _device(raw: str | None) -> str | int | None:
    """sounddevice accepts a name or an index; env vars only carry text."""

    if raw is None:
        return None
    return int(raw) if raw.lstrip("-").isdigit() else raw

def _pcm16_rms(pcm: bytes) -> float:
    """Normalized RMS for little-endian mono PCM16 without copying samples."""

    if not pcm:
        return 0.0
    samples = memoryview(pcm).cast("h")
    sum_squares = sum(sample * sample for sample in samples)
    return min(1.0, math.sqrt(sum_squares / len(samples)) / 32_768)

def _downsample_24k_to_8k(pcm: bytes) -> bytes:
    """Decimate PCM16 by three for WebRTC VAD's supported 8 kHz input."""

    source = memoryview(pcm).cast("h")
    target = bytearray((len(source) // 3) * SAMPLE_WIDTH)
    samples = memoryview(target).cast("h")
    for index in range(len(samples)):
        samples[index] = source[index * 3]
    return bytes(target)


class _PulseWebRtcAudio:
    """Process-local PulseAudio WebRTC AEC/NS route for default Linux devices."""

    def __init__(self) -> None:
        self._pactl: str | None = None
        self._module_id: str | None = None
        self._previous_source: str | None = None
        self._previous_sink: str | None = None

    @property
    def active(self) -> bool:
        """Whether capture already comes from PulseAudio's echo canceller."""

        return self._module_id is not None

    def start(
        self,
        input_device: str | int | None,
        output_device: str | int | None,
    ) -> tuple[str | int | None, str | int | None]:
        if input_device is not None or output_device is not None or sys.platform != "linux":
            return input_device, output_device
        pactl = shutil.which("pactl")
        if pactl is None:
            return input_device, output_device

        suffix = str(os.getpid())
        source_name = f"hermes_talk_aec_{suffix}"
        sink_name = f"hermes_talk_aec_sink_{suffix}"
        try:
            result = subprocess.run(
                [
                    pactl,
                    "load-module",
                    "module-echo-cancel",
                    "aec_method=webrtc",
                    f"source_name={source_name}",
                    f"sink_name={sink_name}",
                    "aec_args=analog_gain_control=0 digital_gain_control=1 noise_suppression=1",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            module_id = result.stdout.strip()
            if not module_id.isdigit():
                raise ValueError("pactl returned no module id")
        except (OSError, subprocess.SubprocessError, ValueError):
            return input_device, output_device

        self._pactl = pactl
        self._module_id = module_id
        self._previous_source = os.environ.get("PULSE_SOURCE")
        self._previous_sink = os.environ.get("PULSE_SINK")
        os.environ["PULSE_SOURCE"] = source_name
        os.environ["PULSE_SINK"] = sink_name
        return "pulse", "pulse"

    def stop(self) -> None:
        if self._previous_source is None:
            os.environ.pop("PULSE_SOURCE", None)
        else:
            os.environ["PULSE_SOURCE"] = self._previous_source
        if self._previous_sink is None:
            os.environ.pop("PULSE_SINK", None)
        else:
            os.environ["PULSE_SINK"] = self._previous_sink

        if self._pactl is not None and self._module_id is not None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    [self._pactl, "unload-module", self._module_id],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
        self._pactl = None
        self._module_id = None


class DuplexAudio:
    """Full-duplex pcm16 capture and playback over PortAudio."""

    def __init__(self, speech_detector=None) -> None:
        self._input: queue.Queue[bytes] = queue.Queue(maxsize=MAX_INPUT_BLOCKS)
        self._playback: queue.Queue[bytes] = queue.Queue(maxsize=MAX_PLAYBACK_BLOCKS)
        self._residual = b""
        self._lock = threading.Lock()
        self._played_frames = 0
        self._output_level = 0.0
        self._speech_detector = speech_detector
        self._speech_run_frames = 0
        self._in_stream = None
        self._out_stream = None
        self._pulse_webrtc = _PulseWebRtcAudio()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Open both streams. Raises :class:`TalkAudioError` when it cannot."""

        sd = import_sounddevice()
        if self._speech_detector is None:
            self._speech_detector = import_webrtcvad().Vad(3)
        input_device = _device(talk_config.audio_input_device())
        output_device = _device(talk_config.audio_output_device())
        input_device, output_device = self._pulse_webrtc.start(input_device, output_device)
        try:
            self._in_stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCKSIZE,
                device=input_device,
                channels=CHANNELS,
                dtype="int16",
                callback=self._input_callback,
            )
            self._out_stream = sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCKSIZE,
                device=output_device,
                channels=CHANNELS,
                dtype="int16",
                callback=self._output_callback,
            )
            self._in_stream.start()
            self._out_stream.start()
        except Exception as exc:  # PortAudio raises its own exception types
            self.stop()
            raise TalkAudioError(f"could not open audio devices: {exc}") from exc

    def stop(self) -> None:
        """Close both streams. Safe to call twice, and on a failed start."""

        for attr in ("_in_stream", "_out_stream"):
            stream = getattr(self, attr, None)
            if stream is None:
                continue
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass
            setattr(self, attr, None)
        self._pulse_webrtc.stop()

    def _contains_speech(self, pcm: bytes) -> bool:
        detector = self._speech_detector
        if detector is None:
            return True
        downsampled = _downsample_24k_to_8k(pcm)
        for offset in range(0, len(downsampled) - VAD_FRAME_BYTES + 1, VAD_FRAME_BYTES):
            frame = downsampled[offset : offset + VAD_FRAME_BYTES]
            if detector.is_speech(frame, VAD_SAMPLE_RATE):
                self._speech_run_frames += 1
                if self._speech_run_frames >= VAD_SPEECH_FRAMES:
                    return True
            else:
                self._speech_run_frames = 0
        return False

    # -- PortAudio callbacks (audio thread) -----------------------------------

    def _input_callback(self, indata, _frames, _time, _status) -> None:
        pcm = bytes(indata)
        input_level = _pcm16_rms(pcm)
        with self._lock:
            output_level = self._output_level
        if not self._pulse_webrtc.active:
            output_active = output_level > OUTPUT_ACTIVE_LEVEL
            echo_threshold = max(MIN_BARGE_IN_LEVEL, output_level * OUTPUT_ECHO_RATIO)
            if output_active and input_level < echo_threshold:
                return
            if output_active and not self._contains_speech(pcm):
                return
        # Drop the block rather than stall the device: a blocked PortAudio
        # callback is a glitch the operator hears.
        with contextlib.suppress(queue.Full):
            self._input.put_nowait(pcm)

    def _output_callback(self, outdata, frames, _time, _status) -> None:
        wanted = frames * FRAME_BYTES
        chunk = self._take_playback(wanted)
        outdata[: len(chunk)] = chunk
        if len(chunk) < wanted:
            outdata[len(chunk) :] = b"\x00" * (wanted - len(chunk))
        with self._lock:
            self._output_level = _pcm16_rms(chunk)
            self._played_frames += len(chunk) // FRAME_BYTES

    def _take_playback(self, wanted: int) -> bytes:
        buf = self._residual
        while len(buf) < wanted:
            try:
                buf += self._playback.get_nowait()
            except queue.Empty:
                break
        self._residual = buf[wanted:]
        return buf[:wanted]

    # -- session interface ----------------------------------------------------

    def read_input_chunk(self) -> bytes | None:
        """One captured block, or ``None`` when the microphone has nothing yet."""

        try:
            return self._input.get_nowait()
        except queue.Empty:
            return None

    def queue_playback(self, pcm: bytes) -> None:
        """Queue model audio for the speaker. Drops on overflow, never blocks."""

        if not pcm:
            return
        with contextlib.suppress(queue.Full):
            self._playback.put_nowait(pcm)

    def drain_playback(self) -> None:
        """Barge-in: stop speaking now. Everything not yet played is discarded."""

        while True:
            try:
                self._playback.get_nowait()
            except queue.Empty:
                break
        self._residual = b""

    @property
    def played_ms(self) -> int:
        """Milliseconds of the current response actually sent to the speaker."""

        with self._lock:
            return int(self._played_frames * 1000 / SAMPLE_RATE)

    def reset_played_ms(self) -> None:
        """Zero the counter — called when a new response starts speaking."""

        with self._lock:
            self._played_frames = 0


__all__ = [
    "BLOCKSIZE",
    "CHANNELS",
    "FRAME_BYTES",
    "SAMPLE_RATE",
    "SAMPLE_WIDTH",
    "DuplexAudio",
    "TalkAudioError",
    "audio_available",
    "import_sounddevice",
]
