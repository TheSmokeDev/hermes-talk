"""Typed, provider-neutral contract for one duplex Realtime session.

Hermes policy consumes these events and emits these commands.  A provider
adapter owns authentication, connection details, and its wire vocabulary.
Keeping that direction explicit lets a future adapter implement the same
contract without moving speaker authority, tool policy, or lifecycle hooks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

MAX_IDENTIFIER_CHARS = 512


def _identifier(value: str | None, field_name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_IDENTIFIER_CHARS
        or value != value.strip()
    ):
        raise ValueError(f"{field_name} must be a non-empty, trimmed protocol identifier")


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


def _freeze_json(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def wire_value(value):
    """Copy immutable provider evidence back to plain wire data."""
    if isinstance(value, Mapping):
        return {key: wire_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [wire_value(item) for item in value]
    return value


class SessionState(StrEnum):
    """Observable lifecycle of a provider session."""

    NEW = "new"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    CLOSED = "closed"
    FAILED = "failed"


class RealtimeTurnDetectionMode(StrEnum):
    """Provider-neutral input-turn detection strategy."""

    PROVIDER_NATIVE = "provider_native"
    SERVER_VAD = "server_vad"
    SEMANTIC_VAD = "semantic_vad"


class RealtimeSemanticEagerness(StrEnum):
    """How readily semantic endpointing should close an input turn."""

    AUTO = "auto"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class RealtimeTurnDetection:
    mode: RealtimeTurnDetectionMode = RealtimeTurnDetectionMode.PROVIDER_NATIVE
    semantic_eagerness: RealtimeSemanticEagerness | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RealtimeTurnDetectionMode):
            raise TypeError("mode must be RealtimeTurnDetectionMode")
        if self.semantic_eagerness is not None and not isinstance(
            self.semantic_eagerness, RealtimeSemanticEagerness
        ):
            raise TypeError("semantic_eagerness must be None or RealtimeSemanticEagerness")
        if (
            self.semantic_eagerness is not None
            and self.mode is not RealtimeTurnDetectionMode.SEMANTIC_VAD
        ):
            raise ValueError("semantic_eagerness is valid only for semantic_vad")


class TranscriptRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class TranscriptProvenance(StrEnum):
    """Which audio direction produced a transcript."""

    INPUT_AUDIO = "input_audio"
    OUTPUT_AUDIO = "output_audio"


class ContextRole(StrEnum):
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        _identifier(self.name, "name")
        object.__setattr__(self, "parameters", _frozen_mapping(self.parameters))


@dataclass(frozen=True, slots=True)
class SessionSetup:
    model: str
    voice: str
    instructions: str
    tools: tuple[ToolDefinition, ...] = ()
    automatic_response: bool = True
    #: Ask the provider for TEXT output instead of synthesized audio. The
    #: cascade voice mode sets this so the provider remains the brain while
    #: an external TTS speaks; providers that cannot do text-only output
    #: refuse at their own boundary rather than silently speaking anyway.
    text_output: bool = False
    turn_detection: RealtimeTurnDetection = RealtimeTurnDetection()
    task_continuity: bool = False

    def __post_init__(self) -> None:
        _identifier(self.model, "model")
        _identifier(self.voice, "voice")
        object.__setattr__(self, "tools", tuple(self.tools))
        if not isinstance(self.turn_detection, RealtimeTurnDetection):
            raise TypeError("turn_detection must be RealtimeTurnDetection")


class RealtimeEvent:
    """Marker base for events emitted by a provider session."""


@dataclass(frozen=True, slots=True)
class SessionReady(RealtimeEvent):
    session_id: str

    def __post_init__(self) -> None:
        _identifier(self.session_id, "session_id")


@dataclass(frozen=True, slots=True)
class SpeechStarted(RealtimeEvent):
    input_id: str | None = None
    offset_ms: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.input_id, "input_id", optional=True)


@dataclass(frozen=True, slots=True)
class SpeechStopped(RealtimeEvent):
    input_id: str | None = None
    offset_ms: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.input_id, "input_id", optional=True)


@dataclass(frozen=True, slots=True)
class InputAudioCommitted(RealtimeEvent):
    input_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.input_id, "input_id", optional=True)


@dataclass(frozen=True, slots=True)
class ResponseStarted(RealtimeEvent):
    response_id: str | None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.response_id, "response_id", optional=True)
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class OutputAudio(RealtimeEvent):
    data: bytes
    item_id: str | None = None
    #: Which response produced this audio. A cancelled response keeps emitting
    #: deltas, so playback has to be able to tell whose audio it is holding.
    response_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id", optional=True)
        _identifier(self.response_id, "response_id", optional=True)


@dataclass(frozen=True, slots=True)
class Transcript(RealtimeEvent):
    role: TranscriptRole
    text: str
    final: bool
    provenance: TranscriptProvenance
    #: Which response produced this transcript. Always None for input audio —
    #: the operator's own speech belongs to no response.
    response_id: str | None = None
    item_id: str | None = None

    def __post_init__(self) -> None:
        expected_role = {
            TranscriptProvenance.INPUT_AUDIO: TranscriptRole.USER,
            TranscriptProvenance.OUTPUT_AUDIO: TranscriptRole.ASSISTANT,
        }.get(self.provenance)
        if self.role is not expected_role:
            raise ValueError("Transcript role must match its audio provenance")
        _identifier(self.response_id, "response_id", optional=True)
        _identifier(self.item_id, "item_id", optional=True)


@dataclass(frozen=True, slots=True)
class FunctionCall(RealtimeEvent):
    call_id: str
    name: str
    arguments: str
    response_id: str | None = None
    item_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.call_id, "call_id")
        _identifier(self.name, "name")
        _identifier(self.response_id, "response_id", optional=True)
        _identifier(self.item_id, "item_id", optional=True)


@dataclass(frozen=True, slots=True)
class ToolCallsCancelled(RealtimeEvent):
    """Calls the provider retracted; their results must never be submitted.

    Emitted by providers whose wire can discard a pending call mid-turn
    (Gemini Live's ``toolCallCancellation``). Without it a cancellation is
    only observable on the send path, as a dropped result — which tells
    policy nothing until it has already produced work nobody wants.
    """

    call_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.call_ids, (str, bytes)):
            raise ValueError("call_ids must be a sequence of identifiers")
        call_ids = tuple(self.call_ids)
        if not call_ids:
            raise ValueError("call_ids must contain at least one identifier")
        for call_id in call_ids:
            _identifier(call_id, "call_id")
        object.__setattr__(self, "call_ids", call_ids)


@dataclass(frozen=True, slots=True)
class ResponseFinished(RealtimeEvent):
    response_id: str | None = None
    status: str | None = None
    output: tuple[Mapping[str, Any], ...] | None = None

    def __post_init__(self) -> None:
        _identifier(self.response_id, "response_id", optional=True)
        if self.status not in {None, "completed", "cancelled", "failed", "incomplete"}:
            raise ValueError("Invalid terminal response status")
        if self.output is not None:
            if (not isinstance(self.output, (list, tuple)) or len(self.output) > 64
                or any(not isinstance(item, Mapping) for item in self.output)):
                raise ValueError("Invalid terminal response output")
            object.__setattr__(self, "output", tuple(_freeze_json(item) for item in self.output))


@dataclass(frozen=True, slots=True)
class ProviderFailure(RealtimeEvent):
    detail: str
    terminal: bool = False
    response_metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "response_metadata", _frozen_mapping(self.response_metadata)
        )


@dataclass(frozen=True, slots=True)
class SessionTerminated(RealtimeEvent):
    state: SessionState
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in (SessionState.CLOSED, SessionState.FAILED):
            raise ValueError("terminal session state must be closed or failed")


class RealtimeCommand:
    """Marker base for commands accepted by a provider session."""


@dataclass(frozen=True, slots=True)
class AppendInputAudio(RealtimeCommand):
    data: bytes


@dataclass(frozen=True, slots=True)
class AddContext(RealtimeCommand):
    item_id: str
    text: str
    role: ContextRole = ContextRole.SYSTEM

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")


@dataclass(frozen=True, slots=True)
class RemoveContext(RealtimeCommand):
    item_id: str

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")


@dataclass(frozen=True, slots=True)
class StartResponse(RealtimeCommand):
    metadata: Mapping[str, str] = field(default_factory=dict)
    allow_tools: bool | None = None
    input: tuple[Mapping[str, Any], ...] | None = None
    conversation: str | None = None
    instructions: str | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))
        if self.conversation not in {None, "auto", "none"}:
            raise ValueError("Invalid response conversation")
        if self.conversation == "none" and (self.input is None or self.allow_tools is not False):
            raise ValueError("Isolated responses require explicit input and disabled tools")
        if self.input is not None:
            if (not isinstance(self.input, (list, tuple)) or len(self.input) > 128
                or any(not isinstance(item, Mapping) for item in self.input)):
                raise ValueError("Invalid response input")
            object.__setattr__(self, "input", tuple(_freeze_json(item) for item in self.input))
        if self.instructions is not None and (not isinstance(self.instructions, str)
                                             or len(self.instructions) > 16000):
            raise ValueError("Invalid response instructions")
        if self.max_output_tokens is not None and (type(self.max_output_tokens) is not int
                                                  or not 1 <= self.max_output_tokens <= 4096):
            raise ValueError("Invalid response token bound")


@dataclass(frozen=True, slots=True)
class CancelResponse(RealtimeCommand):
    response_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.response_id, "response_id", optional=True)


@dataclass(frozen=True, slots=True)
class TruncateOutput(RealtimeCommand):
    item_id: str
    audio_end_ms: int

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")
        if self.audio_end_ms < 0:
            raise ValueError("audio_end_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class SubmitToolResult(RealtimeCommand):
    call_id: str
    output: str
    item_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.call_id, "call_id")
        _identifier(self.item_id, "item_id", optional=True)


class RealtimeSessionError(RuntimeError):
    """Provider-neutral connection or transport failure."""


@runtime_checkable
class RealtimeSession(Protocol):
    """One connected provider session; implementations are async iterators."""

    state: SessionState

    async def connect(self, setup: SessionSetup) -> None: ...

    async def send(self, commands: Sequence[RealtimeCommand]) -> None: ...

    def __aiter__(self) -> AsyncIterator[RealtimeEvent]: ...

    async def close(self) -> None: ...


__all__ = [
    "MAX_IDENTIFIER_CHARS",
    "AddContext",
    "AppendInputAudio",
    "CancelResponse",
    "ContextRole",
    "FunctionCall",
    "InputAudioCommitted",
    "OutputAudio",
    "ProviderFailure",
    "RealtimeCommand",
    "RealtimeEvent",
    "RealtimeSemanticEagerness",
    "RealtimeSession",
    "RealtimeSessionError",
    "RealtimeTurnDetection",
    "RealtimeTurnDetectionMode",
    "RemoveContext",
    "ResponseFinished",
    "ResponseStarted",
    "SessionReady",
    "SessionSetup",
    "SessionState",
    "SessionTerminated",
    "SpeechStarted",
    "SpeechStopped",
    "StartResponse",
    "SubmitToolResult",
    "ToolCallsCancelled",
    "ToolDefinition",
    "Transcript",
    "TranscriptProvenance",
    "TranscriptRole",
    "TruncateOutput",
    "wire_value",
]
