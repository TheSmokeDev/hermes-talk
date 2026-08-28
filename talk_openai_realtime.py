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
        automatic_response=setup.automatic_response,
        text_output=setup.text_output,
    )
    return {
        "type": "session.update",
        "session": {key: value for key, value in session.items() if key != "model"},
    }


CORE_RENDER_VERBATIM_INSTRUCTION = (
    "Render the sole user input as speech verbatim. Speak every character's "
    "words and punctuation naturally, but add, remove, or paraphrase nothing. "
    "Do not preface or explain."
)


def build_core_response_create(
    *, canonical_text: str, correlation: str, voice: str, event_id: str
) -> dict[str, Any]:
    """Build the exact explicit canonical response for the Hermes core lane."""

    return {
        "type": "response.create",
        "event_id": event_id,
        "response": {
            "conversation": "none",
            "metadata": {"correlation": correlation},
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": canonical_text}],
                }
            ],
            "instructions": CORE_RENDER_VERBATIM_INSTRUCTION,
            "output_modalities": ["audio"],
            "audio": {
                "output": {
                    "voice": voice,
                    "format": {"type": "audio/pcm", "rate": 24_000},
                }
            },
            "tools": [],
            "tool_choice": "none",
        },
    }


def build_core_response_cancel(*, response_id: str, event_id: str) -> dict[str, Any]:
    """Build one exact response-local cancellation for the Hermes core lane."""

    return {
        "type": "response.cancel",
        "event_id": event_id,
        "response_id": response_id,
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
        if event_type == "response.output_text.delta":
            # Text-output mode (the cascade voice lane): the model emits its
            # answer as text instead of synthesizing audio. The delta rides
            # the SAME assistant-transcript event as the audio transcript —
            # the relay's captions and the cascade's chunker both consume it,
            # and neither cares which provider channel carried the words.
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
        if event_type == "response.output_text.done":
            completed = event.get("text")
            return rt.Transcript(
                role=rt.TranscriptRole.ASSISTANT,
                text=completed if isinstance(completed, str) else "",
                final=True,
                provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
                response_id=event.get("response_id"),
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
                item_id=event.get("item_id"),
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


class OpenAIWireError(RuntimeError):
    """A terminal low-level OpenAI transport or wire-protocol failure."""


class OpenAIWireEOF(EOFError):
    """The WebSocket iterator ended; ``detail`` is blank only for a clean EOF."""

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(detail)


class _OpenAIWireSession:
    """Shared HTTP/WebSocket implementation used by legacy and core facades."""

    def __init__(
        self,
        *,
        auth_token: str,
        auth_source: str,
        aiohttp_module=None,
        mint_session: Callable[..., Any] | None = None,
    ) -> None:
        self._auth_token: str | None = auth_token
        self._auth_source: str | None = auth_source
        self._aiohttp = aiohttp_module
        self._mint_session = mint_session or self._mint
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

    def _mint(self, **configuration):
        auth_token = self._auth_token
        if auth_token is None:
            raise OpenAIWireError("Realtime wire credentials are unavailable")
        return talk_wire.mint_ephemeral_session(
            auth_token=auth_token,
            model=configuration["model"],
            voice=configuration["voice"],
            instructions=configuration["instructions"],
            tools=configuration.get("tools"),
            automatic_response=configuration["automatic_response"],
            text_output=configuration.get("text_output", False),
        )

    async def connect(
        self,
        *,
        model: str,
        voice: str,
        instructions: str,
        tools: list[dict] | None,
        automatic_response: bool,
        session_update: dict[str, Any],
        text_output: bool = False,
    ) -> None:
        if self._connect_started or self._closed:
            raise OpenAIWireError("Realtime wire connect may only run once")
        self._connect_started = True
        stack = AsyncExitStack()
        try:
            try:
                descriptor = self._mint_session(
                    model=model,
                    voice=voice,
                    instructions=instructions,
                    tools=tools,
                    automatic_response=automatic_response,
                    text_output=text_output,
                )
            finally:
                self._clear_raw_credentials()
            aiohttp = self._aiohttp or _import_aiohttp()
            http = await stack.enter_async_context(aiohttp.ClientSession())
            ws = await stack.enter_async_context(
                http.ws_connect(
                    f"{talk_wire.OPENAI_REALTIME_WS_URL}?model={model}",
                    headers={"Authorization": f"Bearer {descriptor.client_secret}"},
                    timeout=CONNECT_TIMEOUT_S,
                    heartbeat=20.0,
                )
            )
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
            raise OpenAIWireError("Realtime wire is not connected")
        try:
            async with self._send_lock:
                for message in messages:
                    await self._ws.send_json(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise OpenAIWireError(str(exc)) from exc

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
            raise OpenAIWireEOF("")
        while True:
            try:
                message = await self._iterator.__anext__()
            except StopAsyncIteration:
                raise OpenAIWireEOF(self._eof_failure_detail()) from None
            aiohttp = self._aiohttp or _import_aiohttp()
            if message.type == getattr(aiohttp.WSMsgType, "ERROR", object()):
                detail = str(getattr(message, "data", "") or "").strip()
                raise OpenAIWireError(detail or "Provider WebSocket receive failed")
            if message.type is not aiohttp.WSMsgType.TEXT:
                passive_types = {
                    getattr(aiohttp.WSMsgType, name, object())
                    for name in ("CLOSE", "CLOSED", "CLOSING", "PING", "PONG")
                }
                if message.type in passive_types:
                    continue
                frame_name = getattr(message.type, "name", str(message.type)).lower()
                raise OpenAIWireError(
                    f"Provider sent unsupported WebSocket frame type: {frame_name}"
                )
            try:
                wire_event = json.loads(message.data)
            except (TypeError, ValueError) as exc:
                raise OpenAIWireError("Provider sent malformed JSON") from exc
            if not isinstance(wire_event, dict):
                raise OpenAIWireError("Provider sent a non-object event")
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


class OpenAIRealtimeSession:
    """Legacy Talk-neutral facade over the shared OpenAI wire session."""

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
        self._legacy_mint = mint_session
        self._wire = _OpenAIWireSession(
            auth_token=auth_token,
            auth_source=auth_source,
            aiohttp_module=aiohttp_module,
            mint_session=None,
        )
        self._terminal_emitted = False

    async def connect(self, setup: rt.SessionSetup) -> None:
        if self.state is not rt.SessionState.NEW:
            raise rt.RealtimeSessionError("Realtime session connect may only run once")
        self.state = rt.SessionState.CONNECTING
        if self._legacy_mint is not None:
            self._wire._mint_session = lambda **_configuration: self._legacy_mint(setup)
        tools = [_tool_wire(tool) for tool in setup.tools] or None
        try:
            await self._wire.connect(
                model=setup.model,
                voice=setup.voice,
                instructions=setup.instructions,
                tools=tools,
                automatic_response=setup.automatic_response,
                session_update=build_session_update(setup),
                text_output=setup.text_output,
            )
        except asyncio.CancelledError:
            self.state = rt.SessionState.CLOSED
            raise
        except Exception as exc:
            self.state = rt.SessionState.FAILED
            if isinstance(exc, rt.RealtimeSessionError):
                raise
            raise rt.RealtimeSessionError(str(exc)) from exc
        self.state = rt.SessionState.CONNECTED

    async def send(self, commands: Sequence[rt.RealtimeCommand]) -> None:
        clean_eof_flush = self.state is rt.SessionState.CLOSED and self._terminal_emitted
        if self.state is not rt.SessionState.CONNECTED and not clean_eof_flush:
            raise rt.RealtimeSessionError("Realtime session is not connected")
        try:
            await self._wire.send_json(tuple(encode_command(command) for command in commands))
        except Exception as exc:
            self.state = rt.SessionState.FAILED
            raise rt.RealtimeSessionError(str(exc)) from exc

    def __aiter__(self):
        return self

    async def __anext__(self) -> rt.RealtimeEvent:
        if self._terminal_emitted:
            raise StopAsyncIteration
        while True:
            try:
                wire_event = await self._wire.__anext__()
            except OpenAIWireEOF as exc:
                self._terminal_emitted = True
                self.state = (
                    rt.SessionState.FAILED
                    if self.state is rt.SessionState.FAILED or exc.detail
                    else rt.SessionState.CLOSED
                )
                return rt.SessionTerminated(state=self.state, detail=exc.detail)
            except OpenAIWireError as exc:
                self.state = rt.SessionState.FAILED
                return rt.ProviderFailure(detail=str(exc), terminal=True)
            event = decode_event(wire_event)
            if event is not None:
                if isinstance(event, rt.ProviderFailure) and event.terminal:
                    self.state = rt.SessionState.FAILED
                return event

    async def close(self) -> None:
        await self._wire.close()
        if self.state is not rt.SessionState.FAILED:
            self.state = rt.SessionState.CLOSED


__all__ = [
    "CONNECT_TIMEOUT_S",
    "OpenAIRealtimeSession",
    "OpenAIWireEOF",
    "OpenAIWireError",
    "_OpenAIWireSession",
    "build_session_update",
    "decode_event",
    "encode_command",
]
