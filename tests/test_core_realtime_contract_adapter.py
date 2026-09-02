"""Hermes #95147 adapter tests; provider transport stays out of core."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("agent.realtime_voice", reason="Hermes #95147 contract is optional")
from agent.realtime_voice import (
    HeardAudioBoundary,
    RealtimeEventType,
    RealtimeVoiceProvider,
)
from agent.realtime_voice_coordinator import RealtimeVoiceCoordinator

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


def test_gemini_provider_uses_live_model_voice_and_registry_name(monkeypatch):
    harness = Harness()
    monkeypatch.setattr(core_v1.talk_config, "talk_provider", lambda: "gemini")
    monkeypatch.setattr(
        core_v1.talk_config,
        "talk_gemini_model",
        lambda: "gemini-live-test",
    )
    monkeypatch.setattr(core_v1.talk_config, "talk_gemini_voice", lambda: "Kore")
    provider = core_v1.TalkGeminiRealtimeProvider(
        auth_resolver=harness.auth,
        session_factory=harness.session_factory,
    )

    session = run(provider.open_session(instructions="Hermes owns policy", tools=[]))

    assert isinstance(session, core_v1.TalkRealtimeSession)
    assert provider.name == core_v1.GEMINI_PROVIDER_NAME
    assert harness.session.setup.model == "gemini-live-test"
    assert harness.session.setup.voice == "Kore"
    assert isinstance(core_v1.configured_provider(), core_v1.TalkGeminiRealtimeProvider)
    assert core_v1.configured_provider_name() == core_v1.GEMINI_PROVIDER_NAME


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


def test_cancel_is_not_sent_while_provider_is_idle_or_after_finish():
    harness = Harness(
        (
            rt.ResponseStarted(response_id="response-1"),
            rt.ResponseFinished(response_id="response-1"),
        )
    )
    session = core_v1.TalkRealtimeSession(harness.session)

    async def scenario():
        await session.cancel_response()
        events = session.events()
        await anext(events)
        await anext(events)
        await session.cancel_response()

    run(scenario())

    assert harness.session.sent == []


def test_cancelled_response_tail_and_late_finish_cannot_clear_new_response():
    harness = Harness(
        (
            rt.ResponseStarted(response_id="response-a"),
            rt.OutputAudio(data=b"stale", item_id="item-a", response_id="response-a"),
            rt.Transcript(
                role=rt.TranscriptRole.ASSISTANT,
                text="stale transcript",
                final=True,
                provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
                response_id="response-a",
            ),
            rt.ResponseStarted(response_id="response-b"),
            rt.ResponseFinished(response_id="response-a"),
            rt.OutputAudio(data=b"current", item_id="item-b", response_id="response-b"),
        )
    )
    session = core_v1.TalkRealtimeSession(harness.session)

    async def scenario():
        events = session.events()
        assert (await anext(events)).type is RealtimeEventType.TURN_STARTED
        await session.cancel_response()
        started_b = await anext(events)
        current_audio = await anext(events)
        await session.cancel_response()
        return started_b, current_audio

    started_b, current_audio = run(scenario())

    assert started_b.type is RealtimeEventType.TURN_STARTED
    assert current_audio.audio_bytes == b"current"
    assert harness.session.sent == [rt.CancelResponse(), rt.CancelResponse()]


def test_completed_tool_call_replay_is_ignored_without_sticking_pending():
    duplicate = rt.FunctionCall(
        call_id="call-1",
        name="weather",
        arguments="{}",
        response_id="response-1",
    )
    harness = Harness(
        (
            rt.ResponseStarted(response_id="response-1"),
            duplicate,
            rt.ResponseFinished(response_id="response-1"),
            duplicate,
        )
    )
    session = core_v1.TalkRealtimeSession(harness.session)

    async def scenario():
        events = session.events()
        for _ in range(3):
            await anext(events)
        await session.submit_tool_result("call-1", "sunny")
        return [event async for event in events]

    assert run(scenario()) == []
    assert session._pending_tool_calls == []
    assert harness.session.sent == [
        rt.SubmitToolResult(call_id="call-1", output="sunny"),
        rt.StartResponse(),
    ]


def test_unexpected_clean_provider_close_surfaces_terminal_error():
    harness = Harness((rt.SessionTerminated(state=rt.SessionState.CLOSED),))
    session = core_v1.TalkRealtimeSession(harness.session)

    async def collect():
        return [event async for event in session.events()]

    events = run(collect())

    assert len(events) == 1
    assert events[0].type is RealtimeEventType.ERROR
    assert events[0].text == "realtime voice provider closed unexpectedly"


def test_readiness_and_recoverable_failure_are_preserved_without_ending_session():
    harness = Harness(
        (
            rt.SessionReady(session_id="provider-session"),
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

    assert [event.type for event in events] == [
        RealtimeEventType.SESSION_READY,
        RealtimeEventType.WARNING,
        RealtimeEventType.TRANSCRIPT,
    ]
    assert events[0].session_id == "provider-session"
    assert [event.text for event in events[1:]] == ["bad audio chunk", "still here"]


def test_real_coordinator_delegates_one_client_tool_through_adapter():
    harness = Harness(
        (
            rt.ResponseStarted(response_id="response-1"),
            rt.FunctionCall(
                call_id="call-1",
                name="client_delegate",
                arguments='{"request":"search for current weather"}',
                response_id="response-1",
            ),
            rt.ResponseFinished(response_id="response-1"),
        )
    )
    adapter = core_v1.TalkRealtimeSession(harness.session)
    dispatched = []

    class Provider(RealtimeVoiceProvider):
        @property
        def name(self):
            return "test"

        @property
        def display_name(self):
            return "Test"

        def is_available(self):
            return True

        def get_setup_schema(self):
            return {}

        async def open_session(self, **_kwargs):
            return adapter

    async def dispatch(name, arguments):
        dispatched.append((name, arguments))
        return "72°F and clear"

    async def scenario():
        coordinator = RealtimeVoiceCoordinator(Provider(), dispatch_tool=dispatch)
        await coordinator.open(instructions="delegate", tools=[])
        observed = [event async for event in coordinator.events()]
        pending = tuple(coordinator._tool_tasks.values())
        if pending:
            await asyncio.gather(*pending)
        await coordinator.close()
        return observed

    observed = run(scenario())

    assert dispatched == [
        ("client_delegate", {"request": "search for current weather"})
    ]
    assert [event.type for event in observed] == [
        RealtimeEventType.TURN_STARTED,
        RealtimeEventType.TOOL_CALL,
        RealtimeEventType.TURN_ENDED,
    ]
    assert harness.session.sent == [
        rt.SubmitToolResult(call_id="call-1", output="72°F and clear"),
        rt.StartResponse(),
    ]



def test_invalid_tool_arguments_surface_error_without_dispatch():
    harness = Harness((rt.FunctionCall(call_id="call-1", name="weather", arguments="[]"),))
    session = core_v1.TalkRealtimeSession(harness.session)

    async def collect():
        return [event async for event in session.events()]

    events = run(collect())

    assert len(events) == 1
    assert events[0].type is RealtimeEventType.ERROR
    assert events[0].text == "invalid tool arguments: expected an object"
