"""Discord voice as an audio device — the same seven methods, a different room.

:class:`DiscordAudio` implements exactly the surface
:class:`talk_audio.DuplexAudio` exposes (``start`` / ``stop`` /
``read_input_chunk`` / ``queue_playback`` / ``drain_playback`` /
``played_ms`` / ``reset_played_ms``), so the Realtime session, its tool
calls, the steering ledger, and the announcement pump all run unchanged.
Only the room changes: instead of a microphone and a speaker, the frames
come from and go to a Discord voice channel.

**We do not open a Discord connection.** The host already has one: its
Discord adapter joins a channel, receives RTP, decrypts DAVE, and decodes
Opus to 48 kHz stereo PCM in this same process. We borrow that — one live
connection, one bot, the host's own E2EE handling. The cost is that the
host exposes no plugin hook for voice, so we reach the adapter through the
runner reference the host's own tools use (``gateway.run._gateway_runner_ref``).
That coupling is deliberate and contained to :func:`resolve_voice_bridge`;
every attribute it touches is checked, and a host that renames any of them
degrades to a spoken refusal instead of an exception.

**Rates.** The host's Discord path is 48 kHz stereo s16le; a Realtime
session is 24 kHz mono s16le. That is an exact 2:1 ratio in both
directions, so the conversions here are integer decimation and linear
interpolation over :mod:`array` — no resampler dependency, and in
particular no ``audioop``, which was removed from the standard library in
Python 3.13 (this package supports 3.11 through 3.13).

**Threading.** Frames arrive on the host's socket-reader thread and leave
on its player thread; the session runs on an asyncio loop. Every hand-off
is a bounded :class:`queue.Queue`, exactly as the PortAudio path does it —
no locks are held across a yield, and no callback ever blocks.
"""

from __future__ import annotations

import array
import logging
import queue
import threading
from contextlib import suppress
from typing import Any

try:
    from . import talk_audio
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_audio

_log = logging.getLogger(__name__)

#: Discord's wire geometry. 20 ms of 48 kHz stereo s16le.
DISCORD_SAMPLE_RATE = 48_000
DISCORD_CHANNELS = 2
DISCORD_FRAME_BYTES = 3_840

#: A silent Discord frame. Returning this — never ``b""`` — is load-bearing:
#: an empty read ends the host's player thread, and the session goes mute
#: for good with no error anywhere.
SILENCE_FRAME = b"\x00" * DISCORD_FRAME_BYTES

#: Bounded like the PortAudio path: overflow drops the oldest audio rather
#: than growing without limit when one side stalls.
MAX_INPUT_FRAMES = 100  # 2 s of inbound speech
MAX_OUTPUT_FRAMES = 250  # 5 s of queued reply


class TalkDiscordError(Exception):
    """The host's Discord voice surface is unavailable or unrecognized."""


# -- rate conversion ----------------------------------------------------------


def discord_to_session(pcm48_stereo: bytes) -> bytes:
    """48 kHz stereo → 24 kHz mono. Both steps average, never drop.

    Averaging the channel pair and then the sample pair is a crude two-tap
    low-pass, which is the point: plain decimation would alias the top of
    the band straight back down into speech.
    """

    if not pcm48_stereo:
        return b""
    samples = array.array("h")
    samples.frombytes(pcm48_stereo[: len(pcm48_stereo) - (len(pcm48_stereo) % 2)])
    out = array.array("h")
    # Two stereo frames (4 samples) collapse to one mono sample.
    for i in range(0, len(samples) - 3, 4):
        total = samples[i] + samples[i + 1] + samples[i + 2] + samples[i + 3]
        out.append(int(total / 4))
    return out.tobytes()


def session_to_discord(pcm24_mono: bytes, *, carry: int | None = None) -> tuple[bytes, int | None]:
    """24 kHz mono → 48 kHz stereo, linearly interpolated.

    ``carry`` is the previous chunk's final sample; threading it through
    keeps the interpolation continuous across chunk boundaries. Restarting
    at zero every chunk puts a click at every boundary — audible as the
    voice "ticking" while it speaks.
    """

    if not pcm24_mono:
        return b"", carry
    samples = array.array("h")
    samples.frombytes(pcm24_mono[: len(pcm24_mono) - (len(pcm24_mono) % 2)])
    if not samples:
        return b"", carry
    out = array.array("h")
    previous = samples[0] if carry is None else carry
    for sample in samples:
        midpoint = int((previous + sample) / 2)
        # Each mono sample becomes two stereo frames: the interpolated
        # midpoint, then the sample itself.
        out.append(midpoint)
        out.append(midpoint)
        out.append(sample)
        out.append(sample)
        previous = sample
    return out.tobytes(), previous


# -- the host's voice surface -------------------------------------------------


def resolve_voice_bridge(guild_id: int | None = None) -> dict[str, Any]:
    """Find the host's live Discord voice connection. Raises on any miss.

    Returns ``{guild_id, voice_client, receiver, adapter}``. Every lookup
    is guarded: this reaches into host internals that carry no stability
    promise, so a rename must surface as one refusal sentence rather than a
    traceback on the voice path.
    """

    try:
        from gateway.run import _gateway_runner_ref  # host-only, lazy by design
    except Exception as exc:  # no host in this process
        raise TalkDiscordError(
            "this build has no Hermes gateway to borrow a voice connection from"
        ) from exc

    runner = _gateway_runner_ref() if callable(_gateway_runner_ref) else None
    if runner is None:
        raise TalkDiscordError("the Hermes gateway isn't running in this process")

    adapter = None
    try:
        from models import Platform

        adapter = (getattr(runner, "adapters", None) or {}).get(Platform.DISCORD)
    except Exception:  # noqa: BLE001 — fall through to the refusal below
        adapter = None
    if adapter is None:
        raise TalkDiscordError("the Discord adapter isn't loaded on this gateway")

    clients = getattr(adapter, "_voice_clients", None)
    receivers = getattr(adapter, "_voice_receivers", None)
    if not isinstance(clients, dict) or not isinstance(receivers, dict):
        raise TalkDiscordError(
            "this Hermes version's Discord voice internals aren't in the shape I know"
        )
    if not clients:
        raise TalkDiscordError("I'm not in a voice channel — run the join command first")

    if guild_id is None:
        if len(clients) > 1:
            raise TalkDiscordError(
                "I'm in more than one voice channel — say which server"
            )
        guild_id = next(iter(clients))

    voice_client = clients.get(guild_id)
    if voice_client is None:
        raise TalkDiscordError("I'm not in a voice channel on that server")
    return {
        "guild_id": guild_id,
        "voice_client": voice_client,
        "receiver": receivers.get(guild_id),
        "adapter": adapter,
    }


class _RealtimeSource:
    """The host's outbound audio, fed live instead of from a finished file.

    Duck-typed rather than subclassed: ``discord.AudioSource`` only requires
    ``read()``, ``is_opus()``, and ``cleanup()``, and importing the host's
    discord module at class-definition time would make this module
    unimportable off-host (where the whole test suite runs).
    """

    def __init__(self, frames: queue.Queue) -> None:
        self._frames = frames
        self._carry = bytearray()
        self.frames_served = 0

    def read(self) -> bytes:
        while len(self._carry) < DISCORD_FRAME_BYTES:
            try:
                self._carry += self._frames.get_nowait()
            except queue.Empty:
                break
        if len(self._carry) >= DISCORD_FRAME_BYTES:
            frame = bytes(self._carry[:DISCORD_FRAME_BYTES])
            del self._carry[:DISCORD_FRAME_BYTES]
            self.frames_served += 1
            return frame
        # Underrun: silence, never b"". An empty read retires the player
        # thread and the call goes permanently mute.
        return SILENCE_FRAME

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self._carry.clear()


class DiscordAudio:
    """A voice channel wearing :class:`talk_audio.DuplexAudio`'s interface."""

    def __init__(self, guild_id: int | None = None) -> None:
        self._guild_id = guild_id
        self._bridge: dict[str, Any] | None = None
        self._source: _RealtimeSource | None = None
        self._inbound: queue.Queue[bytes] = queue.Queue(maxsize=MAX_INPUT_FRAMES)
        self._outbound: queue.Queue[bytes] = queue.Queue(maxsize=MAX_OUTPUT_FRAMES)
        self._lock = threading.Lock()
        self._played_frames = 0
        self._carry_sample: int | None = None
        self._original_on_packet = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Borrow the host's connection. Raises :class:`TalkAudioError` on a miss.

        The error type is the PortAudio path's on purpose — the session's
        startup already turns that into one spoken line, and a voice
        surface should fail the same way whichever room it's in.
        """

        try:
            bridge = resolve_voice_bridge(self._guild_id)
        except TalkDiscordError as exc:
            raise talk_audio.TalkAudioError(str(exc)) from exc

        receiver = bridge.get("receiver")
        if receiver is None or not hasattr(receiver, "_on_packet"):
            raise talk_audio.TalkAudioError(
                "the voice connection has no receiver I can listen through"
            )

        self._bridge = bridge
        self._source = _RealtimeSource(self._outbound)

        # Tap the host's receive path. We wrap rather than replace so the
        # host's own transcription keeps working — its silence-gated turn
        # loop and our continuous stream read the same frames.
        original = receiver._on_packet

        def tapped(data: bytes) -> None:
            try:
                original(data)
            finally:
                self._drain_receiver(receiver)

        self._original_on_packet = original
        receiver._on_packet = tapped

        voice_client = bridge["voice_client"]
        try:
            if getattr(voice_client, "is_playing", lambda: False)():
                voice_client.stop()
            voice_client.play(self._source)
        except Exception as exc:  # host playback surface, any type
            self.stop()
            raise talk_audio.TalkAudioError(
                f"couldn't take over playback in the voice channel: {exc}"
            ) from exc

    def stop(self) -> None:
        """Hand the connection back. Safe twice, and after a failed start."""

        bridge, self._bridge = self._bridge, None
        # Teardown is best-effort at every step: a half-torn-down bridge
        # must still hand the connection back to the host.
        if bridge is not None and self._original_on_packet is not None:
            with suppress(Exception):
                bridge.get("receiver")._on_packet = self._original_on_packet
        self._original_on_packet = None
        if bridge is not None:
            with suppress(Exception):
                bridge["voice_client"].stop()
        self._source = None

    # -- capture (host socket-reader thread) ----------------------------------

    def _drain_receiver(self, receiver: Any) -> None:
        """Move whatever the host just decoded into our queue, at our rate.

        The host buffers PCM per SSRC and releases it only after 1.5 s of
        silence — a turn boundary. A realtime session cannot wait for that,
        so we take the buffers continuously and leave the host's own gate
        to its own bookkeeping.
        """

        try:
            buffers = getattr(receiver, "_buffers", None)
            if not buffers:
                return
            lock = getattr(receiver, "_lock", None)
            if lock is not None:
                with lock:
                    chunks = [bytes(buf) for buf in buffers.values() if buf]
                    for buf in buffers.values():
                        del buf[:]
            else:  # pragma: no cover - every shipped host has the lock
                chunks = [bytes(buf) for buf in buffers.values() if buf]
                for buf in buffers.values():
                    del buf[:]
            for chunk in chunks:
                converted = discord_to_session(chunk)
                if not converted:
                    continue
                try:
                    self._inbound.put_nowait(converted)
                except queue.Full:
                    # Drop the oldest: a stalled consumer must not grow this
                    # without bound, and stale speech is worse than none.
                    try:
                        self._inbound.get_nowait()
                        self._inbound.put_nowait(converted)
                    except queue.Empty:  # pragma: no cover - racy but harmless
                        pass
        except Exception:  # noqa: BLE001 — this runs on the HOST's thread
            _log.debug("discord capture drain failed", exc_info=True)

    def read_input_chunk(self) -> bytes | None:
        """One chunk of 24 kHz mono for the session, or ``None`` when idle."""

        try:
            return self._inbound.get_nowait()
        except queue.Empty:
            return None

    # -- playback -------------------------------------------------------------

    def queue_playback(self, pcm: bytes) -> None:
        """Queue 24 kHz mono from the model for the voice channel."""

        if not pcm:
            return
        converted, self._carry_sample = session_to_discord(pcm, carry=self._carry_sample)
        if not converted:
            return
        try:
            self._outbound.put_nowait(converted)
        except queue.Full:
            _log.debug("discord playback queue full — dropping a chunk")
            return
        with self._lock:
            self._played_frames += len(pcm) // talk_audio.FRAME_BYTES

    def drain_playback(self) -> None:
        """Barge-in: drop everything not yet spoken."""

        while True:
            try:
                self._outbound.get_nowait()
            except queue.Empty:
                break
        source = self._source
        if source is not None:
            source.cleanup()
        self._carry_sample = None

    @property
    def played_ms(self) -> int:
        """Milliseconds handed to the channel since the last reset."""

        with self._lock:
            return int(self._played_frames * 1000 / talk_audio.SAMPLE_RATE)

    def reset_played_ms(self) -> None:
        with self._lock:
            self._played_frames = 0


__all__ = [
    "DISCORD_FRAME_BYTES",
    "JOIN_USAGE",
    "SILENCE_FRAME",
    "DiscordAudio",
    "TalkDiscordError",
    "discord_to_session",
    "reset_for_tests",
    "resolve_voice_bridge",
    "session_status",
    "session_to_discord",
    "start_session",
    "stop_session",
]


# -- session lifecycle (gateway-side) -----------------------------------------

#: The one live Discord voice session, if any. A voice channel holds one
#: conversation; a second would fight the first for the same connection.
_SESSION: dict[str, Any] = {}
_SESSION_LOCK = threading.Lock()

JOIN_USAGE = "Say `talk join` once I'm in a voice channel, or `talk leave` to hand it back."


def session_status() -> str:
    """One speakable line about the live Discord voice session."""

    with _SESSION_LOCK:
        task = _SESSION.get("task")
        guild_id = _SESSION.get("guild_id")
    if task is None or task.done():
        return "No live voice session — I'm not talking in a voice channel right now."
    return f"Live in the voice channel on server {guild_id} — say `talk leave` to stop."


def start_session(guild_id: int | None = None) -> str:
    """Start a realtime session on the host's voice connection.

    Returns the sentence to speak back. Requires a running event loop —
    this is the gateway path; the terminal path is ``hermes talk``.
    """

    try:
        import asyncio as _asyncio

        loop = _asyncio.get_running_loop()
    except RuntimeError:
        return "That needs the gateway's event loop — run `hermes talk` in a shell instead."

    with _SESSION_LOCK:
        existing = _SESSION.get("task")
        if existing is not None and not existing.done():
            return "I'm already live in a voice channel — say `talk leave` first."

    try:
        bridge = resolve_voice_bridge(guild_id)
    except TalkDiscordError as exc:
        return str(exc)

    try:
        from . import talk_cli
    except ImportError:  # pragma: no cover - flat-module fallback
        import talk_cli

    audio = DiscordAudio(bridge["guild_id"])
    task = loop.create_task(talk_cli.run_talk_session(audio=audio))

    def _done(finished) -> None:
        with _SESSION_LOCK:
            if _SESSION.get("task") is finished:
                _SESSION.clear()
        try:
            audio.stop()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            _log.debug("discord audio teardown failed", exc_info=True)

    task.add_done_callback(_done)
    with _SESSION_LOCK:
        _SESSION.update({"task": task, "guild_id": bridge["guild_id"], "audio": audio})
    return (
        "I'm live in the voice channel — talk to me. I can hear you while I "
        "speak, and I'll steer your background agents out loud."
    )


def stop_session() -> str:
    """End the live Discord voice session and hand the connection back."""

    with _SESSION_LOCK:
        task = _SESSION.get("task")
        audio = _SESSION.get("audio")
    if task is None or task.done():
        return "I'm not in a voice session right now."
    task.cancel()
    if audio is not None:
        try:
            audio.stop()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            _log.debug("discord audio teardown failed", exc_info=True)
    return "Left the voice session — the channel is yours again."


def reset_for_tests() -> None:
    with _SESSION_LOCK:
        _SESSION.clear()
