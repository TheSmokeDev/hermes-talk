"""Provider-neutral Realtime contract tests.  Offline by construction."""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import talk_realtime as rt


class FakeProviderSession:
    """Small contract fake; no provider vocabulary and no network."""

    def __init__(self, events=()):
        self.state = rt.SessionState.NEW
        self.setup = None
        self.sent: list[tuple[rt.RealtimeCommand, ...]] = []
        self.events = list(events)
        self.closed = False

    async def connect(self, setup: rt.SessionSetup) -> None:
        self.state = rt.SessionState.CONNECTING
        self.setup = setup
        self.state = rt.SessionState.CONNECTED

    async def send(self, commands) -> None:
        self.sent.append(tuple(commands))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            self.state = rt.SessionState.CLOSED
            raise StopAsyncIteration
        return self.events.pop(0)

    async def close(self) -> None:
        self.closed = True
        self.state = rt.SessionState.CLOSED


def test_contract_is_structural_and_provider_neutral():
    session = FakeProviderSession()

    assert isinstance(session, rt.RealtimeSession)
    source = inspect.getsource(rt)
    for provider_wire_term in (
        "session.update",
        "response.create",
        "response.cancel",
        "input_audio_buffer",
        "conversation.item",
        "function_call_output",
    ):
        assert provider_wire_term not in source


def test_setup_events_and_commands_cover_one_ordinary_turn():
    tool = rt.ToolDefinition(
        name="search_memory",
        description="Search durable memory",
        parameters={"type": "object", "properties": {}},
    )
    setup = rt.SessionSetup(
        model="provider-model",
        voice="provider-voice",
        instructions="Be brief.",
        tools=(tool,),
        automatic_response=True,
    )

    events: tuple[rt.RealtimeEvent, ...] = (
        rt.SessionReady(session_id="session-1"),
        rt.SpeechStarted(input_id="input-1", offset_ms=0),
        rt.SpeechStopped(input_id="input-1", offset_ms=120),
        rt.InputAudioCommitted(input_id="input-1"),
        rt.Transcript(
            role=rt.TranscriptRole.USER,
            text="hello",
            final=True,
            provenance=rt.TranscriptProvenance.INPUT_AUDIO,
        ),
        rt.ResponseStarted(response_id="response-1", metadata={"scope": "one"}),
        rt.OutputAudio(data=b"pcm", item_id="output-1"),
        rt.Transcript(
            role=rt.TranscriptRole.ASSISTANT,
            text="hi",
            final=False,
            provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
        ),
        rt.ResponseFinished(response_id="response-1"),
        rt.SessionTerminated(state=rt.SessionState.CLOSED),
    )
    commands: tuple[rt.RealtimeCommand, ...] = (
        rt.AppendInputAudio(data=b"mic"),
        rt.StartResponse(metadata={"scope": "one"}),
        rt.CancelResponse(),
        rt.TruncateOutput(item_id="output-1", audio_end_ms=80),
        rt.SubmitToolResult(call_id="call-1", output="done"),
    )

    assert setup.tools == (tool,)
    assert all(isinstance(event, rt.RealtimeEvent) for event in events)
    assert all(isinstance(command, rt.RealtimeCommand) for command in commands)


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (lambda: rt.SessionReady(session_id=""), "session_id"),
        (lambda: rt.ResponseStarted(response_id=" padded "), "response_id"),
        (
            lambda: rt.FunctionCall(
                call_id="x" * 513,
                response_id="response-1",
                name="search_memory",
                arguments="{}",
            ),
            "call_id",
        ),
        (lambda: rt.SubmitToolResult(call_id="\t", output="no"), "call_id"),
        (lambda: rt.OutputAudio(data=b"pcm", response_id=" padded "), "response_id"),
        (
            lambda: rt.Transcript(
                role=rt.TranscriptRole.ASSISTANT,
                text="hi",
                final=False,
                provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
                response_id="",
            ),
            "response_id",
        ),
    ],
)
def test_malformed_provider_identifiers_fail_at_the_contract_boundary(factory, match):
    with pytest.raises(ValueError, match=match):
        factory()


@pytest.mark.parametrize(
    ("role", "provenance"),
    [
        (rt.TranscriptRole.USER, rt.TranscriptProvenance.OUTPUT_AUDIO),
        (rt.TranscriptRole.ASSISTANT, rt.TranscriptProvenance.INPUT_AUDIO),
    ],
)
def test_transcript_role_must_match_audio_provenance(role, provenance):
    with pytest.raises(ValueError, match=r"role.*provenance"):
        rt.Transcript(
            role=role,
            text="contradictory",
            final=True,
            provenance=provenance,
        )


def test_realtime_contract_and_openai_adapter_are_shipped_modules():
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    shipped = project["tool"]["setuptools"]["py-modules"]

    assert "talk_realtime" in shipped
    assert "talk_openai_realtime" in shipped
    assert "talk_core_realtime" in shipped


_CORE_SHIM_PROBE = r'''
import enum
import json
import sys
import types

module = types.ModuleType("agent.realtime_voice_provider")
module.REALTIME_VOICE_PROVIDER_API_VERSION = 2

class Capability(enum.Enum):
    INPUT_TRANSCRIPTION = "input_transcription"
    INPUT_COMMIT_EVENTS = "input_commit_events"
    EXPLICIT_RESPONSE = "explicit_response"
    RESPONSE_CANCELLATION = "response_cancellation"

class Role(enum.Enum):
    OPERATOR = "operator"

class Provenance(enum.Enum):
    OPERATOR_INPUT = "operator_input"

class Session:
    def __init__(self, capabilities): self.capabilities = capabilities; self._closed = False
    async def close(self): self._closed = True; await self._close()
    async def commit_audio(self): await self._commit_audio()

class Provider: pass
class Setup: pass
class Audio:
    def __init__(self, mime_type, sample_rate_hz, channels, *args, **kwargs):
        self.mime_type, self.sample_rate_hz, self.channels = mime_type, sample_rate_hz, channels

for name, value in {
    "RealtimeCapability": Capability,
    "TranscriptRole": Role,
    "TranscriptProvenance": Provenance,
    "RealtimeVoiceSession": Session,
    "RealtimeVoiceProvider": Provider,
    "RealtimeVoiceSetup": Setup,
    "RealtimeAudioFormat": Audio,
    "SessionReady": type("SessionReady", (), {}),
    "SessionClosed": type("SessionClosed", (), {}),
    "SessionFailure": type("SessionFailure", (), {}),
    "InputTranscript": type("InputTranscript", (), {}),
}.items(): setattr(module, name, value)

for name in sys.argv[1].split(",") if sys.argv[1] else ():
    setattr(module, name, type(name, (Audio,) if name.endswith("AudioFormat") else (), {}))

agent = types.ModuleType("agent")
agent.realtime_voice_provider = module
sys.modules["agent"] = agent
sys.modules["agent.realtime_voice_provider"] = module
import talk_core_realtime as core
print(json.dumps({
    "available": core.core_provider_available(),
    "explicit": core._EXPLICIT_RESPONSE_SURFACE_AVAILABLE,
    "cancellation": core._RESPONSE_CANCELLATION_SURFACE_AVAILABLE,
    "capabilities": sorted(item.value for item in core.CORE_CAPABILITIES),
}))
'''


def _probe_core_shim(symbols):
    completed = subprocess.run(
        [sys.executable, "-c", _CORE_SHIM_PROBE, ",".join(symbols)],
        cwd=Path.cwd(),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_optional_core_surfaces_are_detected_independently_and_stay_unadvertised():
    explicit = {
        "RealtimeInputAudioFormat",
        "RealtimeOutputAudioFormat",
        "RealtimeResponseRequest",
        "ResponseStarted",
        "OutputAudio",
        "OutputTranscript",
        "ResponseCompleted",
    }
    cancellation = {"InputSpeechStarted", "Interruption"}

    baseline = _probe_core_shim(set())
    assert baseline == {
        "available": True,
        "explicit": False,
        "cancellation": False,
        "capabilities": ["input_commit_events", "input_transcription"],
    }

    explicit_only = _probe_core_shim(explicit)
    assert explicit_only["available"] is True
    assert explicit_only["explicit"] is True
    assert explicit_only["cancellation"] is False
    assert "explicit_response" not in explicit_only["capabilities"]

    partial_cancellation = _probe_core_shim(explicit | {"InputSpeechStarted"})
    assert partial_cancellation["available"] is True
    assert partial_cancellation["explicit"] is True
    assert partial_cancellation["cancellation"] is False

    complete = _probe_core_shim(explicit | cancellation)
    assert complete["available"] is True
    assert complete["explicit"] is True
    assert complete["cancellation"] is True
    assert "response_cancellation" not in complete["capabilities"]


def test_output_events_carry_optional_response_identity():
    # Audio and transcript deltas name the response that produced them so a
    # cancelled response's tail can be told apart from the next answer's head.
    unattributed = rt.OutputAudio(data=b"pcm", item_id="output-1")
    attributed = rt.OutputAudio(data=b"pcm", item_id="output-1", response_id="response-1")
    user_speech = rt.Transcript(
        role=rt.TranscriptRole.USER,
        text="hello",
        final=True,
        provenance=rt.TranscriptProvenance.INPUT_AUDIO,
    )
    answer = rt.Transcript(
        role=rt.TranscriptRole.ASSISTANT,
        text="hi",
        final=False,
        provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
        response_id="response-1",
    )

    assert unattributed.response_id is None
    assert attributed.response_id == "response-1"
    assert unattributed != attributed
    # The operator's own speech belongs to no response.
    assert user_speech.response_id is None
    assert answer.response_id == "response-1"
