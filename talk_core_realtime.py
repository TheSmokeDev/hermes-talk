"""Defensive Hermes core API-v2 input-only OpenAI Realtime adapter.

The optional core boundary is deliberately contained in this module.  Legacy
Talk never imports Hermes core through its OpenAI transport, and this module
exposes no partially subclassed provider when the exact API-v2 contract is
unavailable.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

try:
    from . import talk_auth, talk_config, talk_wire
    from .talk_openai_realtime import (
        OpenAIWireEOF,
        OpenAIWireError,
        _OpenAIWireSession,
    )
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_auth
    import talk_config
    import talk_wire
    from talk_openai_realtime import OpenAIWireEOF, OpenAIWireError, _OpenAIWireSession

_CORE_IMPORT_ERROR: BaseException | None = None
try:
    from agent.realtime_voice_provider import (
        REALTIME_VOICE_PROVIDER_API_VERSION,
        InputTranscript,
        RealtimeAudioFormat,
        RealtimeCapability,
        RealtimeVoiceProvider,
        RealtimeVoiceSession,
        RealtimeVoiceSetup,
        SessionClosed,
        SessionFailure,
        SessionReady,
        TranscriptProvenance,
        TranscriptRole,
    )

    if REALTIME_VOICE_PROVIDER_API_VERSION != 2:
        raise ImportError("Hermes realtime voice provider API is not version 2")
    if not all(
        isinstance(symbol, type)
        for symbol in (
            RealtimeVoiceProvider,
            RealtimeVoiceSession,
            RealtimeVoiceSetup,
            RealtimeAudioFormat,
            SessionReady,
            SessionClosed,
            SessionFailure,
            InputTranscript,
        )
    ):
        raise ImportError("Hermes realtime voice API-v2 class shape is incompatible")
    for enum_type, names in (
        (
            RealtimeCapability,
            ("INPUT_TRANSCRIPTION", "INPUT_COMMIT_EVENTS"),
        ),
        (TranscriptRole, ("OPERATOR",)),
        (TranscriptProvenance, ("OPERATOR_INPUT",)),
    ):
        if not isinstance(enum_type, type) or any(not hasattr(enum_type, name) for name in names):
            raise ImportError("Hermes realtime voice API-v2 enum shape is incompatible")
except Exception as exc:  # noqa: BLE001 - optional core must never poison legacy imports
    _CORE_IMPORT_ERROR = exc

DEFAULT_INPUT_LEDGER_CAPACITY = 1024
MAX_IDENTIFIER_LENGTH = 512
MAX_TRANSCRIPT_LENGTH = 1_000_000
PROVIDER_NAME = "talk_openai_realtime"


def core_provider_available() -> bool:
    """Return only whether the exact optional Hermes core API-v2 is importable."""

    return _CORE_IMPORT_ERROR is None


def core_provider_diagnostic() -> dict[str, bool]:
    """Return passive contract/provider readiness without exposing credentials."""

    contract_available = core_provider_available()
    provider_available = False
    if contract_available and TalkOpenAIRealtimeProvider is not None:
        try:
            provider_available = bool(TalkOpenAIRealtimeProvider().is_available())
        except Exception:  # noqa: BLE001 - diagnostics must stay passive and bounded
            provider_available = False
    return {
        "contract_available": contract_available,
        "provider_available": provider_available,
    }


if _CORE_IMPORT_ERROR is None:
    SUPPORTED_AUDIO_FORMAT = RealtimeAudioFormat(
        mime_type="audio/pcm", sample_rate_hz=24_000, channels=1
    )
    CORE_CAPABILITIES = frozenset(
        {
            RealtimeCapability.INPUT_TRANSCRIPTION,
            RealtimeCapability.INPUT_COMMIT_EVENTS,
        }
    )

    def _validate_identifier(value: Any, field_name: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > MAX_IDENTIFIER_LENGTH
        ):
            raise ValueError(
                f"{field_name} must be a nonblank, trimmed identifier no longer "
                f"than {MAX_IDENTIFIER_LENGTH} characters"
            )
        return value

    def _validate_text(value: Any, *, final: bool) -> str:
        if not isinstance(value, str):
            raise TypeError("transcript text must be a string")
        if len(value) > MAX_TRANSCRIPT_LENGTH:
            raise ValueError("transcript text exceeds the supported size")
        if final and not value.strip():
            raise ValueError("final transcript text must be nonblank")
        if not final and not value:
            raise ValueError("partial transcript text must be nonempty")
        return value

    def _requested_capabilities(value: Any) -> frozenset[RealtimeCapability]:
        if isinstance(value, (str, bytes)):
            values = (value,)
        else:
            try:
                values = tuple(value)
            except TypeError as exc:
                raise TypeError("provider capability request must be iterable") from exc
        requested: set[RealtimeCapability] = set()
        for item in values:
            try:
                requested.add(
                    item if isinstance(item, RealtimeCapability) else RealtimeCapability(item)
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unsupported realtime capability request: {item!r}") from exc
        unsupported = requested - CORE_CAPABILITIES
        if unsupported:
            name = sorted(capability.value for capability in unsupported)[0]
            raise ValueError(f"unsupported realtime capability request: {name}")
        return frozenset(requested)

    def _validate_setup(setup: RealtimeVoiceSetup) -> None:
        if not isinstance(setup, RealtimeVoiceSetup):
            raise TypeError("setup must be a RealtimeVoiceSetup")
        if setup.tools:
            raise ValueError("the input-only core provider does not accept tools")
        if setup.audio is not None and setup.audio != SUPPORTED_AUDIO_FORMAT:
            raise ValueError("the input-only core provider requires audio/pcm at 24000 Hz mono")
        options = setup.provider_options
        unknown = set(options) - {"automatic_response", "capabilities"}
        if unknown:
            raise ValueError(f"unsupported provider option: {sorted(unknown)[0]}")
        automatic_response = options.get("automatic_response", False)
        if not isinstance(automatic_response, bool):
            raise TypeError("automatic_response must be boolean")
        if automatic_response:
            raise ValueError("automatic_response must remain false in the core lane")
        if "capabilities" in options:
            _requested_capabilities(options["capabilities"])

    def _session_update(*, model: str, voice: str, instructions: str) -> dict[str, Any]:
        session = talk_wire.build_session_payload(
            model=model,
            voice=voice,
            instructions=instructions,
            tools=None,
            automatic_response=False,
        )
        session.pop("model", None)
        return {"type": "session.update", "session": session}

    class _TalkCoreRealtimeSession(RealtimeVoiceSession):
        """One input-only core session over the shared low-level wire."""

        def __init__(
            self,
            *,
            wire,
            audio_format: RealtimeAudioFormat,
            ledger_capacity: int,
        ) -> None:
            super().__init__(CORE_CAPABILITIES)
            self._wire = wire
            self._audio_format = audio_format
            self._ledger_capacity = ledger_capacity
            self._input_ledger: dict[str, str | None] = {}
            self._provider_session_id: str | None = None
            self._terminal = False
            self._pending_send_failure: Exception | None = None

        def _reserve_input(self, item_id: Any) -> str:
            item_id = _validate_identifier(item_id, "item_id")
            if item_id not in self._input_ledger:
                if len(self._input_ledger) >= self._ledger_capacity:
                    raise ValueError("input ledger capacity exhausted")
                self._input_ledger[item_id] = None
            return item_id

        async def send_audio(self, audio: bytes, *, mime_type: str | None = None) -> None:
            if mime_type is not None and mime_type != self._audio_format.mime_type:
                raise ValueError("audio MIME type does not match the opened session")
            if not isinstance(audio, (bytes, bytearray, memoryview)):
                raise TypeError("audio must be bytes-like")
            if self._terminal or self._closed:
                raise RuntimeError("realtime voice session is closed")
            await self._send_wire(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(bytes(audio)).decode("ascii"),
                }
            )

        async def _commit_audio(self) -> None:
            if self._terminal or self._closed:
                raise RuntimeError("realtime voice session is closed")
            await self._send_wire({"type": "input_audio_buffer.commit"})

        async def _send_wire(self, message: Mapping[str, Any]) -> None:
            try:
                await self._wire.send_json((message,))
            except Exception as exc:
                if self._pending_send_failure is None:
                    self._pending_send_failure = exc
                try:
                    await self._wire.close()
                except Exception as close_exc:  # noqa: BLE001 - retain cleanup class
                    if self._pending_send_failure is exc:
                        self._pending_send_failure = RuntimeError(
                            f"{type(exc).__name__}: {exc}; cleanup failed: "
                            f"{type(close_exc).__name__}"
                        )
                raise

        async def _submit_tool_results(self, batch_id, results) -> None:
            del batch_id, results
            raise RuntimeError("the input-only core provider does not accept tool results")

        def _input_transcript(self, event: Mapping[str, Any], *, final: bool) -> InputTranscript:
            explicit_finality = event.get("final")
            if "final" in event and not isinstance(explicit_finality, bool):
                raise TypeError("transcript finality must be boolean")
            if "final" in event and explicit_finality is not final:
                raise ValueError("transcript finality contradicts its event type")
            item_id = self._reserve_input(event.get("item_id"))
            field = "transcript" if final else "delta"
            text = _validate_text(event.get(field), final=final)
            terminal_text = self._input_ledger[item_id]
            if terminal_text is not None and (not final or terminal_text != text):
                raise ValueError("conflicting terminal transcript for input item")
            if final:
                self._input_ledger[item_id] = text
            return InputTranscript(
                item_id=item_id,
                turn_id=item_id,
                text=text,
                final=final,
                role=TranscriptRole.OPERATOR,
                provenance=TranscriptProvenance.OPERATOR_INPUT,
            )

        def _map_event(self, event: Mapping[str, Any]):
            event_type = event.get("type")
            if not isinstance(event_type, str) or not event_type:
                raise ValueError("provider event type must be a nonblank string")
            if event_type == "session.created":
                session = event.get("session")
                session_id = _validate_identifier(
                    session.get("id") if isinstance(session, Mapping) else None,
                    "session_id",
                )
                if self._provider_session_id is None:
                    self._provider_session_id = session_id
                    return SessionReady(session_id=session_id)
                if self._provider_session_id != session_id:
                    raise ValueError("provider session identity changed")
                return None
            if event_type == "session.updated":
                return None
            if event_type == "input_audio_buffer.committed":
                self._reserve_input(event.get("item_id"))
                return None
            if event_type == "conversation.item.input_audio_transcription.delta":
                return self._input_transcript(event, final=False)
            if event_type == "conversation.item.input_audio_transcription.completed":
                return self._input_transcript(event, final=True)
            if event_type == "error":
                error = event.get("error")
                detail = error.get("message") if isinstance(error, Mapping) else error
                raise OpenAIWireError(str(detail).strip() or "Provider reported a session error")
            item = event.get("item")
            unsolicited_item = (
                event_type in {"conversation.item.created", "conversation.item.done"}
                and isinstance(item, Mapping)
                and (
                    item.get("type") in {"function_call", "function_call_output"}
                    or item.get("role") == "assistant"
                )
            )
            if (
                event_type.startswith("response.")
                or "output_audio" in event_type
                or "function_call" in event_type
                or unsolicited_item
            ):
                raise ValueError(
                    f"unsolicited output/tool event in input-only session: {event_type}"
                )
            # Speech/VAD and rate-limit diagnostics carry no API-v2 input event.
            return None

        async def _terminate(self) -> None:
            self._terminal = True
            await self._wire.close()
            self._input_ledger.clear()

        def _take_send_failure(self) -> Exception | None:
            failure, self._pending_send_failure = self._pending_send_failure, None
            return failure

        async def _events(self) -> AsyncIterator[Any]:
            if self._terminal:
                return
            failure = self._take_send_failure()
            if failure is not None:
                await self._terminate()
                yield SessionFailure(
                    code="provider_send_failure",
                    message=f"{type(failure).__name__}: {failure}",
                )
                return
            try:
                while True:
                    wire_event = await self._wire.__anext__()
                    mapped = self._map_event(wire_event)
                    if mapped is not None:
                        yield mapped
            except OpenAIWireEOF as exc:
                failure = self._take_send_failure()
                await self._terminate()
                if failure is not None:
                    yield SessionFailure(
                        code="provider_send_failure",
                        message=f"{type(failure).__name__}: {failure}",
                    )
                elif exc.detail:
                    yield SessionFailure(code="provider_eof", message=exc.detail)
                else:
                    yield SessionClosed()
            except asyncio.CancelledError:
                await self._terminate()
                raise
            except Exception as exc:  # noqa: BLE001 - protocol errors converge here
                failure = self._take_send_failure()
                root = failure or exc
                try:
                    await self._terminate()
                except Exception as close_exc:  # noqa: BLE001 - report cleanup class only
                    message = (
                        f"{type(root).__name__}: {root}; cleanup failed: {type(close_exc).__name__}"
                    )
                else:
                    message = f"{type(root).__name__}: {root}"
                failure_code = (
                    "provider_send_failure"
                    if failure is not None
                    else "provider_protocol_failure"
                )
                yield SessionFailure(
                    code=failure_code,
                    message=message,
                )

        async def _close(self) -> None:
            await self._terminate()

    class TalkOpenAIRealtimeProvider(RealtimeVoiceProvider):
        """Hermes core API-v2 input-only OpenAI Realtime provider."""

        api_version = 2
        capabilities = CORE_CAPABILITIES

        def __init__(
            self,
            *,
            auth_resolver: Callable[[], Any] = talk_auth.resolve_auth,
            wire_factory: Callable[..., Any] = _OpenAIWireSession,
            ledger_capacity: int = DEFAULT_INPUT_LEDGER_CAPACITY,
        ) -> None:
            if isinstance(ledger_capacity, bool) or not isinstance(ledger_capacity, int):
                raise TypeError("ledger_capacity must be a positive integer")
            if ledger_capacity <= 0:
                raise ValueError("ledger_capacity must be a positive integer")
            self._auth_resolver = auth_resolver
            self._wire_factory = wire_factory
            self._ledger_capacity = ledger_capacity

        @property
        def name(self) -> str:
            return PROVIDER_NAME

        @property
        def display_name(self) -> str:
            return "Hermes Talk OpenAI Realtime (input only)"

        def is_available(self) -> bool:
            try:
                import aiohttp  # noqa: F401 - passive dependency probe

                talk_config.talk_voice()
                model = talk_config.talk_model()
                auth = talk_auth.auth_diagnostic()
            except Exception:  # noqa: BLE001 - passive readiness must not escape
                return False
            return bool(model and auth.get("configured"))

        def list_models(self):
            return ({"id": talk_config.talk_model(), "input_only": True},)

        def list_voices(self):
            return tuple({"id": voice} for voice in talk_config.OPENAI_REALTIME_VOICES)

        async def open_session(self, setup: RealtimeVoiceSetup) -> RealtimeVoiceSession:
            _validate_setup(setup)
            model = setup.model or talk_config.talk_model()
            voice = setup.voice or talk_config.talk_voice()
            instructions = setup.instructions or ""
            auth = self._auth_resolver()
            wire = self._wire_factory(
                auth_token=auth.token,
                auth_source=auth.source,
            )
            try:
                await wire.connect(
                    model=model,
                    voice=voice,
                    instructions=instructions,
                    tools=None,
                    automatic_response=False,
                    session_update=_session_update(
                        model=model, voice=voice, instructions=instructions
                    ),
                )
            except BaseException:
                await wire.close()
                raise
            return _TalkCoreRealtimeSession(
                wire=wire,
                audio_format=SUPPORTED_AUDIO_FORMAT,
                ledger_capacity=self._ledger_capacity,
            )

else:
    SUPPORTED_AUDIO_FORMAT = None
    CORE_CAPABILITIES = frozenset()
    TalkOpenAIRealtimeProvider = None


__all__ = [
    "CORE_CAPABILITIES",
    "DEFAULT_INPUT_LEDGER_CAPACITY",
    "PROVIDER_NAME",
    "SUPPORTED_AUDIO_FORMAT",
    "OpenAIWireEOF",
    "OpenAIWireError",
    "TalkOpenAIRealtimeProvider",
    "core_provider_available",
    "core_provider_diagnostic",
]
