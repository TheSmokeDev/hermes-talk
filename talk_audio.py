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
import uuid
from typing import ClassVar

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
MAX_PLAYBACK_BYTES = SAMPLE_RATE * FRAME_BYTES * 20

# Match OMP's live controller: while model audio is playing, microphone blocks
# below the acoustic echo floor are local playback leakage, not barge-in.
# This software gate runs on every platform whenever PulseAudio WebRTC AEC is
# not active (always on Windows/macOS). On headphones there is no echo to
# suppress, so the operator can retune or disable it:
#   TALK_ECHO_GATE=off                       disable the gate entirely
#   TALK_ECHO_GATE_OUTPUT_ACTIVE_LEVEL=0.015 playback RMS that counts as "active"
#   TALK_ECHO_GATE_MIN_BARGE_IN_LEVEL=0.04   floor a mic block must clear
#   TALK_ECHO_GATE_RATIO=0.65                 fraction of playback RMS a block must clear
# Read when DuplexAudio is constructed, not at import.
OUTPUT_ACTIVE_LEVEL = 0.015
MIN_BARGE_IN_LEVEL = 0.04
OUTPUT_ECHO_RATIO = 0.65


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _echo_gate_settings() -> tuple[bool, float, float, float]:
    """(enabled, output_active_level, min_barge_in_level, echo_ratio) from env."""

    raw = (os.environ.get("TALK_ECHO_GATE") or "").strip().lower()
    enabled = raw not in {"0", "off", "false", "no", "disabled"}
    return (
        enabled,
        _env_float("TALK_ECHO_GATE_OUTPUT_ACTIVE_LEVEL", OUTPUT_ACTIVE_LEVEL),
        _env_float("TALK_ECHO_GATE_MIN_BARGE_IN_LEVEL", MIN_BARGE_IN_LEVEL),
        _env_float("TALK_ECHO_GATE_RATIO", OUTPUT_ECHO_RATIO),
    )

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



def audio_available() -> bool:
    """True when all dependencies needed by a duplex session are importable."""

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



class _PulseWebRtcAudio:
    """Process-local PulseAudio WebRTC AEC/NS route for default Linux devices."""

    _environment_lock = threading.Lock()
    _active_routes: ClassVar[list[_PulseWebRtcAudio]] = []
    _baseline_source: object | str = object()
    _baseline_sink: object | str = object()
    _missing = object()

    def __init__(self) -> None:
        self._pactl: str | None = None
        self._module_id: str | None = None
        self._source_name: str | None = None
        self._sink_name: str | None = None
        self._changed_environment = False

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

        suffix = f"{os.getpid()}_{uuid.uuid4().hex}"
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
        self._source_name = source_name
        self._sink_name = sink_name
        with self._environment_lock:
            if not self._active_routes:
                type(self)._baseline_source = os.environ.get("PULSE_SOURCE", self._missing)
                type(self)._baseline_sink = os.environ.get("PULSE_SINK", self._missing)
            self._active_routes.append(self)
            os.environ["PULSE_SOURCE"] = source_name
            os.environ["PULSE_SINK"] = sink_name
            self._changed_environment = True
        return "pulse", "pulse"

    @classmethod
    def _restore(cls, name: str, baseline: object | str) -> None:
        if baseline is cls._missing:
            os.environ.pop(name, None)
        else:
            os.environ[name] = baseline

    def stop(self) -> None:
        with self._environment_lock:
            if self._changed_environment:
                self._changed_environment = False
                with contextlib.suppress(ValueError):
                    self._active_routes.remove(self)
                managed_values = {
                    route._source_name
                    for route in self._active_routes
                } | {self._source_name}
                if os.environ.get("PULSE_SOURCE") in managed_values:
                    if self._active_routes:
                        os.environ["PULSE_SOURCE"] = self._active_routes[-1]._source_name
                    else:
                        self._restore("PULSE_SOURCE", self._baseline_source)
                managed_values = {
                    route._sink_name
                    for route in self._active_routes
                } | {self._sink_name}
                if os.environ.get("PULSE_SINK") in managed_values:
                    if self._active_routes:
                        os.environ["PULSE_SINK"] = self._active_routes[-1]._sink_name
                    else:
                        self._restore("PULSE_SINK", self._baseline_sink)

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
        self._source_name = None
        self._sink_name = None


class DuplexAudio:
    """Full-duplex pcm16 capture and playback over PortAudio."""

    _startup_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        self._input: queue.Queue[bytes] = queue.Queue(maxsize=MAX_INPUT_BLOCKS)
        self._playback: queue.Queue[tuple[str | None, bytes]] = queue.Queue(
            maxsize=MAX_PLAYBACK_BLOCKS
        )
        self._residual: tuple[str | None, bytes] | None = None
        self._queued_playback_bytes = 0
        self._lock = threading.Lock()
        self._played_item_id: str | None = None
        self._dropped_input_blocks = 0
        self._dropped_playback_bytes = 0
        self._played_frames = 0
        self._output_level = 0.0
        self._in_stream = None
        self._out_stream = None
        self._pulse_webrtc = _PulseWebRtcAudio()
        (
            self._echo_gate_enabled,
            self._output_active_level,
            self._min_barge_in_level,
            self._output_echo_ratio,
        ) = _echo_gate_settings()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Open both streams. Raises :class:`TalkAudioError` when it cannot."""

        sd = import_sounddevice()
        original_input = _device(talk_config.audio_input_device())
        original_output = _device(talk_config.audio_output_device())
        with self._startup_lock:
            input_device, output_device = self._pulse_webrtc.start(
                original_input, original_output
            )
            try:
                self._open_streams(sd, input_device, output_device)
            except Exception as routed_exc:  # PortAudio raises its own exception types
                self._close_streams()
                if not self._pulse_webrtc.active:
                    raise TalkAudioError(
                        f"could not open audio devices: {routed_exc}"
                    ) from routed_exc
                self._pulse_webrtc.stop()
                try:
                    self._open_streams(sd, original_input, original_output)
                except Exception as exc:
                    self._close_streams()
                    raise TalkAudioError(f"could not open audio devices: {exc}") from exc

    def _open_streams(self, sd, input_device, output_device) -> None:
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

    def _close_streams(self) -> None:
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

    def stop(self) -> None:
        """Close both streams. Safe to call twice, and on a failed start."""

        self._close_streams()
        self._pulse_webrtc.stop()


    # -- PortAudio callbacks (audio thread) -----------------------------------

    def _input_callback(self, indata, _frames, _time, _status) -> None:
        pcm = bytes(indata)
        input_level = _pcm16_rms(pcm)
        with self._lock:
            output_level = self._output_level
        if self._echo_gate_enabled and not self._pulse_webrtc.active:
            output_active = output_level > self._output_active_level
            echo_threshold = max(
                self._min_barge_in_level,
                output_level * self._output_echo_ratio,
            )
            if output_active and input_level < echo_threshold:
                return
        try:
            self._input.put_nowait(pcm)
        except queue.Full:
            # Capture is realtime: preserving a five-second-old block while
            # discarding the operator's current speech corrupts the turn.
            # Evict one oldest block and retain the newest available audio.
            with contextlib.suppress(queue.Empty):
                self._input.get_nowait()
            with contextlib.suppress(queue.Full):
                self._input.put_nowait(pcm)
            with self._lock:
                self._dropped_input_blocks += 1

    def _output_callback(self, outdata, frames, _time, _status) -> None:
        wanted = frames * FRAME_BYTES
        with self._lock:
            chunk = self._take_playback(wanted)
        output_level = _pcm16_rms(chunk)
        with self._lock:
            self._output_level = output_level
        outdata[: len(chunk)] = chunk
        if len(chunk) < wanted:
            outdata[len(chunk) :] = b"\x00" * (wanted - len(chunk))

    def _take_playback(self, wanted: int) -> bytes:
        parts: list[bytes] = []
        remaining = wanted
        packet = self._residual
        self._residual = None
        while remaining > 0:
            if packet is None:
                try:
                    packet = self._playback.get_nowait()
                except queue.Empty:
                    break
            item_id, data = packet
            taken = data[:remaining]
            parts.append(taken)
            self._queued_playback_bytes -= len(taken)
            played_frames = len(taken) // FRAME_BYTES
            if played_frames:
                if item_id != self._played_item_id:
                    self._played_item_id = item_id
                    self._played_frames = 0
                self._played_frames += played_frames
            remaining -= len(taken)
            packet = (item_id, data[len(taken) :]) if len(taken) < len(data) else None
        self._residual = packet
        return b"".join(parts)

    # -- session interface ----------------------------------------------------

    def read_input_chunk(self) -> bytes | None:
        """One captured block, or ``None`` when the microphone has nothing yet."""

        try:
            return self._input.get_nowait()
        except queue.Empty:
            return None

    def queue_playback(self, pcm: bytes, item_id: str | None = None) -> None:
        """Queue model audio for the speaker. Drops on overflow, never blocks."""

        if not pcm:
            return
        with self._lock:
            if self._queued_playback_bytes + len(pcm) > MAX_PLAYBACK_BYTES:
                self._dropped_playback_bytes += len(pcm)
                return
            try:
                self._playback.put_nowait((item_id, pcm))
            except queue.Full:
                self._dropped_playback_bytes += len(pcm)
                return
            self._queued_playback_bytes += len(pcm)

    def drain_playback(self) -> tuple[str | None, int]:
        """Discard unheard audio and atomically return the heard boundary."""

        with self._lock:
            while True:
                try:
                    self._playback.get_nowait()
                except queue.Empty:
                    break
            self._residual = None
            self._queued_playback_bytes = 0
            boundary = (
                self._played_item_id,
                int(self._played_frames * 1000 / SAMPLE_RATE),
            )
            self._played_item_id = None
            self._played_frames = 0
            return boundary

    @property
    def played_ms(self) -> int:
        """Milliseconds of the current response actually sent to the speaker."""

        with self._lock:
            return int(self._played_frames * 1000 / SAMPLE_RATE)

    def reset_played_ms(self) -> None:
        """Reset legacy callers that do not attach item identity to audio."""

        with self._lock:
            self._played_item_id = None
            self._played_frames = 0

    @property
    def dropped_input_blocks(self) -> int:
        """Count capture overflows handled with a newest-audio preference."""

        with self._lock:
            return self._dropped_input_blocks

    @property
    def dropped_playback_bytes(self) -> int:
        """Count provider audio bytes rejected by playback capacity limits."""

        with self._lock:
            return self._dropped_playback_bytes


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
