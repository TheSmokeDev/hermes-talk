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


class SessionState(StrEnum):
    """Observable lifecycle of a provider session."""

    NEW = "new"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    CLOSED = "closed"
    FAILED = "failed"


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

    def __post_init__(self) -> None:
        _identifier(self.model, "model")
        _identifier(self.voice, "voice")
        object.__setattr__(self, "tools", tuple(self.tools))


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

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id", optional=True)


@dataclass(frozen=True, slots=True)
class Transcript(RealtimeEvent):
    role: TranscriptRole
    text: str
    final: bool
    provenance: TranscriptProvenance

    def __post_init__(self) -> None:
        expected_role = {
            TranscriptProvenance.INPUT_AUDIO: TranscriptRole.USER,
            TranscriptProvenance.OUTPUT_AUDIO: TranscriptRole.ASSISTANT,
        }.get(self.provenance)
        if self.role is not expected_role:
            raise ValueError("Transcript role must match its audio provenance")


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
class ResponseFinished(RealtimeEvent):
    response_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.response_id, "response_id", optional=True)


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class CancelResponse(RealtimeCommand):
    pass


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

    def __post_init__(self) -> None:
        _identifier(self.call_id, "call_id")


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
    "RealtimeSession",
    "RealtimeSessionError",
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
    "ToolDefinition",
    "Transcript",
    "TranscriptProvenance",
    "TranscriptRole",
    "TruncateOutput",
]
