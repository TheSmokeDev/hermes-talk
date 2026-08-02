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
import queue
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


class DuplexAudio:
    """Full-duplex pcm16 capture and playback over PortAudio."""

    def __init__(self) -> None:
        self._input: queue.Queue[bytes] = queue.Queue(maxsize=MAX_INPUT_BLOCKS)
        self._playback: queue.Queue[bytes] = queue.Queue(maxsize=MAX_PLAYBACK_BLOCKS)
        self._residual = b""
        self._lock = threading.Lock()
        self._played_frames = 0
        self._in_stream = None
        self._out_stream = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Open both streams. Raises :class:`TalkAudioError` when it cannot."""

        sd = import_sounddevice()
        try:
            self._in_stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCKSIZE,
                device=_device(talk_config.audio_input_device()),
                channels=CHANNELS,
                dtype="int16",
                callback=self._input_callback,
            )
            self._out_stream = sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCKSIZE,
                device=_device(talk_config.audio_output_device()),
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

    # -- PortAudio callbacks (audio thread) -----------------------------------

    def _input_callback(self, indata, _frames, _time, _status) -> None:
        # Drop the block rather than stall the device: a blocked PortAudio
        # callback is a glitch the operator hears.
        with contextlib.suppress(queue.Full):
            self._input.put_nowait(bytes(indata))

    def _output_callback(self, outdata, frames, _time, _status) -> None:
        wanted = frames * FRAME_BYTES
        chunk = self._take_playback(wanted)
        outdata[: len(chunk)] = chunk
        if len(chunk) < wanted:
            outdata[len(chunk) :] = b"\x00" * (wanted - len(chunk))
        with self._lock:
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
