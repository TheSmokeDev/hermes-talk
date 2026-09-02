"""Grok (xAI) implementation of the provider-neutral Realtime session contract.

xAI's Grok Voice Agent API speaks the OpenAI Realtime GA vocabulary over a
plain bearer-authenticated WebSocket — no ephemeral client-secret mint exists,
so the resolved xAI key itself is the socket's Authorization header (verified
live 2026-08-28: HTTP 101 upgrade on ``?model=grok-voice-latest``).

Probe ground truth this module is written against (live API, 2026-08-28):

- ``session.created`` carries PREFIXED wire voice ids (``xai_ara``); the
  friendly config names (``ara``) gain the ``xai_`` prefix at encode time.
- An OpenAI-GA-shaped ``session.update`` (nested ``audio.input`` /
  ``audio.output``, flat function tools, ``output_modalities``) is ACCEPTED;
  the ``session.updated`` echo is a xAI-normalized receipt, never authority.
- The tool loop round-trips verbatim with OpenAI command vocabulary
  (``response.function_call_arguments.done`` -> ``conversation.item.create``
  ``function_call_output`` -> ``response.create``).
- Server events use GA names only; application-level ``{"type": "ping"}``
  events arrive and are tolerated, and ``response.status_details`` can be the
  literal string ``"unimplemented"`` — unknown fields/values must never
  choke the decoder.
- ``conversation.item.truncate`` is documented but was NOT exercised live.
  On a wire-level refusal naming the event as unsupported, barge-in degrades
  to cancel-only with one logged receipt per session — a truncation that did
  not happen is never faked.

The command/event encoding deliberately duplicates the OpenAI adapter's GA
vocabulary rather than importing across providers: the two vocabularies will
drift as each platform normalizes differently, and a shared decoder would
make every provider's edit a risk to the other.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import replace
from typing import Any

try:
    from . import talk_realtime as rt
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_realtime as rt

logger = logging.getLogger(__name__)

XAI_REALTIME_WS_URL = "wss://api.x.ai/v1/realtime"
CONNECT_TIMEOUT_S = 30.0
NORMAL_WEBSOCKET_CLOSE_CODES = (None, 1000, 1001)
#: xAI-native input transcription model (docs.x.ai speech-to-speech). Setting
#: it enables user-turn transcripts; the OpenAI lane's transcription model id
#: is provider vocabulary and must never cross onto this wire.
GROK_TRANSCRIPTION_MODEL = "grok-transcribe"
#: How the server names an event it does not implement, per the live probe
#: ("unimplemented") and the usual invalid-request vocabulary.
_TRUNCATE_REFUSAL_MARKERS = (
    "unknown event",
    "unrecognized",
    "unsupported",
    "not supported",
    "unimplemented",
)


def _import_aiohttp():
    try:
        import aiohttp
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise rt.RealtimeSessionError(
            "aiohttp is required for the voice session — run: pip install hermes-talk"
        ) from exc
    return aiohttp


def _wire_voice(voice: str) -> str:
    """The friendly config voice with xAI's wire prefix, exactly once."""

    return voice if voice.startswith("xai_") else f"xai_{voice}"


def _tool_wire(tool: rt.ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": dict(tool.parameters),
    }


def build_session_update(setup: rt.SessionSetup) -> dict[str, Any]:
    """Map neutral setup to the OpenAI-GA-shaped update xAI accepts.

    The model is NOT part of the payload — it is fixed by the socket URL's
    ``?model=`` query, same split as the OpenAI lane. ``session.type`` stays:
    the GA shape requires it and the live endpoint accepted it.
    """

    session: dict[str, Any] = {
        "type": "realtime",
        "instructions": setup.instructions,
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24_000},
                "turn_detection": {
                    "type": "server_vad",
                    "create_response": setup.automatic_response,
                    "interrupt_response": True,
                },
                "transcription": {"model": GROK_TRANSCRIPTION_MODEL},
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": 24_000},
                "voice": _wire_voice(setup.voice),
            },
        },
    }
    if setup.tools:
        session["tools"] = [_tool_wire(tool) for tool in setup.tools]
        session["tool_choice"] = "auto"
    return {"type": "session.update", "session": session}


def encode_command(command: rt.RealtimeCommand) -> dict[str, Any]:
    """Map one provider-neutral command to the shared GA wire JSON."""

    if isinstance(command, rt.AppendInputAudio):
        return {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(command.data).decode("ascii"),
        }
    if isinstance(command, rt.AddContext):
        return {
            "type": "conversation.item.create",
            "item": {
                "id": command.item_id,
                "type": "message",
                "role": command.role.value,
                "content": [{"type": "input_text", "text": command.text}],
            },
        }
    if isinstance(command, rt.RemoveContext):
        return {"type": "conversation.item.delete", "item_id": command.item_id}
    if isinstance(command, rt.StartResponse):
        response: dict[str, Any] = {}
        if command.metadata:
            response["metadata"] = dict(command.metadata)
        if command.allow_tools is False:
            response["tool_choice"] = "none"
        return {"type": "response.create", **({"response": response} if response else {})}
    if isinstance(command, rt.CancelResponse):
        return {"type": "response.cancel"}
    if isinstance(command, rt.TruncateOutput):
        return {
            "type": "conversation.item.truncate",
            "item_id": command.item_id,
            "content_index": 0,
            "audio_end_ms": command.audio_end_ms,
        }
    if isinstance(command, rt.SubmitToolResult):
        return {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": command.call_id,
                "output": command.output,
            },
        }
    raise TypeError(f"unsupported Realtime command: {type(command).__name__}")


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _protocol_failure(
    exc: ValueError, *, response_metadata: dict[str, str] | None = None
) -> rt.ProviderFailure:
    return rt.ProviderFailure(
        detail=f"Provider sent a malformed identifier: {exc}",
        terminal=True,
        response_metadata=response_metadata or {},
    )


def _error_detail(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or "")
    return str(error or "")


def _is_truncate_refusal(detail: str) -> bool:
    """True only when an error NAMES the truncate event as unimplemented.

    Both halves are required: a generic truncation failure (bad item id, bad
    offset) must surface as a normal provider failure, never silently flip
    the session into cancel-only mode.
    """

    lowered = detail.lower()
    return "truncate" in lowered and any(
        marker in lowered for marker in _TRUNCATE_REFUSAL_MARKERS
    )


def decode_event(event: dict[str, Any]) -> rt.RealtimeEvent | None:
    """Map one xAI server event to a neutral event.

    Unknown event types (``ping``, ``conversation.item.added``/``.created``,
    the ``response.output_item.*``/``content_part.*`` scaffolding, MCP/DTMF
    notifications) and unknown fields (``response.status_details``) are
    tolerated: they decode to ``None`` and the transport keeps going.
    """

    event_type = str(event.get("type") or "")
    try:
        if event_type == "session.created":
            session = _mapping(event.get("session"))
            return rt.SessionReady(session_id=session.get("id"))
        if event_type == "session.updated":
            # xAI echoes a NORMALIZED session shape as a receipt; it is never
            # authority, and may omit the id — an echo without one is ignored
            # rather than promoted to a terminal identifier failure.
            session_id = _mapping(event.get("session")).get("id")
            if session_id is None:
                return None
            return rt.SessionReady(session_id=session_id)
        if event_type == "input_audio_buffer.speech_started":
            return rt.SpeechStarted(
                input_id=event.get("item_id"),
                offset_ms=_optional_int(event.get("audio_start_ms")),
            )
        if event_type == "input_audio_buffer.speech_stopped":
            return rt.SpeechStopped(
                input_id=event.get("item_id"),
                offset_ms=_optional_int(event.get("audio_end_ms")),
            )
        if event_type == "input_audio_buffer.committed":
            return rt.InputAudioCommitted(input_id=event.get("item_id"))
        if event_type == "response.created":
            response = _mapping(event.get("response"))
            metadata = _mapping(response.get("metadata"))
            response_metadata = {str(key): str(value) for key, value in metadata.items()}
            try:
                return rt.ResponseStarted(
                    response_id=response.get("id"),
                    metadata=response_metadata,
                )
            except ValueError as exc:
                # Preserve opaque echoed metadata so Hermes can consume any
                # one-time authority token before terminating the bad session.
                return _protocol_failure(exc, response_metadata=response_metadata)
        if event_type == "response.output_audio.delta":
            delta = event.get("delta")
            if not isinstance(delta, str) or not delta:
                return rt.ProviderFailure(detail="Provider sent an empty audio payload")
            try:
                pcm = base64.b64decode(delta, validate=True)
            except (binascii.Error, ValueError, TypeError):
                return rt.ProviderFailure(detail="Provider sent a malformed audio payload")
            return rt.OutputAudio(
                data=pcm,
                item_id=event.get("item_id"),
                response_id=event.get("response_id"),
            )
        if event_type == "response.output_audio_transcript.delta":
            delta = event.get("delta")
            if not isinstance(delta, str) or not delta:
                return None
            return rt.Transcript(
                role=rt.TranscriptRole.ASSISTANT,
                text=delta,
                final=False,
                provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
                response_id=event.get("response_id"),
            )
        if event_type == "response.output_audio_transcript.done":
            completed = event.get("transcript")
            return rt.Transcript(
                role=rt.TranscriptRole.ASSISTANT,
                text=completed if isinstance(completed, str) else "",
                final=True,
                provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
                response_id=event.get("response_id"),
            )
        if event_type in {
            "conversation.item.input_audio_transcription.delta",
            "conversation.item.input_audio_transcription.updated",
        }:
            # xAI's streaming input transcription is CUMULATIVE — every event
            # repeats the full text so far, not a delta — so snapshots decode
            # as non-final partials; the session suppresses identical repeats.
            snapshot = event.get("delta")
            if not isinstance(snapshot, str):
                snapshot = event.get("transcript")
            if not isinstance(snapshot, str) or not snapshot.strip():
                return None
            return rt.Transcript(
                role=rt.TranscriptRole.USER,
                text=snapshot.strip(),
                final=False,
                provenance=rt.TranscriptProvenance.INPUT_AUDIO,
            )
        if event_type == "conversation.item.input_audio_transcription.completed":
            # On the live wire (smoke, 2026-08-28) xAI emits this event more
            # than once per input item — pre-commit copies are cumulative
            # snapshots, and a final copy can repeat after the commit. The
            # neutral "exactly one final per utterance" rule is enforced by
            # the session's _InputTranscriptDedupe, not here.
            transcript = event.get("transcript")
            if not isinstance(transcript, str) or not transcript.strip():
                return None
            return rt.Transcript(
                role=rt.TranscriptRole.USER,
                text=transcript.strip(),
                final=True,
                provenance=rt.TranscriptProvenance.INPUT_AUDIO,
            )
        if event_type == "response.function_call_arguments.done":
            return rt.FunctionCall(
                call_id=event.get("call_id"),
                name=event.get("name"),
                arguments=event.get("arguments") if isinstance(event.get("arguments"), str) else "",
                response_id=event.get("response_id"),
                item_id=event.get("item_id"),
            )
        if event_type == "response.done":
            response = _mapping(event.get("response"))
            return rt.ResponseFinished(response_id=response.get("id"))
        if event_type == "error":
            return rt.ProviderFailure(
                detail=_error_detail(event) or "Provider reported a session error"
            )
    except ValueError as exc:
        return _protocol_failure(exc)
    return None


class _InputTranscriptDedupe:
    """Per-utterance filter over xAI's cumulative input transcription stream.

    Live wire behavior (smoke, 2026-08-28): xAI repeats cumulative snapshots
    verbatim and can send ``.completed`` several times per input item, so the
    decoded stream stutters. This filter keeps the neutral contract — live
    non-final partials plus exactly one final per utterance:

    - a new input item (``speech_started``/``committed`` with a new id)
      resets the utterance, so two identical turns both print;
    - a pre-commit ``.completed`` is still a cumulative snapshot, so it is
      downgraded to a non-final partial;
    - an identical repeat of the last emitted snapshot is suppressed;
    - the first post-commit completion is the one final; later copies are
      dropped. A final is never snapshot-suppressed — the relay only prints
      finals, so suppressing one would erase the turn entirely.
    """

    def __init__(self) -> None:
        self._item_id: str | None = None
        self._committed = False
        self._snapshot: str | None = None
        self._final_emitted = False

    def _reset(self) -> None:
        self._committed = False
        self._snapshot = None
        self._final_emitted = False

    def begin_item(self, item_id: str | None) -> None:
        if isinstance(item_id, str) and item_id and item_id != self._item_id:
            self._item_id = item_id
            self._reset()

    def mark_committed(self) -> None:
        self._committed = True

    def admit(self, event: rt.Transcript) -> rt.Transcript | None:
        final = event.final and self._committed
        if final:
            if self._final_emitted:
                return None
            self._final_emitted = True
        elif event.text == self._snapshot:
            return None
        self._snapshot = event.text
        if final == event.final:
            return event
        return replace(event, final=False)


class GrokWireError(RuntimeError):
    """A terminal low-level xAI transport or wire-protocol failure."""


class GrokWireEOF(EOFError):
    """The WebSocket iterator ended; ``detail`` is blank only for a clean EOF."""

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(detail)


class _GrokWireSession:
    """The bearer-authenticated WebSocket under :class:`GrokRealtimeSession`.

    Unlike the OpenAI lane there is no ephemeral mint: the raw xAI key is the
    socket's bearer, so it is held only until the upgrade attempt resolves and
    cleared on every terminal path.
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

    async def connect(self, *, model: str, session_update: dict[str, Any]) -> None:
        if self._connect_started or self._closed:
            raise GrokWireError("Realtime wire connect may only run once")
        self._connect_started = True
        stack = AsyncExitStack()
        try:
            auth_token = self._auth_token
            if auth_token is None:
                raise GrokWireError("Realtime wire credentials are unavailable")
            aiohttp = self._aiohttp or _import_aiohttp()
            http = await stack.enter_async_context(aiohttp.ClientSession())
            try:
                ws = await stack.enter_async_context(
                    http.ws_connect(
                        f"{XAI_REALTIME_WS_URL}?model={model}",
                        headers={"Authorization": f"Bearer {auth_token}"},
                        timeout=CONNECT_TIMEOUT_S,
                        heartbeat=20.0,
                    )
                )
            finally:
                self._clear_raw_credentials()
            self._stack = stack
            self._ws = ws
            self._iterator = ws.__aiter__()
            await self.send_json((session_update,))
        except BaseException:
            self._clear_raw_credentials()
            self._stack = None
            await stack.aclose()
            raise

    async def send_json(self, messages: Sequence[dict[str, Any]]) -> None:
        if self._ws is None:
            raise GrokWireError("Realtime wire is not connected")
        try:
            async with self._send_lock:
                for message in messages:
                    await self._ws.send_json(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise GrokWireError(str(exc)) from exc

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
            reasons.append(detail)
        return f"Provider WebSocket closed abnormally ({'; '.join(reasons)})"

    async def __anext__(self) -> dict[str, Any]:
        if self._iterator is None:
            raise GrokWireEOF("")
        while True:
            try:
                message = await self._iterator.__anext__()
            except StopAsyncIteration:
                raise GrokWireEOF(self._eof_failure_detail()) from None
            aiohttp = self._aiohttp or _import_aiohttp()
            if message.type == getattr(aiohttp.WSMsgType, "ERROR", object()):
                detail = str(getattr(message, "data", "") or "").strip()
                raise GrokWireError(detail or "Provider WebSocket receive failed")
            if message.type is not aiohttp.WSMsgType.TEXT:
                passive_types = {
                    getattr(aiohttp.WSMsgType, name, object())
                    for name in ("CLOSE", "CLOSED", "CLOSING", "PING", "PONG")
                }
                if message.type in passive_types:
                    continue
                frame_name = getattr(message.type, "name", str(message.type)).lower()
                raise GrokWireError(
                    f"Provider sent unsupported WebSocket frame type: {frame_name}"
                )
            try:
                wire_event = json.loads(message.data)
            except (TypeError, ValueError) as exc:
                raise GrokWireError("Provider sent malformed JSON") from exc
            if not isinstance(wire_event, dict):
                raise GrokWireError("Provider sent a non-object event")
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


#: The bearer is checked only at the WebSocket handshake. A token that dies
#: mid-call keeps the socket; one that is already dead never gets a socket,
#: and aiohttp reports that as a handshake error carrying the HTTP status.
_HANDSHAKE_ERROR_NAMES = frozenset({"WSServerHandshakeError"})

#: What xAI's handshake status means for each auth lane (source → status → text).
_HANDSHAKE_REMEDIATION: dict[int, tuple[str, str]] = {
    401: (
        "xAI OAuth token rejected — run `hermes auth add xai-oauth`",
        "xAI API key rejected (401)",
    ),
    403: (
        "your xAI subscription tier does not include realtime API access; "
        "set `XAI_API_KEY` for Grok voice",
        "xAI refused this key for realtime (403)",
    ),
}


def handshake_remediation(exc: BaseException, *, auth_source: str | None) -> str | None:
    """A one-line operator remediation for an auth-shaped handshake failure.

    Returns ``None`` for anything that is not a 401/403 WebSocket handshake
    rejection, so every other failure keeps its original text. The check is
    by class name and ``status`` so callers and tests need no aiohttp.
    """

    if type(exc).__name__ not in _HANDSHAKE_ERROR_NAMES:
        return None
    status = getattr(exc, "status", None)
    texts = _HANDSHAKE_REMEDIATION.get(status) if isinstance(status, int) else None
    if texts is None:
        return None
    oauth_text, key_text = texts
    return oauth_text if auth_source == "xai-oauth" else key_text


class GrokRealtimeSession:
    """Talk-neutral facade over the xAI realtime wire session."""

    def __init__(self, *, auth_token: str, auth_source: str, aiohttp_module=None) -> None:
        self.auth_token = auth_token
        self.auth_source = auth_source
        self.state = rt.SessionState.NEW
        self._wire = _GrokWireSession(
            auth_token=auth_token,
            auth_source=auth_source,
            aiohttp_module=aiohttp_module,
        )
        self._terminal_emitted = False
        self._truncate_supported = True
        self._truncate_degrade_logged = False
        self._input_dedupe = _InputTranscriptDedupe()

    async def connect(self, setup: rt.SessionSetup) -> None:
        if self.state is not rt.SessionState.NEW:
            raise rt.RealtimeSessionError("Realtime session connect may only run once")
        self.state = rt.SessionState.CONNECTING
        try:
            await self._wire.connect(
                model=setup.model,
                session_update=build_session_update(setup),
            )
        except asyncio.CancelledError:
            self.state = rt.SessionState.CLOSED
            raise
        except Exception as exc:
            self.state = rt.SessionState.FAILED
            if isinstance(exc, rt.RealtimeSessionError):
                raise
            remediation = handshake_remediation(exc, auth_source=self.auth_source)
            if remediation is not None:
                raise rt.RealtimeSessionError(remediation) from exc
            raise rt.RealtimeSessionError(str(exc)) from exc
        self.state = rt.SessionState.CONNECTED

    async def send(self, commands: Sequence[rt.RealtimeCommand]) -> None:
        clean_eof_flush = self.state is rt.SessionState.CLOSED and self._terminal_emitted
        if self.state is not rt.SessionState.CONNECTED and not clean_eof_flush:
            raise rt.RealtimeSessionError("Realtime session is not connected")
        encoded = tuple(
            encode_command(rt.CancelResponse())
            if isinstance(command, rt.TruncateOutput) and not self._truncate_supported
            else encode_command(command)
            for command in commands
        )
        try:
            await self._wire.send_json(encoded)
        except Exception as exc:
            self.state = rt.SessionState.FAILED
            raise rt.RealtimeSessionError(str(exc)) from exc

    def __aiter__(self):
        return self

    def _observe_truncate_refusal(self, wire_event: dict[str, Any]) -> bool:
        """Consume a truncate-unsupported error as a degrade receipt."""

        if str(wire_event.get("type") or "") != "error":
            return False
        detail = _error_detail(wire_event)
        if not _is_truncate_refusal(detail):
            return False
        self._truncate_supported = False
        if not self._truncate_degrade_logged:
            self._truncate_degrade_logged = True
            logger.warning(
                "grok realtime: server refused conversation.item.truncate (%s); "
                "barge-in degrades to cancel-only for the rest of this session",
                detail,
            )
        return True

    async def __anext__(self) -> rt.RealtimeEvent:
        if self._terminal_emitted:
            raise StopAsyncIteration
        while True:
            try:
                wire_event = await self._wire.__anext__()
            except GrokWireEOF as exc:
                self._terminal_emitted = True
                self.state = (
                    rt.SessionState.FAILED
                    if self.state is rt.SessionState.FAILED or exc.detail
                    else rt.SessionState.CLOSED
                )
                return rt.SessionTerminated(state=self.state, detail=exc.detail)
            except GrokWireError as exc:
                self.state = rt.SessionState.FAILED
                return rt.ProviderFailure(detail=str(exc), terminal=True)
            if self._observe_truncate_refusal(wire_event):
                continue
            event = decode_event(wire_event)
            if event is None:
                continue
            if isinstance(event, rt.SpeechStarted):
                self._input_dedupe.begin_item(event.input_id)
            elif isinstance(event, rt.InputAudioCommitted):
                self._input_dedupe.begin_item(event.input_id)
                self._input_dedupe.mark_committed()
            elif (
                isinstance(event, rt.Transcript)
                and event.provenance is rt.TranscriptProvenance.INPUT_AUDIO
            ):
                event = self._input_dedupe.admit(event)
                if event is None:
                    continue
            if isinstance(event, rt.ProviderFailure) and event.terminal:
                self.state = rt.SessionState.FAILED
            return event

    async def close(self) -> None:
        await self._wire.close()
        if self.state is not rt.SessionState.FAILED:
            self.state = rt.SessionState.CLOSED


__all__ = [
    "CONNECT_TIMEOUT_S",
    "GROK_TRANSCRIPTION_MODEL",
    "XAI_REALTIME_WS_URL",
    "GrokRealtimeSession",
    "GrokWireEOF",
    "GrokWireError",
    "_GrokWireSession",
    "build_session_update",
    "decode_event",
    "encode_command",
    "handshake_remediation",
]
