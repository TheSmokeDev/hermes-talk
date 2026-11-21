"""An Amazon Echo running echo-dot-assist's hassmic (Wyoming mode) as a Talk surface.

hassmic keeps Amazon's audio front end (echo canceller, beamforming) and wake
word engine and serves the Wyoming satellite protocol on TCP 16700. This
module is the client that Home Assistant would otherwise be: it connects to
the Echo, and each wake word starts one Talk session whose microphone is the
Echo's and whose speaker is the Echo's. The session ends itself after a quiet
spell, and the Echo goes back to waiting for its wake word.

Duplex needs hassmic to keep streaming the microphone while it speaks; stock
hassmic stops it at the first reply, which leaves one exchange per wake word.
"""

from __future__ import annotations

import array
import asyncio
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

try:
    from . import talk_audio
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_audio

_log = logging.getLogger(__name__)

LANE = "echo"
DEFAULT_PORT = 16700
DEFAULT_IDLE_S = 20.0

#: hassmic's microphone stream, fixed in its proto_wyoming.c.
MIC_RATE = 16_000
RATE = talk_audio.SAMPLE_RATE
FRAME_BYTES = talk_audio.FRAME_BYTES

#: Audio handed to the Echo ahead of the clock. Barge-in cannot recall what
#: was already sent, so this is also how long the Echo talks over the
#: operator after an interruption: enough to ride out network jitter, short
#: enough not to be heard as ignoring them.
PLAYBACK_LEAD_S = 0.15
PACE_TICK_S = 0.02
#: Captured audio held while the provider session is still connecting after
#: the wake word, so the request said in one breath is not lost.
MAX_INPUT_BYTES = RATE * FRAME_BYTES * 5
MAX_PLAYBACK_BYTES = talk_audio.MAX_PLAYBACK_BYTES
#: Microphone level (normalized RMS) that counts as someone talking, for the
#: quiet timer only. hassmic brings speech to about -26 dBFS (0.05).
SPEECH_LEVEL = 0.02
#: While the Echo speaks, quieter microphone audio goes to the session as
#: silence: what Amazon's echo canceller leaves of the reply would otherwise
#: be heard as the operator, and answered. Someone talking over the reply is
#: louder and still cuts it. Normalized RMS; 0.02 is -34 dBFS.
DEFAULT_BARGE_IN_LEVEL = 0.02
#: How long the room still rings with the reply after the last audio played.
ECHO_TAIL_S = 0.3

CONNECT_TIMEOUT_S = 10.0
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0
MAX_HEADER_BYTES = 64 * 1024
MAX_PAYLOAD_BYTES = 1 << 20

#: Ends a run without the error animation (hassmic: core_no_speech).
QUIET_END_CODE = "stt-no-text-recognized"


# -- Wyoming framing ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WyomingEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    payload: bytes = b""


class WyomingError(Exception):
    """The peer sent something that is not a Wyoming event."""


def encode_event(
    event_type: str, data: dict[str, Any] | None = None, payload: bytes = b""
) -> bytes:
    """One event as header line, data block and payload, as hassmic writes them."""

    body = json.dumps(data, separators=(",", ":")).encode() if data else b""
    header: dict[str, Any] = {"type": event_type, "version": "1.5.4"}
    if body:
        header["data_length"] = len(body)
    if payload:
        header["payload_length"] = len(payload)
    return json.dumps(header, separators=(",", ":")).encode() + b"\n" + body + payload


def _length(header: dict[str, Any], key: str, limit: int) -> int:
    value = header.get(key) or 0
    if not isinstance(value, int) or not 0 <= value <= limit:
        raise WyomingError(f"bad {key}")
    return value


async def read_event(reader: asyncio.StreamReader) -> WyomingEvent | None:
    """The next event, or ``None`` when the peer closed the connection."""

    try:
        line = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            raise WyomingError("connection closed inside a header") from exc
        return None
    except asyncio.LimitOverrunError as exc:
        raise WyomingError("header too long") from exc
    try:
        header = json.loads(line)
    except ValueError as exc:
        raise WyomingError("header is not JSON") from exc
    if not isinstance(header, dict) or not isinstance(header.get("type"), str):
        raise WyomingError("header has no type")
    data = header.get("data") if isinstance(header.get("data"), dict) else {}
    data_length = _length(header, "data_length", MAX_HEADER_BYTES)
    payload_length = _length(header, "payload_length", MAX_PAYLOAD_BYTES)
    try:
        if data_length:
            extra = json.loads(await reader.readexactly(data_length))
            if isinstance(extra, dict):
                data = {**data, **extra}
        payload = await reader.readexactly(payload_length) if payload_length else b""
    except asyncio.IncompleteReadError as exc:
        raise WyomingError("connection closed inside an event") from exc
    except ValueError as exc:
        raise WyomingError("data block is not JSON") from exc
    return WyomingEvent(header["type"], data, payload)


# -- 16 kHz to 24 kHz -------------------------------------------------------------


class Upsampler:
    """16 kHz to 24 kHz mono PCM16, linearly interpolated, continuous across chunks.

    Three output samples per two input samples; the third needs the next
    group's first sample, so up to two samples (and an odd byte) wait for the
    next chunk instead of restarting the interpolation at every boundary.
    """

    def __init__(self) -> None:
        self._carry = array.array("h")
        self._odd = b""

    def feed(self, pcm: bytes) -> bytes:
        data = self._odd + pcm
        usable = len(data) - len(data) % 2
        self._odd = data[usable:]
        samples = self._carry
        samples.frombytes(data[:usable])
        out = array.array("h")
        groups = (len(samples) - 1) // 2
        for g in range(groups):
            x0, x1, x2 = samples[2 * g], samples[2 * g + 1], samples[2 * g + 2]
            out.append(x0)
            out.append((x0 + 2 * x1) // 3)
            out.append((2 * x1 + x2) // 3)
        self._carry = samples[2 * groups :]
        return out.tobytes()


# -- the audio device -------------------------------------------------------------


class EchoAudio:
    """DuplexAudio's session interface, over one Echo's Wyoming connection.

    Playback is paced here, not on the Echo: audio goes out at the rate it
    plays, :data:`PLAYBACK_LEAD_S` ahead, so a barge-in can still discard what
    has not been heard. ``write`` sends bytes to the Echo; it is called on the
    event loop that runs the pacing task.
    """

    def __init__(
        self,
        write: Callable[[bytes], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        barge_in_level: float = DEFAULT_BARGE_IN_LEVEL,
    ) -> None:
        self._write = write
        self._clock = clock
        self._barge_in_level = barge_in_level
        self._levels: dict[bool, list[float]] = {True: [], False: []}
        self._gated = 0
        self._lock = threading.Lock()
        self._upsampler = Upsampler()
        self._input: deque[bytes] = deque()
        self._input_bytes = 0
        self._input_paused = False
        self._dropped_input_blocks = 0
        self._playback: deque[tuple[str | None, bytes]] = deque()
        self._queued_playback_bytes = 0
        self._dropped_playback_bytes = 0
        self._consumed_playback_bytes = 0
        self._discarded_playback_bytes = 0
        self._played_item_id: str | None = None
        self._played_frames = 0
        self._sent_until = 0.0
        self._started_reply = False
        self._last_activity = clock()
        self._pacer: asyncio.Task | None = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._pacer is None or self._pacer.done():
            self._pacer = asyncio.get_running_loop().create_task(self._pace())

    def stop(self) -> None:
        if self._pacer is not None:
            self._pacer.cancel()
            self._pacer = None

    def end_event(self, *, failed: bool = False) -> bytes:
        """What hands the Echo back to its wake word when the session is over."""

        if failed:
            return encode_event(
                "error", {"text": "Talk session failed", "code": "talk-session-failed"}
            )
        if self._started_reply:
            return encode_event("audio-stop")
        return encode_event("error", {"text": "Talk session ended", "code": QUIET_END_CODE})

    # -- from the Echo ------------------------------------------------------------

    def feed_microphone(self, pcm16k: bytes) -> None:
        pcm = self._upsampler.feed(pcm16k)
        if not pcm:
            return
        level = talk_audio._pcm16_rms(pcm)
        with self._lock:
            echo_playing = self._sent_until + ECHO_TAIL_S > self._clock()
            levels = self._levels[echo_playing]
            if len(levels) < 20_000:
                levels.append(level)
            if echo_playing and level < self._barge_in_level:
                pcm = bytes(len(pcm))
                self._gated += 1
            elif level >= SPEECH_LEVEL:
                self._last_activity = self._clock()
            if self._input_paused:
                return
            self._input.append(pcm)
            self._input_bytes += len(pcm)
            # Newest audio wins, as on the terminal: stale speech corrupts the turn.
            while self._input_bytes > MAX_INPUT_BYTES:
                self._input_bytes -= len(self._input.popleft())
                self._dropped_input_blocks += 1

    def level_report(self) -> str:
        """Microphone levels with and without the Echo speaking, to tune the barge-in level."""

        def summary(values: list[float]) -> str:
            if not values:
                return "none"
            ordered = sorted(values)
            median, p95, top = (ordered[int(q * (len(ordered) - 1))] for q in (0.5, 0.95, 1.0))
            return f"median {median:.3f}, p95 {p95:.3f}, max {top:.3f} ({len(ordered)} chunks)"

        with self._lock:
            return (
                f"microphone while the Echo spoke: {summary(self._levels[True])}, "
                f"{self._gated} gated under {self._barge_in_level}; "
                f"otherwise: {summary(self._levels[False])}"
            )

    def quiet_s(self) -> float:
        """Seconds since anyone spoke, the Echo included."""

        with self._lock:
            speaking = self._queued_playback_bytes > 0 or self._sent_until > self._clock()
        if speaking:
            self._last_activity = self._clock()
        return self._clock() - self._last_activity

    # -- pacing ----------------------------------------------------------------

    async def _pace(self) -> None:
        while True:
            self.pace_once()
            await asyncio.sleep(PACE_TICK_S)

    def pace_once(self) -> None:
        """Send whatever is due at this moment. One tick of the pacing task."""

        now = self._clock()
        with self._lock:
            if not self._playback:
                return
            self._sent_until = max(self._sent_until, now)
            due = round((now + PLAYBACK_LEAD_S - self._sent_until) * RATE) * FRAME_BYTES
            if due <= 0:
                return
            chunk = self._take_playback(due)
            self._sent_until += len(chunk) / (RATE * FRAME_BYTES)
            begin = not self._started_reply
            self._started_reply = True
        if begin:
            self._write(encode_event("audio-start", {"rate": RATE, "width": 2, "channels": 1}))
        self._write(encode_event("audio-chunk", {"rate": RATE, "width": 2, "channels": 1}, chunk))

    def _take_playback(self, wanted: int) -> bytes:
        parts: list[bytes] = []
        while wanted > 0 and self._playback:
            item_id, data = self._playback.popleft()
            taken, rest = data[:wanted], data[wanted:]
            if rest:
                self._playback.appendleft((item_id, rest))
            parts.append(taken)
            wanted -= len(taken)
            self._queued_playback_bytes -= len(taken)
            self._consumed_playback_bytes += len(taken)
            if item_id != self._played_item_id:
                self._played_item_id = item_id
                self._played_frames = 0
            self._played_frames += len(taken) // FRAME_BYTES
        return b"".join(parts)

    # -- session interface (talk_audio.DuplexAudio) --------------------------------

    def read_input_chunk(self) -> bytes | None:
        with self._lock:
            if self._input_paused or not self._input:
                return None
            chunk = self._input.popleft()
            self._input_bytes -= len(chunk)
            return chunk

    def pause_input(self) -> None:
        with self._lock:
            self._input_paused = True
            self._input.clear()
            self._input_bytes = 0

    def resume_input(self) -> None:
        with self._lock:
            self._input_paused = False

    @property
    def input_paused(self) -> bool:
        with self._lock:
            return self._input_paused

    def queue_playback(self, pcm: bytes, item_id: str | None = None) -> None:
        if not pcm:
            return
        with self._lock:
            if self._queued_playback_bytes + len(pcm) > MAX_PLAYBACK_BYTES:
                self._dropped_playback_bytes += len(pcm)
                return
            self._playback.append((item_id, pcm))
            self._queued_playback_bytes += len(pcm)

    def drain_playback(self) -> tuple[str | None, int]:
        """Discard unsent audio and return the heard boundary.

        Up to :data:`PLAYBACK_LEAD_S` already on the Echo still plays; it is
        counted as heard, which it will be.
        """

        with self._lock:
            self._playback.clear()
            self._discarded_playback_bytes += self._queued_playback_bytes
            self._queued_playback_bytes = 0
            boundary = (self._played_item_id, int(self._played_frames * 1000 / RATE))
            self._played_item_id = None
            self._played_frames = 0
            return boundary

    @property
    def playback_pending(self) -> bool:
        with self._lock:
            return self._queued_playback_bytes > 0

    @property
    def playback_progress(self) -> talk_audio.PlaybackProgress:
        with self._lock:
            return talk_audio.PlaybackProgress(
                self._consumed_playback_bytes,
                self._discarded_playback_bytes + self._dropped_playback_bytes,
                self._queued_playback_bytes > 0,
            )

    @property
    def played_ms(self) -> int:
        with self._lock:
            return int(self._played_frames * 1000 / RATE)

    def reset_played_ms(self) -> None:
        with self._lock:
            self._played_item_id = None
            self._played_frames = 0

    @property
    def dropped_input_blocks(self) -> int:
        with self._lock:
            return self._dropped_input_blocks

    @property
    def dropped_playback_bytes(self) -> int:
        with self._lock:
            return self._dropped_playback_bytes


# -- the surface --------------------------------------------------------------------

SessionRunner = Callable[..., Awaitable[int]]


class _Conversation:
    """One wake word's Talk session, from the wake word until it goes quiet."""

    def __init__(
        self,
        write: Callable[[bytes], None],
        runner: SessionRunner,
        idle_s: float,
        on_end: Callable[[_Conversation], None],
        barge_in_level: float = DEFAULT_BARGE_IN_LEVEL,
    ) -> None:
        self.audio = EchoAudio(write, barge_in_level=barge_in_level)
        self._write = write
        self._idle_s = idle_s
        self._on_end = on_end
        self._session = asyncio.get_running_loop().create_task(runner(audio=self.audio, lane=LANE))
        self._watch = asyncio.get_running_loop().create_task(self._watch_quiet())

    async def _watch_quiet(self) -> None:
        while not self._session.done() and self.audio.quiet_s() < self._idle_s:
            await asyncio.sleep(0.25)
        failed = False
        if self._session.done():
            failed = (
                self._session.cancelled()
                or self._session.exception() is not None
                or bool(self._session.result())
            )
            if failed:
                _log.warning("echo: the Talk session ended unsuccessfully")
        else:
            _log.info("echo: quiet for %.0f s, ending the session", self._idle_s)
        await self.close(failed=failed)

    async def close(self, *, failed: bool = False, notify_echo: bool = True) -> None:
        self._on_end(self)
        if not self._session.done():
            self._session.cancel()
        await asyncio.gather(self._session, return_exceptions=True)
        self.audio.stop()
        _log.info("echo: %s", self.audio.level_report())
        if notify_echo:
            self._write(self.audio.end_event(failed=failed))
        if asyncio.current_task() is not self._watch:
            self._watch.cancel()


async def _linked(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    runner: SessionRunner,
    idle_s: float,
    barge_in_level: float,
) -> None:
    """Drive one connection to the Echo until it closes."""

    conversation: _Conversation | None = None

    def ended(c: _Conversation) -> None:
        nonlocal conversation
        if conversation is c:
            conversation = None

    writer.write(encode_event("describe"))
    writer.write(encode_event("run-satellite"))
    await writer.drain()
    try:
        while (event := await read_event(reader)) is not None:
            if event.type == "audio-chunk":
                if conversation is not None:
                    conversation.audio.feed_microphone(event.payload)
            elif event.type == "run-pipeline":
                if conversation is None:
                    _log.info("echo: wake word, starting a Talk session")
                    conversation = _Conversation(
                        writer.write, runner, idle_s, ended, barge_in_level
                    )
            elif event.type == "ping":
                writer.write(encode_event("pong"))
            elif event.type == "info":
                name = (event.data.get("satellite") or {}).get("name")
                _log.info("echo: linked to %s", name or "an unnamed satellite")
    finally:
        if conversation is not None:
            await conversation.close(notify_echo=False)


async def serve(
    host: str,
    port: int = DEFAULT_PORT,
    *,
    idle_s: float = DEFAULT_IDLE_S,
    barge_in_level: float = DEFAULT_BARGE_IN_LEVEL,
    runner: SessionRunner | None = None,
) -> None:
    """Stay linked to the Echo at ``host``, reconnecting with backoff. Runs until cancelled."""

    if runner is None:
        try:
            from . import talk_cli
        except ImportError:  # pragma: no cover - flat-module fallback
            import talk_cli
        runner = talk_cli.run_talk_session
    delay = RECONNECT_MIN_S
    while True:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, limit=MAX_HEADER_BYTES), CONNECT_TIMEOUT_S
            )
        except (OSError, TimeoutError) as exc:
            _log.warning(
                "echo: cannot reach %s:%d (%s), retrying in %.0f s", host, port, exc, delay
            )
        else:
            delay = RECONNECT_MIN_S
            try:
                await _linked(reader, writer, runner, idle_s, barge_in_level)
                _log.warning("echo: %s:%d closed the connection", host, port)
            except (OSError, WyomingError) as exc:
                _log.warning("echo: link to %s:%d lost (%s)", host, port, exc)
            finally:
                writer.close()
        await asyncio.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX_S)


def cli_entry(
    host: str,
    port: int = DEFAULT_PORT,
    idle_s: float = DEFAULT_IDLE_S,
    barge_in_level: float = DEFAULT_BARGE_IN_LEVEL,
) -> int:
    """``hermes talk echo HOST``: serve one Echo until Ctrl+C."""

    # Our own handler: the Hermes host configures the root logger first, which
    # makes logging.basicConfig a no-op and these lines vanish.
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("talk: %(message)s"))
    _log.addHandler(handler)
    _log.setLevel(logging.INFO)
    _log.propagate = False
    print(f"talk: serving the Echo at {host}:{port}; say its wake word. Ctrl+C to stop.")
    try:
        asyncio.run(serve(host, port, idle_s=idle_s, barge_in_level=barge_in_level))
    except KeyboardInterrupt:
        print("\ntalk: stopped.")
    return 0


__all__ = [
    "DEFAULT_IDLE_S",
    "DEFAULT_PORT",
    "LANE",
    "EchoAudio",
    "Upsampler",
    "WyomingError",
    "WyomingEvent",
    "cli_entry",
    "encode_event",
    "read_event",
    "serve",
]
