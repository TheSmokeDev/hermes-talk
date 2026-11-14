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


def twiml_stream(stream_url: str, parameters: Mapping[str, str] | None = None) -> str:
    """Return the TwiML that opens a bidirectional Media Stream.

    ``parameters`` become ``<Parameter>`` children, which Twilio echoes back in
    the stream's ``start.customParameters``. That is how the caller's number
    reaches the media socket: the WebSocket handshake carries no call metadata.
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
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect>{stream}</Connect></Response>'


def twiml_hangup(message: str = "") -> str:
    """Refuse the call: optionally say one line, then hang up."""

    say = f"<Say>{_xml_escape(message)}</Say>" if message else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{say}<Hangup/></Response>'


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
#: Runs one provider function call and returns the text handed back to it.
ToolExecutor = Callable[[str, Mapping[str, Any]], Awaitable[str]]
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
        greeting: str | None = None,
        tool_executor: ToolExecutor | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self.session = session
        self._send_media = send_media
        self._clear_playback = clear_playback
        self._inbound = PcmResampler(TWILIO_RATE, input_rate)
        self._outbound = PcmResampler(output_rate, TWILIO_RATE)
        self._greeting = greeting
        self._tool_executor = tool_executor
        self._cancelled_calls: set[str] = set()
        self._on_error = on_error
        self._closed = False

    async def connect(self, setup: rt.SessionSetup) -> None:
        await self.session.connect(setup)
        if self._greeting:
            # A phone call has no visual cue: without this the caller waits for
            # the agent and the agent waits for the caller.
            await self.session.send(
                [
                    rt.AddInputText(item_id="twilio-greeting", text=self._greeting),
                    rt.StartResponse(),
                ]
            )

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
            elif isinstance(event, rt.ToolCallsCancelled):
                self._cancelled_calls.update(getattr(event, "call_ids", ()))
            elif isinstance(event, rt.FunctionCall):
                await self._answer_tool_call(event)

    async def _answer_tool_call(self, call: rt.FunctionCall) -> None:
        """Run one function call and hand the result back to the provider."""

        if self._tool_executor is None:
            output = "error: no tools are available on this transport"
        else:
            try:
                arguments = json.loads(call.arguments) if call.arguments else {}
            except json.JSONDecodeError:
                arguments = {}
            try:
                output = await self._tool_executor(call.name, arguments)
            except Exception as exc:  # noqa: BLE001 - report, never kill the call
                output = f"error: {type(exc).__name__}: {exc}"
        if call.call_id in self._cancelled_calls:
            self._cancelled_calls.discard(call.call_id)
            return
        await self.session.send([rt.SubmitToolResult(call_id=call.call_id, output=output)])

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
    session_factory: Callable[[str | None], rt.RealtimeSession],
    setup_factory: Callable[[str | None], rt.SessionSetup],
    greeting: str | None = None,
    tool_executor: ToolExecutor | None = None,
    caller_policy: Callable[[str | None], bool] | None = None,
    deny_message: str = "",
    aiohttp_module: Any = None,
):
    """Build the aiohttp app exposing the Voice webhook and the stream socket."""

    aiohttp: Any = aiohttp_module or _import_aiohttp()
    web = aiohttp.web

    async def voice(request):
        if request.method == "POST":
            form = await request.post()
            params = {key: str(value) for key, value in form.items()}
        else:
            params = {key: str(value) for key, value in request.query.items()}

        if config.require_signature:
            url = _signed_url(config, request.path, request.query_string)
            signature = request.headers.get("X-Twilio-Signature")
            if not validate_signature(config.auth_token or "", url, params, signature):
                raise web.HTTPForbidden(text="invalid Twilio signature")

        caller = params.get("From")
        if caller_policy is not None and not caller_policy(caller):
            return web.Response(text=twiml_hangup(deny_message), content_type="text/xml")

        document = twiml_stream(config.stream_url, {"caller": caller or ""})
        return web.Response(text=document, content_type="text/xml")

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
                    caller = frame.parameters.get("caller") or None
                    session = session_factory(caller)

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
                        greeting=greeting,
                        tool_executor=tool_executor,
                    )
                    await bridge.connect(setup_factory(caller))
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
    session_factory: Callable[[str | None], rt.RealtimeSession],
    setup_factory: Callable[[str | None], rt.SessionSetup],
    greeting: str | None = None,
    tool_executor: ToolExecutor | None = None,
    caller_policy: Callable[[str | None], bool] | None = None,
    deny_message: str = "",
    host: str = "127.0.0.1",
    port: int = 8767,
) -> None:
    """Run the transport until cancelled."""

    aiohttp = _import_aiohttp()
    app = create_app(
        config=config,
        session_factory=session_factory,
        setup_factory=setup_factory,
        greeting=greeting,
        tool_executor=tool_executor,
        caller_policy=caller_policy,
        deny_message=deny_message,
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
