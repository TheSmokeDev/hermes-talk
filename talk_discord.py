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
import time
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

#: One 20 ms frame of the session's own format (24 kHz mono s16le), and the
#: silence we synthesize when Discord goes quiet.
SESSION_FRAME_MS = 20
SESSION_FRAME_BYTES = 960
SESSION_SILENCE = bytes(SESSION_FRAME_BYTES)

#: Bounded like the PortAudio path: overflow drops the oldest audio rather
#: than growing without limit when one side stalls.
MAX_INPUT_FRAMES = 100  # 2 s of inbound speech
MAX_OUTPUT_FRAMES = 250  # 5 s of queued reply

#: 24 kHz mono s16le.
_SESSION_BYTES_PER_SECOND = 24_000 * 2


#: Distinguishes "never captured" from a legitimately stored ``None``.
_UNSET = object()

#: How often, at most, we re-arm the host's inactivity timer while audio
#: is flowing. The host's own timer is minutes wide; this only has to be
#: comfortably under it.
KEEPALIVE_INTERVAL_S = 20.0


def _running_loop():
    """The loop to marshal host calls onto, or ``None`` off-loop."""

    import asyncio

    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class TalkDiscordError(Exception):
    """The host's Discord voice surface is unavailable or unrecognized."""


# -- rate conversion ----------------------------------------------------------


def discord_to_session(pcm48_stereo: bytes, *, carry: bytes = b"") -> tuple[bytes, bytes]:
    """48 kHz stereo → 24 kHz mono. Both steps average, never drop.

    Averaging the channel pair and then the sample pair is a crude two-tap
    low-pass, which is the point: plain decimation would alias the top of
    the band straight back down into speech.

    Returns ``(converted, remainder)``. The remainder is whatever did not
    fill a whole 4-sample group; feed it back as ``carry`` next call. A
    dropped remainder would transpose L/R for every subsequent sample in
    the stream — one partial frame and the channels stay swapped forever.
    """

    data = carry + pcm48_stereo
    if not data:
        return b"", b""
    usable = len(data) - (len(data) % 8)  # 4 samples * 2 bytes
    remainder = data[usable:]
    samples = array.array("h")
    samples.frombytes(data[:usable])
    out = array.array("h")
    # Two stereo frames (L,R,L,R) collapse to one mono sample.
    for i in range(0, len(samples), 4):
        total = samples[i] + samples[i + 1] + samples[i + 2] + samples[i + 3]
        out.append(total >> 2)
    return out.tobytes(), remainder


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

    # Find the Discord adapter without pinning one import path. The enum has
    # lived in more than one module across host versions, and guessing wrong
    # is indistinguishable from "Discord isn't running" — which is how this
    # first shipped, refusing on a gateway that was connected fine.
    adapters = getattr(runner, "adapters", None) or {}
    adapter = None
    for module_name in ("gateway.config", "models", "gateway.platform_registry"):
        try:
            module = __import__(module_name, fromlist=["Platform"])
            adapter = adapters.get(module.Platform.DISCORD)
        except Exception:  # noqa: BLE001 — try the next candidate
            continue
        if adapter is not None:
            break
    if adapter is None:
        # Last resort: match on the key's own name, so a host that moves or
        # renames the enum entirely still resolves.
        for key, value in adapters.items():
            label = getattr(key, "value", None) or getattr(key, "name", None) or key
            if str(label).strip().lower() == "discord":
                adapter = value
                break
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

    Defined without a base class so this module imports off-host, where the
    whole test suite runs. That is NOT sufficient on a real call:
    ``VoiceClient.play`` does ``isinstance(source, AudioSource)`` and
    rejects a duck type outright ("source must be an AudioSource not
    _RealtimeSource"). :func:`_new_source` mixes in the host's real base at
    runtime — the behaviour lives here, the pedigree is added there.
    """

    def __init__(self, frames: queue.Queue) -> None:
        self._frames = frames
        self._carry = bytearray()
        # read() runs on the host's player thread; cleanup() runs on the
        # event loop during barge-in — which is exactly when both fire at
        # once. An unguarded bytearray tears there, and an exception inside
        # read() retires playback for the rest of the call.
        self._carry_lock = threading.Lock()
        self._served_lock = threading.Lock()
        self._frames_served = 0

    @property
    def frames_served(self) -> int:
        with self._served_lock:
            return self._frames_served

    def read(self) -> bytes:
        with self._carry_lock:
            while len(self._carry) < DISCORD_FRAME_BYTES:
                try:
                    self._carry += self._frames.get_nowait()
                except queue.Empty:
                    break
            if len(self._carry) >= DISCORD_FRAME_BYTES:
                frame = bytes(self._carry[:DISCORD_FRAME_BYTES])
                del self._carry[:DISCORD_FRAME_BYTES]
                with self._served_lock:
                    self._frames_served += 1
                return frame
        # Underrun: silence, never b"". An empty read retires the player
        # thread and the call goes permanently mute.
        return SILENCE_FRAME

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        with self._carry_lock:
            self._carry.clear()


#: Concrete source classes, cached per host base class.
_SOURCE_CLASSES: dict[Any, Any] = {}


def _new_source(frames: queue.Queue) -> Any:
    """A playback source ``discord.VoiceClient.play`` will actually accept.

    On a host, that means a genuine ``discord.AudioSource`` subclass — the
    play() call isinstance-checks it. Off-host there is nothing to inherit
    from and the plain implementation is what the tests exercise.
    """

    try:
        import discord

        base = discord.AudioSource
    except Exception:  # noqa: BLE001 — no host discord in this process
        return _RealtimeSource(frames)
    cls = _SOURCE_CLASSES.get(base)
    if cls is None:
        # _RealtimeSource first in the MRO so its methods win.
        cls = type("_RealtimeAudioSource", (_RealtimeSource, base), {})
        _SOURCE_CLASSES[base] = cls
    return cls(frames)


class DiscordAudio:
    """A voice channel wearing :class:`talk_audio.DuplexAudio`'s interface."""

    def __init__(self, guild_id: int | None = None) -> None:
        self._guild_id = guild_id
        self._bridge: dict[str, Any] | None = None
        self._source: _RealtimeSource | None = None
        self._inbound: queue.Queue[bytes] = queue.Queue(maxsize=MAX_INPUT_FRAMES)
        self._outbound: queue.Queue[bytes] = queue.Queue(maxsize=MAX_OUTPUT_FRAMES)
        self._lock = threading.Lock()
        self._played_baseline = 0
        self._carry_sample: int | None = None
        self._capture_remainder: dict[Any, bytes] = {}
        #: Wall-clock instant up to which we have handed the session audio.
        #: Zero until start(), which is what gates silence synthesis.
        self._audio_clock = 0.0
        self._original_on_packet = None
        self._tapped = None
        self._loop = None
        self._last_keepalive = 0.0
        self._restore_voice_mode: Any = _UNSET
        self._restore_play: Any = _UNSET
        self._final_played = 0

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
        self._source = _new_source(self._outbound)
        self._audio_clock = time.monotonic()
        self._loop = _running_loop()
        if self._loop is None:
            # Without a loop we cannot marshal the host's inactivity-timer
            # reset, and the host would evict us mid-conversation with no
            # signal. Say it once rather than fail silently.
            _log.warning(
                "no running loop — the host's voice inactivity timer will not be re-armed"
            )
        voice_client = bridge["voice_client"]

        # Tap the host's receive path. We wrap rather than replace so the
        # host's own bookkeeping keeps working.
        original = receiver._on_packet

        def tapped(data: bytes) -> None:
            try:
                original(data)
            finally:
                self._drain_receiver(receiver)

        # The registration is what matters, NOT the attribute. discord.py
        # appends the BOUND METHOD OBJECT to its listener list and calls the
        # stored objects; rebinding ``receiver._on_packet`` alone shadows an
        # attribute nothing reads, so the tap never fires and the session
        # hears silence with nothing logged. Swap the registration, and keep
        # the attribute in sync so the host's own stop() still unregisters
        # the object it will look up.
        # Record BEFORE touching the host: if the swap half-completes, stop()
        # is the only thing that can put the original listener back, and it
        # is gated on these being set. Losing that leg leaves the host
        # permanently deaf with no path back.
        self._original_on_packet = original
        self._tapped = tapped
        try:
            connection = voice_client._connection
            # Add first, then remove: unregistering the only callback pauses
            # discord.py's reader thread until something re-registers
            # (voice_state.py:99-104), and packets in that window are lost.
            # Overlapping costs one duplicated packet instead of a gap.
            connection.add_socket_listener(tapped)
            connection.remove_socket_listener(original)
        except Exception as exc:  # host receive surface, any type
            with suppress(Exception):  # never leave the host deaf
                connection.remove_socket_listener(tapped)
            with suppress(Exception):
                connection.add_socket_listener(original)
            self._original_on_packet = None
            self._tapped = None
            self._bridge = None
            raise talk_audio.TalkAudioError(
                f"couldn't listen in on the voice channel: {exc}"
            ) from exc
        receiver._on_packet = tapped

        # Park the host's own speech while we own the conversation. Its
        # reply path waits on ``is_playing()`` — which our continuous source
        # keeps True — for a two-minute timeout, then force-stops us
        # mid-sentence and never gives the channel back. The mode getter is
        # read dynamically at reply time, so swapping it actually takes
        # (unlike the listener above), and the mixer is popped so the host
        # cannot block on a mixer nobody is draining.
        adapter = bridge.get("adapter")

        # THE chokepoint. Every route the host uses to speak — the runner's
        # reply path and the adapter's own auto-TTS — funnels through
        # play_in_voice_channel, and it waits on is_playing() (which our
        # continuous source keeps True) for a two-minute timeout before
        # force-stopping us mid-sentence and never handing back. Parking the
        # adapter's voice-mode getter does NOT do this: that getter is a
        # read-only view onto the runner's own dict and is only consulted by
        # the inactivity timer.
        self._restore_play = getattr(adapter, "play_in_voice_channel", _UNSET)

        async def _parked_play(*_args: Any, **_kwargs: Any) -> bool:
            _log.debug("host playback suppressed — the realtime session owns the channel")
            return False

        with suppress(Exception):
            adapter.play_in_voice_channel = _parked_play

        # Keep parking the mode getter too: it makes the host's inactivity
        # handler bail out early, which double-covers the auto-leave.
        self._restore_voice_mode = getattr(adapter, "_voice_mode_getter", None)
        with suppress(Exception):
            adapter._voice_mode_getter = lambda _chat_id: "off"

        # Take the mixer out of the host's reach. It is NOT handed back:
        # voice_client.stop() below makes discord.py call cleanup() on it,
        # which sets _closed and drops the ambient bed permanently, and a
        # closed mixer still reports speech_active forever — so returning it
        # would make every later host reply stall the full playback timeout
        # and vanish. Dropping it falls back to the plain playback path,
        # which works. A fresh bed needs the host's own installer.
        mixers = getattr(adapter, "_voice_mixers", None)
        if isinstance(mixers, dict):
            mixers.pop(bridge["guild_id"], None)

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
        # must still hand the connection back to the host, in the reverse
        # order it was taken.
        if bridge is not None and self._original_on_packet is not None:
            receiver = bridge.get("receiver")
            with suppress(Exception):
                connection = bridge["voice_client"]._connection
                connection.remove_socket_listener(self._tapped)
                # Only re-register for a receiver that is still alive. If the
                # host already tore it down, re-adding an inert callback keeps
                # its reader thread awake for the life of the connection.
                if getattr(receiver, "_running", False):
                    connection.add_socket_listener(self._original_on_packet)
            with suppress(Exception):
                receiver._on_packet = self._original_on_packet
        self._original_on_packet = None
        self._tapped = None
        self._audio_clock = 0.0
        if bridge is not None:
            adapter = bridge.get("adapter")
            if self._restore_play is not _UNSET:
                with suppress(Exception):
                    adapter.play_in_voice_channel = self._restore_play
            self._restore_play = _UNSET
            if self._restore_voice_mode is not _UNSET:
                with suppress(Exception):
                    adapter._voice_mode_getter = self._restore_voice_mode
            self._restore_voice_mode = _UNSET
            with suppress(Exception):
                bridge["voice_client"].stop()
        source, self._source = self._source, None
        if source is not None:
            with self._lock:
                self._final_played = max(0, source.frames_served - self._played_baseline) * 20

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
                    chunks = [(ssrc, bytes(buf)) for ssrc, buf in buffers.items() if buf]
                    for buf in buffers.values():
                        del buf[:]
            else:  # pragma: no cover - every shipped host has the lock
                chunks = [(ssrc, bytes(buf)) for ssrc, buf in buffers.items() if buf]
                for buf in buffers.values():
                    del buf[:]
            if chunks:
                self._touch_host_timer()
            for ssrc, chunk in chunks:
                # Per speaker: one speaker's partial sample group prepended
                # to another's audio would shift it and transpose L/R for
                # that chunk.
                converted, remainder = discord_to_session(
                    chunk, carry=self._capture_remainder.get(ssrc, b"")
                )
                self._capture_remainder[ssrc] = remainder
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

    def _touch_host_timer(self) -> None:
        """Tell the host somebody is talking (throttled, loop-marshalled).

        The host auto-leaves the channel after an inactivity window, and it
        only re-arms that timer on its OWN playback or on a COMPLETED
        utterance from its silence gate. We drain the buffers that gate
        measures, so from its side the room looks abandoned and it pulls the
        bot out mid-conversation. Re-arming keeps that from happening.

        This runs on the host's socket-reader thread and the timer is an
        asyncio task, so the call is marshalled onto the loop rather than
        made here.
        """

        now = time.monotonic()
        if now - self._last_keepalive < KEEPALIVE_INTERVAL_S:
            return
        self._last_keepalive = now
        bridge, loop = self._bridge, self._loop
        if bridge is None or loop is None:
            return
        adapter = bridge.get("adapter")
        reset = getattr(adapter, "_reset_voice_timeout", None)
        if not callable(reset):
            return
        with suppress(RuntimeError):  # loop closed mid-teardown
            loop.call_soon_threadsafe(reset, bridge["guild_id"])

    def read_input_chunk(self) -> bytes | None:
        """One chunk of 24 kHz mono for the session, paced to real time.

        A microphone streams continuously — silence included — and the
        session's turn detection depends on that: the server decides the
        operator stopped talking by measuring silence IN THE AUDIO IT
        RECEIVES, not by watching a clock. Discord does the opposite. It
        stops transmitting when nobody speaks, so a bridge that only
        forwards what arrives sends nothing during a pause, the server's
        silence never accumulates, and end-of-turn fires late or not at all
        — which the operator experiences as "it takes forever to answer".

        So this synthesizes silence to fill the gaps, on a wall clock: one
        frame per frame-duration, real audio when there is any, zeros when
        there is not. Pacing is as load-bearing as the silence itself —
        unpaced zeros would run the stream faster than real time and eat
        the pauses that separate sentences.
        """

        now = time.monotonic()
        try:
            chunk = self._inbound.get_nowait()
        except queue.Empty:
            chunk = None
        if chunk:
            # Real audio: advance the clock by however much we just sent, so
            # a burst of buffered speech does not earn extra silence after.
            duration = len(chunk) / _SESSION_BYTES_PER_SECOND
            self._audio_clock = max(self._audio_clock, now) + duration
            return chunk
        if self._audio_clock <= 0.0:  # not started
            return None
        if now < self._audio_clock:
            return None  # already sent audio covering this instant
        self._audio_clock += SESSION_FRAME_MS / 1000
        return SESSION_SILENCE

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

    def drain_playback(self) -> None:
        """Barge-in: drop everything not yet spoken."""

        with self._lock:
            source = self._source
            self._played_baseline = source.frames_served if source is not None else 0
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
        """Milliseconds the channel has actually HEARD since the last reset.

        Counted from frames the host's player thread pulled — never from
        what we queued. The model streams far faster than realtime, so
        queue-time would tell a barge-in truncate that the operator heard
        everything generated, and the model would carry on as if it had
        said sentences nobody heard. That desync is the entire reason this
        number exists.
        """

        source = self._source
        with self._lock:
            if source is None:
                return self._final_played
            served = source.frames_served - self._played_baseline
        return max(0, served) * 20  # each Discord frame is 20 ms

    def reset_played_ms(self) -> None:
        source = self._source
        with self._lock:
            self._played_baseline = source.frames_served if source is not None else 0


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

    try:
        bridge = resolve_voice_bridge(guild_id)
    except TalkDiscordError as exc:
        return str(exc)

    try:
        from . import talk_cli
    except ImportError:  # pragma: no cover - flat-module fallback
        import talk_cli

    audio = DiscordAudio(bridge["guild_id"])

    def _done(finished) -> None:
        with _SESSION_LOCK:
            if _SESSION.get("task") is finished:
                _SESSION.clear()
        # The weakest receipt in this surface: a session that dies during
        # mint or connect leaves a Discord operator hearing silence, because
        # the failure lands on stderr where nobody in the channel is looking.
        # `talk status` is the detector the join reply already hands them —
        # this makes sure the reason is on the record when they go looking.
        try:
            if not finished.cancelled():
                error = finished.exception()
                if error is not None:
                    _log.warning("discord voice session ended: %r", error)
                elif finished.result():
                    _log.warning(
                        "discord voice session exited %s — say `talk status`",
                        finished.result(),
                    )
        except Exception:  # noqa: BLE001 — a receipt must not raise
            _log.debug("could not read the session outcome", exc_info=True)
        try:
            audio.stop()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            _log.debug("discord audio teardown failed", exc_info=True)

    # Claim the slot and publish the task under ONE lock: checking in one
    # acquisition and publishing in another lets two joins both pass the
    # check, both tap the receiver, and the second orphan the first.
    with _SESSION_LOCK:
        existing = _SESSION.get("task")
        if existing is not None and not existing.done():
            return "I'm already live in a voice channel — say `talk leave` first."
        task = loop.create_task(talk_cli.run_talk_session(audio=audio))
        task.add_done_callback(_done)
        _SESSION.update({"task": task, "guild_id": bridge["guild_id"], "audio": audio})

    # Deliberately not "I'm live": nothing has connected yet. The session
    # still has to mint, open a socket, and take the channel, and any of
    # those can refuse. Overclaiming here would be the one thing this
    # plugin refuses to do anywhere else.
    return (
        "Starting up in the voice channel — give me a second, then talk to "
        "me. Say `talk status` if you want to check, or `talk leave` to stop."
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
