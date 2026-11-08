"""Twilio Media Streams transport for Talk.

Twilio Programmable Voice carries call audio as 8 kHz G.711 µ-law frames over a
WebSocket ("Media Streams"). A Talk realtime session speaks PCM16 at a provider
rate: Gemini Live wants 16 kHz in and returns 24 kHz, the OpenAI Realtime lane
wants 24 kHz both ways. This module is the transport side only — it converts
between those worlds and drives a :class:`talk_realtime.RealtimeSession`, and it
takes no view on which provider sits behind that session.

Design notes:

* **No new dependencies.** ``audioop`` was removed in Python 3.13 and the repo
  ships flat modules with only ``httpx`` and ``aiohttp`` as runtime deps, so the
  µ-law codec and the resampler are implemented here. Both are table/loop based
  and comfortably fast at 8 kHz (50 frames a second, 160 samples a frame).
* **No local VAD.** Twilio does not detect speech, and Gemini Live does its own
  server-side endpointing, so inbound audio is forwarded continuously. Barge-in
  arrives as :class:`talk_realtime.OutputInterrupted`, which maps to Twilio's
  ``clear`` message so already-buffered agent audio stops immediately.
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
import struct
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

try:
    from . import talk_realtime as rt
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_realtime as rt

#: Twilio Media Streams are always 8 kHz mono G.711 µ-law.
TWILIO_RATE = 8000
#: Twilio sends and expects 20 ms frames: 160 samples / 160 µ-law bytes.
TWILIO_FRAME_MS = 20
TWILIO_FRAME_BYTES = TWILIO_RATE * TWILIO_FRAME_MS // 1000
#: Refuse obviously oversized frames rather than buffering them.
MAX_MEDIA_BYTES = 8192
MAX_MESSAGE_BYTES = 64 * 1024

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
        raw=data,
    )


def twiml_stream(stream_url: str) -> str:
    """Return the TwiML document that opens a bidirectional Media Stream."""

    if not stream_url.startswith(("wss://", "ws://")):
        raise ValueError("stream_url must be a WebSocket URL")
    escaped = (
        stream_url.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Connect><Stream url="{escaped}"/></Connect></Response>'
    )


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
    return hmac.compare_digest(expected_signature(auth_token, url, params), signature)


@dataclass(frozen=True, slots=True)
class TwilioConfig:
    """Everything the transport needs that is not per-call."""

    #: Public origin Twilio calls back on, e.g. ``https://voice.example.com``.
    public_base_url: str
    #: Account auth token, used to verify inbound webhook signatures.
    auth_token: str | None = None
    #: Sample rate the realtime provider expects for input audio.
    input_rate: int = 16000
    #: Sample rate the realtime provider emits for output audio.
    output_rate: int = 24000
    #: Twilio signs every webhook; disable only for local tunnel testing.
    require_signature: bool = True

    def __post_init__(self) -> None:
        if not self.public_base_url:
            raise ValueError("public_base_url is required")
        if self.require_signature and not self.auth_token:
            raise ValueError("auth_token is required when signature checks are on")

    @property
    def voice_url(self) -> str:
        return self.public_base_url.rstrip("/") + "/twilio/voice"

    @property
    def stream_url(self) -> str:
        base = self.public_base_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return base + "/twilio/stream"


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
        input_rate: int = 16000,
        output_rate: int = 24000,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self.session = session
        self._send_media = send_media
        self._clear_playback = clear_playback
        self._inbound = PcmResampler(TWILIO_RATE, input_rate)
        self._outbound = PcmResampler(output_rate, TWILIO_RATE)
        self._on_error = on_error
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
        """Consume session events until the session ends."""

        async for event in self.session:
            if isinstance(event, rt.OutputAudio):
                pcm = self._outbound.process(event.data)
                for frame in frame_ulaw(pcm16_to_ulaw(pcm)):
                    await self._send_media(frame)
            elif isinstance(event, rt.OutputInterrupted) and self._clear_playback is not None:
                await self._clear_playback()

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
        raise RuntimeError("aiohttp is required for the Twilio transport") from None
    return aiohttp


def _signed_url(config: TwilioConfig, path: str, query: str = "") -> str:
    url = config.public_base_url.rstrip("/") + path
    return f"{url}?{query}" if query else url


def create_app(
    *,
    config: TwilioConfig,
    session_factory: Callable[[], rt.RealtimeSession],
    setup_factory: Callable[[], rt.SessionSetup],
    aiohttp_module: Any = None,
):
    """Build the aiohttp app exposing the Voice webhook and the stream socket."""

    aiohttp: Any = aiohttp_module or _import_aiohttp()
    web = aiohttp.web

    async def voice(request):
        if config.require_signature:
            if request.method == "POST":
                form = await request.post()
                params = {key: str(value) for key, value in form.items()}
            else:
                params = {key: str(value) for key, value in request.query.items()}
            url = _signed_url(config, request.path, request.query_string)
            signature = request.headers.get("X-Twilio-Signature")
            if not validate_signature(config.auth_token or "", url, params, signature):
                raise web.HTTPForbidden(text="invalid Twilio signature")
        return web.Response(text=twiml_stream(config.stream_url), content_type="text/xml")

    async def stream(request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        state: dict[str, Any] = {"sid": None, "bridge": None, "task": None}
        try:
            async for message in ws:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = parse_message(message.data)
                except ValueError:
                    continue

                if frame.event == "start":
                    state["sid"] = frame.stream_sid
                    session = session_factory()

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
                        session,
                        send_media=send_media,
                        clear_playback=clear_playback,
                        input_rate=config.input_rate,
                        output_rate=config.output_rate,
                    )
                    await bridge.connect(setup_factory())
                    state["bridge"] = bridge
                    state["task"] = asyncio.create_task(bridge.run())
                elif frame.event == "media" and state["bridge"] is not None:
                    await state["bridge"].feed_media(frame.payload or b"")
                elif frame.event == "stop":
                    break
        finally:
            task = state.get("task")
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            bridge = state.get("bridge")
            if bridge is not None:
                await bridge.close()
            if not ws.closed:
                await ws.close()
        return ws

    app = web.Application()
    # Twilio posts by default, but the console lets the URL be configured as GET.
    app.router.add_route("*", "/twilio/voice", voice)
    app.router.add_get("/twilio/stream", stream)
    return app


async def serve(
    *,
    config: TwilioConfig,
    session_factory: Callable[[], rt.RealtimeSession],
    setup_factory: Callable[[], rt.SessionSetup],
    host: str = "127.0.0.1",
    port: int = 8767,
) -> None:
    """Run the transport until cancelled."""

    aiohttp = _import_aiohttp()
    app = create_app(
        config=config,
        session_factory=session_factory,
        setup_factory=setup_factory,
        aiohttp_module=aiohttp,
    )
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, host, port)
    await site.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
