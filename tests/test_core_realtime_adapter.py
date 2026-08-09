"""Hermes core API-v2 input-only adapter tests (no network)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

pytest.importorskip(
    "agent.realtime_voice_provider",
    reason="Hermes core API-v2 is optional for standalone Talk",
)
from agent.realtime_voice_provider import (
    InputTranscript,
    RealtimeAudioFormat,
    RealtimeCapability,
    RealtimeTool,
    RealtimeVoiceSetup,
    SessionClosed,
    SessionFailure,
    SessionReady,
    TranscriptProvenance,
    TranscriptRole,
)

import talk_core_realtime as core_rt


class FakeWire:
    def __init__(self, events=()):
        self.events = iter(events)
        self.connected: dict | None = None
        self.sent: list[dict] = []
        self.closed = False
        self.send_error: BaseException | None = None

    async def connect(self, **kwargs):
        self.connected = kwargs

    async def send_json(self, messages):
        if self.send_error is not None:
            raise self.send_error
        self.sent.extend(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            event = next(self.events)
        except StopIteration:
            raise core_rt.OpenAIWireEOF("") from None
        if isinstance(event, BaseException):
            raise event
        return event

    async def close(self):
        self.closed = True


class Harness:
    def __init__(self, events=()):
        self.auth_calls = 0
        self.wire_calls = 0
        self.wire = FakeWire(events)

    def resolve_auth(self):
        self.auth_calls += 1
        return type("Auth", (), {"token": "secret", "source": "test"})()

    def wire_factory(self, **kwargs):
        assert kwargs == {"auth_token": "secret", "auth_source": "test"}
        self.wire_calls += 1
        return self.wire

    def provider(self, *, ledger_capacity=1024):
        return core_rt.TalkOpenAIRealtimeProvider(
            auth_resolver=self.resolve_auth,
            wire_factory=self.wire_factory,
            ledger_capacity=ledger_capacity,
        )


def setup(**changes):
    values = {
        "model": "gpt-realtime-test",
        "voice": "cedar",
        "instructions": "transcribe only",
        "audio": core_rt.SUPPORTED_AUDIO_FORMAT,
    }
    values.update(changes)
    return RealtimeVoiceSetup(**values)


def collect(session):
    async def scenario():
        return [event async for event in session.events()]

    return asyncio.run(scenario())


def test_exact_api_v2_contract_and_fixed_capabilities():
    expected = frozenset(
        {
            RealtimeCapability.INPUT_TRANSCRIPTION,
            RealtimeCapability.INPUT_COMMIT_EVENTS,
        }
    )

    assert core_rt.core_provider_available() is True
    provider = Harness().provider()
    assert provider.name == "talk_openai_realtime"
    assert provider.api_version == 2
    assert provider.capabilities == expected

    async def scenario():
        session = await provider.open_session(setup())
        try:
            assert session.capabilities == expected
        finally:
            await session.close()

    asyncio.run(scenario())


def test_passive_diagnostic_distinguishes_contract_from_provider_readiness(monkeypatch):
    class UnavailableProvider:
        def is_available(self):
            return False

    monkeypatch.setattr(core_rt, "TalkOpenAIRealtimeProvider", UnavailableProvider)
    monkeypatch.setattr(core_rt, "core_provider_available", lambda: True)

    assert core_rt.core_provider_diagnostic() == {
        "contract_available": True,
        "provider_available": False,
    }

    class ConstructionTrap:
        def __init__(self):
            raise AssertionError("provider must not be constructed without the core contract")

    monkeypatch.setattr(core_rt, "TalkOpenAIRealtimeProvider", ConstructionTrap)
    monkeypatch.setattr(core_rt, "core_provider_available", lambda: False)
    assert core_rt.core_provider_diagnostic() == {
        "contract_available": False,
        "provider_available": False,
    }


@pytest.mark.parametrize(
    "bad_setup",
    [
        setup(
            tools=(RealtimeTool(name="forbidden", description="must stay inert", parameters={}),)
        ),
        setup(provider_options={"automatic_response": True}),
        setup(provider_options={"capabilities": ["tool_calling"]}),
        setup(audio=RealtimeAudioFormat("audio/pcm", 16_000, 1)),
        setup(audio=RealtimeAudioFormat("audio/wav", 24_000, 1)),
    ],
)
def test_unsupported_setup_is_rejected_before_credentials_or_network(bad_setup):
    harness = Harness()

    async def scenario():
        with pytest.raises((TypeError, ValueError)):
            await harness.provider().open_session(bad_setup)

    asyncio.run(scenario())
    assert harness.auth_calls == 0
    assert harness.wire_calls == 0


def test_core_connect_disables_response_in_update_and_sends_only_input_commands():
    harness = Harness()

    async def scenario():
        session = await harness.provider().open_session(setup())
        await session.send_audio(bytearray(b"pcm"), mime_type="audio/pcm")
        await session.commit_audio()
        await session.close()

    asyncio.run(scenario())

    assert harness.wire.connected is not None
    assert harness.wire.connected["automatic_response"] is False
    update = harness.wire.connected["session_update"]
    assert update["session"]["audio"]["input"]["turn_detection"]["create_response"] is False
    assert "tools" not in update["session"]
    assert harness.wire.sent == [
        {"type": "input_audio_buffer.append", "audio": "cGNt"},
        {"type": "input_audio_buffer.commit"},
    ]
    assert harness.wire.closed is True


def test_send_failure_closes_and_remains_visible_as_one_terminal_event():
    harness = Harness()
    harness.wire.send_error = core_rt.OpenAIWireError("send exploded")

    async def scenario():
        session = await harness.provider().open_session(setup())
        with pytest.raises(core_rt.OpenAIWireError, match="send exploded"):
            await session.send_audio(b"pcm")
        return [event async for event in session.events()]

    events = asyncio.run(scenario())

    assert harness.wire.closed is True
    assert len(events) == 1
    assert isinstance(events[0], SessionFailure)
    assert "send exploded" in events[0].message


def test_send_failure_remains_visible_when_event_pump_is_already_waiting():
    class BlockingWire(FakeWire):
        def __init__(self):
            super().__init__()
            self.receive_started = asyncio.Event()
            self.receive_released = asyncio.Event()

        async def __anext__(self):
            self.receive_started.set()
            await self.receive_released.wait()
            raise core_rt.OpenAIWireEOF("")

        async def close(self):
            self.closed = True
            self.receive_released.set()

    harness = Harness()
    harness.wire = BlockingWire()
    harness.wire.send_error = core_rt.OpenAIWireError("concurrent send exploded")

    async def scenario():
        session = await harness.provider().open_session(setup())
        pump = asyncio.create_task(_collect_async(session))
        await harness.wire.receive_started.wait()
        with pytest.raises(core_rt.OpenAIWireError, match="concurrent send exploded"):
            await session.send_audio(b"pcm")
        return await pump

    async def _collect_async(session):
        return [event async for event in session.events()]

    events = asyncio.run(scenario())

    assert harness.wire.closed is True
    assert len(events) == 1
    assert isinstance(events[0], SessionFailure)
    assert events[0].code == "provider_send_failure"
    assert "concurrent send exploded" in events[0].message


def test_exact_input_item_identity_maps_partial_and_final_operator_transcripts():
    events = [
        {"type": "session.created", "session": {"id": "session-1"}},
        {"type": "session.created", "session": {"id": "session-1"}},
        {"type": "input_audio_buffer.committed", "item_id": "item-1"},
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "item-1",
            "delta": "hel",
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-1",
            "transcript": "hello",
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-1",
            "transcript": "hello",
        },
    ]
    harness = Harness(events)

    async def scenario():
        session = await harness.provider().open_session(setup())
        return session, [event async for event in session.events()]

    session, received = asyncio.run(scenario())

    assert received[0] == SessionReady(session_id="session-1")
    transcripts = [event for event in received if isinstance(event, InputTranscript)]
    assert [(event.text, event.final) for event in transcripts] == [
        ("hel", False),
        ("hello", True),
        ("hello", True),
    ]
    assert all(event.item_id == event.turn_id == "item-1" for event in transcripts)
    assert all(event.role is TranscriptRole.OPERATOR for event in transcripts)
    assert all(event.provenance is TranscriptProvenance.OPERATOR_INPUT for event in transcripts)
    assert received[-1] == SessionClosed()
    assert harness.wire.closed is True
    assert not session._input_ledger


@pytest.mark.parametrize(
    "events,match",
    [
        (
            [
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "item-1",
                    "transcript": "hello",
                },
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "item-1",
                    "transcript": "changed",
                },
            ],
            "conflicting terminal transcript",
        ),
        ([{"type": "response.created", "response": {"id": "response-1"}}], "unsolicited"),
        (
            [{"type": "response.function_call_arguments.done", "call_id": "call-1"}],
            "unsolicited",
        ),
        (
            [
                {
                    "type": "conversation.item.created",
                    "item": {"id": "call-item", "type": "function_call"},
                }
            ],
            "unsolicited",
        ),
        (
            [
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": " padded ",
                    "transcript": "hello",
                }
            ],
            "item_id",
        ),
        (
            [
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "item-1",
                    "transcript": "hello",
                    "final": "yes",
                }
            ],
            "finality",
        ),
    ],
)
def test_protocol_violations_emit_one_terminal_failure_and_close(events, match):
    harness = Harness(events)

    async def scenario():
        session = await harness.provider().open_session(setup())
        return [event async for event in session.events()]

    received = asyncio.run(scenario())
    failures = [event for event in received if isinstance(event, SessionFailure)]
    assert len(failures) == 1
    assert match in failures[0].message
    assert harness.wire.closed is True


def test_input_ledger_capacity_is_non_evicting_and_terminal():
    events = [
        {"type": "input_audio_buffer.committed", "item_id": "item-1"},
        {"type": "input_audio_buffer.committed", "item_id": "item-2"},
    ]
    harness = Harness(events)

    async def scenario():
        session = await harness.provider(ledger_capacity=1).open_session(setup())
        return [event async for event in session.events()]

    received = asyncio.run(scenario())
    assert len(received) == 1
    assert isinstance(received[0], SessionFailure)
    assert "capacity" in received[0].message
    assert harness.wire.closed is True


def test_provider_data_is_diagnostic_only_and_frozen():
    harness = Harness(
        [
            {"type": "session.created", "session": {"id": "session-1"}},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "item-1",
                "transcript": "hello",
                "diagnostic": {"region": "test"},
            },
        ]
    )

    async def scenario():
        session = await harness.provider().open_session(setup())
        return [event async for event in session.events()]

    transcript = next(
        event for event in asyncio.run(scenario()) if isinstance(event, InputTranscript)
    )
    assert isinstance(transcript.provider_data, Mapping)
    assert not transcript.provider_data
