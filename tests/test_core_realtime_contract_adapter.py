"""Hermes #95147 adapter tests; provider transport stays out of core."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("agent.realtime_voice", reason="Hermes #95147 contract is optional")
from agent.realtime_voice import HeardAudioBoundary, RealtimeEventType

import talk_core_realtime_contract as core_v1
import talk_realtime as rt


class FakeSession:
    def __init__(self, events=(), **kwargs):
        self.events = iter(events)
        self.init = kwargs
        self.setup = None
        self.sent = []
        self.closed = 0

    async def connect(self, setup):
        self.setup = setup

    async def send(self, commands):
        self.sent.extend(commands)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.events)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self):
        self.closed += 1


class Harness:
    def __init__(self, events=()):
        self.session = FakeSession(events)

    def auth(self):
        return type("Auth", (), {"token": "secret", "source": "test"})()

    def session_factory(self, **kwargs):
        self.session.init = kwargs
        return self.session

    def provider(self):
        return core_v1.TalkOpenAIRealtimeProvider(
            auth_resolver=self.auth,
            session_factory=self.session_factory,
        )


def run(coro):
    return asyncio.run(coro)


def test_provider_opens_plugin_transport_with_hermes_setup(monkeypatch):
    harness = Harness()
    monkeypatch.setattr(core_v1.talk_config, "talk_model", lambda: "gpt-realtime-test")
    monkeypatch.setattr(core_v1.talk_config, "talk_voice", lambda: "cedar")

    session = run(
        harness.provider().open_session(
            instructions="Hermes owns policy",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "description": "Read weather",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )
    )

    assert isinstance(session, core_v1.TalkRealtimeSession)
    assert harness.session.init == {"auth_token": "secret", "auth_source": "test"}
    assert harness.session.setup.model == "gpt-realtime-test"
    assert harness.session.setup.voice == "cedar"
    assert harness.session.setup.instructions == "Hermes owns policy"
    assert harness.session.setup.tools[0].name == "weather"
    assert harness.session.setup.automatic_response is True


def test_grok_provider_uses_xai_model_voice_and_registry_name(monkeypatch):
    harness = Harness()
    monkeypatch.setattr(core_v1.talk_config, "talk_provider", lambda: "grok")
    monkeypatch.setattr(core_v1.talk_config, "talk_grok_model", lambda: "grok-voice-test")
    monkeypatch.setattr(core_v1.talk_config, "talk_grok_voice", lambda: "ara")
    provider = core_v1.TalkGrokRealtimeProvider(
        auth_resolver=harness.auth,
        session_factory=harness.session_factory,
    )

    session = run(provider.open_session(instructions="Hermes owns policy", tools=[]))

    assert isinstance(session, core_v1.TalkRealtimeSession)
    assert provider.name == core_v1.GROK_PROVIDER_NAME
    assert harness.session.setup.model == "grok-voice-test"
    assert harness.session.setup.voice == "ara"
    assert isinstance(core_v1.configured_provider(), core_v1.TalkGrokRealtimeProvider)
    assert core_v1.configured_provider_name() == core_v1.GROK_PROVIDER_NAME


def test_grok_provider_uses_supported_oauth_resolver_by_default(monkeypatch):
    harness = Harness()
    monkeypatch.setattr(
        core_v1.talk_grok_auth,
        "resolve_grok_auth",
        lambda: type("Auth", (), {"token": "oauth-token", "source": "xai-oauth"})(),
    )
    monkeypatch.setattr(core_v1.talk_config, "talk_grok_model", lambda: "grok-voice-test")
    monkeypatch.setattr(core_v1.talk_config, "talk_grok_voice", lambda: "ara")
    provider = core_v1.TalkGrokRealtimeProvider(
        session_factory=harness.session_factory,
    )

    run(provider.open_session(instructions="Hermes owns policy", tools=[]))

    assert harness.session.init == {
        "auth_token": "oauth-token",
        "auth_source": "xai-oauth",
    }

def test_session_maps_audio_transcript_turns_and_tool_calls():
    harness = Harness(
        (
            rt.SpeechStarted(input_id="input-1"),
            rt.Transcript(
                role=rt.TranscriptRole.USER,
                text="hello",
                final=True,
                provenance=rt.TranscriptProvenance.INPUT_AUDIO,
            ),
            rt.OutputAudio(data=b"pcm", item_id="item-1", response_id="response-1"),
            rt.FunctionCall(call_id="call-1", name="weather", arguments='{"city":"Paris"}'),
            rt.ResponseFinished(response_id="response-1"),
        )
    )
    session = core_v1.TalkRealtimeSession(harness.session)

    async def collect():
        return [event async for event in session.events()]

    events = run(collect())

    assert [event.role for event in events] == ["user", "user", None, None, "assistant"]
    assert [event.type for event in events] == [
        RealtimeEventType.TURN_STARTED,
        RealtimeEventType.TRANSCRIPT,
        RealtimeEventType.AUDIO,
        RealtimeEventType.TOOL_CALL,
        RealtimeEventType.TURN_ENDED,
    ]
    assert events[2].audio_bytes == b"pcm"
    assert events[2].item_id == "item-1"
    assert events[3].arguments == {"city": "Paris"}


def test_session_commands_preserve_host_authority_and_barge_in_boundary():
    harness = Harness(
        (
            rt.ResponseStarted(response_id="response-1"),
            rt.FunctionCall(call_id="call-1", name="weather", arguments="{}"),
            rt.ResponseFinished(response_id="response-1"),
        )
    )
    session = core_v1.TalkRealtimeSession(harness.session)

    async def scenario():
        await session.send_audio(b"mic")
        events = session.events()
        await anext(events)
        await anext(events)
        await session.submit_tool_result("call-1", "sunny")
        assert harness.session.sent == [rt.AppendInputAudio(b"mic")]
        await anext(events)
        await session.truncate_response(HeardAudioBoundary("item-1", 420))
        await session.add_context("progress-1", "Checked the tests.")
        await session.cancel_response()
        await session.close()
        await session.close()

    run(scenario())

    assert harness.session.sent == [
        rt.AppendInputAudio(b"mic"),
        rt.SubmitToolResult(call_id="call-1", output="sunny"),
        rt.StartResponse(),
        rt.TruncateOutput(item_id="item-1", audio_end_ms=420),
        rt.AddContext(
            item_id="progress-1",
            text="Checked the tests.",
            role=rt.ContextRole.SYSTEM,
        ),
    ]
    assert harness.session.closed == 1


def test_tool_results_batch_in_call_order_after_response_finishes():
    harness = Harness(
        (
            rt.ResponseStarted(response_id="response-1"),
            rt.FunctionCall(call_id="call-1", name="first", arguments="{}"),
            rt.FunctionCall(call_id="call-2", name="second", arguments="{}"),
            rt.ResponseFinished(response_id="response-1"),
        )
    )
    session = core_v1.TalkRealtimeSession(harness.session)

    async def scenario():
        events = session.events()
        for _ in range(4):
            await anext(events)
        await session.submit_tool_result("call-2", "second result")
        assert harness.session.sent == []
        await session.submit_tool_result("call-1", "first result")

    run(scenario())

    assert harness.session.sent == [
        rt.SubmitToolResult(call_id="call-1", output="first result"),
        rt.SubmitToolResult(call_id="call-2", output="second result"),
        rt.StartResponse(),
    ]


def test_cancel_is_sent_only_while_provider_response_is_active():
    harness = Harness((rt.ResponseStarted(response_id="response-1"),))
    session = core_v1.TalkRealtimeSession(harness.session)

    async def scenario():
        await anext(session.events())
        await session.cancel_response()

    run(scenario())
    assert harness.session.sent == [rt.CancelResponse()]


def test_recoverable_provider_failure_does_not_end_the_session():
    harness = Harness(
        (
            rt.ProviderFailure(detail="bad audio chunk", terminal=False),
            rt.Transcript(
                role=rt.TranscriptRole.USER,
                text="still here",
                final=True,
                provenance=rt.TranscriptProvenance.INPUT_AUDIO,
            ),
        )
    )
    session = core_v1.TalkRealtimeSession(harness.session)

    async def collect():
        return [event async for event in session.events()]

    events = run(collect())

    assert [event.text for event in events] == ["still here"]


def test_invalid_tool_arguments_surface_error_without_dispatch():
    harness = Harness((rt.FunctionCall(call_id="call-1", name="weather", arguments="[]"),))
    session = core_v1.TalkRealtimeSession(harness.session)

    async def collect():
        return [event async for event in session.events()]

    events = run(collect())

    assert len(events) == 1
    assert events[0].type is RealtimeEventType.ERROR
    assert events[0].text == "invalid tool arguments: expected an object"
