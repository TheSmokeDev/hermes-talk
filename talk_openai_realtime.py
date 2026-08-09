"""OpenAI implementation of the provider-neutral Realtime session contract."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from typing import Any

try:
    from . import talk_realtime as rt
    from . import talk_wire
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_realtime as rt
    import talk_wire

CONNECT_TIMEOUT_S = 30.0
NORMAL_WEBSOCKET_CLOSE_CODES = (None, 1000, 1001)


def _import_aiohttp():
    try:
        import aiohttp
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise rt.RealtimeSessionError(
            "aiohttp is required for the voice session — run: pip install hermes-talk"
        ) from exc
    return aiohttp


def _tool_wire(tool: rt.ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": dict(tool.parameters),
    }


def build_session_update(setup: rt.SessionSetup) -> dict[str, Any]:
    """Map neutral setup to the OpenAI GA session update."""

    session = talk_wire.build_session_payload(
        model=setup.model,
        voice=setup.voice,
        instructions=setup.instructions,
        tools=[_tool_wire(tool) for tool in setup.tools] or None,
    )
    session["audio"]["input"]["turn_detection"]["create_response"] = (
        setup.automatic_response
    )
    return {
        "type": "session.update",
        "session": {key: value for key, value in session.items() if key != "model"},
    }


def encode_command(command: rt.RealtimeCommand) -> dict[str, Any]:
    """Map one provider-neutral command to OpenAI wire JSON."""

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


def decode_event(event: dict[str, Any]) -> rt.RealtimeEvent | None:
    """Map one OpenAI server event to a neutral event."""

    event_type = str(event.get("type") or "")
    try:
        if event_type in {"session.created", "session.updated"}:
            session = _mapping(event.get("session"))
            return rt.SessionReady(session_id=session.get("id"))
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
            response_metadata = {
                str(key): str(value) for key, value in metadata.items()
            }
            try:
                return rt.ResponseStarted(
                    response_id=response.get("id"),
                    metadata=response_metadata,
                )
            except ValueError as exc:
                # Preserve opaque echoed metadata so Hermes can consume any
                # one-time authority token before terminating the bad session.
                return _protocol_failure(
                    exc, response_metadata=response_metadata
                )
        if event_type == "response.output_audio.delta":
            delta = event.get("delta")
            if not isinstance(delta, str) or not delta:
                return rt.ProviderFailure(detail="Provider sent an empty audio payload")
            try:
                pcm = base64.b64decode(delta, validate=True)
            except (binascii.Error, ValueError, TypeError):
                return rt.ProviderFailure(detail="Provider sent a malformed audio payload")
            return rt.OutputAudio(data=pcm, item_id=event.get("item_id"))
        if event_type == "response.output_audio_transcript.delta":
            delta = event.get("delta")
            if not isinstance(delta, str) or not delta:
                return None
            return rt.Transcript(
                role=rt.TranscriptRole.ASSISTANT,
                text=delta,
                final=False,
                provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
            )
        if event_type == "response.output_audio_transcript.done":
            completed = event.get("transcript")
            return rt.Transcript(
                role=rt.TranscriptRole.ASSISTANT,
                text=completed if isinstance(completed, str) else "",
                final=True,
                provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
            )
        if event_type == "conversation.item.input_audio_transcription.completed":
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
            )
        if event_type == "response.done":
            response = _mapping(event.get("response"))
            return rt.ResponseFinished(response_id=response.get("id"))
        if event_type == "error":
            error = event.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("type") or "")
            else:
                detail = str(error or "")
            return rt.ProviderFailure(detail=detail or "Provider reported a session error")
    except ValueError as exc:
        return _protocol_failure(exc)
    return None


class OpenAIRealtimeSession:
    """OpenAI WebSocket adapter implementing :class:`rt.RealtimeSession`."""

    def __init__(
        self,
        *,
        auth_token: str,
        auth_source: str,
        aiohttp_module=None,
        mint_session: Callable[[rt.SessionSetup], Any] | None = None,
    ) -> None:
        self.auth_token = auth_token
        self.auth_source = auth_source
        self.state = rt.SessionState.NEW
        self._aiohttp = aiohttp_module
        self._mint_session = mint_session or self._mint
        self._stack: AsyncExitStack | None = None
        self._ws = None
        self._iterator = None
        self._send_lock = asyncio.Lock()
        self._terminal_emitted = False

    def _mint(self, setup: rt.SessionSetup):
        return talk_wire.mint_ephemeral_session(
            auth_token=self.auth_token,
            model=setup.model,
            voice=setup.voice,
            instructions=setup.instructions,
            tools=[_tool_wire(tool) for tool in setup.tools] or None,
        )

    async def connect(self, setup: rt.SessionSetup) -> None:
        if self.state is not rt.SessionState.NEW:
            raise rt.RealtimeSessionError("Realtime session connect may only run once")
        self.state = rt.SessionState.CONNECTING
        stack = AsyncExitStack()
        try:
            descriptor = self._mint_session(setup)
            aiohttp = self._aiohttp or _import_aiohttp()
            http = await stack.enter_async_context(aiohttp.ClientSession())
            ws = await stack.enter_async_context(
                http.ws_connect(
                    f"{talk_wire.OPENAI_REALTIME_WS_URL}?model={setup.model}",
                    headers={"Authorization": f"Bearer {descriptor.client_secret}"},
                    timeout=CONNECT_TIMEOUT_S,
                    heartbeat=20.0,
                )
            )
            self._stack = stack
            self._ws = ws
            self._iterator = ws.__aiter__()
            self.state = rt.SessionState.CONNECTED
            await self._send_wire((build_session_update(setup),))
        except asyncio.CancelledError:
            self.state = rt.SessionState.CLOSED
            await stack.aclose()
            raise
        except Exception as exc:
            self.state = rt.SessionState.FAILED
            await stack.aclose()
            if isinstance(exc, rt.RealtimeSessionError):
                raise
            raise rt.RealtimeSessionError(str(exc)) from exc

    async def _send_wire(self, messages: Sequence[dict[str, Any]]) -> None:
        if self._ws is None:
            raise rt.RealtimeSessionError("Realtime session is not connected")
        try:
            async with self._send_lock:
                for message in messages:
                    await self._ws.send_json(message)
        except Exception as exc:
            self.state = rt.SessionState.FAILED
            raise rt.RealtimeSessionError(str(exc)) from exc

    async def send(self, commands: Sequence[rt.RealtimeCommand]) -> None:
        clean_eof_flush = (
            self.state is rt.SessionState.CLOSED
            and self._terminal_emitted
            and self._stack is not None
        )
        if self.state is not rt.SessionState.CONNECTED and not clean_eof_flush:
            raise rt.RealtimeSessionError("Realtime session is not connected")
        await self._send_wire(tuple(encode_command(command) for command in commands))

    def __aiter__(self):
        return self

    def _eof_failure_detail(self) -> str:
        """Describe transport failure hidden by aiohttp's iterator EOF."""

        if self._ws is None:
            return ""
        close_code = getattr(self._ws, "close_code", None)
        socket_exception = None
        exception = getattr(self._ws, "exception", None)
        if callable(exception):
            try:
                socket_exception = exception()
            except Exception as exc:  # noqa: BLE001 — a broken status probe is failure
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

    async def __anext__(self) -> rt.RealtimeEvent:
        if self._iterator is None:
            raise StopAsyncIteration
        while True:
            try:
                message = await self._iterator.__anext__()
            except StopAsyncIteration:
                if self._terminal_emitted:
                    raise
                self._terminal_emitted = True
                failure_detail = self._eof_failure_detail()
                terminal_state = (
                    rt.SessionState.FAILED
                    if self.state is rt.SessionState.FAILED or failure_detail
                    else rt.SessionState.CLOSED
                )
                self.state = terminal_state
                # A clean EOF may still race an already-admitted tool result;
                # send() preserves that pre-teardown flush without reopening a
                # failed transport or changing the synchronized terminal state.
                return rt.SessionTerminated(
                    state=terminal_state,
                    detail=failure_detail,
                )
            aiohttp = self._aiohttp or _import_aiohttp()
            if message.type == getattr(aiohttp.WSMsgType, "ERROR", object()):
                self.state = rt.SessionState.FAILED
                detail = str(getattr(message, "data", "") or "").strip()
                return rt.ProviderFailure(
                    detail=detail or "Provider WebSocket receive failed",
                    terminal=True,
                )
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            try:
                wire_event = json.loads(message.data)
            except (TypeError, ValueError):
                continue
            if not isinstance(wire_event, dict):
                continue
            event = decode_event(wire_event)
            if event is not None:
                if isinstance(event, rt.ProviderFailure) and event.terminal:
                    self.state = rt.SessionState.FAILED
                return event

    async def close(self) -> None:
        stack, self._stack = self._stack, None
        if stack is not None:
            await stack.aclose()
        if self.state is not rt.SessionState.FAILED:
            self.state = rt.SessionState.CLOSED

__all__ = [
    "CONNECT_TIMEOUT_S",
    "OpenAIRealtimeSession",
    "build_session_update",
    "decode_event",
    "encode_command",
]
