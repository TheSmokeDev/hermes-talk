"""EXPERIMENTAL: Twilio Media Streams transport for Talk.

Used only by the standalone ``talk_twilio_server`` process. Nothing on the
normal Talk path imports it, and it is not the built-in Phone mode (that is
Dograh, in ``talk_phone``).

Twilio Programmable Voice carries call audio as 8 kHz G.711 µ-law frames over a
WebSocket ("Media Streams"). A Talk realtime session takes and returns PCM16 at
24 kHz whatever the provider (the Gemini facade downsamples to Gemini's 16 kHz
input itself). This module is the transport side only — it converts between
those worlds and drives a :class:`talk_realtime.RealtimeSession`, and it takes
no view on which provider sits behind that session.

Design notes:

* **No new dependencies.** ``audioop`` was removed in Python 3.13 and the repo
  ships flat modules with only ``httpx`` and ``aiohttp`` as runtime deps, so the
  µ-law codec and the resampler are implemented here. Both are table/loop based
  and comfortably fast at 8 kHz (50 frames a second, 160 samples a frame).
* **No local VAD.** Twilio does not detect speech, and Gemini Live does its own
  server-side endpointing, so inbound audio is forwarded continuously. Barge-in
  arrives as :class:`talk_realtime.SpeechStarted` (``OutputInterrupted`` on some
  paths), which maps to Twilio's ``clear`` message so the agent audio Twilio has
  already buffered stops instead of finishing the sentence.
* **Two gates before a session exists.** The voice webhook must carry a valid
  ``X-Twilio-Signature``. It then hands Twilio a single-use stream token as a
  ``<Parameter>`` (Twilio forbids a query string on the stream URL and echoes
  parameters back in ``start.customParameters``). The media socket opens a
  provider session only for a fresh token issued to that same CallSid, so the
  socket cannot be dialled directly for a free provider session.
* **Identity is weak on a phone line.** A PSTN caller proves a number, not a
  person, so a transport built on this must not treat the caller as the account
  owner. That decision belongs to the surface, not here — see issue #44.
"""

from __future__ import annotations

import array
import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import struct
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

try:
    from . import talk_realtime as rt
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_realtime as rt

_log = logging.getLogger(__name__)

#: Twilio Media Streams are always 8 kHz mono G.711 µ-law.
TWILIO_RATE = 8000
#: Talk's RealtimeSession contract: PCM16 at 24 kHz in both directions.
SESSION_RATE = 24000
#: Twilio sends and expects 20 ms frames: 160 samples / 160 µ-law bytes.
TWILIO_FRAME_MS = 20
TWILIO_FRAME_BYTES = TWILIO_RATE * TWILIO_FRAME_MS // 1000
#: Refuse obviously oversized frames rather than buffering them.
MAX_MEDIA_BYTES = 8192
MAX_MESSAGE_BYTES = 64 * 1024
#: Plain ``http://`` / ``ws://`` is accepted only for these hosts (local tests).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
#: A stream token is good for one socket within this window; Twilio opens the
#: stream straight after it reads the TwiML.
STREAM_TOKEN_TTL_S = 60.0
#: A socket that has not presented a valid ``start`` by then is closed.
STREAM_START_TIMEOUT_S = 10.0

TWILIO_EVENTS = frozenset({"connected", "start", "media", "mark", "stop", "dtmf", "clear"})

_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635


def _build_ulaw_tables() -> tuple[tuple[int, ...], tuple[bytes, ...]]:
    """Return the 256-entry µ-law expansion and its little-endian encoding."""

    samples: list[int] = []
    for code in range(256):
        value = ~code & 0xFF
        sign = value & 0x80
        exponent = (value >> 4) & 0x07
        mantissa = value & 0x0F
        sample = ((mantissa << 3) + _ULAW_BIAS) << exponent
        sample -= _ULAW_BIAS
        samples.append(-sample if sign else sample)
    encoded = tuple(struct.pack("<h", sample) for sample in samples)
    return tuple(samples), encoded


ULAW_SAMPLES, _ULAW_ENCODED = _build_ulaw_tables()


def ulaw_to_pcm16(payload: bytes) -> bytes:
    """Decode G.711 µ-law bytes to little-endian PCM16."""

    if not payload:
        return b""
    return b"".join(_ULAW_ENCODED[byte] for byte in payload)


def _linear_to_ulaw(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    if sample > _ULAW_CLIP:
        sample = _ULAW_CLIP
    sample += _ULAW_BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not sample & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    """Encode little-endian PCM16 to G.711 µ-law."""

    if len(pcm) % 2:
        raise ValueError("PCM16 payload must be an even number of bytes")
    if not pcm:
        return b""
    samples = array.array("h")
    samples.frombytes(pcm)
    if struct.pack("=h", 1) != struct.pack("<h", 1):  # pragma: no cover - big-endian host
        samples.byteswap()
    return bytes(_linear_to_ulaw(sample) for sample in samples)


class PcmResampler:
    """Streaming mono PCM16 rate converter.

    Linear interpolation is enough for speech at these rates and keeps the
    module dependency free. State is carried between calls so consecutive
    frames join without a click.
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self._step = src_rate / dst_rate
        self._buffer = array.array("h")
        self._position = 0.0
        self._primed = False

    def process(self, pcm: bytes) -> bytes:
        """Convert one PCM16 chunk, returning whatever is ready to emit."""

        if len(pcm) % 2:
            raise ValueError("PCM16 payload must be an even number of bytes")
        if not pcm:
            return b""
        chunk = array.array("h")
        chunk.frombytes(pcm)
        if struct.pack("=h", 1) != struct.pack("<h", 1):  # pragma: no cover - big-endian host
            chunk.byteswap()
        self._buffer.extend(chunk)
        if not self._primed:
            # Interpolate from zero into the first sample rather than jumping.
            self._buffer.insert(0, 0)
            self._primed = True

        out = array.array("h")
        position = self._position
        while position + 1 < len(self._buffer):
            index = int(position)
            fraction = position - index
            first = self._buffer[index]
            second = self._buffer[index + 1]
            out.append(int(first + (second - first) * fraction))
            position += self._step

        consumed = int(position)
        if consumed:
            del self._buffer[:consumed]
            position -= consumed
        self._position = position

        if struct.pack("=h", 1) != struct.pack("<h", 1):  # pragma: no cover - big-endian host
            out.byteswap()
        return out.tobytes()


def frame_ulaw(payload: bytes, frame_bytes: int = TWILIO_FRAME_BYTES) -> list[bytes]:
    """Split µ-law bytes into Twilio-sized frames, padding the final one."""

    if frame_bytes <= 0:
        raise ValueError("frame_bytes must be positive")
    frames = [payload[i : i + frame_bytes] for i in range(0, len(payload), frame_bytes)]
    if frames and len(frames[-1]) < frame_bytes:
        frames[-1] = frames[-1] + b"\xff" * (frame_bytes - len(frames[-1]))
    return frames


@dataclass(frozen=True, slots=True)
class TwilioFrame:
    """One parsed message from a Media Stream."""

    event: str
    stream_sid: str | None = None
    call_sid: str | None = None
    payload: bytes | None = None
    mark: str | None = None
    parameters: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)


def parse_message(message: str | bytes | Mapping[str, Any]) -> TwilioFrame:
    """Parse and validate one Twilio Media Streams message."""

    if isinstance(message, Mapping):
        data = dict(message)
    else:
        raw = message.encode("utf-8") if isinstance(message, str) else message
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("Twilio message exceeds the size limit")
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Twilio message is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Twilio message must be a JSON object")

    event = data.get("event")
    if event not in TWILIO_EVENTS:
        raise ValueError(f"unsupported Twilio event: {event!r}")

    stream_sid = data.get("streamSid")
    if stream_sid is not None and not isinstance(stream_sid, str):
        raise ValueError("streamSid must be a string")
    if event in {"media", "mark", "clear", "stop"} and not stream_sid:
        raise ValueError(f"Twilio {event} message requires a streamSid")

    start = data.get("start")
    custom = start.get("customParameters") if isinstance(start, Mapping) else None
    parameters = (
        {str(key): str(value) for key, value in custom.items()}
        if isinstance(custom, Mapping)
        else {}
    )
    call_sid = start.get("callSid") if isinstance(start, Mapping) else None
    if call_sid is not None and not isinstance(call_sid, str):
        raise ValueError("callSid must be a string")

    payload: bytes | None = None
    if event == "media":
        media = data.get("media")
        encoded = media.get("payload") if isinstance(media, Mapping) else None
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("Twilio media message requires a payload")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Twilio media payload is not valid base64") from exc
        if len(payload) > MAX_MEDIA_BYTES:
            raise ValueError("Twilio media payload exceeds the size limit")

    return TwilioFrame(
        event=event,
        stream_sid=stream_sid,
        call_sid=call_sid,
        payload=payload,
        mark=data.get("mark", {}).get("name") if isinstance(data.get("mark"), Mapping) else None,
        parameters=parameters,
        raw=data,
    )


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _twiml(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


def twiml_stream(stream_url: str, parameters: Mapping[str, str] | None = None) -> str:
    """Return the TwiML that opens a bidirectional Media Stream.

    ``parameters`` become ``<Parameter>`` children, which Twilio echoes back in
    the stream's ``start.customParameters``: the WebSocket handshake itself
    carries no call metadata, and the stream URL may not carry a query string.
    """

    if not stream_url.startswith(("wss://", "ws://")):
        raise ValueError("stream_url must be a WebSocket URL")
    children = "".join(
        f'<Parameter name="{_xml_escape(name)}" value="{_xml_escape(value)}"/>'
        for name, value in (parameters or {}).items()
        if value
    )
    if children:
        stream = f'<Stream url="{_xml_escape(stream_url)}">{children}</Stream>'
    else:
        stream = f'<Stream url="{_xml_escape(stream_url)}"/>'
    return _twiml(f"<Connect>{stream}</Connect>")


def twiml_reject(reason: str = "rejected") -> str:
    """Decline a call without answering it, so the call is never billed."""

    if reason not in {"rejected", "busy"}:
        raise ValueError("reason must be 'rejected' or 'busy'")
    return _twiml(f'<Reject reason="{reason}"/>')


def expected_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    """Twilio's ``X-Twilio-Signature`` for a webhook request.

    Twilio signs the full URL followed by every POST parameter, key then value,
    in sorted key order, with HMAC-SHA1 keyed on the account auth token.
    """

    if not auth_token:
        raise ValueError("auth_token is required")
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(digest.digest()).decode("ascii")


def validate_signature(
    auth_token: str,
    url: str,
    params: Mapping[str, str],
    signature: str | None,
) -> bool:
    """Constant-time check of an inbound webhook signature."""

    if not signature:
        return False
    return hmac.compare_digest(
        expected_signature(auth_token, url, params).encode("ascii"),
        signature.encode("utf-8", "replace"),
    )


@dataclass(frozen=True, slots=True)
class TwilioConfig:
    """Everything the transport needs that is not per-call."""

    #: Public origin Twilio calls back on, e.g. ``https://voice.example.com``.
    public_base_url: str
    #: Account auth token. Every webhook is signature-checked against it, and
    #: it never appears in a repr.
    auth_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            parts = urlsplit(self.public_base_url or "")
            host = (parts.hostname or "").lower()
        except ValueError:
            parts, host = None, ""
        if parts is None or parts.scheme not in ("https", "http") or not host:
            raise ValueError("public_base_url must be an https:// URL with a host")
        if parts.query or parts.fragment:
            raise ValueError("public_base_url must not carry a query or fragment")
        if parts.scheme == "http" and host not in LOOPBACK_HOSTS:
            raise ValueError(
                f"public_base_url uses plain http:// to '{host}'; use https:// off loopback"
            )
        if not self.auth_token:
            raise ValueError("auth_token is required: every webhook is signature-checked")

    @property
    def voice_url(self) -> str:
        return self.public_base_url.rstrip("/") + "/twilio/voice"

    @property
    def stream_url(self) -> str:
        parts = urlsplit(self.public_base_url)  # lowercases the scheme
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}{parts.path.rstrip('/')}/twilio/stream"


class StreamTokens:
    """Single-use tokens that tie one media socket to one signed webhook call."""

    def __init__(
        self,
        *,
        ttl_s: float = STREAM_TOKEN_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._pending: dict[str, tuple[str, float]] = {}

    def __len__(self) -> int:
        self._prune()
        return len(self._pending)

    def issue(self, call_sid: str) -> str:
        if not call_sid:
            raise ValueError("call_sid is required")
        self._prune()
        token = secrets.token_urlsafe(32)
        self._pending[token] = (call_sid, self._clock() + self._ttl_s)
        return token

    def redeem(self, token: str | None, call_sid: str | None) -> bool:
        """True once, for a fresh token that was issued to this same CallSid."""

        if not token or not call_sid:
            return False
        entry = self._pending.pop(token, None)
        if entry is None:
            return False
        issued_to, deadline = entry
        return issued_to == call_sid and deadline > self._clock()

    def _prune(self) -> None:
        now = self._clock()
        for token in [key for key, (_, deadline) in self._pending.items() if deadline <= now]:
            del self._pending[token]


SendMedia = Callable[[bytes], Awaitable[None]]
SendControl = Callable[[], Awaitable[None]]


class TwilioCallBridge:
    """Bridges one Twilio Media Stream to one realtime session."""

    def __init__(
        self,
        session: rt.RealtimeSession,
        *,
        send_media: SendMedia,
        clear_playback: SendControl | None = None,
        input_rate: int = SESSION_RATE,
        output_rate: int = SESSION_RATE,
    ) -> None:
        self.session = session
        self._send_media = send_media
        self._clear_playback = clear_playback
        self._inbound = PcmResampler(TWILIO_RATE, input_rate)
        self._outbound = PcmResampler(output_rate, TWILIO_RATE)
        self._closed = False

    async def connect(self, setup: rt.SessionSetup) -> None:
        await self.session.connect(setup)

    async def feed_media(self, payload: bytes) -> None:
        """Take one inbound µ-law frame from Twilio and feed the session."""

        if self._closed:
            return
        pcm8 = ulaw_to_pcm16(payload)
        upsampled = self._inbound.process(pcm8)
        if upsampled:
            await self.session.send([rt.AppendInputAudio(data=upsampled)])

    async def run(self) -> None:
        """Consume session events until the session ends or fails for good."""

        async for event in self.session:
            if isinstance(event, rt.OutputAudio):
                pcm = self._outbound.process(event.data)
                for frame in frame_ulaw(pcm16_to_ulaw(pcm)):
                    await self._send_media(frame)
            elif isinstance(event, (rt.SpeechStarted, rt.OutputInterrupted)):
                # Gemini reports barge-in as SpeechStarted (OutputInterrupted
                # only on its delegation path). Twilio has already buffered
                # the reply, so it has to be told to drop it.
                if self._clear_playback is not None:
                    await self._clear_playback()
            elif isinstance(event, rt.SessionTerminated) or (
                isinstance(event, rt.ProviderFailure) and event.terminal
            ):
                return

    async def close(self) -> None:
        self._closed = True
        close = getattr(self.session, "close", None)
        if close is not None:
            await close()


def _import_aiohttp():
    try:
        import aiohttp
        import aiohttp.web
    except ImportError:  # pragma: no cover - dependency gate
        raise RuntimeError("the Twilio server needs aiohttp: pip install aiohttp") from None
    return aiohttp


def create_app(
    *,
    config: TwilioConfig,
    session_factory: Callable[[], rt.RealtimeSession],
    setup_factory: Callable[[], rt.SessionSetup],
    caller_policy: Callable[[str | None], bool] | None = None,
    max_calls: int = 2,
    max_call_s: float = 15 * 60,
    tokens: StreamTokens | None = None,
    aiohttp_module: Any = None,
):
    """Build the aiohttp app exposing the Voice webhook and the stream socket.

    ``caller_policy`` sees the webhook's ``From`` number; a call it refuses, or
    one past ``max_calls``, is declined with ``<Reject>`` and never answered.
    """

    aiohttp: Any = aiohttp_module or _import_aiohttp()
    web = aiohttp.web
    tokens = tokens if tokens is not None else StreamTokens()
    live = {"calls": 0}

    def xml(document: str):
        return web.Response(text=document, content_type="text/xml")

    async def voice(request):
        form = await request.post()
        params = {key: str(value) for key, value in form.items()}
        # Sign against the configured public URL, not the local one: a tunnel
        # or proxy in front changes the scheme, host and maybe the path.
        query = request.rel_url.raw_query_string
        url = config.voice_url + (f"?{query}" if query else "")
        signature = request.headers.get("X-Twilio-Signature")
        if not validate_signature(config.auth_token or "", url, params, signature):
            _log.warning("twilio webhook refused: missing or invalid signature")
            raise web.HTTPForbidden(text="invalid Twilio signature")

        call_sid = params.get("CallSid") or ""
        if not call_sid:
            raise web.HTTPBadRequest(text="CallSid is required")
        if caller_policy is not None and not caller_policy(params.get("From")):
            _log.info("twilio call declined: caller is not allowed")
            return xml(twiml_reject())
        if live["calls"] + len(tokens) >= max_calls:
            _log.info("twilio call declined as busy: the %d-call limit is reached", max_calls)
            return xml(twiml_reject("busy"))
        return xml(twiml_stream(config.stream_url, {"token": tokens.issue(call_sid)}))

    async def stream(request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_MESSAGE_BYTES)
        await ws.prepare(request)
        loop = asyncio.get_running_loop()
        closing: set[asyncio.Task] = set()
        bridge: TwilioCallBridge | None = None
        run_task: asyncio.Task | None = None

        def hang_up() -> None:
            if not ws.closed:
                task = loop.create_task(ws.close())
                closing.add(task)
                task.add_done_callback(closing.discard)

        def start_deadline() -> None:
            if bridge is None:
                _log.info("twilio stream closed: no valid start in time")
                hang_up()

        def session_ended(_task: asyncio.Task) -> None:
            hang_up()

        timers = [loop.call_later(STREAM_START_TIMEOUT_S, start_deadline)]
        try:
            async for message in ws:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = parse_message(message.data)
                except ValueError:
                    continue

                if frame.event == "start":
                    if bridge is not None:
                        continue  # one session per socket
                    if not tokens.redeem(frame.parameters.get("token"), frame.call_sid):
                        _log.warning("twilio stream refused: missing, reused or expired token")
                        break

                    async def send_media(payload: bytes, sid=frame.stream_sid) -> None:
                        if ws.closed:
                            return
                        await ws.send_json(
                            {
                                "event": "media",
                                "streamSid": sid,
                                "media": {"payload": base64.b64encode(payload).decode("ascii")},
                            }
                        )

                    async def clear_playback(sid=frame.stream_sid) -> None:
                        if not ws.closed:
                            await ws.send_json({"event": "clear", "streamSid": sid})

                    bridge = TwilioCallBridge(
                        session_factory(),
                        send_media=send_media,
                        clear_playback=clear_playback,
                    )
                    live["calls"] += 1
                    _log.info("twilio call started")
                    await bridge.connect(setup_factory())
                    run_task = asyncio.create_task(bridge.run())
                    run_task.add_done_callback(session_ended)
                    timers.append(loop.call_later(max_call_s, hang_up))
                elif frame.event == "media" and bridge is not None and run_task is not None:
                    await bridge.feed_media(frame.payload or b"")
                elif frame.event == "stop":
                    break
        except Exception as exc:  # noqa: BLE001 - end this call, never log its stack
            # Type only: a provider error string can carry a URL with its key.
            _log.warning("twilio call ended on an error (%s)", type(exc).__name__)
        finally:
            for timer in timers:
                timer.cancel()
            if bridge is not None:
                # Free the slot before any await, so a cancelled cleanup cannot leak it.
                live["calls"] -= 1
                _log.info("twilio call ended")
            if run_task is not None:
                run_task.remove_done_callback(session_ended)
                run_task.cancel()
                # gather, not suppress: this handler's own cancellation still propagates.
                await asyncio.gather(run_task, return_exceptions=True)
            if bridge is not None:
                with contextlib.suppress(Exception):
                    await bridge.close()
            if not ws.closed:
                await ws.close()
            if closing:
                await asyncio.gather(*closing, return_exceptions=True)
        return ws

    app = web.Application()
    # POST only, Twilio's default: a GET webhook would put the caller's number
    # in the URL.
    app.router.add_post("/twilio/voice", voice)
    app.router.add_get("/twilio/stream", stream)
    return app


async def serve(
    *,
    config: TwilioConfig,
    session_factory: Callable[[], rt.RealtimeSession],
    setup_factory: Callable[[], rt.SessionSetup],
    caller_policy: Callable[[str | None], bool] | None = None,
    max_calls: int = 2,
    max_call_s: float = 15 * 60,
    host: str = "127.0.0.1",
    port: int = 8767,
) -> None:
    """Run the transport on loopback until cancelled.

    The server speaks plain HTTP, so it never binds off loopback: a TLS tunnel
    or reverse proxy on the same machine is what Twilio talks to.
    """

    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"refusing to listen on {host!r}: loopback only (plain HTTP)")
    aiohttp = _import_aiohttp()
    app = create_app(
        config=config,
        session_factory=session_factory,
        setup_factory=setup_factory,
        caller_policy=caller_policy,
        max_calls=max_calls,
        max_call_s=max_call_s,
        aiohttp_module=aiohttp,
    )
    # No access log: request lines are not needed and should never carry call data.
    runner = aiohttp.web.AppRunner(app, access_log=None)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, host, port)
    await site.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
