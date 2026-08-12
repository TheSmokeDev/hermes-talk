"""Defensive Hermes core API-v2 OpenAI Realtime adapter.

The optional core boundary is deliberately contained in this module.  Legacy
Talk never imports Hermes core through its OpenAI transport, and this module
exposes no partially subclassed provider when the exact API-v2 contract is
unavailable.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import re
import secrets
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

try:
    from . import talk_auth, talk_config, talk_wire
    from .talk_openai_realtime import (
        OpenAIWireEOF,
        OpenAIWireError,
        _OpenAIWireSession,
        build_core_response_cancel,
        build_core_response_create,
    )
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_auth
    import talk_config
    import talk_wire
    from talk_openai_realtime import (
        OpenAIWireEOF,
        OpenAIWireError,
        _OpenAIWireSession,
        build_core_response_cancel,
        build_core_response_create,
    )

_CORE_IMPORT_ERROR: BaseException | None = None
_EXPLICIT_OUTPUT_AVAILABLE = False
try:
    from agent import realtime_voice_provider as _core_contract
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
    try:
        RealtimeInputAudioFormat = _core_contract.RealtimeInputAudioFormat
        RealtimeOutputAudioFormat = _core_contract.RealtimeOutputAudioFormat
        RealtimeResponseRequest = _core_contract.RealtimeResponseRequest
        ResponseStarted = _core_contract.ResponseStarted
        OutputAudio = _core_contract.OutputAudio
        OutputTranscript = _core_contract.OutputTranscript
        ResponseCompleted = _core_contract.ResponseCompleted
        InputSpeechStarted = _core_contract.InputSpeechStarted
        Interruption = _core_contract.Interruption
        _EXPLICIT_OUTPUT_AVAILABLE = all(
            isinstance(symbol, type)
            for symbol in (
                RealtimeInputAudioFormat,
                RealtimeOutputAudioFormat,
                RealtimeResponseRequest,
                ResponseStarted,
                OutputAudio,
                OutputTranscript,
                ResponseCompleted,
            )
        ) and all(
            hasattr(RealtimeCapability, name)
            for name in (
                "EXPLICIT_RESPONSE",
                "RESPONSE_METADATA_ECHO",
                "OUTPUT_TRANSCRIPTION",
            )
        )
        _RESPONSE_CANCELLATION_AVAILABLE = (
            _EXPLICIT_OUTPUT_AVAILABLE
            and all(isinstance(symbol, type) for symbol in (InputSpeechStarted, Interruption))
            and hasattr(RealtimeCapability, "RESPONSE_CANCELLATION")
            and callable(getattr(RealtimeVoiceSession, "cancel_response", None))
            and getattr(RealtimeVoiceSession, "_CAPABILITY_HOOKS", {}).get(
                RealtimeCapability.RESPONSE_CANCELLATION
            ) == "_cancel_response"
        )
    except (AttributeError, ImportError):
        _EXPLICIT_OUTPUT_AVAILABLE = False
        _RESPONSE_CANCELLATION_AVAILABLE = False
except Exception as exc:  # noqa: BLE001 - optional core must never poison legacy imports
    _CORE_IMPORT_ERROR = exc

# Compatibility diagnostics intentionally distinguish the detected data surface
# from the complete production capability (which also requires enum claims and
# the concrete mapper/hook implemented below).
_EXPLICIT_RESPONSE_SURFACE_AVAILABLE = _CORE_IMPORT_ERROR is None and all(
    isinstance(getattr(_core_contract, name, None), type)
    for name in (
        "RealtimeInputAudioFormat",
        "RealtimeOutputAudioFormat",
        "RealtimeResponseRequest",
        "ResponseStarted",
        "OutputAudio",
        "OutputTranscript",
        "ResponseCompleted",
    )
)
_RESPONSE_CANCELLATION_SURFACE_AVAILABLE = _CORE_IMPORT_ERROR is None and all(
    isinstance(getattr(_core_contract, name, None), type)
    for name in ("InputSpeechStarted", "Interruption")
) and hasattr(RealtimeCapability, "RESPONSE_CANCELLATION")

DEFAULT_INPUT_LEDGER_CAPACITY = 1024
MAX_IDENTIFIER_LENGTH = 512
MAX_TRANSCRIPT_LENGTH = 1_000_000
PROVIDER_NAME = "talk_openai_realtime"
_PUBLIC_ERROR_SECRET = re.compile(
    r"(?i)(?:bearer\s+[^\s,;]+|(?:password|api[_-]?key|token)\s*[=:]\s*[^\s,;]+|sk-[A-Za-z0-9_-]{8,})"
)


def _sanitize_public_error(exc: BaseException | str) -> str:
    text = str(exc).strip() or "provider operation failed"
    return _PUBLIC_ERROR_SECRET.sub("<redacted-secret>", text)


def core_provider_available() -> bool:
    """Return only whether the exact optional Hermes core API-v2 is importable."""

    return _CORE_IMPORT_ERROR is None


def explicit_output_available() -> bool:
    """Return whether the installed API-v2 exposes native explicit output."""

    return _CORE_IMPORT_ERROR is None and _EXPLICIT_OUTPUT_AVAILABLE


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
    SUPPORTED_OUTPUT_AUDIO_FORMAT = (
        RealtimeOutputAudioFormat(
            mime_type="audio/pcm",
            sample_rate_hz=24_000,
            channels=1,
            sample_encoding="pcm_s16le",
            sample_width_bytes=2,
            endianness="little",
        )
        if _EXPLICIT_OUTPUT_AVAILABLE
        else None
    )
    SUPPORTED_INPUT_AUDIO_FORMAT = (
        RealtimeInputAudioFormat(
            mime_type="audio/pcm",
            sample_rate_hz=24_000,
            channels=1,
            sample_encoding="pcm_s16le",
            sample_width_bytes=2,
            endianness="little",
        )
        if _EXPLICIT_OUTPUT_AVAILABLE
        else None
    )
    CORE_CAPABILITIES = frozenset(
        {RealtimeCapability.INPUT_TRANSCRIPTION, RealtimeCapability.INPUT_COMMIT_EVENTS}
        | (
            {
                RealtimeCapability.EXPLICIT_RESPONSE,
                RealtimeCapability.RESPONSE_METADATA_ECHO,
                RealtimeCapability.OUTPUT_TRANSCRIPTION,
            }
            if _EXPLICIT_OUTPUT_AVAILABLE
            else set()
        )
        | (
            {RealtimeCapability.RESPONSE_CANCELLATION}
            if _RESPONSE_CANCELLATION_AVAILABLE
            else set()
        )
    )
    _OUTPUT_ITEM_KEYS = frozenset({"id", "type", "role", "status", "content"})
    _OUTPUT_ITEM_ALLOWED_KEYS = _OUTPUT_ITEM_KEYS | {"object"}
    _OUTPUT_AUDIO_PART_KEYS = frozenset({"type", "transcript"})
    _OUTPUT_EVENT_TYPES = frozenset(
        {
            "response.created",
            "response.output_item.added",
            "response.output_item.done",
            "response.content_part.added",
            "response.content_part.done",
            "response.output_audio.delta",
            "response.output_audio.done",
            "response.output_audio_transcript.delta",
            "response.output_audio_transcript.done",
            "conversation.item.done",
            "response.done",
        }
    )
    _AUTHORITY_VALIDATED_EVENT_TYPES = _OUTPUT_EVENT_TYPES | frozenset(
        {
            "session.created",
            "session.updated",
            "input_audio_buffer.speech_started",
            "input_audio_buffer.speech_stopped",
            "input_audio_buffer.committed",
            "conversation.item.input_audio_transcription.delta",
            "conversation.item.input_audio_transcription.completed",
            "rate_limits.updated",
            "error",
        }
    )
    _REQUIRED_OUTPUT_LIFECYCLE = (
        "response.output_item.added",
        "response.content_part.added",
        "response.output_audio.delta",
        "response.output_audio_transcript.delta",
        "response.output_audio.done",
        "response.output_audio_transcript.done",
        "response.content_part.done",
        "response.output_item.done",
        "conversation.item.done",
    )
    _FORBIDDEN_OUTPUT_AUTHORITY_KEYS = frozenset(
        {
            "tool_calls",
            "function_call",
            "function",
            "tool",
            "tools",
            "tool_choice",
            "call_id",
            "name",
            "arguments",
            "function_call_output",
        }
    )
    _FORBIDDEN_OUTPUT_AUTHORITY_TYPES = frozenset(
        {"function_call", "function_call_output", "tool", "tool_call"}
    )
    MAX_AUTHORITY_VALIDATION_DEPTH = 66
    MAX_AUTHORITY_VALIDATION_NODES = 4096

    def _validate_output_event_authority(value: Any) -> None:
        pending = [(value, 0)]
        visited = 0
        while pending:
            current, depth = pending.pop()
            visited += 1
            if visited > MAX_AUTHORITY_VALIDATION_NODES:
                raise ValueError("output event authority node limit exceeded")
            if depth > MAX_AUTHORITY_VALIDATION_DEPTH:
                raise ValueError("output event authority depth limit exceeded")
            if isinstance(current, Mapping):
                forbidden = _FORBIDDEN_OUTPUT_AUTHORITY_KEYS.intersection(current)
                if forbidden:
                    raise ValueError(
                        "output event has forbidden structured authority: "
                        f"{sorted(forbidden)[0]}"
                    )
                discriminator = current.get("type")
                if (
                    isinstance(discriminator, str)
                    and discriminator in _FORBIDDEN_OUTPUT_AUTHORITY_TYPES
                ):
                    raise ValueError(
                        "output event has forbidden structured authority type: "
                        f"{discriminator}"
                    )
                pending.extend((nested, depth + 1) for nested in current.values())
            elif isinstance(current, Sequence) and not isinstance(
                current, (str, bytes, bytearray)
            ):
                pending.extend((nested, depth + 1) for nested in current)

    @dataclass(frozen=True, slots=True)
    class _ResponseBinding:
        durable_session_id: str
        assistant_message_id: int
        turn_marker: str
        content_digest: str

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
        if _EXPLICIT_OUTPUT_AVAILABLE:
            if (
                setup.input_audio is not None
                and setup.input_audio != SUPPORTED_INPUT_AUDIO_FORMAT
            ):
                raise ValueError("the core provider requires exact 24kHz mono PCM input audio")
            if (
                setup.output_audio is not None
                and setup.output_audio != SUPPORTED_OUTPUT_AUDIO_FORMAT
            ):
                raise ValueError("the core provider requires exact 24kHz mono PCM output audio")
            if setup.automatic_response:
                raise ValueError("automatic_response must remain false in the core lane")
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
            voice: str,
            response_ledger_capacity: int,
            token_factory: Callable[[], str],
        ) -> None:
            super().__init__(CORE_CAPABILITIES)
            self._wire = wire
            self._audio_format = audio_format
            self._ledger_capacity = ledger_capacity
            self._voice = voice
            self._response_ledger_capacity = response_ledger_capacity
            self._token_factory = token_factory
            self._response_lock = asyncio.Lock()
            self._pending_responses: dict[str, _ResponseBinding] = {}
            self._active_responses: dict[str, tuple[str, _ResponseBinding]] = {}
            self._response_items: dict[str, str] = {}
            self._item_responses: dict[str, str] = {}
            self._final_output_transcripts: dict[tuple[str, str], str] = {}
            self._audio_delta_items: set[tuple[str, str]] = set()
            self._audio_done_items: set[tuple[str, str]] = set()
            self._audio_byte_carries: dict[tuple[str, str], bytes] = {}
            self._output_item_added: set[tuple[str, str]] = set()
            self._content_part_added: set[tuple[str, str]] = set()
            self._content_part_done: set[tuple[str, str]] = set()
            self._output_item_done: set[tuple[str, str]] = set()
            self._conversation_item_done: set[tuple[str, str]] = set()
            self._response_lifecycle: dict[tuple[str, str], int] = {}
            self._completed_responses: OrderedDict[str, str] = OrderedDict()
            self._consumed_correlations: OrderedDict[str, None] = OrderedDict()
            self._cancelling_responses: set[str] = set()
            # Exact cancellation operations whose provider terminal won.  This
            # authority outlives response/session ledgers only until that
            # already-bounded retained operation settles.
            self._terminal_cancel_supersessions: set[str] = set()
            self._input_ledger: dict[str, str | None] = {}
            self._provider_session_id: str | None = None
            self._terminal = False
            self._pending_send_failure: str | None = None
            self._enforce_output_lifecycle = False

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
                await self._record_send_failure(exc)
                raise

        async def _record_send_failure(self, exc: Exception) -> None:
            self._pending_send_failure = _sanitize_public_error(exc)
            with contextlib.suppress(Exception):
                await self._wire.close()

        async def _submit_tool_results(self, batch_id, results) -> None:
            del batch_id, results
            raise RuntimeError("the input-only core provider does not accept tool results")

        async def _start_response(self, request) -> None:
            if request.output_audio_format != SUPPORTED_OUTPUT_AUDIO_FORMAT:
                raise ValueError("explicit response output audio format is unsupported")
            correlation = _validate_identifier(self._token_factory(), "correlation token")
            binding = _ResponseBinding(
                durable_session_id=request.durable_session_id,
                assistant_message_id=request.assistant_message_id,
                turn_marker=request.turn_marker,
                content_digest=request.content_digest,
            )
            async with self._response_lock:
                active_correlations = {
                    active_correlation
                    for active_correlation, _binding in self._active_responses.values()
                }
                if (
                    correlation in self._pending_responses
                    or correlation in self._consumed_correlations
                    or correlation in active_correlations
                ):
                    raise ValueError("correlation token was already used")
                accepted_responses = (
                    len(self._pending_responses)
                    + len(self._active_responses)
                    + len(self._completed_responses)
                )
                if accepted_responses >= self._response_ledger_capacity:
                    raise ValueError("response lifetime capacity exhausted")
                self._pending_responses[correlation] = binding
            message = build_core_response_create(
                canonical_text=request.canonical_text,
                correlation=correlation,
                voice=self._voice,
                event_id=f"evt-{secrets.token_urlsafe(24)}",
            )
            try:
                await self._send_wire(message)
            except BaseException:
                async with self._response_lock:
                    if self._pending_responses.get(correlation) == binding:
                        del self._pending_responses[correlation]
                raise

        async def _cancel_response(self, response_id: str) -> None:
            async with self._response_lock:
                if response_id in self._completed_responses:
                    raise ValueError("response is already terminal")
                active = self._active_responses.get(response_id)
                if active is None:
                    raise ValueError("cancellation references an unbound response")
                if response_id in self._cancelling_responses:
                    raise ValueError("response cancellation is already pending")
                if len(self._cancelling_responses) >= self._response_ledger_capacity:
                    raise ValueError("response cancellation capacity exhausted")
                correlation, _binding = active
                self._cancelling_responses.add(response_id)
            try:
                await self._wire.send_json(
                    (
                        build_core_response_cancel(
                            response_id=response_id,
                            event_id=f"evt-{secrets.token_urlsafe(24)}",
                        ),
                    )
                )
            except Exception as exc:
                async with self._response_lock:
                    terminal_won = (
                        self._completed_responses.get(response_id) == correlation
                        or response_id in self._terminal_cancel_supersessions
                    )
                    if not terminal_won:
                        self._cancelling_responses.discard(response_id)
                if terminal_won:
                    return
                await self._record_send_failure(exc)
                raise
            except BaseException:
                async with self._response_lock:
                    self._cancelling_responses.discard(response_id)
                raise
            finally:
                self._terminal_cancel_supersessions.discard(response_id)

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

        @staticmethod
        def _response_metadata(response: Mapping[str, Any]) -> str:
            metadata = response.get("metadata")
            if not isinstance(metadata, Mapping) or set(metadata) != {"correlation"}:
                raise ValueError("response metadata must contain only the correlation token")
            return _validate_identifier(metadata.get("correlation"), "correlation token")

        def _bind_response_created(self, event: Mapping[str, Any]):
            response = event.get("response")
            if not isinstance(response, Mapping):
                raise TypeError("response.created requires a response object")
            response_id = _validate_identifier(response.get("id"), "response_id")
            if response_id in self._active_responses or response_id in self._completed_responses:
                raise ValueError("duplicate or conflicting provider response_id")
            correlation = self._response_metadata(response)
            if correlation in self._consumed_correlations:
                raise ValueError("replayed correlation token")
            binding = self._pending_responses.get(correlation)
            if binding is None:
                raise ValueError("unknown or unsolicited correlation token")
            accepted_responses = (
                len(self._pending_responses)
                + len(self._active_responses)
                + len(self._completed_responses)
            )
            if accepted_responses > self._response_ledger_capacity:
                raise ValueError("response lifetime capacity exhausted")
            if len(self._consumed_correlations) >= self._response_ledger_capacity:
                raise ValueError("consumed correlation capacity exhausted")
            del self._pending_responses[correlation]
            self._consumed_correlations[correlation] = None
            self._active_responses[response_id] = (correlation, binding)
            return ResponseStarted(response_id=response_id, turn_id=binding.turn_marker)

        def _validate_active_response(
            self, event: Mapping[str, Any]
        ) -> tuple[str, str, _ResponseBinding, str]:
            response_id = _validate_identifier(event.get("response_id"), "response_id")
            active = self._active_responses.get(response_id)
            if active is None:
                raise ValueError("output event references an unbound response")
            item_id = _validate_identifier(event.get("item_id"), "item_id")
            known_item = self._response_items.get(response_id)
            known_response = self._item_responses.get(item_id)
            if known_item is not None and known_item != item_id:
                raise ValueError("output item identity changed for response")
            if known_response is not None and known_response != response_id:
                raise ValueError("output item is already bound to another response")
            return response_id, active[0], active[1], item_id

        def _active_response(self, event: Mapping[str, Any]) -> tuple[str, _ResponseBinding, str]:
            response_id, correlation, binding, item_id = self._validate_active_response(event)
            self._response_items[response_id] = item_id
            self._item_responses[item_id] = response_id
            return correlation, binding, item_id

        def _validate_output_audio_part(
            self,
            part: Any,
            *,
            response_id: str,
            item_id: str,
            context: str,
        ) -> Mapping[str, Any]:
            if not isinstance(part, Mapping):
                raise TypeError(f"{context} requires an output_audio part object")
            if not set(part).issubset(_OUTPUT_AUDIO_PART_KEYS):
                raise ValueError(f"{context} output_audio part has an invalid authority shape")
            if part.get("type") != "output_audio":
                raise ValueError(f"{context} content type must be output_audio")
            if "transcript" in part:
                transcript = _validate_text(part["transcript"], final=True)
                terminal = self._final_output_transcripts.get((response_id, item_id))
                if terminal is not None and transcript != terminal:
                    raise ValueError(f"{context} transcript conflicts with final text")
            return part

        def _validate_output_item_shape(
            self,
            item: Any,
            *,
            response_id: str,
            stage: str,
        ) -> tuple[str, Mapping[str, Any] | None]:
            if not isinstance(item, Mapping):
                raise TypeError(f"{stage} requires an output item object")
            if not set(item).issubset(_OUTPUT_ITEM_ALLOWED_KEYS):
                raise ValueError(f"{stage} output item has an invalid authority shape")
            if "object" in item and (
                type(item["object"]) is not str or item["object"] != "realtime.item"
            ):
                raise ValueError(f"{stage} output item object must be realtime.item")
            if item.get("type") != "message":
                raise ValueError(f"{stage} output item type must be message")
            if item.get("role") != "assistant":
                raise ValueError(f"{stage} output item role must be assistant")
            item_id = _validate_identifier(item.get("id"), "output item_id")
            status = item.get("status")
            if stage == "response.output_item.added":
                if status is not None and status != "in_progress":
                    raise ValueError("added output item status must be in_progress")
            elif status is not None and status != "completed":
                raise ValueError(f"{stage} output item status must be completed")
            if "content" not in item:
                raise ValueError(f"{stage} output item content is required")
            content = item["content"]
            if not isinstance(content, (list, tuple)):
                raise TypeError(f"{stage} output item content must be a sequence")
            if stage == "response.output_item.added" and not content:
                return item_id, None
            if len(content) != 1:
                raise ValueError(f"{stage} output item content must contain exactly one part")
            part = self._validate_output_audio_part(
                content[0],
                response_id=response_id,
                item_id=item_id,
                context=stage,
            )
            return item_id, part

        @staticmethod
        def _note_lifecycle_stage(
            observed: set[tuple[str, str]], key: tuple[str, str], event_type: str
        ) -> None:
            if key in observed:
                raise ValueError(f"{event_type} was replayed")
            observed.add(key)

        def _advance_lifecycle_stage(
            self, key: tuple[str, str], event_type: str
        ) -> None:
            if key not in self._response_lifecycle:
                if event_type != _REQUIRED_OUTPUT_LIFECYCLE[0]:
                    if (
                        not self._enforce_output_lifecycle
                        or key[0] not in self._active_responses
                    ):
                        return
                    raise ValueError(
                        "output lifecycle is out of order: expected "
                        f"{_REQUIRED_OUTPUT_LIFECYCLE[0]}, got {event_type}"
                    )
                self._response_lifecycle[key] = 1
                return
            stage = self._response_lifecycle[key]
            if stage >= len(_REQUIRED_OUTPUT_LIFECYCLE):
                raise ValueError(f"{event_type} was replayed")
            if (
                event_type == "response.output_audio.delta"
                and stage
                == _REQUIRED_OUTPUT_LIFECYCLE.index("response.output_audio.delta") + 1
            ):
                return
            expected = _REQUIRED_OUTPUT_LIFECYCLE[stage]
            if event_type != expected:
                raise ValueError(
                    f"output lifecycle is out of order: expected {expected}, got {event_type}"
                )
            self._response_lifecycle[key] = stage + 1

        def _complete_response(self, event: Mapping[str, Any]):
            response = event.get("response")
            if not isinstance(response, Mapping):
                raise TypeError("response.done requires a response object")
            response_id = _validate_identifier(response.get("id"), "response_id")
            if response_id in self._completed_responses:
                raise ValueError("response completion was replayed")
            active = self._active_responses.get(response_id)
            if active is None:
                raise ValueError("completion references an unbound response")
            correlation, binding = active
            if self._response_metadata(response) != correlation:
                raise ValueError("response completion correlation mismatch")
            if response.get("status") != "completed":
                raise ValueError("response status must be completed")
            if "output" not in response:
                raise ValueError("response output is required")
            output = response["output"]
            if not isinstance(output, (list, tuple)):
                raise TypeError("response output must be a sequence")
            if not output:
                raise ValueError("response output must be nonempty")
            if len(output) != 1:
                raise ValueError("response output must contain exactly one item")
            item = output[0]
            output_item_id, _part = self._validate_output_item_shape(
                item,
                response_id=response_id,
                stage="response.done",
            )
            known_item_id = self._response_items.get(response_id)
            if known_item_id is None or output_item_id != known_item_id:
                raise ValueError("response completion output item identity changed")
            stream_key = (response_id, output_item_id)
            final_transcript = self._final_output_transcripts.get(stream_key)
            if final_transcript is None:
                raise ValueError("response completion requires transcript done")
            if stream_key not in self._audio_delta_items:
                raise ValueError("response completion requires audio delta")
            if stream_key not in self._audio_done_items:
                raise ValueError("response completion requires audio done")
            try:
                final_digest = hashlib.sha256(final_transcript.encode("utf-8")).hexdigest()
            except UnicodeEncodeError as exc:
                raise ValueError("response terminal transcript is not valid UTF-8") from exc
            if final_digest != binding.content_digest:
                raise ValueError("response terminal transcript digest mismatch")
            if self._response_lifecycle.get(stream_key) != len(
                _REQUIRED_OUTPUT_LIFECYCLE
            ):
                raise ValueError("response completion requires complete output lifecycle")
            if len(self._completed_responses) >= self._response_ledger_capacity:
                raise ValueError("completed response capacity exhausted")
            del self._active_responses[response_id]
            item_id = self._response_items.pop(response_id, None)
            if item_id is not None:
                self._item_responses.pop(item_id, None)
                self._final_output_transcripts.pop((response_id, item_id), None)
                self._audio_delta_items.discard((response_id, item_id))
                self._audio_done_items.discard((response_id, item_id))
                self._audio_byte_carries.pop((response_id, item_id), None)
                self._output_item_added.discard((response_id, item_id))
                self._content_part_added.discard((response_id, item_id))
                self._content_part_done.discard((response_id, item_id))
                self._output_item_done.discard((response_id, item_id))
                self._conversation_item_done.discard((response_id, item_id))
                self._response_lifecycle.pop((response_id, item_id), None)
            self._completed_responses[response_id] = correlation
            self._cancelling_responses.discard(response_id)
            return ResponseCompleted(response_id=response_id, turn_id=binding.turn_marker)

        def _cancelled_response(self, event: Mapping[str, Any]):
            response = event.get("response")
            if not isinstance(response, Mapping):
                raise TypeError("response.done requires a response object")
            response_id = _validate_identifier(response.get("id"), "response_id")
            if response_id in self._completed_responses:
                raise ValueError("response completion was replayed")
            active = self._active_responses.get(response_id)
            if active is None:
                raise ValueError("cancellation completion references an unbound response")
            if response_id not in self._cancelling_responses:
                raise ValueError("unsolicited response cancellation terminal")
            correlation, binding = active
            if self._response_metadata(response) != correlation:
                raise ValueError("response cancellation correlation mismatch")
            if type(response.get("status")) is not str or response["status"] != "cancelled":
                raise ValueError("response status must be cancelled")
            details = response.get("status_details")
            if details is not None:
                if type(details) is not dict:
                    raise TypeError("cancelled status_details must be an exact mapping")
                if not set(details).issubset({"type", "reason", "error"}):
                    raise ValueError("cancelled status_details has unknown keys")
                if details.get("type") != "cancelled":
                    raise ValueError("status_details type must be cancelled")
                if details.get("reason") not in {"client_cancelled", "turn_detected"}:
                    raise ValueError("status_details cancellation reason is invalid")
                if details.get("error") is not None:
                    raise ValueError("cancelled status_details error must be absent")
            output = response.get("output", [])
            if type(output) is not list:
                raise TypeError("cancelled response output must be a sequence")
            known_item_id = self._response_items.get(response_id)
            if known_item_id is None:
                if output:
                    raise ValueError("cancelled response cannot mint an output item")
            elif len(output) > 1:
                raise ValueError("cancelled response output must contain at most one item")
            elif output:
                item = output[0]
                if type(item) is not dict or not set(item).issubset(
                    _OUTPUT_ITEM_ALLOWED_KEYS
                ):
                    raise ValueError(
                        "cancelled response output item has an invalid authority shape"
                    )
                if "object" in item and (
                    type(item["object"]) is not str
                    or item["object"] != "realtime.item"
                ):
                    raise ValueError("cancelled response output item object must be realtime.item")
                if item.get("type") != "message" or item.get("role") != "assistant":
                    raise ValueError("cancelled response output item must be an assistant message")
                if item.get("status") not in (None, "in_progress", "completed"):
                    raise ValueError("cancelled response output item status is invalid")
                item_id = _validate_identifier(item.get("id"), "output item_id")
                content = item.get("content")
                if type(content) is not list or len(content) != 1:
                    raise ValueError("cancelled response output item must have one audio part")
                self._validate_output_audio_part(
                    content[0],
                    response_id=response_id,
                    item_id=item_id,
                    context="response.cancelled",
                )
                if item_id != known_item_id:
                    raise ValueError("cancelled response output item identity changed")
            if len(self._completed_responses) >= self._response_ledger_capacity:
                raise ValueError("completed response capacity exhausted")
            del self._active_responses[response_id]
            item_id = self._response_items.pop(response_id, None)
            if item_id is not None:
                self._item_responses.pop(item_id, None)
                key = (response_id, item_id)
                self._final_output_transcripts.pop(key, None)
                self._audio_delta_items.discard(key)
                self._audio_done_items.discard(key)
                self._audio_byte_carries.pop(key, None)
                self._output_item_added.discard(key)
                self._content_part_added.discard(key)
                self._content_part_done.discard(key)
                self._output_item_done.discard(key)
                self._conversation_item_done.discard(key)
                self._response_lifecycle.pop(key, None)
            self._cancelling_responses.remove(response_id)
            self._completed_responses[response_id] = correlation
            if response_id in self._in_flight_response_cancellations:
                self._terminal_cancel_supersessions.add(response_id)
            return Interruption(response_id=response_id, turn_id=binding.turn_marker)

        def _map_event(self, event: Mapping[str, Any]):
            event_type = event.get("type")
            if not isinstance(event_type, str) or not event_type:
                raise ValueError("provider event type must be a nonblank string")
            if event_type in _AUTHORITY_VALIDATED_EVENT_TYPES:
                _validate_output_event_authority(event)
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
            if event_type == "input_audio_buffer.speech_started":
                item_id = event.get("item_id")
                if type(item_id) is not str:
                    raise TypeError("speech-start item_id must be an exact str")
                item_id = _validate_identifier(item_id, "item_id")
                audio_start_ms = event.get("audio_start_ms")
                if type(audio_start_ms) is not int:
                    raise TypeError("audio_start_ms must be an exact int")
                if audio_start_ms < 0:
                    raise ValueError("audio_start_ms must be nonnegative")
                return InputSpeechStarted(item_id=item_id, audio_start_ms=audio_start_ms)
            if event_type in {"input_audio_buffer.speech_stopped", "rate_limits.updated"}:
                return None
            if event_type == "conversation.item.input_audio_transcription.delta":
                return self._input_transcript(event, final=False)
            if event_type == "conversation.item.input_audio_transcription.completed":
                return self._input_transcript(event, final=True)
            if event_type == "response.created":
                return self._bind_response_created(event)
            if event_type == "response.output_audio.delta":
                response_id, _correlation, binding, item_id = (
                    self._validate_active_response(event)
                )
                delta = event.get("delta")
                if not isinstance(delta, str) or not delta:
                    raise ValueError("output audio delta must be nonempty base64 text")
                try:
                    data = base64.b64decode(delta, validate=True)
                except (binascii.Error, ValueError, TypeError) as exc:
                    raise ValueError("output audio delta is malformed base64") from exc
                if not data:
                    raise ValueError("output audio delta decoded to empty data")
                stream_key = (response_id, item_id)
                buffered = self._audio_byte_carries.get(stream_key, b"") + data
                complete_length = len(buffered) - (len(buffered) % 2)
                complete_data = buffered[:complete_length]
                next_carry = buffered[complete_length:]
                if (
                    next_carry
                    and stream_key not in self._audio_byte_carries
                    and len(self._audio_byte_carries) >= self._response_ledger_capacity
                ):
                    raise ValueError("output audio carry capacity exhausted")
                self._advance_lifecycle_stage(stream_key, event_type)
                self._response_items[response_id] = item_id
                self._item_responses[item_id] = response_id
                self._audio_delta_items.add(stream_key)
                if next_carry:
                    self._audio_byte_carries[stream_key] = next_carry
                else:
                    self._audio_byte_carries.pop(stream_key, None)
                if not complete_data:
                    return None
                return OutputAudio(
                    data=complete_data,
                    item_id=item_id,
                    response_id=response_id,
                    turn_id=binding.turn_marker,
                )
            if event_type in {
                "response.output_audio_transcript.delta",
                "response.output_audio_transcript.done",
            }:
                final = event_type.endswith(".done")
                text = _validate_text(
                    event.get("transcript" if final else "delta"), final=final
                )
                response_id, _correlation, binding, item_id = (
                    self._validate_active_response(event)
                )
                transcript_key = (response_id, item_id)
                terminal_text = self._final_output_transcripts.get(transcript_key)
                if terminal_text is not None:
                    if not final:
                        raise ValueError("output transcript is already terminal")
                    if terminal_text == text:
                        raise ValueError("output transcript terminal event was replayed")
                    raise ValueError("conflicting terminal output transcript")
                self._advance_lifecycle_stage(transcript_key, event_type)
                self._response_items[response_id] = item_id
                self._item_responses[item_id] = response_id
                if final:
                    self._final_output_transcripts[transcript_key] = text
                return OutputTranscript(
                    item_id=item_id,
                    response_id=response_id,
                    turn_id=binding.turn_marker,
                    text=text,
                    final=final,
                )
            if event_type == "response.done":
                response = event.get("response")
                if isinstance(response, Mapping) and response.get("status") == "cancelled":
                    return self._cancelled_response(event)
                return self._complete_response(event)
            if "function_call" in event_type:
                raise ValueError(f"function/tool output is forbidden: {event_type}")
            if event_type == "response.output_audio.done":
                response_id, _correlation, _binding, item_id = (
                    self._validate_active_response(event)
                )
                stream_key = (response_id, item_id)
                if stream_key in self._audio_done_items:
                    raise ValueError("output audio done event was replayed")
                if stream_key in self._audio_byte_carries:
                    raise ValueError("output audio ended with an incomplete PCM16LE sample")
                self._advance_lifecycle_stage(stream_key, event_type)
                self._response_items[response_id] = item_id
                self._item_responses[item_id] = response_id
                self._audio_done_items.add(stream_key)
                return None
            if event_type in {"response.output_item.added", "response.output_item.done"}:
                response_id = _validate_identifier(event.get("response_id"), "response_id")
                item_id, _part = self._validate_output_item_shape(
                    event.get("item"),
                    response_id=response_id,
                    stage=event_type,
                )
                normalized = dict(event)
                normalized["item_id"] = item_id
                response_id, _correlation, _binding, item_id = self._validate_active_response(
                    normalized
                )
                self._advance_lifecycle_stage((response_id, item_id), event_type)
                observed = (
                    self._output_item_added
                    if event_type.endswith(".added")
                    else self._output_item_done
                )
                self._note_lifecycle_stage(observed, (response_id, item_id), event_type)
                self._response_items[response_id] = item_id
                self._item_responses[item_id] = response_id
                return None
            if event_type in {"response.content_part.added", "response.content_part.done"}:
                response_id, _correlation, _binding, item_id = self._validate_active_response(event)
                self._validate_output_audio_part(
                    event.get("part"),
                    response_id=response_id,
                    item_id=item_id,
                    context=event_type,
                )
                self._advance_lifecycle_stage((response_id, item_id), event_type)
                observed = (
                    self._content_part_added
                    if event_type.endswith(".added")
                    else self._content_part_done
                )
                self._note_lifecycle_stage(observed, (response_id, item_id), event_type)
                self._response_items[response_id] = item_id
                self._item_responses[item_id] = response_id
                return None
            if event_type == "conversation.item.done":
                item = event.get("item")
                if not isinstance(item, Mapping):
                    raise TypeError("conversation.item.done requires an item object")
                item_id = _validate_identifier(item.get("id"), "item_id")
                response_id = self._item_responses.get(item_id)
                if response_id is None or response_id not in self._active_responses:
                    raise ValueError("conversation item is not bound to an active response")
                validated_item_id, _part = self._validate_output_item_shape(
                    item,
                    response_id=response_id,
                    stage=event_type,
                )
                if validated_item_id != item_id:
                    raise ValueError("conversation output item identity changed")
                self._advance_lifecycle_stage((response_id, item_id), event_type)
                self._note_lifecycle_stage(
                    self._conversation_item_done,
                    (response_id, item_id),
                    event_type,
                )
                return None
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
            try:
                await self._wire.close()
            finally:
                self._pending_send_failure = None
                self._input_ledger.clear()
                self._pending_responses.clear()
                self._active_responses.clear()
                self._response_items.clear()
                self._item_responses.clear()
                self._final_output_transcripts.clear()
                self._audio_delta_items.clear()
                self._audio_done_items.clear()
                self._audio_byte_carries.clear()
                self._output_item_added.clear()
                self._content_part_added.clear()
                self._content_part_done.clear()
                self._output_item_done.clear()
                self._conversation_item_done.clear()
                self._response_lifecycle.clear()
                self._completed_responses.clear()
                self._consumed_correlations.clear()
                self._cancelling_responses.clear()

        def _take_send_failure(self) -> str | None:
            failure, self._pending_send_failure = self._pending_send_failure, None
            return failure

        async def _events(self) -> AsyncIterator[Any]:
            if self._terminal:
                return
            failure = self._take_send_failure()
            if failure:
                await self._terminate()
                yield SessionFailure(
                    code="provider_send_failure",
                    message=failure,
                )
                return
            try:
                self._enforce_output_lifecycle = True
                while True:
                    wire_event = await self._wire.__anext__()
                    mapped = self._map_event(wire_event)
                    if mapped is not None:
                        yield mapped
            except OpenAIWireEOF as exc:
                failure = self._take_send_failure()
                await self._terminate()
                if failure:
                    yield SessionFailure(
                        code="provider_send_failure",
                        message=failure,
                    )
                elif exc.detail:
                    yield SessionFailure(
                        code="provider_eof",
                        message=_sanitize_public_error(exc.detail),
                    )
                else:
                    yield SessionClosed()
            except asyncio.CancelledError:
                await self._terminate()
                raise
            except Exception as exc:  # noqa: BLE001 - protocol errors converge here
                failure = self._take_send_failure()
                try:
                    await self._terminate()
                except Exception:  # noqa: BLE001 - public text is fixed and secret-safe
                    message = "provider operation failed; cleanup failed"
                else:
                    message = _sanitize_public_error(exc)
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
            response_ledger_capacity: int = 1024,
            token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        ) -> None:
            if isinstance(ledger_capacity, bool) or not isinstance(ledger_capacity, int):
                raise TypeError("ledger_capacity must be a positive integer")
            if ledger_capacity <= 0:
                raise ValueError("ledger_capacity must be a positive integer")
            self._auth_resolver = auth_resolver
            self._wire_factory = wire_factory
            self._ledger_capacity = ledger_capacity
            if (
                isinstance(response_ledger_capacity, bool)
                or not isinstance(response_ledger_capacity, int)
                or response_ledger_capacity <= 0
            ):
                raise ValueError("response_ledger_capacity must be a positive integer")
            if not callable(token_factory):
                raise TypeError("token_factory must be callable")
            self._response_ledger_capacity = response_ledger_capacity
            self._token_factory = token_factory

        @property
        def name(self) -> str:
            return PROVIDER_NAME

        @property
        def display_name(self) -> str:
            if _EXPLICIT_OUTPUT_AVAILABLE:
                return "Hermes Talk OpenAI Realtime (native explicit output)"
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
            return (
                {
                    "id": talk_config.talk_model(),
                    "input_only": not _EXPLICIT_OUTPUT_AVAILABLE,
                    "native_explicit_output": _EXPLICIT_OUTPUT_AVAILABLE,
                },
            )

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
                voice=voice,
                response_ledger_capacity=self._response_ledger_capacity,
                token_factory=self._token_factory,
            )

else:
    SUPPORTED_AUDIO_FORMAT = None
    SUPPORTED_INPUT_AUDIO_FORMAT = None
    SUPPORTED_OUTPUT_AUDIO_FORMAT = None
    CORE_CAPABILITIES = frozenset()
    TalkOpenAIRealtimeProvider = None


__all__ = [
    "CORE_CAPABILITIES",
    "DEFAULT_INPUT_LEDGER_CAPACITY",
    "PROVIDER_NAME",
    "SUPPORTED_AUDIO_FORMAT",
    "SUPPORTED_INPUT_AUDIO_FORMAT",
    "SUPPORTED_OUTPUT_AUDIO_FORMAT",
    "OpenAIWireEOF",
    "OpenAIWireError",
    "TalkOpenAIRealtimeProvider",
    "core_provider_available",
    "core_provider_diagnostic",
    "explicit_output_available",
]
