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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class InputAudioPacket:
    """One speaker snapshot attached to the exact PCM decoded with it."""

    speaker: dict[str, Any] | None
    pcm: bytes


#: Distinguishes "never captured" from a legitimately stored ``None``.
_UNSET = object()

#: How often, at most, we re-arm the host's inactivity timer while audio
#: is flowing. The host's own timer is minutes wide; this only has to be
#: comfortably under it.
KEEPALIVE_INTERVAL_S = 20.0

#: Allow a brief discord.py reconnect transition, but never synthesize
#: healthy-looking silence forever on a client that is no longer connected.
BRIDGE_DISCONNECT_GRACE_S = 1.0


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
            raise TalkDiscordError("I'm in more than one voice channel — say which server")
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


def _add_socket_listener_once(connection: Any, callback: Any) -> None:
    """Register an exact callback only when the host registry does not have it."""

    reader = getattr(connection, "_socket_reader", None)
    registries = [
        getattr(reader, "_callbacks", None),
        getattr(connection, "callbacks", None),
    ]
    for callbacks in registries:
        if callbacks is not None:
            try:
                callback_self = getattr(callback, "__self__", _UNSET)
                callback_func = getattr(callback, "__func__", _UNSET)
                if any(
                    registered is callback
                    or (
                        callback_self is not _UNSET
                        and callback_func is not _UNSET
                        and getattr(registered, "__self__", _UNSET) is callback_self
                        and getattr(registered, "__func__", _UNSET) is callback_func
                    )
                    for registered in callbacks
                ):
                    return
            except (TypeError, RuntimeError):
                # An opaque or concurrently changing host registry cannot be
                # inspected reliably; fall back to its registration API.
                continue
    connection.add_socket_listener(callback)


class DiscordAudio:
    """A voice channel wearing :class:`talk_audio.DuplexAudio`'s interface."""

    # Opt-in capability consumed by run_talk_session. Generic microphones and
    # dashboard sessions deliberately keep their existing authorization path.
    discord_speaker_authorization = True

    def __init__(self, guild_id: int | None = None, *, capture_only: bool = False) -> None:
        self._guild_id = guild_id
        self._capture_only = capture_only
        self._bridge: dict[str, Any] | None = None
        self._source: _RealtimeSource | None = None
        self._inbound: queue.Queue[InputAudioPacket] = queue.Queue(maxsize=MAX_INPUT_FRAMES)
        self._outbound: queue.Queue[bytes] = queue.Queue(maxsize=MAX_OUTPUT_FRAMES)
        self._lock = threading.Lock()
        # Serialize every externally visible lifecycle mutation, including
        # the host listener registry calls below. Reentrant because startup
        # unwinds an existing bridge and playback failures self-stop.
        self._lifecycle_lock = threading.RLock()
        self._listener_lock = threading.RLock()
        self._listener_condition = threading.Condition(self._listener_lock)
        self._played_baseline = 0
        self._carry_sample: int | None = None
        self._capture_remainder: dict[Any, bytes] = {}
        #: SSRCs already warned about (E2EE audio withheld, speaker unknown).
        self._unidentified_ssrcs: set[Any] = set()
        #: Wall-clock instant up to which we have handed the session audio.
        #: Zero until start(), which is what gates silence synthesis.
        self._audio_clock = 0.0
        self._original_on_packet = None
        self._tapped = None
        self._listener_generation = 0
        self._installed_listener_generation: int | None = None
        self._capture_listener_generation: int | None = None
        self._closing_listener_handoff_generation: int | None = None
        self._active_listener_callbacks: dict[int, int] = {}
        self._loop = None
        self._last_keepalive = 0.0
        self._restore_voice_mode: Any = _UNSET
        self._restore_play: Any = _UNSET
        self._final_played = 0
        self._bridge_failure: str | None = None
        self._disconnected_since: float | None = None
        self._speaker_notifier = None
        self._speaker_notifier_generation = 0
        self._last_speaker_key: Any = _UNSET

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Borrow the host's connection. Raises :class:`TalkAudioError` on a miss.

        The error type is the PortAudio path's on purpose — the session's
        startup already turns that into one spoken line, and a voice
        surface should fail the same way whichever room it's in.
        """

        with self._lifecycle_lock:
            self._start_locked()

    def _start_locked(self) -> None:
        """Start while owning the complete lifecycle transaction."""

        # A repeated start while this exact tap generation is still installed
        # is a no-op. Rewrapping our own tap would save that tap as the
        # "original" and later restore it instead of the host callback.
        if self._listener_install_is_owned():
            return
        if self._bridge is not None:
            # The host may have replaced our listener on the live receiver.
            # Unwind only what we still own before borrowing that replacement
            # as the next generation's original.
            self.stop()

        # A reused bridge must never carry a callback from an earlier session.
        # Do this before any operation that can fail so every failed-start path
        # also invalidates deliveries already queued on the old loop.
        self._detach_speaker_notifier()
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
        self._bridge_failure = None
        self._disconnected_since = None
        if not self._capture_only:
            self._source = _new_source(self._outbound)
        self._audio_clock = time.monotonic()
        self._loop = _running_loop()
        if self._loop is None:
            # Without a loop we cannot marshal the host's inactivity-timer
            # reset, and the host would evict us mid-conversation with no
            # signal. Say it once rather than fail silently.
            _log.warning("no running loop — the host's voice inactivity timer will not be re-armed")
        voice_client = bridge["voice_client"]

        # Tap the host's receive path. We wrap rather than replace so the
        # host's own bookkeeping keeps working.
        original = receiver._on_packet
        generation = self._listener_generation + 1

        def tapped(data: bytes) -> None:
            with self._listener_condition:
                while (
                    self._closing_listener_handoff_generation == generation
                    and self._installed_listener_generation == generation
                    and self._tapped is tapped
                ):
                    self._listener_condition.wait()
                self._active_listener_callbacks[generation] = (
                    self._active_listener_callbacks.get(generation, 0) + 1
                )
            try:
                original(data)
            finally:
                # Linearize draining with stop()'s generation invalidation.
                # Once teardown acquires this lock, no copied/in-flight tap
                # from the old generation can enqueue afterward.
                with self._listener_condition:
                    if (
                        self._installed_listener_generation == generation
                        and self._capture_listener_generation == generation
                        and self._tapped is tapped
                    ):
                        self._drain_receiver(receiver)
                    remaining = self._active_listener_callbacks[generation] - 1
                    if remaining:
                        self._active_listener_callbacks[generation] = remaining
                    else:
                        self._active_listener_callbacks.pop(generation, None)
                        self._listener_condition.notify_all()

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
        with self._listener_lock:
            self._original_on_packet = original
            self._tapped = tapped
            self._listener_generation = generation
            self._installed_listener_generation = generation
            self._capture_listener_generation = None
        try:
            connection = voice_client._connection
            # Add first, then remove: unregistering the only callback pauses
            # discord.py's reader thread until something re-registers
            # (voice_state.py:99-104), and packets in that window are lost.
            # Overlapping costs one duplicated packet instead of a gap.
            connection.add_socket_listener(tapped)
            connection.remove_socket_listener(original)
        except Exception as exc:  # host receive surface, any type
            # Fence this generation before any rollback callback operation.
            # A dispatcher may already have copied the tap and still be inside
            # the original callback; once it returns it must not drain into a
            # failed generation while remove/add repairs the host registry.
            with self._listener_condition:
                self._installed_listener_generation = None
                self._capture_listener_generation = None
                self._closing_listener_handoff_generation = None
                self._original_on_packet = None
                self._tapped = None
                self._bridge = None
                self._listener_condition.notify_all()
            # Removal and restoration are independent best-effort operations:
            # a half-completed swap can leave either callback absent.
            with suppress(Exception):  # never leave the host deaf
                connection.remove_socket_listener(tapped)
            with suppress(Exception):
                _add_socket_listener_once(connection, original)
            self._wait_for_listener_generation(generation)
            self._reset_capture_generation()
            self._source = None
            raise talk_audio.TalkAudioError(
                f"couldn't listen in on the voice channel: {exc}"
            ) from exc
        receiver._on_packet = tapped
        # New packets now route through the parked tap. Drop PCM accumulated
        # before this handoff, then publish the generation and release callbacks.
        with self._listener_condition:
            self._closing_listener_handoff_generation = generation
            while self._active_listener_callbacks.get(generation, 0):
                self._listener_condition.wait()
            self._discard_receiver_buffers(receiver)
            self._capture_listener_generation = generation
            self._closing_listener_handoff_generation = None
            self._listener_condition.notify_all()
        with suppress(Exception):  # receipt only; host attributes may differ
            mapping = getattr(receiver, "_ssrc_to_user", None)
            _log.info(
                "discord capture live: bot_ssrc=%s e2ee=%s mapped_ssrcs=%s",
                getattr(receiver, "_bot_ssrc", None),
                getattr(receiver, "_dave_session", None) is not None,
                sorted(mapping) if isinstance(mapping, dict) else None,
            )

        if self._capture_only:
            return

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

        with self._lifecycle_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        """Stop while owning the complete lifecycle transaction."""

        self._detach_speaker_notifier()
        with self._listener_condition:
            listener_is_owned = self._listener_install_is_owned()
            generation = self._installed_listener_generation
            bridge, self._bridge = self._bridge, None
            original, self._original_on_packet = self._original_on_packet, None
            tapped, self._tapped = self._tapped, None
            # Fence callbacks copied by the host before touching its listener
            # registry. Removal can invoke or race such a callback, and it must
            # not drain receiver buffers into a detached generation.
            self._installed_listener_generation = None
            self._capture_listener_generation = None
            self._closing_listener_handoff_generation = None
            self._listener_condition.notify_all()
        # Teardown is best-effort at every step: a half-torn-down bridge
        # must still hand the connection back to the host, in the reverse
        # order it was taken.
        if bridge is not None and original is not None:
            receiver = bridge.get("receiver")
            connection = bridge["voice_client"]._connection
            with suppress(Exception):
                connection.remove_socket_listener(tapped)
            # Removal and restoration are independent: a concurrent host
            # removal may make the first operation raise, but the host still
            # needs its original listener back. Re-check callback identity so
            # a newer host replacement remains authoritative.
            if (
                listener_is_owned
                and getattr(receiver, "_running", False)
                and getattr(receiver, "_on_packet", None) is tapped
                and self._published_bridge_is_exact(bridge)
            ):
                with suppress(Exception):
                    _add_socket_listener_once(connection, original)
                if not self._published_bridge_is_exact(bridge):
                    with suppress(Exception):
                        connection.remove_socket_listener(original)
                else:
                    with suppress(Exception):
                        if getattr(receiver, "_on_packet", None) is tapped:
                            receiver._on_packet = original
        if generation is not None:
            self._wait_for_listener_generation(generation)
        # The old tap is fenced before this reset, so no old generation can
        # refill the queue or rebuild a partial conversion afterward.
        self._reset_capture_generation()
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
            if not self._capture_only:
                with suppress(Exception):
                    bridge["voice_client"].stop()
        source, self._source = self._source, None
        if source is not None:
            with self._lock:
                self._final_played = max(0, source.frames_served - self._played_baseline) * 20

    # -- capture (host socket-reader thread) ----------------------------------

    def set_speaker_notifier(self, notifier) -> None:
        """Receive attribution transitions; ``None`` detaches the consumer."""

        self._speaker_notifier_generation += 1
        self._speaker_notifier = notifier
        self._last_speaker_key = _UNSET

    def _detach_speaker_notifier(self) -> None:
        self.set_speaker_notifier(None)

    def _reset_capture_generation(self) -> None:
        while True:
            try:
                self._inbound.get_nowait()
            except queue.Empty:
                break
        self._capture_remainder.clear()
        self._unidentified_ssrcs.clear()
        self._audio_clock = 0.0
        self._last_keepalive = 0.0
        self._loop = None
        self._last_speaker_key = _UNSET
        self._bridge_failure = None
        self._disconnected_since = None

    @staticmethod
    def _discard_receiver_buffers(receiver: Any) -> None:
        """Drop host PCM that predates a newly published Talk generation."""

        buffers = getattr(receiver, "_buffers", None)
        if not buffers:
            return
        lock = getattr(receiver, "_lock", None)
        if lock is not None:
            with lock:
                for buf in buffers.values():
                    del buf[:]
            return
        for buf in buffers.values():  # pragma: no cover - shipped hosts have the lock
            del buf[:]

    def _wait_for_listener_generation(self, generation: int) -> None:
        """Wait until admitted callbacks finish the host's original bookkeeping."""

        with self._listener_condition:
            while self._active_listener_callbacks.get(generation, 0):
                self._listener_condition.wait()

    def _deliver_speaker(self, generation: int, notifier: Any, speaker: dict[str, Any]) -> None:
        if (
            generation == self._speaker_notifier_generation
            and notifier is self._speaker_notifier
            and notifier is not None
        ):
            notifier(speaker)

    def _speaker_for_ssrc(self, ssrc: Any, raw_user_id: Any) -> dict[str, Any]:
        """Resolve display metadata from the receiver's already-captured mapping."""

        user_id = raw_user_id if type(raw_user_id) is int and raw_user_id > 0 else None
        display_name = ""
        if user_id is not None and self._bridge is not None:
            voice_client = self._bridge.get("voice_client")
            channel = getattr(voice_client, "channel", None)
            for member in getattr(channel, "members", ()) or ():
                if getattr(member, "id", None) == user_id:
                    display_name = str(getattr(member, "display_name", "") or "")
                    break
        return {
            "ssrc": int(ssrc),
            "user_id": user_id or None,
            "display_name": display_name,
        }

    def _notify_speaker(self, speaker: dict[str, Any]) -> None:
        notifier = self._speaker_notifier
        if notifier is None:
            return
        user_id = speaker["user_id"]
        key = ("user", user_id) if user_id is not None else ("ssrc", speaker["ssrc"])
        if key == self._last_speaker_key:
            return
        self._last_speaker_key = key
        generation = self._speaker_notifier_generation
        loop = self._loop
        if loop is None:
            self._deliver_speaker(generation, notifier, speaker)
            return
        with suppress(RuntimeError):  # loop closed while the receiver thread drains
            loop.call_soon_threadsafe(self._deliver_speaker, generation, notifier, speaker)

    def _drain_receiver(self, receiver: Any) -> None:
        """Move whatever the host just decoded into our queue, at our rate.

        The host buffers PCM per SSRC and releases it only after 1.5 s of
        silence — a turn boundary. A realtime session cannot wait for that,
        so we take the buffers continuously and leave the host's own gate
        to its own bookkeeping.
        """

        if not self._bridge_identity_is_current(receiver):
            self._mark_bridge_failure("the Discord voice connection changed while I was listening")
            return

        try:
            buffers = getattr(receiver, "_buffers", None)
            if not buffers:
                return
            lock = getattr(receiver, "_lock", None)
            if lock is not None:
                with lock:
                    chunks = self._take_receiver_chunks(receiver)
            else:  # pragma: no cover - every shipped host has the lock
                chunks = self._take_receiver_chunks(receiver)
            if chunks:
                self._touch_host_timer()
            for ssrc, raw_user_id, chunk in chunks:
                # Per speaker: one speaker's partial sample group prepended
                # to another's audio would shift it and transpose L/R for
                # that chunk.
                converted, remainder = discord_to_session(
                    chunk, carry=self._capture_remainder.get(ssrc, b"")
                )
                self._capture_remainder[ssrc] = remainder
                if not converted:
                    continue
                speaker = self._speaker_for_ssrc(ssrc, raw_user_id)
                self._notify_speaker(speaker)
                try:
                    self._inbound.put_nowait(InputAudioPacket(speaker=speaker, pcm=converted))
                except queue.Full:
                    # Drop the oldest: a stalled consumer must not grow this
                    # without bound, and stale speech is worse than none.
                    try:
                        self._inbound.get_nowait()
                        self._inbound.put_nowait(InputAudioPacket(speaker=speaker, pcm=converted))
                    except queue.Empty:  # pragma: no cover - racy but harmless
                        pass
        except Exception:  # noqa: BLE001 — this runs on the HOST's thread
            _log.debug("discord capture drain failed", exc_info=True)

    def _take_receiver_chunks(self, receiver: Any) -> list[tuple[Any, Any, bytes]]:
        """Swap out every SSRC buffer; the caller holds the receiver lock.

        Discord voice is end-to-end encrypted (DAVE). The host only runs the
        DAVE decrypt for an SSRC it has already mapped to a user, and it
        learns that mapping from Discord's SPEAKING event (which never
        arrives for someone already talking when the bot joined) or from its
        own silence gate, which our continuous drain starves. An unmapped
        SSRC therefore reaches the Opus decoder still encrypted and comes
        out as white noise. Identify the speaker here the way the host
        would, and withhold the frames decoded before that mapping existed
        rather than hand the model static.
        """

        buffers = receiver._buffers
        mapping = getattr(receiver, "_ssrc_to_user", None)
        if not isinstance(mapping, dict):
            mapping = None
        encrypted = getattr(receiver, "_dave_session", None) is not None
        chunks: list[tuple[Any, Any, bytes]] = []
        for ssrc, buf in buffers.items():
            if not buf:
                continue
            raw_user_id = mapping.get(ssrc) if mapping is not None else None
            if not raw_user_id and mapping is not None:
                raw_user_id = self._identify_speaker(receiver, ssrc)
                if encrypted:
                    # Everything buffered so far was decoded before the
                    # mapping existed: ciphertext through Opus. Drop it and
                    # reset the host decoder for this stream so the first
                    # real frame starts clean.
                    del buf[:]
                    decoders = getattr(receiver, "_decoders", None)
                    if isinstance(decoders, dict):
                        decoders.pop(ssrc, None)
                    self._capture_remainder.pop(ssrc, None)
                    if raw_user_id:
                        self._unidentified_ssrcs.discard(ssrc)
                        _log.info(
                            "identified discord speaker ssrc=%s -> user=%s; "
                            "E2EE audio flows from the next frame",
                            ssrc,
                            raw_user_id,
                        )
                    elif ssrc not in self._unidentified_ssrcs:
                        self._unidentified_ssrcs.add(ssrc)
                        _log.warning(
                            "withholding E2EE audio from unidentified discord "
                            "speaker ssrc=%s until Discord says who it is",
                            ssrc,
                        )
                    continue
            chunks.append((ssrc, raw_user_id, bytes(buf)))
            del buf[:]
        return chunks

    def _identify_speaker(self, receiver: Any, ssrc: Any) -> Any:
        """Map an unknown SSRC to a user the way the host's silence gate does.

        The caller holds the receiver lock. Prefers the host's own inference
        so the mapping (and its log line) stay the host's; falls back to the
        same sole-allowed-member rule on a host build without it.
        """

        infer = getattr(receiver, "_infer_user_for_ssrc", None)
        if callable(infer):
            with suppress(Exception):
                found = infer(ssrc)
                if found:
                    return found
            return None
        mapping = getattr(receiver, "_ssrc_to_user", None)
        if not isinstance(mapping, dict):
            return None
        try:
            voice_client = getattr(receiver, "_vc", None)
            channel = getattr(voice_client, "channel", None)
            bot_id = getattr(getattr(voice_client, "user", None), "id", None)
            allowed = {str(uid) for uid in (getattr(receiver, "_allowed_user_ids", None) or ())}
            candidates = [
                member.id
                for member in getattr(channel, "members", None) or ()
                if member.id != bot_id and (not allowed or str(member.id) in allowed)
            ]
        except Exception:  # noqa: BLE001 - host objects, host thread
            return None
        if len(candidates) != 1:
            return None
        mapping[ssrc] = candidates[0]
        _log.info(
            "auto-mapped discord ssrc=%s -> user=%s (sole allowed member)", ssrc, candidates[0]
        )
        return candidates[0]

    def _bridge_identity_is_current(self, receiver: Any | None = None) -> bool:
        """Whether the adapter still publishes the exact bridge we borrowed."""

        bridge = self._bridge
        if bridge is None:
            return True
        return self._published_bridge_is_exact(bridge, receiver)

    @staticmethod
    def _published_bridge_is_exact(bridge: dict[str, Any], receiver: Any | None = None) -> bool:
        """Whether the adapter still publishes this exact borrowed generation."""

        try:
            adapter = bridge["adapter"]
            guild_id = bridge["guild_id"]
            clients = getattr(adapter, "_voice_clients", None)
            receivers = getattr(adapter, "_voice_receivers", None)
            if not isinstance(clients, dict) or not isinstance(receivers, dict):
                return False
            return (
                clients.get(guild_id) is bridge["voice_client"]
                and receivers.get(guild_id) is bridge["receiver"]
                and (receiver is None or receiver is bridge["receiver"])
            )
        except Exception:  # noqa: BLE001 — host internals are an untrusted boundary
            return False

    def _listener_install_is_owned(self) -> bool:
        """Whether our exact current tap generation still owns the receiver slot."""

        bridge = self._bridge
        tapped = self._tapped
        return bool(
            bridge is not None
            and tapped is not None
            and self._installed_listener_generation == self._listener_generation
            and self._bridge_identity_is_current()
            and getattr(bridge.get("receiver"), "_on_packet", None) is tapped
        )

    def _mark_bridge_failure(self, message: str) -> None:
        if self._bridge_failure is None:
            self._bridge_failure = message
            _log.error("discord voice bridge lost: %s", message)

    def _bridge_credentials_are_current(self) -> bool:
        """Whether the receiver decrypts with the live connection generation."""

        bridge = self._bridge
        if bridge is None:
            return True
        try:
            receiver = bridge["receiver"]
            voice_client = bridge["voice_client"]
            connection = voice_client._connection
            return (
                receiver._vc is voice_client
                and bytes(receiver._secret_key) == bytes(connection.secret_key)
                and receiver._dave_session is connection.dave_session
            )
        except Exception:  # noqa: BLE001 — unverifiable credentials are not healthy
            return False

    def _fail_if_bridge_lost(self) -> None:
        if not self._bridge_identity_is_current():
            self._mark_bridge_failure("the Discord voice connection changed while I was listening")
        elif not self._bridge_credentials_are_current():
            self._mark_bridge_failure("the Discord voice credentials changed while I was listening")
        bridge = self._bridge
        if bridge is not None and self._bridge_failure is None:
            try:
                connected = bool(bridge["voice_client"].is_connected())
            except Exception:  # noqa: BLE001 — host liveness is an untrusted boundary
                self._mark_bridge_failure(
                    "the Discord voice connection liveness could not be verified"
                )
            else:
                now = time.monotonic()
                if connected:
                    self._disconnected_since = None
                elif self._disconnected_since is None:
                    self._disconnected_since = now
                elif now - self._disconnected_since >= BRIDGE_DISCONNECT_GRACE_S:
                    self._mark_bridge_failure(
                        "the Discord voice connection disconnected while I was listening"
                    )
        failure = self._bridge_failure
        if failure is None:
            return
        self.stop()
        raise talk_audio.TalkAudioError(failure)

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

    def read_input_packet(self) -> InputAudioPacket | None:
        """One atomic metadata+PCM packet for the session, paced to real time.

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

        self._fail_if_bridge_lost()
        now = time.monotonic()
        try:
            packet = self._inbound.get_nowait()
        except queue.Empty:
            packet = None
        if packet:
            # Accept raw bytes placed by older embedders while the public
            # read_input_chunk compatibility seam remains supported. Raw bytes
            # carry no immutable identity and must never look like synthesized
            # silence to the authorization ledger.
            if isinstance(packet, bytes):
                packet = InputAudioPacket(
                    speaker={"user_id": None, "ssrc": 0, "display_name": ""},
                    pcm=packet,
                )
            # Real audio: advance the clock by however much we just sent, so
            # a burst of buffered speech does not earn extra silence after.
            duration = len(packet.pcm) / _SESSION_BYTES_PER_SECOND
            self._audio_clock = max(self._audio_clock, now) + duration
            return packet
        if self._audio_clock <= 0.0:  # not started
            return None
        if now < self._audio_clock:
            return None  # already sent audio covering this instant
        self._audio_clock += SESSION_FRAME_MS / 1000
        return InputAudioPacket(speaker=None, pcm=SESSION_SILENCE)

    def read_input_chunk(self) -> bytes | None:
        """Generic audio-device seam; unwrap metadata for legacy consumers."""

        packet = self.read_input_packet()
        return packet.pcm if packet is not None else None

    # -- playback -------------------------------------------------------------

    def queue_playback(self, pcm: bytes) -> None:
        """Queue 24 kHz mono from the model for the voice channel."""

        if self._capture_only:
            raise talk_audio.TalkAudioError("capture-only Discord audio cannot queue playback")
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
    "InputAudioPacket",
    "TalkDiscordError",
    "discord_to_session",
    "reset_for_tests",
    "resolve_voice_bridge",
    "session_status",
    "session_to_discord",
    "start_core_session",
    "start_session",
    "stop_session",
]


# -- session lifecycle (gateway-side) -----------------------------------------

#: The one live Discord voice session, if any. A voice channel holds one
#: conversation; a second would fight the first for the same connection.
_SESSION: dict[str, Any] = {}
_SESSION_LOCK = threading.Lock()
_LAST_FAILURE: str | None = None
_LAST_FAILURE_GENERATION: int | None = None
_SESSION_GENERATION = 0
_NOTIFICATION_TASKS: set[Any] = set()

_SAFE_EXCEPTION_NAMES = frozenset(
    {
        "ConnectionError",
        "OSError",
        "RuntimeError",
        "TalkAudioError",
        "TimeoutError",
        "TypeError",
        "ValueError",
    }
)


def _safe_exception_name(error: BaseException) -> str:
    name = type(error).__name__
    return name if name in _SAFE_EXCEPTION_NAMES else "unexpected error"


def _public_exception_failure(error: BaseException) -> str:
    """Return a bounded allowlisted failure with no exception text or repr."""

    return f"session error ({_safe_exception_name(error)})"


JOIN_USAGE = "Say `talk join` once I'm in a voice channel, or `talk leave` to hand it back."


def session_status() -> str:
    """One speakable line about the live Discord voice session."""

    with _SESSION_LOCK:
        task = _SESSION.get("task")
        guild_id = _SESSION.get("guild_id")
        mode = _SESSION.get("mode", "legacy")
        core_status = _SESSION.get("status", "starting")
        last_failure = _LAST_FAILURE
    if task is None or task.done():
        if last_failure:
            return f"The last voice session failed: {last_failure}"
        return "No live voice session — I'm not talking in a voice channel right now."
    if mode == "core":
        if core_status == "listening":
            return (
                f"Canonical core voice is listening live on server {guild_id} — "
                "say `talk leave` to stop."
            )
        return f"Canonical core voice is starting on server {guild_id} — say `talk leave` to stop."
    if mode == "provider-host-tools":
        return (
            f"Provider-owned Realtime voice with canonical Hermes tools is live on server "
            f"{guild_id}. Say `talk leave` to stop."
        )
    return (
        f"Limited legacy provider-owned voice is live on server {guild_id}. "
        "Use `/talk core join` for full canonical Hermes parity; say `talk leave` to stop."
    )


async def _deliver_failure_receipt(adapter: Any, guild_id: int, receipt: str) -> bool:
    """Put a failed-closed receipt in the voice channel's linked text room."""

    try:
        text_channels = getattr(adapter, "_voice_text_channels", None)
        channel_id = text_channels.get(guild_id) if isinstance(text_channels, dict) else None
        if channel_id is None:
            return False

        client = getattr(adapter, "_client", None)
        channel = None
        if client is not None:
            get_channel = getattr(client, "get_channel", None)
            if callable(get_channel):
                channel = get_channel(int(channel_id))
            if channel is None:
                fetch_channel = getattr(client, "fetch_channel", None)
                if callable(fetch_channel):
                    channel = await fetch_channel(int(channel_id))
        send_to_channel = getattr(channel, "send", None)
        if callable(send_to_channel):
            try:
                await send_to_channel(receipt)
                return True
            except Exception:  # noqa: BLE001 — try the adapter's guarded path
                _log.warning(
                    "direct Discord voice failure receipt failed; trying adapter send",
                    exc_info=True,
                )

        # Older/minimal adapters may expose only their public send path. It is
        # also the guarded fallback when a cached channel object has gone stale.
        adapter_send = getattr(adapter, "send", None)
        if callable(adapter_send):
            result = await adapter_send(str(channel_id), receipt)
            return getattr(result, "success", True) is not False
    except Exception:  # noqa: BLE001 — status remains the durable fallback
        _log.warning("could not deliver Discord voice failure receipt", exc_info=True)
    return False


def start_core_session(factory: object, guild_id: int | None = None) -> str:
    """Start the canonical input-only lane without falling back to legacy Talk."""

    global _LAST_FAILURE, _LAST_FAILURE_GENERATION, _SESSION_GENERATION

    try:
        import asyncio as _asyncio

        loop = _asyncio.get_running_loop()
    except RuntimeError:
        return "Canonical core voice requires the running Hermes gateway."

    try:
        bridge = resolve_voice_bridge(guild_id)
    except TalkDiscordError as exc:
        return str(exc)

    try:
        from . import talk_core_session
    except ImportError:  # pragma: no cover - flat-module fallback
        import talk_core_session

    audio = DiscordAudio(bridge["guild_id"], capture_only=True)
    task = None
    generation = None

    def _project_status(status: str) -> None:
        if status != "listening":
            return
        with _SESSION_LOCK:
            if _SESSION.get("task") is task and _SESSION.get("generation") == generation:
                _SESSION["status"] = status

    def _done(finished) -> None:
        global _LAST_FAILURE, _LAST_FAILURE_GENERATION

        failure = None
        diagnostic_name = None
        try:
            if not finished.cancelled():
                error = finished.exception()
                if error is not None:
                    failure = _public_exception_failure(error)
                    diagnostic_name = _safe_exception_name(error)
        except Exception as exc:  # noqa: BLE001 - callback must not escape
            failure = "session outcome unavailable"
            diagnostic_name = _safe_exception_name(exc)
        with _SESSION_LOCK:
            owns_session = (
                _SESSION.get("task") is finished and _SESSION.get("generation") == generation
            )
            if owns_session:
                _SESSION.clear()
                if failure:
                    _LAST_FAILURE = f"canonical core voice: {failure}"
                    _LAST_FAILURE_GENERATION = generation
        if not owns_session:
            try:
                audio.stop()
            except Exception as exc:  # noqa: BLE001 - runner also owns idempotent teardown
                _log.debug("canonical core audio teardown failed (%s)", _safe_exception_name(exc))
            return
        if failure:
            _log.warning(
                "canonical core voice session failed closed (%s)",
                diagnostic_name or "unknown outcome",
            )
            receipt = (
                f"Canonical core voice failed closed: {failure}. "
                "No legacy voice session was started."
            )

            async def _notify() -> None:
                global _LAST_FAILURE, _LAST_FAILURE_GENERATION

                delivered = await _deliver_failure_receipt(
                    bridge["adapter"], bridge["guild_id"], receipt
                )
                if delivered:
                    with _SESSION_LOCK:
                        if generation == _LAST_FAILURE_GENERATION:
                            _LAST_FAILURE = None
                            _LAST_FAILURE_GENERATION = None

            notification = loop.create_task(_notify())
            _NOTIFICATION_TASKS.add(notification)
            notification.add_done_callback(_NOTIFICATION_TASKS.discard)
        try:
            audio.stop()
        except Exception as exc:  # noqa: BLE001 - runner also owns idempotent teardown
            _log.debug("canonical core audio teardown failed (%s)", _safe_exception_name(exc))

    with _SESSION_LOCK:
        existing = _SESSION.get("task")
        if existing is not None and not existing.done():
            return "I'm already live in a voice channel — say `talk leave` first."
        _LAST_FAILURE = None
        _LAST_FAILURE_GENERATION = None
        _SESSION_GENERATION += 1
        generation = _SESSION_GENERATION
        task = loop.create_task(
            talk_core_session.run_core_session(
                factory,
                guild_id=bridge["guild_id"],
                audio=audio,
                status_callback=_project_status,
            )
        )
        task.add_done_callback(_done)
        _SESSION.update(
            {
                "task": task,
                "guild_id": bridge["guild_id"],
                "audio": audio,
                "mode": "core",
                "status": "starting",
                "generation": generation,
            }
        )
    return (
        "Starting the canonical core input lane in the voice channel — "
        "say `talk status` to check it or `talk leave` to stop."
    )


def start_session(
    guild_id: int | None = None, *, host_execution_attachment=None
) -> str:
    """Start a realtime session on the host's voice connection.

    Returns the sentence to speak back. Requires a running event loop —
    this is the gateway path; the terminal path is ``hermes talk``.
    """

    global _LAST_FAILURE, _LAST_FAILURE_GENERATION, _SESSION_GENERATION

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
    generation = None

    def _done(finished) -> None:
        global _LAST_FAILURE, _LAST_FAILURE_GENERATION

        failure = None
        diagnostic_name = None
        try:
            if not finished.cancelled():
                error = finished.exception()
                if error is not None:
                    failure = _public_exception_failure(error)
                    diagnostic_name = _safe_exception_name(error)
                else:
                    status = finished.result()
                    if status:
                        bridge_failure = audio._bridge_failure
                        safe_bridge_failures = {
                            "the Discord voice connection changed",
                            "the Discord voice credentials changed",
                            "the Discord voice connection changed while I was listening",
                            "the Discord voice credentials changed while I was listening",
                            "the Discord voice connection liveness could not be verified",
                            "the Discord voice connection disconnected while I was listening",
                        }
                        failure = (
                            bridge_failure
                            if bridge_failure in safe_bridge_failures
                            else "session exited unsuccessfully"
                        )
        except Exception as exc:  # noqa: BLE001 — a receipt must not raise
            failure = "session outcome unavailable"
            diagnostic_name = _safe_exception_name(exc)

        with _SESSION_LOCK:
            owns_session = (
                _SESSION.get("task") is finished and _SESSION.get("generation") == generation
            )
            if owns_session:
                _SESSION.clear()
                if failure:
                    _LAST_FAILURE = failure
                    _LAST_FAILURE_GENERATION = generation

        if not owns_session:
            try:
                audio.stop()
            except Exception as exc:  # noqa: BLE001 — teardown is best-effort
                _log.debug("discord audio teardown failed (%s)", _safe_exception_name(exc))
            return

        if failure:
            _log.warning(
                "discord voice session failed closed (%s)",
                diagnostic_name or "nonzero status",
            )
            receipt = (
                f"Voice session failed closed: {failure}. The voice channel was handed "
                "back; say `talk join` when you want to start a new session."
            )

            async def _notify() -> None:
                global _LAST_FAILURE, _LAST_FAILURE_GENERATION

                delivered = await _deliver_failure_receipt(
                    bridge["adapter"], bridge["guild_id"], receipt
                )
                if delivered:
                    with _SESSION_LOCK:
                        if generation == _LAST_FAILURE_GENERATION:
                            _LAST_FAILURE = None
                            _LAST_FAILURE_GENERATION = None

            notification = loop.create_task(_notify())
            _NOTIFICATION_TASKS.add(notification)
            notification.add_done_callback(_NOTIFICATION_TASKS.discard)

        try:
            audio.stop()
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            _log.debug("discord audio teardown failed (%s)", _safe_exception_name(exc))

    # Claim the slot and publish the task under ONE lock: checking in one
    # acquisition and publishing in another lets two joins both pass the
    # check, both tap the receiver, and the second orphan the first.
    with _SESSION_LOCK:
        existing = _SESSION.get("task")
        if existing is not None and not existing.done():
            return "I'm already live in a voice channel — say `talk leave` first."
        _LAST_FAILURE = None
        _LAST_FAILURE_GENERATION = None
        _SESSION_GENERATION += 1
        generation = _SESSION_GENERATION
        if host_execution_attachment is None:
            session = talk_cli.run_talk_session(audio=audio, lane="discord")
        else:
            session = talk_cli.run_talk_session(
                audio=audio,
                host_execution_attachment=host_execution_attachment,
                lane="discord",
            )
        task = loop.create_task(session)
        task.add_done_callback(_done)
        _SESSION.update(
            {
                "task": task,
                "guild_id": bridge["guild_id"],
                "audio": audio,
                "mode": (
                    "provider-host-tools"
                    if host_execution_attachment is not None
                    else "legacy"
                ),
                "generation": generation,
            }
        )

    # Deliberately not "I'm live": nothing has connected yet. The session
    # still has to mint, open a socket, and take the channel, and any of
    # those can refuse. Overclaiming here would be the one thing this
    # plugin refuses to do anywhere else.
    if host_execution_attachment is not None:
        return (
            "Starting provider-owned Realtime voice with canonical Hermes tools; say "
            "`talk status` to check or `talk leave` to stop."
        )
    return (
        "Starting limited legacy provider-owned voice. Use `/talk core join` for full "
        "canonical Hermes parity; say `talk status` to check or `talk leave` to stop."
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
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            _log.debug("discord audio teardown failed (%s)", _safe_exception_name(exc))
    return "Left the voice session — the channel is yours again."


def reset_for_tests() -> None:
    global _LAST_FAILURE, _LAST_FAILURE_GENERATION, _SESSION_GENERATION

    with _SESSION_LOCK:
        _SESSION.clear()
        _LAST_FAILURE = None
        _LAST_FAILURE_GENERATION = None
        _SESSION_GENERATION = 0
        for notification in _NOTIFICATION_TASKS:
            notification.cancel()
        _NOTIFICATION_TASKS.clear()
