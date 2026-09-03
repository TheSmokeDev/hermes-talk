"""Gemini Live implementation of the provider-neutral Realtime session contract.

The Live API speaks its own BidiGenerateContent vocabulary over an API-key
WebSocket — and on this lane the key rides the URL query, so the URL itself
is a secret: it is assembled once at connect, held only until the upgrade
resolves, never logged, and scrubbed out of handshake errors before they can
surface.

Probe ground truth this module is written against (live API, 2026-08-28,
operator key):

- ``{"setup": {...}}`` is the first client message, acked with
  ``{"setupComplete": {}}``. Setup carries the model as ``models/<id>``,
  ``generationConfig.responseModalities: ["AUDIO"]`` with a prebuilt voice,
  ``systemInstruction.parts[].text``, function tools whose parameter schema
  types are UPPERCASE (``"OBJECT"``, ``"STRING"``), and empty
  ``outputAudioTranscription`` / ``inputAudioTranscription`` objects enabling
  both transcript lanes.
- Tool calls arrive as ``toolCall.functionCalls[]`` whose ``args`` are a
  PARSED DICT (OpenAI/xAI send a JSON string) and are answered with a
  ``toolResponse.functionResponses[]`` envelope keyed by the call ``id``;
  the model then speaks the result with no further prompting. The full loop
  round-tripped live.
- Assistant audio is ``serverContent.modelTurn.parts[].inlineData`` at
  ``audio/pcm;rate=24000``; output and input transcripts arrive as
  ``serverContent.outputTranscription.text`` / ``inputTranscription.text``
  chunks; ``generationComplete`` / ``turnComplete`` (carrying
  ``usageMetadata``) / ``interrupted`` are flags on ``serverContent``. Empty
  serverContent frames and unknown messages are tolerated.
- ``sessionResumptionUpdate.newHandle`` is native: v1 enables the feature in
  setup and records the latest CONFIRMED handle on the session
  (``resumption_handle``); reconnecting with it is a follow-up feature, not
  this adapter.
- Live smoke, 2026-08-28: the endpoint speaks its JSON in BINARY WebSocket
  frames on some connections. TEXT and BINARY frames are accepted and parse
  identically, and one malformed frame is a NON-terminal failure — a single
  bad frame must never kill a call.

Reference-verified behavior (Google Live docs plus the shipped OpenClaw,
Pipecat, and LiveKit providers — NOT the live probe):

- ``toolCallCancellation.ids`` arrives when the operator interrupts while
  tool calls are pending: those results must never go upstream.
- ``sessionResumptionUpdate`` carrying ``resumable: false`` INVALIDATES the
  cached handle — reusing it would be silent data loss — while an update
  omitting ``resumable`` leaves the last confirmed handle in place.
- Audio-only sessions hard-cap near 15 minutes without
  ``contextWindowCompression``; an empty ``slidingWindow`` takes the server
  defaults and keeps the system instruction intact.
- ``goAway.timeLeft`` warns of imminent server-side termination (the socket
  then dies ABORTED); it surfaces as a terminal failure so the relay speaks
  and closes cleanly instead of hitting a dead socket.
- One ``serverContent`` frame can bundle modelTurn parts WITH
  ``generationComplete``/``turnComplete``, and after ``generationComplete``
  trailing text/audio for that same generation can still arrive — every
  field of a frame is processed before the terminal flag is honored, and
  post-close stragglers are dropped with a one-time warning per window.

Deliberate degrades, each verified against the wire or honestly absent:

- Input audio: the relay feeds 24kHz PCM; Live declares 16kHz input
  (``audio/pcm;rate=16000`` — the input rate itself was NOT exercised live;
  it follows the API reference). A small pure-Python streaming resampler
  downsamples 24k -> 16k on the send path; output stays native 24kHz.
- ``CancelResponse`` / ``TruncateOutput``: the Live protocol has NO client
  cancel or truncate command. Both degrade to local playback handling plus
  one logged receipt per session — nothing is sent upstream, and a
  truncation that did not happen is never faked.
- Barge-in: the server reports ``serverContent.interrupted: true`` after its
  own VAD already stopped the generation. That maps to the contract's
  SpeechStarted (the relay drops local playback and settles the interrupted
  response) followed by ResponseFinished bookkeeping.
- ``StartResponse`` riding a tool-result batch is DROPPED: the probe showed a
  ``toolResponse`` alone makes the model speak, so an extra empty-turn
  trigger would double the answer. A standalone ``StartResponse`` (the
  announcement flow) maps to ``clientContent`` with ``turnComplete: true`` —
  that bare-trigger shape was NOT probed live and is the documented risk.
- ``RemoveContext`` has no wire equivalent (Live cannot delete conversation
  content): it degrades to a no-op with one logged receipt, which weakens
  the announcement flow's self-delete containment to framing-text only.
- ``automatic_response=False`` (the Discord authorization-ledger flow) has no
  Live mapping — disabling automatic activity detection would require manual
  activity signals this pipeline cannot produce — so connect REFUSES it
  rather than silently answering speakers the ledger never vetted.

The command/event encoding deliberately duplicates the sibling adapters'
structure rather than importing across providers: the vocabularies will
drift as each platform normalizes differently, and a shared decoder would
make every provider's edit a risk to the others.
"""

from __future__ import annotations

import array
import asyncio
import base64
import binascii
import json
import logging
import re
import sys
import uuid
from collections import deque
from collections.abc import Sequence
from contextlib import AsyncExitStack
from typing import Any

try:
    from . import talk_realtime as rt
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_realtime as rt

logger = logging.getLogger(__name__)

GEMINI_LIVE_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
CONNECT_TIMEOUT_S = 30.0
NORMAL_WEBSOCKET_CLOSE_CODES = (None, 1000, 1001)
#: Declared input format: Live takes 16kHz PCM in, and emits 24kHz PCM out.
INPUT_AUDIO_MIME_TYPE = "audio/pcm;rate=16000"
#: Bound on remembered server-cancelled tool-call ids (OpenClaw uses the
#: same cap). Past it the oldest id is forgotten; a stale result for a
#: forgotten id would then go upstream — possible, logged, never silent.
MAX_CANCELLED_CALL_IDS = 1024
#: The API key is a URL query parameter on this lane; any error text built
#: from the request (handshake failures name the URL) must never keep it.
#: The pattern also catches a bare `` key=…`` in prose-shaped errors: on this
#: lane any such text is suspect.
_URL_KEY_RE = re.compile(r"(?<=[?&\s])key=[^&\s]+")


def _scrub(detail: str) -> str:
    """Strip the credential-bearing URL query out of transport error text."""

    return _URL_KEY_RE.sub("key=<redacted>", detail)


def _import_aiohttp():
    try:
        import aiohttp
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise rt.RealtimeSessionError(
            "aiohttp is required for the voice session — run: pip install hermes-talk"
        ) from exc
    return aiohttp


class Pcm24To16Resampler:
    """Streaming 24kHz -> 16kHz s16le mono PCM downsampler.

    Linear interpolation at a fixed 3:2 ratio: output sample ``i`` reads the
    input at position ``i * 1.5``, so each input trio ``s0, s1, s2`` yields
    ``s0`` and the midpoint of ``s1, s2``. The trailing incomplete trio
    carries over to the next call, so a chunked feed equals a one-shot feed.
    The midpoint of two int16 samples stays in range by construction — the
    resampler cannot clip. Deliberately minimal: the bar is speech
    intelligibility (the Discord lane's integer decimation set the
    precedent), not studio fidelity, and nothing beyond the stdlib.
    """

    def __init__(self) -> None:
        self._pending = array.array("h")

    def feed(self, pcm: bytes) -> bytes:
        """Downsample one s16le mono chunk; carry any sub-trio remainder."""

        if len(pcm) % 2:
            raise ValueError("PCM s16le chunks must be byte-aligned to 2")
        samples = array.array("h")
        samples.frombytes(pcm)
        if sys.byteorder == "big":  # pragma: no cover - supported hosts are LE
            samples.byteswap()
        buf = self._pending
        buf.extend(samples)
        consumed = (len(buf) // 3) * 3
        out = array.array("h")
        for offset in range(0, consumed, 3):
            out.append(buf[offset])
            out.append(round((buf[offset + 1] + buf[offset + 2]) / 2))
        del buf[:consumed]
        if sys.byteorder == "big":  # pragma: no cover - supported hosts are LE
            out.byteswap()
        return out.tobytes()


def _wire_model(model: str) -> str:
    """The bare config model id with the API's ``models/`` prefix, exactly once."""

    return model if model.startswith("models/") else f"models/{model}"


def _schema_wire(value: Any) -> Any:
    """Uppercase JSON-Schema ``type`` names into the Live API's enum vocabulary."""

    if isinstance(value, dict):
        return {
            key: item.upper() if key == "type" and isinstance(item, str) else _schema_wire(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_schema_wire(item) for item in value]
    return value


def _tool_wire(tool: rt.ToolDefinition) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": _schema_wire(dict(tool.parameters)),
    }


def build_setup_message(setup: rt.SessionSetup) -> dict[str, Any]:
    """Map neutral setup to the Live ``setup`` message.

    The model is part of THIS payload as ``models/<id>`` — only the key rides
    the socket URL, the inverse split of the xAI lane. Server VAD stays at
    its default (automatic activity detection on): ``realtimeInputConfig`` is
    deliberately absent because touching it can only narrow detection, never
    reproduce the OpenAI lane's ``create_response`` gating.
    """

    payload: dict[str, Any] = {
        "model": _wire_model(setup.model),
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": setup.voice}}
            },
        },
        "systemInstruction": {"parts": [{"text": setup.instructions}]},
        "outputAudioTranscription": {},
        "inputAudioTranscription": {},
        # Record-only in v1: handles are tracked (and server-invalidated ones
        # discarded) but never sent back — reconnect is the follow-up feature.
        "sessionResumption": {},
        # Audio-only sessions hard-cap near 15 minutes without compression.
        # An empty slidingWindow takes the server defaults (trigger at 80% of
        # the context window, target half of that; system instruction is
        # never cut).
        "contextWindowCompression": {"slidingWindow": {}},
    }
    if setup.tools:
        payload["tools"] = [{"functionDeclarations": [_tool_wire(t) for t in setup.tools]}]
    return {"setup": payload}


def _audio_message(pcm_16k: bytes) -> dict[str, Any]:
    """One ``realtimeInput.audio`` chunk — the shape the probe notes name."""

    return {
        "realtimeInput": {
            "audio": {
                "mimeType": INPUT_AUDIO_MIME_TYPE,
                "data": base64.b64encode(pcm_16k).decode("ascii"),
            }
        }
    }


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _error_detail(message: dict[str, Any]) -> str:
    error = message.get("error")
    if isinstance(error, dict):
        return _scrub(str(error.get("message") or error.get("code") or ""))
    return _scrub(str(error or ""))


class GeminiWireError(RuntimeError):
    """A terminal low-level Gemini Live transport or wire-protocol failure."""


class GeminiWireFrameError(ValueError):
    """One undecodable frame; non-fatal — the stream continues after it."""


class GeminiWireEOF(EOFError):
    """The WebSocket iterator ended; ``detail`` is blank only for a clean EOF."""

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(detail)


class _GeminiWireSession:
    """The key-in-URL WebSocket under :class:`GeminiRealtimeSession`.

    There is no ephemeral mint on this lane either, and unlike xAI the
    credential is not even a header — it is the URL query. The key is held
    only until the upgrade attempt resolves, cleared on every terminal path,
    and scrubbed from any handshake error that could quote the request URL.
    """

    def __init__(self, *, auth_token: str, auth_source: str, aiohttp_module=None) -> None:
        self._auth_token: str | None = auth_token
        self._auth_source: str | None = auth_source
        self._aiohttp = aiohttp_module
        self._stack: AsyncExitStack | None = None
        self._ws = None
        self._iterator = None
        self._send_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._close_failure: BaseException | None = None
        self._closed = False
        self._connect_started = False

    def _clear_raw_credentials(self) -> None:
        self._auth_token = None
        self._auth_source = None

    async def connect(self, *, setup_message: dict[str, Any]) -> None:
        if self._connect_started or self._closed:
            raise GeminiWireError("Realtime wire connect may only run once")
        self._connect_started = True
        stack = AsyncExitStack()
        try:
            auth_token = self._auth_token
            if auth_token is None:
                raise GeminiWireError("Realtime wire credentials are unavailable")
            aiohttp = self._aiohttp or _import_aiohttp()
            http = await stack.enter_async_context(aiohttp.ClientSession())
            try:
                # The URL is built exactly here and is never stored or logged;
                # a handshake failure's text may quote it, so it is scrubbed.
                ws_url = f"{GEMINI_LIVE_WS_URL}?key={auth_token}"
                try:
                    ws = await stack.enter_async_context(
                        http.ws_connect(
                            ws_url,
                            timeout=CONNECT_TIMEOUT_S,
                            heartbeat=20.0,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - handshake errors can
                    # quote the credential-bearing URL; scrubbed before raise.
                    detail = _scrub(str(exc)).replace(auth_token, "<redacted>")
                    raise GeminiWireError(
                        detail or "Gemini Live WebSocket upgrade failed"
                    ) from None
            finally:
                self._clear_raw_credentials()
            self._stack = stack
            self._ws = ws
            self._iterator = ws.__aiter__()
            await self.send_json((setup_message,))
        except BaseException:
            self._clear_raw_credentials()
            self._stack = None
            await stack.aclose()
            raise

    async def send_json(self, messages: Sequence[dict[str, Any]]) -> None:
        if self._ws is None:
            raise GeminiWireError("Realtime wire is not connected")
        try:
            async with self._send_lock:
                for message in messages:
                    await self._ws.send_json(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise GeminiWireError(_scrub(str(exc))) from exc

    def __aiter__(self):
        return self

    def _eof_failure_detail(self) -> str:
        if self._ws is None:
            return ""
        close_code = getattr(self._ws, "close_code", None)
        socket_exception = None
        exception = getattr(self._ws, "exception", None)
        if callable(exception):
            try:
                socket_exception = exception()
            except Exception as exc:  # noqa: BLE001 - broken status probe is failure
                socket_exception = exc
        if close_code in NORMAL_WEBSOCKET_CLOSE_CODES and socket_exception is None:
            return ""
        reasons = []
        if close_code not in NORMAL_WEBSOCKET_CLOSE_CODES:
            reasons.append(f"close code {close_code}")
        if socket_exception is not None:
            detail = str(socket_exception).strip() or type(socket_exception).__name__
            reasons.append(_scrub(detail))
        return f"Provider WebSocket closed abnormally ({'; '.join(reasons)})"

    async def __anext__(self) -> dict[str, Any]:
        if self._iterator is None:
            raise GeminiWireEOF("")
        while True:
            try:
                message = await self._iterator.__anext__()
            except StopAsyncIteration:
                raise GeminiWireEOF(self._eof_failure_detail()) from None
            aiohttp = self._aiohttp or _import_aiohttp()
            if message.type == getattr(aiohttp.WSMsgType, "ERROR", object()):
                detail = _scrub(str(getattr(message, "data", "") or "").strip())
                raise GeminiWireError(detail or "Provider WebSocket receive failed")
            if message.type is aiohttp.WSMsgType.TEXT:
                payload = message.data
            elif message.type is getattr(aiohttp.WSMsgType, "BINARY", object()):
                # The Google endpoint speaks its JSON in BINARY frames on
                # some connections (live smoke, 2026-08-28): decode UTF-8 and
                # parse exactly like a text frame. UnicodeDecodeError is a
                # ValueError, so malformed bytes land in the same
                # malformed-frame path as bad text JSON below.
                payload = message.data
            else:
                passive_types = {
                    getattr(aiohttp.WSMsgType, name, object())
                    for name in ("CLOSE", "CLOSED", "CLOSING", "PING", "PONG")
                }
                if message.type in passive_types:
                    continue
                frame_name = getattr(message.type, "name", str(message.type)).lower()
                raise GeminiWireError(
                    f"Provider sent unsupported WebSocket frame type: {frame_name}"
                )
            try:
                if isinstance(payload, (bytes, bytearray)):
                    payload = bytes(payload).decode("utf-8")
                wire_event = json.loads(payload)
            except (TypeError, ValueError) as exc:
                raise GeminiWireFrameError("Provider sent a malformed frame") from exc
            if not isinstance(wire_event, dict):
                raise GeminiWireFrameError("Provider sent a non-object frame")
            return wire_event

    async def _close_once(self) -> None:
        self._clear_raw_credentials()
        stack, self._stack = self._stack, None
        if stack is not None:
            try:
                await stack.aclose()
            except BaseException:
                self._stack = stack
                raise

    async def _finish_close(self) -> None:
        task = asyncio.current_task()
        try:
            await self._close_once()
        except BaseException as exc:
            async with self._close_lock:
                self._close_failure = exc
                if self._close_task is task:
                    self._close_task = None
            raise
        async with self._close_lock:
            if self._close_task is task:
                self._close_failure = None
                self._closed = True
                self._close_task = None

    @staticmethod
    def _observe_close_task(task: asyncio.Task[None]) -> None:
        """Retrieve late cleanup failures when every shielded waiter is gone."""

        if task.cancelled():
            return
        task.exception()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            task = self._close_task
            if task is None:
                task = asyncio.create_task(self._finish_close())
                task.add_done_callback(self._observe_close_task)
                self._close_task = task
        await asyncio.wait((task,))
        task.result()


class GeminiRealtimeSession:
    """Talk-neutral facade over the Gemini Live wire session."""

    def __init__(self, *, auth_token: str, auth_source: str, aiohttp_module=None) -> None:
        self.auth_token = auth_token
        self.auth_source = auth_source
        self.state = rt.SessionState.NEW
        self._wire = _GeminiWireSession(
            auth_token=auth_token,
            auth_source=auth_source,
            aiohttp_module=aiohttp_module,
        )
        self._resampler = Pcm24To16Resampler()
        self._pending: deque[rt.RealtimeEvent] = deque()
        self._call_names: dict[str, str] = {}
        #: Call ids the server cancelled mid-turn (``toolCallCancellation``).
        #: The value tracks whether that id's drop receipt was logged, so the
        #: receipt fires once per call id. Bounded by MAX_CANCELLED_CALL_IDS.
        self._cancelled_call_ids: dict[str, bool] = {}
        self._generation_open = False
        #: Armed by ``generationComplete``: 3.x servers can still send
        #: trailing audio/text for the just-closed generation before the turn
        #: ends. While armed, that content is dropped instead of reopening a
        #: phantom response. Disarmed by anything that legitimately starts
        #: the next turn: operator speech (inputTranscription/interrupted), a
        #: toolCall, or a client-sent toolResponse/bare trigger.
        self._trailing_fence = False
        self._trailing_warned = False
        self._user_transcript: list[str] = []
        self._degrade_logged: set[str] = set()
        self._terminal_emitted = False
        #: Live issues no session id; receipts get a locally synthesized one.
        self._session_id = f"gemini-live-{uuid.uuid4().hex[:16]}"
        #: Latest session-resumption handle, recorded for the follow-up
        #: reconnect feature; v1 never sends it back.
        self.resumption_handle: str | None = None

    async def connect(self, setup: rt.SessionSetup) -> None:
        if self.state is not rt.SessionState.NEW:
            raise rt.RealtimeSessionError("Realtime session connect may only run once")
        self.state = rt.SessionState.CONNECTING
        if not setup.automatic_response:
            # The gated-response flow (Discord's authorization ledger) needs
            # "hear everything, answer only when told". Live cannot hold a
            # response open like that, so this refuses instead of silently
            # answering speakers the ledger never vetted.
            self.state = rt.SessionState.FAILED
            raise rt.RealtimeSessionError(
                "the Gemini Live lane always answers on server VAD; "
                "automatic_response=False has no Live wire equivalent and is "
                "refused rather than degraded"
            )
        try:
            await self._wire.connect(setup_message=build_setup_message(setup))
        except asyncio.CancelledError:
            self.state = rt.SessionState.CLOSED
            raise
        except Exception as exc:
            self.state = rt.SessionState.FAILED
            if isinstance(exc, rt.RealtimeSessionError):
                raise
            raise rt.RealtimeSessionError(str(exc)) from exc
        self.state = rt.SessionState.CONNECTED

    def _degrade_receipt(self, kind: str) -> None:
        """Log a once-per-session receipt for a command with no wire form."""

        if kind in self._degrade_logged:
            return
        self._degrade_logged.add(kind)
        logger.warning(
            "gemini realtime: %s has no Live wire command; degraded to local "
            "handling for the rest of this session",
            kind,
        )

    def _tool_response_message(self, command: rt.SubmitToolResult) -> dict[str, Any]:
        """One ``toolResponse`` envelope keyed by the call's id.

        ``name`` rides along only when this session observed the matching
        ``toolCall`` — an unobserved call id never gets an invented name.
        """

        function_response: dict[str, Any] = {
            "id": command.call_id,
            "response": {"result": command.output},
        }
        name = self._call_names.pop(command.call_id, None)
        if name is not None:
            function_response["name"] = name
        return {"toolResponse": {"functionResponses": [function_response]}}

    def _encode(self, commands: Sequence[rt.RealtimeCommand]) -> list[dict[str, Any]]:
        # A toolResponse alone makes the model speak (probe-verified), so the
        # StartResponse continuation the relay appends to a tool batch must
        # not fire a second, empty-turn response on top of the spoken result.
        # A batch whose every result was cancelled by the server therefore
        # sends nothing at all — the barge-in that cancelled it already owns
        # the next turn.
        tool_result_present = any(
            isinstance(command, rt.SubmitToolResult) for command in commands
        )
        messages: list[dict[str, Any]] = []
        for command in commands:
            if isinstance(command, rt.AppendInputAudio):
                pcm = self._resampler.feed(command.data)
                if pcm:  # a sub-trio remainder yields no frame yet
                    messages.append(_audio_message(pcm))
            elif isinstance(command, rt.AddContext):
                # No system-role item exists mid-conversation on this wire;
                # the text keeps its own containment framing, and
                # turnComplete stays False so the model is not triggered early.
                messages.append(
                    {
                        "clientContent": {
                            "turns": [{"role": "user", "parts": [{"text": command.text}]}],
                            "turnComplete": False,
                        }
                    }
                )
            elif isinstance(command, rt.RemoveContext):
                self._degrade_receipt("conversation context delete")
            elif isinstance(command, rt.StartResponse):
                if command.metadata or command.allow_tools is not None:
                    self._degrade_receipt("per-response metadata and tool gating")
                if not tool_result_present:
                    # Bare response trigger for the announcement flow. NOT
                    # probed live — the one shape in this module the live
                    # probe did not exercise.
                    messages.append({"clientContent": {"turnComplete": True}})
                    self._trailing_fence = False  # we asked for this response
            elif isinstance(command, (rt.CancelResponse, rt.TruncateOutput)):
                self._degrade_receipt("response cancel/truncate")
            elif isinstance(command, rt.SubmitToolResult):
                logged = self._cancelled_call_ids.get(command.call_id)
                if logged is not None:
                    # The server discarded this call mid-turn; answering it
                    # would speak a result for a turn the operator cancelled.
                    self._call_names.pop(command.call_id, None)
                    if not logged:
                        self._cancelled_call_ids[command.call_id] = True
                        logger.warning(
                            "gemini realtime: dropping tool result for "
                            "server-cancelled call %s; nothing sent upstream",
                            command.call_id,
                        )
                    continue
                messages.append(self._tool_response_message(command))
                self._trailing_fence = False  # a tool answer opens a live turn
            else:
                raise TypeError(f"unsupported Realtime command: {type(command).__name__}")
        return messages

    async def send(self, commands: Sequence[rt.RealtimeCommand]) -> None:
        clean_eof_flush = self.state is rt.SessionState.CLOSED and self._terminal_emitted
        if self.state is not rt.SessionState.CONNECTED and not clean_eof_flush:
            raise rt.RealtimeSessionError("Realtime session is not connected")
        encoded = self._encode(commands)
        try:
            await self._wire.send_json(encoded)
        except Exception as exc:
            self.state = rt.SessionState.FAILED
            raise rt.RealtimeSessionError(str(exc)) from exc

    def __aiter__(self):
        return self

    def _flush_user_transcript(self, events: list[rt.RealtimeEvent]) -> None:
        """Fold buffered operator-transcript chunks into one final user turn.

        Live sends ``inputTranscription`` as chunks with no done marker, so
        the turn boundary is inferred: the model starting (or ending) its
        answer, calling a tool, or being interrupted closes the operator's
        turn. Chunks are never emitted as finals one at a time — that would
        shatter one sentence into many transcript turns.
        """

        text = "".join(self._user_transcript).strip()
        self._user_transcript.clear()
        if text:
            events.append(
                rt.Transcript(
                    role=rt.TranscriptRole.USER,
                    text=text,
                    final=True,
                    provenance=rt.TranscriptProvenance.INPUT_AUDIO,
                )
            )

    def _decode_tool_call(self, tool_call: dict[str, Any]) -> list[rt.RealtimeEvent]:
        calls = tool_call.get("functionCalls")
        if not isinstance(calls, list):
            return []
        events: list[rt.RealtimeEvent] = []
        self._flush_user_transcript(events)
        if not self._generation_open:
            # Mirror the GA shape the relay's tool coordinator is built on:
            # response opened, call(s) delivered, response closed so the
            # coordinator flushes its results.
            events.append(rt.ResponseStarted(response_id=None))
        self._generation_open = False
        for call in calls:
            if not isinstance(call, dict):
                continue
            args = call.get("args")
            # Live sends args as a PARSED DICT; the contract carries a JSON
            # string, so the translation happens exactly here.
            event = rt.FunctionCall(
                call_id=call.get("id"),
                name=call.get("name"),
                arguments=json.dumps(args if isinstance(args, dict) else {}),
            )
            self._call_names[event.call_id] = event.name
            events.append(event)
        events.append(rt.ResponseFinished(response_id=None))
        return events

    def _decode_server_content(self, content: dict[str, Any]) -> list[rt.RealtimeEvent]:
        events: list[rt.RealtimeEvent] = []
        interrupted = content.get("interrupted") is True

        input_transcription = content.get("inputTranscription")
        if isinstance(input_transcription, dict):
            text = input_transcription.get("text")
            if isinstance(text, str) and text:
                self._user_transcript.append(text)
                # Operator speech onset legitimately starts the next turn, so
                # what follows can never be trailing output of the last one.
                self._trailing_fence = False

        parts: list = []
        model_turn = content.get("modelTurn")
        has_model_turn = isinstance(model_turn, dict)
        if has_model_turn:
            # modelTurn is the generation marker itself, even with no parts.
            raw_parts = model_turn.get("parts")
            if isinstance(raw_parts, list):
                parts = raw_parts
        output_text = None
        output_transcription = content.get("outputTranscription")
        if isinstance(output_transcription, dict):
            candidate = output_transcription.get("text")
            if isinstance(candidate, str) and candidate:
                output_text = candidate

        trailing = False
        if interrupted:
            # The server's own VAD already stopped the in-flight generation.
            # SpeechStarted is the contract's barge-in signal and must land
            # BEFORE this frame's tail parts so the relay's cancellation
            # fence drops them; ResponseFinished then closes the books.
            self._trailing_fence = False
            self._flush_user_transcript(events)
            if self._generation_open:
                events.append(rt.SpeechStarted())
        elif has_model_turn or output_text:
            # A new generation opening also closes the operator's turn.
            self._flush_user_transcript(events)
            if self._trailing_fence:
                # Stragglers of the generation that just closed (3.x sends
                # them before the turn ends): dropped, never replayed as a
                # phantom new response, one warning per fenced window.
                trailing = True
                if not self._trailing_warned:
                    self._trailing_warned = True
                    logger.warning(
                        "gemini realtime: dropping trailing model output that "
                        "arrived after generationComplete"
                    )
            elif not self._generation_open:
                events.append(rt.ResponseStarted(response_id=None))
                self._generation_open = True

        if not trailing:
            for part in parts:
                if not isinstance(part, dict):
                    continue
                inline = part.get("inlineData")
                if not isinstance(inline, dict):
                    continue  # text/thought parts carry no audio for this lane
                data = inline.get("data")
                if not isinstance(data, str) or not data:
                    continue
                try:
                    pcm = base64.b64decode(data, validate=True)
                except (binascii.Error, ValueError, TypeError):
                    events.append(
                        rt.ProviderFailure(detail="Provider sent a malformed audio payload")
                    )
                    continue
                events.append(rt.OutputAudio(data=pcm))
            if output_text:
                events.append(
                    rt.Transcript(
                        role=rt.TranscriptRole.ASSISTANT,
                        text=output_text,
                        final=False,
                        provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
                    )
                )

        generation_complete = content.get("generationComplete") is True
        turn_complete = content.get("turnComplete") is True
        if interrupted:
            if self._generation_open:
                self._generation_open = False
                events.append(rt.ResponseFinished(response_id=None))
        elif turn_complete:
            # turnComplete dominates a bundled generationComplete: the turn
            # is fully done, so this turn's trailing fence ends here.
            self._trailing_fence = False
            if self._generation_open:
                self._generation_open = False
                # The done-shape transcript carries no text of its own; the
                # relay folds its accumulated deltas into the final turn.
                # usageMetadata riding turnComplete is tolerated and unused.
                events.append(
                    rt.Transcript(
                        role=rt.TranscriptRole.ASSISTANT,
                        text="",
                        final=True,
                        provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
                    )
                )
                self._flush_user_transcript(events)  # safety net for odd orderings
                events.append(rt.ResponseFinished(response_id=None))
        elif generation_complete:
            # Arm the straggler fence BEFORE the turn's turnComplete arrives.
            self._trailing_fence = True
            self._trailing_warned = False
            if self._generation_open:
                self._generation_open = False
                events.append(
                    rt.Transcript(
                        role=rt.TranscriptRole.ASSISTANT,
                        text="",
                        final=True,
                        provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
                    )
                )
                self._flush_user_transcript(events)
                events.append(rt.ResponseFinished(response_id=None))
        return events

    def _note_resumption_update(self, update: dict[str, Any]) -> None:
        """Track the latest resumption handle, honoring the server's veto.

        Only a ``resumable: true`` update CONFIRMS a handle. One carrying
        ``resumable: false`` invalidates whatever was cached — reusing an
        invalidated handle is silent data loss — and an update omitting
        ``resumable`` offers no opinion, so the last confirmed handle stays.
        """

        resumable = update.get("resumable")
        if resumable is False:
            self.resumption_handle = None
            return
        if resumable is not True:
            return
        handle = update.get("newHandle")
        if isinstance(handle, str) and handle:
            self.resumption_handle = handle

    def _note_cancelled_call_ids(self, ids: list) -> tuple[str, ...]:
        """Remember server-cancelled tool calls so their results never send.

        Ids for calls this session never saw are recorded all the same — a
        late result for one must still be dropped. Bounded by
        MAX_CANCELLED_CALL_IDS, oldest forgotten first. Returns the ids that
        were actually recorded, so the caller can report the retraction as an
        event; malformed entries are filtered out rather than reported.
        """

        recorded: list[str] = []
        for call_id in ids:
            if not isinstance(call_id, str) or not call_id:
                continue
            self._call_names.pop(call_id, None)
            self._cancelled_call_ids[call_id] = False
            recorded.append(call_id)
        while len(self._cancelled_call_ids) > MAX_CANCELLED_CALL_IDS:
            self._cancelled_call_ids.pop(next(iter(self._cancelled_call_ids)))
        return tuple(recorded)

    def _decode(self, message: dict[str, Any]) -> list[rt.RealtimeEvent]:
        """Map one Live server message to zero or more neutral events.

        Unknown shapes (usage-only frames, future fields) decode to nothing
        and the transport keeps going; a malformed contract identifier is a
        terminal protocol failure, same rule as the sibling adapters.
        """

        try:
            events: list[rt.RealtimeEvent] = []
            if "setupComplete" in message:
                events.append(rt.SessionReady(session_id=self._session_id))
            resumption = message.get("sessionResumptionUpdate")
            if isinstance(resumption, dict):
                self._note_resumption_update(resumption)
            cancellation = message.get("toolCallCancellation")
            if isinstance(cancellation, dict):
                ids = cancellation.get("ids")
                if isinstance(ids, list):
                    # Report the retraction as well as recording it: policy
                    # that already dispatched these calls needs to hear it,
                    # not just find out later that its results went nowhere.
                    cancelled = self._note_cancelled_call_ids(ids)
                    if cancelled:
                        events.append(rt.ToolCallsCancelled(call_ids=cancelled))
            tool_call = message.get("toolCall")
            if isinstance(tool_call, dict):
                # A tool call continues the turn past generationComplete.
                self._trailing_fence = False
                events.extend(self._decode_tool_call(tool_call))
            server_content = message.get("serverContent")
            if isinstance(server_content, dict):
                events.extend(self._decode_server_content(server_content))
            go_away = message.get("goAway")
            if isinstance(go_away, dict):
                # The socket dies ABORTED right after this; surface it as a
                # terminal failure so the relay closes cleanly. timeLeft is
                # server-supplied text, so the key scrubber applies to it too.
                time_left = go_away.get("timeLeft")
                detail = "Provider announced imminent server-side session termination"
                if isinstance(time_left, str) and time_left:
                    detail += f" (goAway, time left {_scrub(time_left)})"
                else:
                    detail += " (goAway)"
                events.append(rt.ProviderFailure(detail=detail, terminal=True))
            if "error" in message:
                events.append(
                    rt.ProviderFailure(
                        detail=_error_detail(message) or "Provider reported a session error"
                    )
                )
            return events
        except ValueError as exc:
            return [
                rt.ProviderFailure(
                    detail=f"Provider sent a malformed identifier: {exc}",
                    terminal=True,
                )
            ]

    async def __anext__(self) -> rt.RealtimeEvent:
        if self._terminal_emitted:
            raise StopAsyncIteration
        while True:
            if self._pending:
                event = self._pending.popleft()
                if isinstance(event, rt.ProviderFailure) and event.terminal:
                    self.state = rt.SessionState.FAILED
                return event
            try:
                wire_event = await self._wire.__anext__()
            except GeminiWireEOF as exc:
                self._terminal_emitted = True
                self.state = (
                    rt.SessionState.FAILED
                    if self.state is rt.SessionState.FAILED or exc.detail
                    else rt.SessionState.CLOSED
                )
                return rt.SessionTerminated(state=self.state, detail=exc.detail)
            except GeminiWireFrameError as exc:
                # One malformed frame (text or binary) never kills the call;
                # the failure is reported and the stream continues.
                return rt.ProviderFailure(detail=str(exc))
            except GeminiWireError as exc:
                self.state = rt.SessionState.FAILED
                return rt.ProviderFailure(detail=str(exc), terminal=True)
            self._pending.extend(self._decode(wire_event))

    async def close(self) -> None:
        await self._wire.close()
        if self.state is not rt.SessionState.FAILED:
            self.state = rt.SessionState.CLOSED


__all__ = [
    "CONNECT_TIMEOUT_S",
    "GEMINI_LIVE_WS_URL",
    "INPUT_AUDIO_MIME_TYPE",
    "MAX_CANCELLED_CALL_IDS",
    "GeminiRealtimeSession",
    "GeminiWireEOF",
    "GeminiWireError",
    "GeminiWireFrameError",
    "Pcm24To16Resampler",
    "_GeminiWireSession",
    "build_setup_message",
]
