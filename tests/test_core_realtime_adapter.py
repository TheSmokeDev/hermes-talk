"""Hermes core API-v2 input-only adapter tests (no network)."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import sys
import types
from collections.abc import Mapping

import pytest

pytest.importorskip(
    "agent.realtime_voice_provider",
    reason="Hermes core API-v2 is optional for standalone Talk",
)
from agent.realtime_voice_provider import (
    InputSpeechStarted,
    InputTranscript,
    Interruption,
    OutputAudio,
    OutputTranscript,
    RealtimeAudioFormat,
    RealtimeCapability,
    RealtimeInputAudioFormat,
    RealtimeOutputAudioFormat,
    RealtimeResponseRequest,
    RealtimeTool,
    RealtimeVoiceSetup,
    ResponseCompleted,
    ResponseStarted,
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

    def provider(
        self, *, ledger_capacity=1024, token_factory=None, response_ledger_capacity=1024
    ):
        kwargs = {}
        if token_factory is not None:
            kwargs["token_factory"] = token_factory
        return core_rt.TalkOpenAIRealtimeProvider(
            auth_resolver=self.resolve_auth,
            wire_factory=self.wire_factory,
            ledger_capacity=ledger_capacity,
            response_ledger_capacity=response_ledger_capacity,
            **kwargs,
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


def response_request(**changes):
    text = changes.pop("canonical_text", "Exact words, exactly.")
    values = {
        "durable_session_id": "durable-1",
        "assistant_message_id": 41,
        "turn_marker": "turn-41",
        "canonical_text": text,
        "content_digest": hashlib.sha256(text.encode()).hexdigest(),
        "output_audio_format": core_rt.SUPPORTED_OUTPUT_AUDIO_FORMAT,
        "allow_tools": False,
    }
    values.update(changes)
    return RealtimeResponseRequest(**values)


def collect(session):
    async def scenario():
        return [event async for event in session.events()]

    return asyncio.run(scenario())


def bind_response(session, correlation, *, response_id="resp-1"):
    return session._map_event(
        {
            "type": "response.created",
            "response": {"id": response_id, "metadata": {"correlation": correlation}},
        }
    )


def response_done_event(
    correlation,
    *,
    response_id="resp-1",
    item_id="item-1",
    transcript="words",
    output=None,
):
    if output is None:
        output = [
            {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_audio", "transcript": transcript}],
            }
        ]
    return {
        "type": "response.done",
        "response": {
            "id": response_id,
            "status": "completed",
            "metadata": {"correlation": correlation},
            "output": output,
        },
    }


def complete_bound_response(
    session,
    correlation,
    *,
    response_id="resp-1",
    item_id="item-1",
    transcript="Exact words, exactly.",
):
    events = valid_output_lifecycle_events(
        correlation,
        response_id=response_id,
        item_id=item_id,
        transcript=transcript,
    )
    for event in events[:-1]:
        session._map_event(event)
    return session._map_event(events[-1])


def valid_output_lifecycle_events(
    correlation="token-1", *, response_id="resp-1", item_id="item-1", transcript="words"
):
    return [
        {
            "type": "response.output_item.added",
            "response_id": response_id,
            "item": {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "response_id": response_id,
            "item_id": item_id,
            "part": {"type": "output_audio"},
        },
        {
            "type": "response.output_audio.delta",
            "response_id": response_id,
            "item_id": item_id,
            "delta": "cGNtAA==",
        },
        {
            "type": "response.output_audio_transcript.delta",
            "response_id": response_id,
            "item_id": item_id,
            "delta": transcript[:1],
        },
        {
            "type": "response.output_audio.done",
            "response_id": response_id,
            "item_id": item_id,
        },
        {
            "type": "response.output_audio_transcript.done",
            "response_id": response_id,
            "item_id": item_id,
            "transcript": transcript,
        },
        {
            "type": "response.content_part.done",
            "response_id": response_id,
            "item_id": item_id,
            "part": {"type": "output_audio", "transcript": transcript},
        },
        {
            "type": "response.output_item.done",
            "response_id": response_id,
            "item": {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_audio", "transcript": transcript}],
            },
        },
        {
            "type": "conversation.item.done",
            "item": {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_audio", "transcript": transcript}],
            },
        },
        response_done_event(
            correlation,
            response_id=response_id,
            item_id=item_id,
            transcript=transcript,
        ),
    ]


def test_exact_api_v2_contract_and_fixed_capabilities():
    expected = frozenset(
        {
            RealtimeCapability.INPUT_TRANSCRIPTION,
            RealtimeCapability.INPUT_COMMIT_EVENTS,
            RealtimeCapability.EXPLICIT_RESPONSE,
            RealtimeCapability.RESPONSE_METADATA_ECHO,
            RealtimeCapability.OUTPUT_TRANSCRIPTION,
            RealtimeCapability.RESPONSE_CANCELLATION,
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


def test_explicit_output_format_is_exact_pcm_s16le():
    assert RealtimeOutputAudioFormat(
        mime_type="audio/pcm",
        sample_rate_hz=24_000,
        channels=1,
        sample_encoding="pcm_s16le",
        sample_width_bytes=2,
        endianness="little",
    ) == core_rt.SUPPORTED_OUTPUT_AUDIO_FORMAT


def test_explicit_response_sends_exact_causally_opaque_no_tool_payload():
    harness = Harness()

    async def scenario():
        session = await harness.provider(token_factory=lambda: "opaque-token-1").open_session(
            setup(output_audio=core_rt.SUPPORTED_OUTPUT_AUDIO_FORMAT)
        )
        await session.start_response(response_request())
        await session.close()

    asyncio.run(scenario())
    message = harness.wire.sent[0]
    assert message.pop("event_id").startswith("evt-")
    assert message == {
        "type": "response.create",
        "response": {
            "conversation": "none",
            "metadata": {"correlation": "opaque-token-1"},
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Exact words, exactly."}],
                }
            ],
            "instructions": (
                "Render the sole user input as speech verbatim. Speak every character's "
                "words and punctuation naturally, but add, remove, or paraphrase nothing. "
                "Do not preface or explain."
            ),
            "output_modalities": ["audio"],
            "audio": {
                "output": {
                    "voice": "cedar",
                    "format": {"type": "audio/pcm", "rate": 24000},
                }
            },
            "tools": [],
            "tool_choice": "none",
        },
    }
    assert not {
        "durable_session_id",
        "assistant_message_id",
        "turn_marker",
        "content_digest",
    }.intersection(message["response"]["metadata"])


def test_output_format_mismatch_is_rejected_before_provider_send():
    harness = Harness()
    wrong = RealtimeOutputAudioFormat(
        mime_type="audio/pcm",
        sample_rate_hz=16_000,
        channels=1,
        sample_encoding="pcm_s16le",
        sample_width_bytes=2,
        endianness="little",
    )

    async def scenario():
        session = await harness.provider(token_factory=lambda: "unused").open_session(setup())
        with pytest.raises(ValueError, match="output audio format"):
            await session.start_response(response_request(output_audio_format=wrong))

    asyncio.run(scenario())
    assert harness.wire.sent == []


def test_bound_response_maps_started_audio_transcript_and_completed():
    correlation = "opaque-bound-token"
    events = [
        {
            "type": "response.created",
            "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
        },
        {
            "type": "response.output_item.added",
            "response_id": "resp-1",
            "item": {
                "id": "item-out-1",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "response_id": "resp-1",
            "item_id": "item-out-1",
            "part": {"type": "output_audio"},
        },
        {
            "type": "response.output_audio.delta",
            "response_id": "resp-1",
            "item_id": "item-out-1",
            "delta": "cGNtAA==",
        },
        {
            "type": "response.output_audio_transcript.delta",
            "response_id": "resp-1",
            "item_id": "item-out-1",
            "delta": "Exact ",
        },
        {
            "type": "response.output_audio.done",
            "response_id": "resp-1",
            "item_id": "item-out-1",
        },
        {
            "type": "response.output_audio_transcript.done",
            "response_id": "resp-1",
            "item_id": "item-out-1",
            "transcript": "Exact words, exactly.",
        },
        {
            "type": "response.content_part.done",
            "response_id": "resp-1",
            "item_id": "item-out-1",
            "part": {"type": "output_audio", "transcript": "Exact words, exactly."},
        },
        {
            "type": "response.output_item.done",
            "response_id": "resp-1",
            "item": {
                "id": "item-out-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_audio", "transcript": "Exact words, exactly."}],
            },
        },
        {
            "type": "conversation.item.done",
            "item": {
                "id": "item-out-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_audio", "transcript": "Exact words, exactly."}],
            },
        },
        {
            "type": "response.done",
            "response": {
                "id": "resp-1",
                "status": "completed",
                "metadata": {"correlation": correlation},
                "output": [
                    {
                        "id": "item-out-1",
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_audio", "transcript": "Exact words, exactly."}
                        ],
                    }
                ],
            },
        },
    ]
    harness = Harness(events)

    async def scenario():
        session = await harness.provider(token_factory=lambda: correlation).open_session(setup())
        await session.start_response(response_request())
        return session, [event async for event in session.events()]

    session, received = asyncio.run(scenario())
    assert received[:-1] == [
        ResponseStarted(response_id="resp-1", turn_id="turn-41"),
        OutputAudio(
            data=b"pcm\x00", item_id="item-out-1", response_id="resp-1", turn_id="turn-41"
        ),
        OutputTranscript(
            item_id="item-out-1",
            response_id="resp-1",
            turn_id="turn-41",
            text="Exact ",
            final=False,
        ),
        OutputTranscript(
            item_id="item-out-1",
            response_id="resp-1",
            turn_id="turn-41",
            text="Exact words, exactly.",
            final=True,
        ),
        ResponseCompleted(response_id="resp-1", turn_id="turn-41"),
    ]
    assert received[-1] == SessionClosed()
    assert not session._pending_responses
    assert not session._active_responses
    assert not session._completed_responses


def test_pcm16le_arbitrary_chunks_use_exact_one_byte_carry_and_clean_terminals():
    async def open_bound_session():
        harness = Harness()
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        bind_response(session, "token-1")
        session._enforce_output_lifecycle = True
        lifecycle = valid_output_lifecycle_events(
            "token-1", transcript="Exact words, exactly."
        )
        session._map_event(lifecycle[0])
        session._map_event(lifecycle[1])
        return harness, session, lifecycle

    def audio_delta(data, *, response_id="resp-1", item_id="item-1"):
        encoded = {
            b"\x01": "AQ==",
            b"\x02\x03\x04": "AgME",
            b"\x05": "BQ==",
            b"\x06": "Bg==",
            b"\x07": "Bw==",
        }[data]
        return {
            "type": "response.output_audio.delta",
            "response_id": response_id,
            "item_id": item_id,
            "delta": encoded,
        }

    async def scenario():
        _harness, session, lifecycle = await open_bound_session()
        assert session._map_event(audio_delta(b"\x01")) is None
        assert session._audio_byte_carries == {("resp-1", "item-1"): b"\x01"}
        assert max(map(len, session._audio_byte_carries.values())) == 1

        before_wrong_id = dict(session._audio_byte_carries)
        with pytest.raises(ValueError, match="identity"):
            session._map_event(audio_delta(b"\x07", item_id="item-wrong"))
        assert session._audio_byte_carries == before_wrong_id

        reconstructed = session._map_event(audio_delta(b"\x02\x03\x04"))
        assert reconstructed == OutputAudio(
            data=b"\x01\x02\x03\x04",
            item_id="item-1",
            response_id="resp-1",
            turn_id="turn-41",
        )
        assert not session._audio_byte_carries

        assert session._map_event(audio_delta(b"\x05")) is None
        before_odd_done = (
            dict(session._audio_byte_carries),
            set(session._audio_done_items),
            dict(session._response_lifecycle),
        )
        with pytest.raises(ValueError, match="incomplete PCM16LE sample"):
            session._map_event(lifecycle[4])
        assert (
            dict(session._audio_byte_carries),
            set(session._audio_done_items),
            dict(session._response_lifecycle),
        ) == before_odd_done

        assert session._map_event(audio_delta(b"\x06")) == OutputAudio(
            data=b"\x05\x06",
            item_id="item-1",
            response_id="resp-1",
            turn_id="turn-41",
        )
        session._map_event(lifecycle[3])
        assert session._map_event(lifecycle[4]) is None
        with pytest.raises(ValueError, match="replayed"):
            session._map_event(lifecycle[4])
        for event in lifecycle[5:-1]:
            session._map_event(event)
        assert session._map_event(lifecycle[-1]) == ResponseCompleted(
            response_id="resp-1", turn_id="turn-41"
        )
        assert not session._audio_byte_carries

        _cancel_harness, cancel_session, _cancel_lifecycle = await open_bound_session()
        assert cancel_session._map_event(audio_delta(b"\x07")) is None
        await cancel_session.cancel_response("resp-1")
        assert cancel_session._map_event(
            {
                "type": "response.done",
                "response": {
                    "id": "resp-1",
                    "status": "cancelled",
                    "metadata": {"correlation": "token-1"},
                    "output": [],
                    "status_details": {
                        "type": "cancelled",
                        "reason": "client_cancelled",
                    },
                },
            }
        ) == Interruption(response_id="resp-1", turn_id="turn-41")
        assert not cancel_session._audio_byte_carries

        _close_harness, close_session, _close_lifecycle = await open_bound_session()
        assert close_session._map_event(audio_delta(b"\x07")) is None
        await close_session.close()
        assert not close_session._audio_byte_carries

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "bad_event,match",
    [
        (
            {
                "type": "response.created",
                "response": {"id": "resp-1", "metadata": {"correlation": "unknown"}},
            },
            "correlation",
        ),
        (
            {
                "type": "response.output_audio.delta",
                "response_id": "resp-wrong",
                "item_id": "item-1",
                "delta": "cGNt",
            },
            "response",
        ),
        (
            {
                "type": "response.output_audio.delta",
                "response_id": "resp-1",
                "item_id": "item-1",
                "delta": "%%%",
            },
            "base64",
        ),
        (
            {"type": "response.function_call_arguments.done", "response_id": "resp-1"},
            "function",
        ),
        (
            {
                "type": "response.done",
                "response": {
                    "id": "resp-1",
                    "status": "failed",
                    "metadata": {"correlation": "opaque-bad-token"},
                },
            },
            "completed",
        ),
        (
            {
                "type": "response.done",
                "response": {
                    "id": "resp-1",
                    "status": "completed",
                    "metadata": {"correlation": "opaque-bad-token"},
                    "output": [{"id": "wrong-item", "type": "message"}],
                },
            },
            "item",
        ),
    ],
)
def test_explicit_output_protocol_violations_fail_closed(bad_event, match):
    correlation = "opaque-bad-token"
    created = {
        "type": "response.created",
        "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
    }
    if bad_event["type"] == "response.created":
        events = [bad_event]
    elif match == "item":
        events = [created, *valid_output_lifecycle_events(correlation)[:-1], bad_event]
    elif match == "base64":
        events = [
            created,
            *valid_output_lifecycle_events(correlation)[:2],
            bad_event,
        ]
    else:
        events = [created, bad_event]
    harness = Harness(events)

    async def scenario():
        session = await harness.provider(token_factory=lambda: correlation).open_session(setup())
        await session.start_response(response_request())
        return [event async for event in session.events()]

    received = asyncio.run(scenario())
    assert isinstance(received[-1], SessionFailure)
    assert received[-1].message == "provider protocol failure"
    assert harness.wire.closed is True


def test_replayed_correlation_and_changed_item_identity_fail_closed():
    correlation = "one-time-token"
    created = {
        "type": "response.created",
        "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
    }
    replay_events = [
        created,
        {
            "type": "response.created",
            "response": {"id": "resp-2", "metadata": {"correlation": correlation}},
        },
    ]
    wrong_item_events = [
        created,
        *valid_output_lifecycle_events(correlation)[:2],
        {
            "type": "response.output_audio.delta",
            "response_id": "resp-1",
            "item_id": "item-1",
            "delta": "cGNt",
        },
        {
            "type": "response.output_audio.delta",
            "response_id": "resp-1",
            "item_id": "item-2",
            "delta": "cGNt",
        },
    ]
    for events in (replay_events, wrong_item_events):
        harness = Harness(events)

        async def scenario(harness=harness, correlation=correlation):
            provider = harness.provider(token_factory=lambda: correlation)
            session = await provider.open_session(setup())
            await session.start_response(response_request())
            return [event async for event in session.events()]

        received = asyncio.run(scenario())
        assert isinstance(received[-1], SessionFailure)
        assert received[-1].message == "provider protocol failure"


def test_exact_setup_formats_are_validated_before_credentials_or_network():
    wrong_input = RealtimeInputAudioFormat(
        mime_type="audio/pcm",
        sample_rate_hz=16_000,
        channels=1,
        sample_encoding="pcm_s16le",
        sample_width_bytes=2,
        endianness="little",
    )
    wrong_output = RealtimeOutputAudioFormat(
        mime_type="audio/pcm",
        sample_rate_hz=24_000,
        channels=2,
        sample_encoding="pcm_s16le",
        sample_width_bytes=2,
        endianness="little",
    )
    for bad_setup in (setup(input_audio=wrong_input), setup(output_audio=wrong_output)):
        harness = Harness()
        with pytest.raises(ValueError, match="audio"):
            asyncio.run(harness.provider().open_session(bad_setup))
        assert harness.auth_calls == harness.wire_calls == 0


def test_response_capacity_and_close_cleanup_are_fail_closed():
    tokens = iter(("token-1", "token-2"))
    harness = Harness()

    async def scenario():
        session = await harness.provider(
            token_factory=lambda: next(tokens), response_ledger_capacity=1
        ).open_session(setup())
        await session.start_response(response_request())
        with pytest.raises(ValueError, match="capacity"):
            await session.start_response(
                response_request(
                    assistant_message_id=42,
                    turn_marker="turn-42",
                    content_digest=hashlib.sha256(b"other").hexdigest(),
                    canonical_text="other",
                )
            )
        await session.close()
        return session

    session = asyncio.run(scenario())
    assert not session._pending_responses
    assert not session._active_responses
    assert not session._completed_responses
    assert not session._consumed_correlations


@pytest.mark.parametrize("saturated_stage", ["pending", "bound", "completed"])
def test_response_lifetime_capacity_is_reserved_before_provider_send(saturated_stage):
    tokens = iter(("token-1", "token-2"))
    harness = Harness()

    async def scenario():
        session = await harness.provider(
            token_factory=lambda: next(tokens), response_ledger_capacity=1
        ).open_session(setup())
        await session.start_response(response_request())
        if saturated_stage != "pending":
            bind_response(session, "token-1")
        if saturated_stage == "completed":
            complete_bound_response(session, "token-1")
        with pytest.raises(ValueError, match="capacity"):
            await session.start_response(
                response_request(
                    assistant_message_id=42,
                    turn_marker="turn-42",
                    canonical_text="other",
                    content_digest=hashlib.sha256(b"other").hexdigest(),
                )
            )
        return session

    session = asyncio.run(scenario())
    assert len(harness.wire.sent) == 1
    assert "token-2" not in session._pending_responses


def test_response_created_validation_is_atomic_and_preserves_pending_reservation():
    tokens = iter(("token-1", "token-2"))
    harness = Harness()

    async def scenario():
        session = await harness.provider(
            token_factory=lambda: next(tokens), response_ledger_capacity=2
        ).open_session(setup())
        await session.start_response(response_request())
        bind_response(session, "token-1")
        await session.start_response(
            response_request(
                assistant_message_id=42,
                turn_marker="turn-42",
                canonical_text="other",
                content_digest=hashlib.sha256(b"other").hexdigest(),
            )
        )
        before = dict(session._pending_responses)
        with pytest.raises(ValueError, match="duplicate"):
            bind_response(session, "token-2", response_id="resp-1")
        assert session._pending_responses == before
        with pytest.raises(ValueError, match="metadata"):
            session._map_event(
                {
                    "type": "response.created",
                    "response": {
                        "id": "resp-2",
                        "metadata": {"correlation": "token-2", "extra": True},
                    },
                }
            )
        assert session._pending_responses == before
        assert bind_response(session, "token-2", response_id="resp-2") == ResponseStarted(
            response_id="resp-2", turn_id="turn-42"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "late_event,match",
    [
        (
            {
                "type": "response.output_audio_transcript.delta",
                "response_id": "resp-1",
                "item_id": "item-1",
                "delta": "later",
            },
            "terminal",
        ),
        (
            {
                "type": "response.output_audio_transcript.done",
                "response_id": "resp-1",
                "item_id": "item-1",
                "transcript": "words",
            },
            "replayed",
        ),
        (
            {
                "type": "response.output_audio_transcript.done",
                "response_id": "resp-1",
                "item_id": "item-1",
                "transcript": "changed",
            },
            "conflicting",
        ),
    ],
)
def test_output_transcript_finality_is_terminal_and_exact(late_event, match):
    harness = Harness()

    async def scenario():
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        bind_response(session, "token-1")
        final = session._map_event(
            {
                "type": "response.output_audio_transcript.done",
                "response_id": "resp-1",
                "item_id": "item-1",
                "transcript": "words",
            }
        )
        with pytest.raises(ValueError, match=match):
            session._map_event(late_event)
        return session, final

    session, final = asyncio.run(scenario())
    assert final == OutputTranscript(
        item_id="item-1",
        response_id="resp-1",
        turn_id="turn-41",
        text="words",
        final=True,
    )
    assert session._final_output_transcripts == {("resp-1", "item-1"): "words"}


@pytest.mark.parametrize(
    "output,match",
    [
        ([], "nonempty"),
        (
            [
                {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_audio", "transcript": "words"}],
                },
                {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_audio", "transcript": "words"}],
                },
            ],
            "exactly one",
        ),
        (
            [
                {
                    "id": "item-1",
                    "type": "function_call",
                    "role": "assistant",
                    "content": [{"type": "output_audio", "transcript": "words"}],
                }
            ],
            "authority",
        ),
        (
            [
                {
                    "id": "item-1",
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "output_audio", "transcript": "words"}],
                }
            ],
            "assistant",
        ),
        (
            [
                {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "words"}],
                }
            ],
            "output_audio",
        ),
        (
            [
                {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_audio", "transcript": "words"},
                        {"type": "output_text", "text": "words"},
                    ],
                }
            ],
            "exactly one",
        ),
        (
            [
                {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_audio",
                            "transcript": "words",
                            "function_call": {"name": "forbidden"},
                        }
                    ],
                }
            ],
            "authority",
        ),
        (
            [
                {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_audio", "transcript": "changed"}],
                }
            ],
            "conflicts",
        ),
        (
            [
                {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [{"type": "output_audio", "transcript": "words"}],
                }
            ],
            "status",
        ),
        ({"id": "item-1"}, "sequence"),
    ],
)
def test_response_done_rejects_non_exact_audio_only_schema_without_consuming_active(
    output, match
):
    harness = Harness()

    async def scenario():
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        bind_response(session, "token-1")
        for event in (
            {
                "type": "response.output_audio.delta",
                "response_id": "resp-1",
                "item_id": "item-1",
                "delta": "cGNtAA==",
            },
            {
                "type": "response.output_audio_transcript.done",
                "response_id": "resp-1",
                "item_id": "item-1",
                "transcript": "words",
            },
            {
                "type": "response.output_audio.done",
                "response_id": "resp-1",
                "item_id": "item-1",
            },
        ):
            session._map_event(event)
        event = response_done_event("token-1", output=output)
        with pytest.raises((TypeError, ValueError), match=match):
            session._map_event(event)
        assert "resp-1" in session._active_responses
        assert "resp-1" not in session._completed_responses

    asyncio.run(scenario())


def test_response_done_requires_output_member():
    harness = Harness()

    async def scenario():
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        bind_response(session, "token-1")
        event = response_done_event("token-1")
        del event["response"]["output"]
        with pytest.raises(ValueError, match="output"):
            session._map_event(event)
        assert "resp-1" in session._active_responses

    asyncio.run(scenario())


@pytest.mark.parametrize("omitted", ["audio_delta", "audio_done", "transcript_done"])
def test_response_done_requires_observed_audio_and_transcript_terminals(omitted):
    harness = Harness()

    async def scenario():
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        bind_response(session, "token-1")
        events = {
            "audio_delta": {
                "type": "response.output_audio.delta",
                "response_id": "resp-1",
                "item_id": "item-1",
                "delta": "cGNtAA==",
            },
            "audio_done": {
                "type": "response.output_audio.done",
                "response_id": "resp-1",
                "item_id": "item-1",
            },
            "transcript_done": {
                "type": "response.output_audio_transcript.done",
                "response_id": "resp-1",
                "item_id": "item-1",
                "transcript": "words",
            },
        }
        for name, event in events.items():
            if name != omitted:
                session._map_event(event)
        with pytest.raises(ValueError, match=omitted.replace("_", " ")):
            session._map_event(response_done_event("token-1"))
        assert "resp-1" in session._active_responses

    asyncio.run(scenario())


def test_response_done_accepts_documented_realtime_item_object():
    correlation = "token-1"
    events = valid_output_lifecycle_events(correlation, transcript="Exact words, exactly.")
    events[-1]["response"]["output"][0]["object"] = "realtime.item"
    harness = Harness(
        [
            {
                "type": "response.created",
                "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
            },
            *events,
        ]
    )

    async def scenario():
        session = await harness.provider(token_factory=lambda: correlation).open_session(setup())
        await session.start_response(response_request())
        return [event async for event in session.events()]

    received = asyncio.run(scenario())
    assert sum(isinstance(event, ResponseCompleted) for event in received) == 1


def test_response_done_accepts_proven_audio_only_shape_and_clears_stream_state():
    harness = Harness()

    async def scenario():
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        bind_response(session, "token-1")
        completed = complete_bound_response(session, "token-1")
        return session, completed

    session, completed = asyncio.run(scenario())
    assert completed == ResponseCompleted(response_id="resp-1", turn_id="turn-41")
    assert not session._active_responses
    assert not session._response_items
    assert not session._item_responses
    assert not session._final_output_transcripts
    assert not session._audio_delta_items
    assert not session._audio_done_items


@pytest.mark.parametrize(
    ("prefix", "bad_event"),
    [
        (
            [],
            {
                "type": "response.output_item.added",
                "response_id": "resp-1",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "tool",
                    "status": "in_progress",
                    "content": [],
                },
            },
        ),
        (
            [],
            {
                "type": "response.output_item.added",
                "response_id": "resp-1",
                "item": {
                    "id": "item-1",
                    "type": "output_text",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            },
        ),
        (
            [],
            {
                "type": "response.output_item.added",
                "response_id": "resp-1",
                "item": {
                    "id": "item-1",
                    "type": "function_call",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            },
        ),
        (
            [],
            {
                "type": "response.output_item.added",
                "response_id": "resp-1",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                    "tool_calls": [],
                },
            },
        ),
        (
            [],
            {
                "type": "response.output_item.added",
                "response_id": "resp-1",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                    "function": {"name": "forbidden"},
                },
            },
        ),
        (
            [],
            {
                "type": "response.output_item.added",
                "response_id": "resp-1",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                    "tool": {"name": "forbidden"},
                },
            },
        ),
        (
            [],
            {
                "type": "response.output_item.added",
                "response_id": "resp-1",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": {},
                },
            },
        ),
        (
            [],
            {
                "type": "response.output_item.added",
                "response_id": "resp-1",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [{"type": "output_text", "text": "forbidden"}],
                },
            },
        ),
        (
            [valid_output_lifecycle_events()[0]],
            {
                "type": "response.output_item.done",
                "response_id": "resp-1",
                "item": {
                    "id": "item-wrong",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_audio", "transcript": "words"}],
                },
            },
        ),
        (
            [],
            {
                "type": "response.output_item.done",
                "response_id": "resp-1",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [],
                },
            },
        ),
        (
            [],
            {
                "type": "response.output_item.done",
                "response_id": "resp-1",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": {"type": "output_audio"},
                },
            },
        ),
        ([], {"type": "response.content_part.added", "response_id": "resp-1", "item_id": "item-1"}),
        (
            [],
            {
                "type": "response.content_part.added",
                "response_id": "resp-1",
                "item_id": "item-1",
                "part": "output_audio",
            },
        ),
        (
            [],
            {
                "type": "response.content_part.added",
                "response_id": "resp-1",
                "item_id": "item-1",
                "part": {"type": "output_text", "text": "forbidden"},
            },
        ),
        (
            [],
            {
                "type": "response.content_part.done",
                "response_id": "resp-1",
                "item_id": "item-1",
                "part": {"type": "function_call", "name": "forbidden"},
            },
        ),
        (
            [],
            {
                "type": "response.content_part.done",
                "response_id": "resp-1",
                "item_id": "item-1",
                "part": {"type": "tool", "name": "forbidden"},
            },
        ),
        (
            [valid_output_lifecycle_events()[0]],
            {
                "type": "response.content_part.done",
                "response_id": "resp-1",
                "item_id": "item-wrong",
                "part": {"type": "output_audio", "transcript": "words"},
            },
        ),
        (
            [],
            {
                "type": "response.content_part.done",
                "response_id": "resp-wrong",
                "item_id": "item-1",
                "part": {"type": "output_audio", "transcript": "words"},
            },
        ),
        (
            [],
            {
                "type": "response.content_part.done",
                "response_id": "resp-1",
                "item_id": "item-1",
                "part": {"type": "output_audio", "transcript": 7},
            },
        ),
        (
            [valid_output_lifecycle_events()[0]],
            {
                "type": "conversation.item.done",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "user",
                    "status": "completed",
                    "content": [{"type": "output_audio", "transcript": "words"}],
                },
            },
        ),
        (
            [valid_output_lifecycle_events()[0]],
            {
                "type": "conversation.item.done",
                "item": {
                    "id": "item-1",
                    "type": "function_call",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_audio", "transcript": "words"}],
                },
            },
        ),
        (
            [valid_output_lifecycle_events()[0]],
            {
                "type": "conversation.item.done",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "forbidden"}],
                },
            },
        ),
        (
            [valid_output_lifecycle_events()[0]],
            {
                "type": "conversation.item.done",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_audio", "transcript": "words"}],
                    "function_call": {"name": "forbidden"},
                },
            },
        ),
        (
            [valid_output_lifecycle_events()[0]],
            {
                "type": "conversation.item.done",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_audio", "transcript": "words"}],
                    "tool": {"name": "forbidden"},
                },
            },
        ),
        (
            [valid_output_lifecycle_events()[0]],
            {
                "type": "conversation.item.done",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_audio", "transcript": "words"}],
                    "tool_calls": [],
                },
            },
        ),
    ],
)
def test_contradictory_output_lifecycle_event_fails_before_sanitized_completion(prefix, bad_event):
    correlation = "token-1"
    created = {
        "type": "response.created",
        "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
    }
    harness = Harness([created, *prefix, bad_event, *valid_output_lifecycle_events(correlation)])

    async def scenario():
        session = await harness.provider(token_factory=lambda: correlation).open_session(setup())
        await session.start_response(response_request())
        return session, [event async for event in session.events()]

    session, received = asyncio.run(scenario())
    assert isinstance(received[-1], SessionFailure)
    assert not any(isinstance(event, ResponseCompleted) for event in received)
    assert harness.wire.closed is True
    assert session._terminal is True
    assert not session._active_responses
    assert not session._response_items
    assert not session._item_responses


@pytest.mark.parametrize(
    "extra",
    [
        {"tool_calls": []},
        {"function_call": {"name": "forbidden"}},
        {"tool": {"name": "forbidden"}},
        {"call_id": "call-forbidden"},
        {"name": "forbidden", "arguments": "{}"},
        {"content": [{"type": "output_audio", "transcript": "words", "tool": {}}]},
    ],
)
def test_response_done_rejects_extra_authority_before_mutating_direct_state(extra):
    harness = Harness()

    async def scenario():
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        bind_response(session, "token-1")
        for event in valid_output_lifecycle_events()[:6]:
            session._map_event(event)
        output = response_done_event("token-1")["response"]["output"]
        item = output[0]
        if "content" in extra:
            item["content"] = extra["content"]
        else:
            item.update(extra)
        before = (
            dict(session._active_responses),
            dict(session._response_items),
            dict(session._item_responses),
            dict(session._final_output_transcripts),
            set(session._audio_delta_items),
            set(session._audio_done_items),
            dict(session._completed_responses),
        )
        with pytest.raises((TypeError, ValueError)):
            session._complete_response(response_done_event("token-1", output=output))
        assert before == (
            session._active_responses,
            session._response_items,
            session._item_responses,
            session._final_output_transcripts,
            session._audio_delta_items,
            session._audio_done_items,
            session._completed_responses,
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "bad_event",
    [
        {
            "type": "response.content_part.done",
            "response_id": "resp-1",
            "item_id": "item-1",
            "part": {"type": "output_audio", "transcript": "changed"},
        },
        {
            "type": "response.output_item.done",
            "response_id": "resp-1",
            "item": {
                "id": "item-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_audio", "transcript": "changed"}],
            },
        },
        {
            "type": "conversation.item.done",
            "item": {
                "id": "item-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_audio", "transcript": "changed"}],
            },
        },
    ],
)
def test_final_lifecycle_transcript_must_match_terminal_text_before_mutation(bad_event):
    harness = Harness()

    async def scenario():
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        bind_response(session, "token-1")
        for event in valid_output_lifecycle_events()[:6]:
            session._map_event(event)
        before = (
            dict(session._response_items),
            dict(session._item_responses),
            set(session._content_part_done),
            set(session._output_item_done),
            set(session._conversation_item_done),
        )
        with pytest.raises(ValueError, match="conflicts"):
            session._map_event(bad_event)
        assert before == (
            session._response_items,
            session._item_responses,
            session._content_part_done,
            session._output_item_done,
            session._conversation_item_done,
        )

    asyncio.run(scenario())


def output_envelope_authority_cases():
    return [
        pytest.param(
            {
                "type": "response.created",
                "response": {
                    "id": "resp-1",
                    "metadata": {"correlation": "token-1"},
                    "tool_calls": [],
                },
            },
            ResponseStarted,
            id="response-created-nested-tool-calls-empty",
        ),
        pytest.param(
            {
                "type": "response.output_item.added",
                "response_id": "resp-1",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
                "tools": [],
            },
            ResponseCompleted,
            id="output-item-added-top-level-tools-empty",
        ),
        pytest.param(
            {
                "type": "response.output_item.done",
                "response_id": "resp-1",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_audio", "transcript": "words"}],
                    "metadata": {"type": "tool_call"},
                },
                "function": {},
            },
            ResponseCompleted,
            id="output-item-done-recursive-type-and-function-empty",
        ),
        pytest.param(
            {
                "type": "response.content_part.added",
                "response_id": "resp-1",
                "item_id": "item-1",
                "part": {"type": "output_audio"},
                "tool": {},
            },
            ResponseCompleted,
            id="content-part-added-top-level-tool-empty",
        ),
        pytest.param(
            {
                "type": "response.content_part.done",
                "response_id": "resp-1",
                "item_id": "item-1",
                "part": {
                    "type": "output_audio",
                    "transcript": "words",
                    "tool_choice": "none",
                },
            },
            ResponseCompleted,
            id="content-part-done-nested-tool-choice",
        ),
        pytest.param(
            {
                "type": "response.output_audio.delta",
                "response_id": "resp-1",
                "item_id": "item-1",
                "delta": "cGNt",
                "function_call": {"name": "forbidden"},
            },
            OutputAudio,
            id="output-audio-delta-function-call",
        ),
        pytest.param(
            {
                "type": "response.output_audio.done",
                "response_id": "resp-1",
                "item_id": "item-1",
                "arguments": "{}",
            },
            ResponseCompleted,
            id="output-audio-done-arguments-empty-object",
        ),
        pytest.param(
            {
                "type": "response.output_audio_transcript.delta",
                "response_id": "resp-1",
                "item_id": "item-1",
                "delta": "w",
                "call_id": "call-forbidden",
            },
            OutputTranscript,
            id="output-transcript-delta-call-id",
        ),
        pytest.param(
            {
                "type": "response.output_audio_transcript.done",
                "response_id": "resp-1",
                "item_id": "item-1",
                "transcript": "words",
                "function_call_output": "",
            },
            OutputTranscript,
            id="output-transcript-done-function-call-output-empty",
        ),
        pytest.param(
            {
                "type": "conversation.item.done",
                "item": {
                    "id": "item-1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_audio", "transcript": "words"}],
                },
                "function_call": {},
            },
            ResponseCompleted,
            id="conversation-item-done-top-level-function-call-empty",
        ),
        pytest.param(
            {**response_done_event("token-1"), "name": "forbidden"},
            ResponseCompleted,
            id="response-done-top-level-name",
        ),
        pytest.param(
            {
                **response_done_event("token-1"),
                "response": {
                    **response_done_event("token-1")["response"],
                    "tools": [],
                },
            },
            ResponseCompleted,
            id="response-done-nested-response-tools-empty",
        ),
    ]


def output_authority_state(session):
    return (
        dict(session._pending_responses),
        dict(session._active_responses),
        dict(session._response_items),
        dict(session._item_responses),
        dict(session._final_output_transcripts),
        set(session._audio_delta_items),
        set(session._audio_done_items),
        dict(session._audio_byte_carries),
        set(session._output_item_added),
        set(session._content_part_added),
        set(session._content_part_done),
        set(session._output_item_done),
        set(session._conversation_item_done),
        dict(session._completed_responses),
        dict(session._consumed_correlations),
    )


@pytest.mark.parametrize(("bad_event", "forbidden_output"), output_envelope_authority_cases())
def test_every_output_event_envelope_authority_fails_before_emission(
    bad_event, forbidden_output
):
    correlation = "token-1"
    created = {
        "type": "response.created",
        "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
    }
    prefix = [] if bad_event["type"] == "response.created" else [created]
    harness = Harness([*prefix, bad_event, *valid_output_lifecycle_events(correlation)])

    async def scenario():
        session = await harness.provider(token_factory=lambda: correlation).open_session(setup())
        await session.start_response(response_request())
        return [event async for event in session.events()]

    received = asyncio.run(scenario())
    assert isinstance(received[-1], SessionFailure)
    assert received[-1].message == "provider protocol failure"
    assert not any(isinstance(event, forbidden_output) for event in received)
    assert not any(isinstance(event, ResponseCompleted) for event in received)
    assert harness.wire.closed is True


@pytest.mark.parametrize(("bad_event", "_forbidden_output"), output_envelope_authority_cases())
def test_every_output_event_envelope_authority_preserves_direct_state(
    bad_event, _forbidden_output
):
    harness = Harness()

    async def scenario():
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        if bad_event["type"] != "response.created":
            bind_response(session, "token-1")
        before = output_authority_state(session)
        with pytest.raises(ValueError, match="authority"):
            session._map_event(bad_event)
        assert output_authority_state(session) == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "discriminator",
    ["function_call", "function_call_output", "tool", "tool_call"],
)
def test_recursive_output_authority_discriminator_is_rejected(discriminator):
    harness = Harness()

    async def scenario():
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        bind_response(session, "token-1")
        before = output_authority_state(session)
        with pytest.raises(ValueError, match="authority type"):
            session._map_event(
                {
                    "type": "response.output_audio.done",
                    "response_id": "resp-1",
                    "item_id": "item-1",
                    "metadata": {"nested": [{"type": discriminator}]},
                }
            )
        assert output_authority_state(session) == before

    asyncio.run(scenario())


def test_recursive_authority_covers_every_recognized_nonoutput_family_atomically():
    forbidden_events = [
        {
            "type": "session.created",
            "session": {
                "id": "session-1",
                "metadata": {"left": [{"nested": {"tools": []}}]},
            },
        },
        {
            "type": "session.updated",
            "session": {"metadata": {"right": {"nested": [{"type": "tool_call"}]}}},
        },
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "input-1",
            "audio_start_ms": 7,
            "provider": [{"left": {"function_call": {}}}],
        },
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "input-1",
            "audio_end_ms": 11,
            "provider": {"right": [{"arguments": "{}"}]},
        },
        {
            "type": "input_audio_buffer.committed",
            "item_id": "input-1",
            "metadata": {"left": [{"nested": {"call_id": "call-1"}}]},
        },
        {
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "input-1",
            "delta": "hel",
            "diagnostic": {"right": [{"nested": {"tool_choice": "none"}}]},
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "input-1",
            "transcript": "hello",
            "diagnostic": [{"left": {"type": "function_call_output"}}],
        },
        {
            "type": "rate_limits.updated",
            "rate_limits": [{"name": "requests", "metadata": {"right": {"name": "tool"}}}],
        },
        {
            "type": "error",
            "error": {
                "message": "provider error",
                "metadata": {"left": [{"function": {}}]},
            },
        },
    ]

    def state(session):
        return (
            session._provider_session_id,
            dict(session._input_ledger),
            output_authority_state(session),
        )

    async def scenario():
        for bad_event in forbidden_events:
            harness = Harness()
            session = await harness.provider().open_session(setup())
            before = state(session)
            with pytest.raises(ValueError, match="authority"):
                session._map_event(bad_event)
            assert state(session) == before
            await session.close()

        harness = Harness()
        session = await harness.provider().open_session(setup())
        assert session._map_event(
            {
                "type": "session.created",
                "session": {
                    "id": "session-1",
                    "metadata": {"left": [{"classification": "tools-disabled"}]},
                },
            }
        ) == SessionReady(session_id="session-1")
        assert session._map_event(
            {
                "type": "input_audio_buffer.committed",
                "item_id": "input-1",
                "metadata": {"right": [{"classification": "operator-audio"}]},
            }
        ) is None
        transcript = session._map_event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "input-1",
                "transcript": "hello",
                "diagnostic": {"left": [{"classification": "legitimate"}]},
            }
        )
        assert transcript == InputTranscript(
            item_id="input-1",
            turn_id="input-1",
            text="hello",
            final=True,
            role=TranscriptRole.OPERATOR,
            provenance=TranscriptProvenance.OPERATOR_INPUT,
        )
        before_unknown = state(session)
        assert session._map_event(
            {
                "type": "vendor.future_diagnostic",
                "metadata": {"nested": [{"tools": []}, {"type": "tool_call"}]},
            }
        ) is None
        assert state(session) == before_unknown
        with pytest.raises(core_rt.OpenAIWireError, match="legitimate provider error"):
            session._map_event(
                {
                    "type": "error",
                    "error": {
                        "message": "legitimate provider error",
                        "metadata": {"classification": "diagnostic-only"},
                    },
                }
            )

    asyncio.run(scenario())


def test_output_event_envelope_authority_fails_before_response_started():
    correlation = "token-1"
    bad_created = {
        "type": "response.created",
        "response": {
            "id": "resp-1",
            "metadata": {"correlation": correlation},
            "tool_calls": [],
        },
    }
    harness = Harness([bad_created, *valid_output_lifecycle_events(correlation)])

    async def scenario():
        session = await harness.provider(token_factory=lambda: correlation).open_session(setup())
        await session.start_response(response_request())
        return session, [event async for event in session.events()]

    session, received = asyncio.run(scenario())
    assert isinstance(received[-1], SessionFailure)
    assert received[-1].message == "provider protocol failure"
    assert not any(isinstance(event, ResponseStarted) for event in received)
    assert not any(isinstance(event, ResponseCompleted) for event in received)
    assert session._terminal is True


def test_response_done_extra_authority_fails_event_pump_and_cleans_terminal_state():
    correlation = "token-1"
    created = {
        "type": "response.created",
        "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
    }
    done = response_done_event(correlation)
    done["response"]["output"][0]["tool_calls"] = []
    harness = Harness([created, *valid_output_lifecycle_events(correlation)[:-1], done])

    async def scenario():
        session = await harness.provider(token_factory=lambda: correlation).open_session(setup())
        await session.start_response(response_request())
        return session, [event async for event in session.events()]

    session, received = asyncio.run(scenario())
    assert isinstance(received[-1], SessionFailure)
    assert not any(isinstance(event, ResponseCompleted) for event in received)
    assert session._terminal is True
    assert not session._active_responses
    assert not session._completed_responses


def test_terminal_transcript_digest_mismatch_fails_closed_and_clears_all_response_state():
    correlation = "token-1"
    created = {
        "type": "response.created",
        "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
    }
    harness = Harness(
        [
            created,
            *valid_output_lifecycle_events(
                correlation, transcript="DIFFERENT WORDS"
            ),
        ]
    )

    async def scenario():
        session = await harness.provider(token_factory=lambda: correlation).open_session(setup())
        await session.start_response(response_request(canonical_text="Exact words, exactly."))
        return session, [event async for event in session.events()]

    session, received = asyncio.run(scenario())
    failures = [event for event in received if isinstance(event, SessionFailure)]
    assert len(failures) == 1
    assert failures[0].code == "provider_protocol_failure"
    assert failures[0].message == "provider protocol failure"
    assert not any(isinstance(event, ResponseCompleted) for event in received)
    assert harness.wire.closed is True
    assert session._terminal is True
    assert not session._pending_responses
    assert not session._active_responses
    assert not session._response_items
    assert not session._item_responses
    assert not session._final_output_transcripts
    assert not session._audio_delta_items
    assert not session._audio_done_items
    assert not session._output_item_added
    assert not session._content_part_added
    assert not session._content_part_done
    assert not session._output_item_done
    assert not session._conversation_item_done
    assert not session._response_lifecycle
    assert not session._completed_responses
    assert not session._consumed_correlations


def test_live_pre_start_audio_delta_fails_before_emission_and_cleans_all_response_state():
    correlation = "token-1"
    created = {
        "type": "response.created",
        "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
    }
    lifecycle = valid_output_lifecycle_events(
        correlation, transcript="Exact words, exactly."
    )
    harness = Harness([created, lifecycle[2], *lifecycle])

    async def scenario():
        session = await harness.provider(token_factory=lambda: correlation).open_session(setup())
        await session.start_response(response_request())
        return session, [event async for event in session.events()]

    session, received = asyncio.run(scenario())
    failures = [event for event in received if isinstance(event, SessionFailure)]
    assert len(failures) == 1
    assert failures[0].code == "provider_protocol_failure"
    assert sum(isinstance(event, ResponseStarted) for event in received) == 1
    assert not any(isinstance(event, OutputAudio) for event in received)
    assert not any(isinstance(event, OutputTranscript) for event in received)
    assert not any(isinstance(event, ResponseCompleted) for event in received)
    assert harness.wire.closed is True
    assert session._terminal is True
    assert not session._pending_responses
    assert not session._active_responses
    assert not session._response_items
    assert not session._item_responses
    assert not session._final_output_transcripts
    assert not session._audio_delta_items
    assert not session._audio_done_items
    assert not session._output_item_added
    assert not session._content_part_added
    assert not session._content_part_done
    assert not session._output_item_done
    assert not session._conversation_item_done
    assert not session._response_lifecycle
    assert not session._completed_responses
    assert not session._consumed_correlations


@pytest.mark.parametrize(
    "omitted_type",
    [
        "response.output_item.added",
        "response.content_part.added",
        "response.output_audio.delta",
        "response.output_audio_transcript.delta",
        "response.output_audio.done",
        "response.output_audio_transcript.done",
        "response.content_part.done",
        "response.output_item.done",
        "conversation.item.done",
    ],
)
def test_response_done_requires_every_live_output_lifecycle_stage(omitted_type):
    correlation = "token-1"
    created = {
        "type": "response.created",
        "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
    }
    lifecycle = valid_output_lifecycle_events(
        correlation, transcript="Exact words, exactly."
    )
    events = [event for event in lifecycle[:-1] if event["type"] != omitted_type]
    harness = Harness([created, *events, lifecycle[-1]])

    async def scenario():
        session = await harness.provider(token_factory=lambda: correlation).open_session(setup())
        await session.start_response(response_request())
        return session, [event async for event in session.events()]

    session, received = asyncio.run(scenario())
    assert sum(isinstance(event, SessionFailure) for event in received) == 1
    assert not any(isinstance(event, ResponseCompleted) for event in received)
    assert harness.wire.closed is True
    assert session._terminal is True
    assert not session._response_lifecycle
    assert output_authority_state(session) == (
        {}, {}, {}, {}, {}, set(), set(), {}, set(), set(), set(), set(), set(), {}, {}
    )


@pytest.mark.parametrize(
    ("earlier_type", "later_type"),
    [
        ("response.output_item.added", "response.content_part.added"),
        ("response.content_part.added", "response.output_audio.delta"),
        ("response.output_audio.done", "response.output_audio_transcript.done"),
        ("response.content_part.done", "response.output_item.done"),
        ("response.output_item.done", "conversation.item.done"),
    ],
)
def test_live_output_lifecycle_rejects_out_of_order_stages(earlier_type, later_type):
    correlation = "token-1"
    created = {
        "type": "response.created",
        "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
    }
    lifecycle = valid_output_lifecycle_events(
        correlation, transcript="Exact words, exactly."
    )
    positions = {event["type"]: index for index, event in enumerate(lifecycle)}
    first, second = positions[earlier_type], positions[later_type]
    lifecycle[first], lifecycle[second] = lifecycle[second], lifecycle[first]
    harness = Harness([created, *lifecycle])

    async def scenario():
        session = await harness.provider(token_factory=lambda: correlation).open_session(setup())
        await session.start_response(response_request())
        return [event async for event in session.events()]

    received = asyncio.run(scenario())
    assert sum(isinstance(event, SessionFailure) for event in received) == 1
    assert not any(isinstance(event, ResponseCompleted) for event in received)
    assert harness.wire.closed is True


def test_valid_live_output_lifecycle_completes_exactly_once_and_cleans_terminal_state():
    correlation = "token-1"
    created = {
        "type": "response.created",
        "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
    }
    harness = Harness(
        [
            created,
            *valid_output_lifecycle_events(
                correlation, transcript="Exact words, exactly."
            ),
        ]
    )

    async def scenario():
        session = await harness.provider(token_factory=lambda: correlation).open_session(setup())
        await session.start_response(response_request())
        return session, [event async for event in session.events()]

    session, received = asyncio.run(scenario())
    assert sum(isinstance(event, ResponseCompleted) for event in received) == 1
    assert received[-1] == SessionClosed()
    assert session._terminal is True
    assert not session._active_responses
    assert not session._response_items
    assert not session._item_responses
    assert not session._response_lifecycle
    assert not session._completed_responses


def test_live_proven_output_envelopes_accept_event_ids_and_indexes_once():
    correlation = "token-1"
    created = {
        "type": "response.created",
        "event_id": "evt-created",
        "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
    }
    events = valid_output_lifecycle_events(correlation, transcript="Exact words, exactly.")
    for index, event in enumerate(events):
        event["event_id"] = f"evt-{index}"
        if event["type"].startswith("response."):
            event["output_index"] = 0
        if event["type"].startswith("response.content_part") or "output_audio" in event["type"]:
            event["content_index"] = 0
    harness = Harness([created, *events])

    async def scenario():
        session = await harness.provider(token_factory=lambda: correlation).open_session(setup())
        await session.start_response(response_request())
        return [event async for event in session.events()]

    received = asyncio.run(scenario())
    assert sum(isinstance(event, ResponseStarted) for event in received) == 1
    assert sum(isinstance(event, OutputAudio) for event in received) == 1
    assert sum(isinstance(event, ResponseCompleted) for event in received) == 1
    assert received[-1] == SessionClosed()


def test_invalid_intermediate_shape_does_not_mutate_binding_state_directly():
    harness = Harness()

    async def scenario():
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        bind_response(session, "token-1")
        before = (
            dict(session._response_items),
            dict(session._item_responses),
            dict(session._final_output_transcripts),
        )
        with pytest.raises((TypeError, ValueError)):
            session._map_event(
                {
                    "type": "response.output_item.added",
                    "response_id": "resp-1",
                    "item": {
                        "id": "item-1",
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                        "tool_calls": [],
                    },
                }
            )
        assert before == (
            session._response_items,
            session._item_responses,
            session._final_output_transcripts,
        )

    asyncio.run(scenario())


def test_explicit_response_send_failure_cleans_pending_and_is_terminally_visible():
    harness = Harness()
    harness.wire.send_error = core_rt.OpenAIWireError("response send exploded")

    async def scenario():
        session = await harness.provider(token_factory=lambda: "failed-token").open_session(setup())
        with pytest.raises(core_rt.OpenAIWireError, match="response send exploded"):
            await session.start_response(response_request())
        return session, [event async for event in session.events()]

    session, received = asyncio.run(scenario())
    assert not session._pending_responses
    assert received == [
        SessionFailure(
            code="explicit_response_failed",
            message="explicit response provider send failed",
        )
    ]
    assert harness.wire.closed is True


def test_missing_task1_symbols_leave_legacy_input_only_contract_available(monkeypatch):
    contract = sys.modules["agent.realtime_voice_provider"]
    shim = types.ModuleType(contract.__name__)
    optional = {
        "RealtimeInputAudioFormat",
        "RealtimeOutputAudioFormat",
        "RealtimeResponseRequest",
        "ResponseStarted",
        "OutputAudio",
        "OutputTranscript",
        "ResponseCompleted",
    }
    shim.__dict__.update(
        {name: value for name, value in contract.__dict__.items() if name not in optional}
    )
    agent_package = sys.modules["agent"]
    monkeypatch.setitem(sys.modules, "agent.realtime_voice_provider", shim)
    monkeypatch.setattr(agent_package, "realtime_voice_provider", shim)
    legacy = importlib.reload(core_rt)
    try:
        assert legacy.core_provider_available() is True
        assert frozenset(
            {
                legacy.RealtimeCapability.INPUT_TRANSCRIPTION,
                legacy.RealtimeCapability.INPUT_COMMIT_EVENTS,
            }
        ) == legacy.CORE_CAPABILITIES
        assert legacy.SUPPORTED_OUTPUT_AUDIO_FORMAT is None
    finally:
        monkeypatch.undo()
        importlib.reload(core_rt)


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
        setup(automatic_response=True),
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
    assert events[0].message == "provider send failed"


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
    assert events[0].message == "provider send failed"


@pytest.mark.parametrize(
    ("operation", "provider_detail", "expected_code", "expected_message"),
    [
        (
            "send_audio",
            "arbitrary-sentinel-send Bearer sentinel-send-secret",
            "provider_send_failure",
            "provider send failed",
        ),
        (
            "start_response",
            "arbitrary-sentinel-response password=sentinel-response-secret",
            "explicit_response_failed",
            "explicit response provider send failed",
        ),
        (
            "protocol",
            "arbitrary-sentinel-protocol token=sentinel-protocol-secret",
            "provider_protocol_failure",
            "provider protocol failure",
        ),
        (
            "eof",
            "arbitrary-sentinel-eof api_key=sentinel-eof-secret",
            "provider_eof",
            "provider connection closed unexpectedly",
        ),
    ],
)
def test_provider_failures_use_fixed_public_receipts_and_release_raw_detail(
    operation, provider_detail, expected_code, expected_message
):
    harness = Harness()
    if operation in {"send_audio", "start_response"}:
        harness.wire.send_error = core_rt.OpenAIWireError(provider_detail)
    elif operation == "protocol":
        harness.wire.events = iter(
            ({"type": "error", "error": {"message": provider_detail}},)
        )
    else:
        harness.wire.events = iter((core_rt.OpenAIWireEOF(provider_detail),))

    async def scenario():
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        if operation in {"send_audio", "start_response"}:
            with pytest.raises(core_rt.OpenAIWireError):
                if operation == "send_audio":
                    await session.send_audio(b"pcm")
                else:
                    await session.start_response(response_request())
        events = [event async for event in session.events()]
        await asyncio.sleep(0)
        return session, events

    session, events = asyncio.run(scenario())
    assert events == [SessionFailure(code=expected_code, message=expected_message)]
    assert provider_detail not in repr(events)
    assert provider_detail not in repr(vars(session))
    assert session._pending_send_failure is None
    assert session._terminal is True
    assert harness.wire.closed is True
    assert not session._response_send_tasks
    assert not session._detached_close_tasks


def test_retained_send_failure_wins_over_secondary_protocol_failure_receipt():
    provider_detail = "arbitrary-sentinel-race Bearer sentinel-race-secret"

    class SendProtocolRaceWire(FakeWire):
        def __init__(self):
            super().__init__()
            self.receive_started = asyncio.Event()
            self.release_protocol = asyncio.Event()
            self.send_started = asyncio.Event()
            self.release_send = asyncio.Event()

        async def __anext__(self):
            self.receive_started.set()
            await self.release_protocol.wait()
            return {"type": "error", "error": {"message": "secondary protocol detail"}}

        async def send_json(self, messages):
            del messages
            self.send_started.set()
            await self.release_send.wait()
            raise core_rt.OpenAIWireError(provider_detail)

        async def close(self):
            self.closed = True

    async def scenario():
        harness = Harness()
        harness.wire = SendProtocolRaceWire()
        session = await harness.provider().open_session(setup())
        pump = asyncio.create_task(_collect(session))
        await harness.wire.receive_started.wait()
        sender = asyncio.create_task(session.send_audio(b"pcm"))
        await harness.wire.send_started.wait()
        harness.wire.release_send.set()
        with pytest.raises(core_rt.OpenAIWireError):
            await sender
        harness.wire.release_protocol.set()
        events = await pump
        await asyncio.sleep(0)
        return session, harness, events

    async def _collect(session):
        return [event async for event in session.events()]

    session, harness, events = asyncio.run(scenario())
    assert events == [
        SessionFailure(code="provider_send_failure", message="provider send failed")
    ]
    assert provider_detail not in repr(events)
    assert provider_detail not in repr(vars(session))
    assert session._pending_send_failure is None
    assert session._terminal is True
    assert harness.wire.closed is True
    assert not session._response_send_tasks
    assert not session._detached_close_tasks


def test_authority_validator_has_controlled_depth_and_node_budgets():
    def nested(depth, leaf):
        value = leaf
        for _ in range(depth):
            value = {"metadata": [value]}
        return value

    accepted = {"type": "session.updated", "metadata": nested(32, {"safe": True})}
    core_rt._validate_output_event_authority(accepted)

    forbidden = {"type": "session.updated", "metadata": nested(32, {"tool": "blocked"})}
    with pytest.raises(ValueError, match="forbidden structured authority"):
        core_rt._validate_output_event_authority(forbidden)

    excessive = {"type": "session.updated", "metadata": nested(33, {"safe": True})}
    with pytest.raises(ValueError, match="depth") as depth_error:
        core_rt._validate_output_event_authority(excessive)
    assert not isinstance(depth_error.value, RecursionError)

    fanout = {"type": "session.updated", "metadata": [{"safe": True}] * 4097}
    with pytest.raises(ValueError, match="node"):
        core_rt._validate_output_event_authority(fanout)

    harness = Harness()

    async def scenario():
        session = await harness.provider().open_session(setup())
        before = session._provider_session_id
        with pytest.raises(ValueError, match="depth"):
            session._map_event(excessive)
        return session, before

    session, before = asyncio.run(scenario())
    assert session._provider_session_id == before is None


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


def test_passive_speech_start_maps_exact_primitives_without_sending():
    harness = Harness()

    async def scenario():
        session = await harness.provider().open_session(setup())
        mapped = session._map_event(
            {
                "type": "input_audio_buffer.speech_started",
                "item_id": "input-1",
                "audio_start_ms": 7,
            }
        )
        assert harness.wire.sent == []
        for item_id, offset in (("", 0), ("input", True), ("input", 1.0), ("input", -1)):
            with pytest.raises((TypeError, ValueError)):
                session._map_event(
                    {
                        "type": "input_audio_buffer.speech_started",
                        "item_id": item_id,
                        "audio_start_ms": offset,
                    }
                )
        return mapped

    assert asyncio.run(scenario()) == InputSpeechStarted(
        item_id="input-1", audio_start_ms=7
    )


def test_cancel_response_exact_authority_and_empty_cancelled_terminal():
    harness = Harness()

    async def scenario():
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        bind_response(session, "token-1")
        await session.cancel_response("resp-1")
        with pytest.raises(ValueError):
            await session.cancel_response("resp-1")
        terminal = session._map_event(
            {
                "type": "response.done",
                "response": {
                    "id": "resp-1",
                    "status": "cancelled",
                    "metadata": {"correlation": "token-1"},
                    "output": [],
                    "status_details": {
                        "type": "cancelled",
                        "reason": "client_cancelled",
                    },
                },
            }
        )
        return session, terminal

    session, terminal = asyncio.run(scenario())
    cancel = harness.wire.sent[-1]
    assert cancel["type"] == "response.cancel"
    assert cancel["response_id"] == "resp-1"
    assert cancel["event_id"].startswith("evt-")
    assert terminal == Interruption(response_id="resp-1", turn_id="turn-41")
    assert not session._active_responses
    assert not session._cancelling_responses
    assert session._completed_responses == {"resp-1": "token-1"}


def test_cancel_send_and_provider_terminal_are_linearized_in_both_race_orders():
    class RacingCancelWire(FakeWire):
        def __init__(self):
            super().__init__()
            self.incoming = asyncio.Queue()
            self.cancel_send_started = asyncio.Event()
            self.release_cancel_send = asyncio.Event()

        async def send_json(self, messages):
            messages = tuple(messages)
            self.sent.extend(messages)
            if messages[0]["type"] == "response.cancel":
                self.cancel_send_started.set()
                await self.release_cancel_send.wait()
                raise RuntimeError("cancel send exploded")

        async def __anext__(self):
            event = await self.incoming.get()
            if event is None:
                raise core_rt.OpenAIWireEOF("")
            return event

        async def close(self):
            if not self.closed:
                self.closed = True
                self.incoming.put_nowait(None)

    cancelled = {
        "type": "response.done",
        "response": {
            "id": "resp-1",
            "status": "cancelled",
            "metadata": {"correlation": "token-1"},
            "output": [],
            "status_details": {
                "type": "cancelled",
                "reason": "client_cancelled",
            },
        },
    }

    async def terminal_wins():
        harness = Harness()
        harness.wire = RacingCancelWire()
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        stream = session.events()
        harness.wire.incoming.put_nowait(
            {
                "type": "response.created",
                "response": {"id": "resp-1", "metadata": {"correlation": "token-1"}},
            }
        )
        assert await anext(stream) == ResponseStarted(response_id="resp-1", turn_id="turn-41")

        waiter = asyncio.create_task(session.cancel_response("resp-1"))
        await harness.wire.cancel_send_started.wait()
        harness.wire.incoming.put_nowait(cancelled)
        assert await anext(stream) == Interruption(response_id="resp-1", turn_id="turn-41")
        harness.wire.release_cancel_send.set()
        await waiter

        harness.wire.incoming.put_nowait(None)
        assert await anext(stream) == SessionClosed()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        await asyncio.sleep(0)
        assert session._pending_send_failure is None
        assert session._completed_responses == {}
        assert not session._active_responses
        assert not session._cancelling_responses
        assert not session._in_flight_response_cancellations
        assert not session._response_cancellation_tasks

    async def send_failure_wins():
        harness = Harness()
        harness.wire = RacingCancelWire()
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        bind_response(session, "token-1")
        stream = session.events()
        pump = asyncio.create_task(_collect_async(stream))

        waiter = asyncio.create_task(session.cancel_response("resp-1"))
        await harness.wire.cancel_send_started.wait()
        harness.wire.release_cancel_send.set()
        with pytest.raises(RuntimeError, match="cancel send exploded"):
            await waiter
        harness.wire.incoming.put_nowait(cancelled)
        received = await pump
        await asyncio.sleep(0)

        assert len(received) == 1
        assert isinstance(received[0], SessionFailure)
        assert received[0].code == "cancellation_failed"
        assert session._terminal_failure is received[0]
        assert not session._active_responses
        assert not session._cancelling_responses
        assert not session._in_flight_response_cancellations
        assert not session._response_cancellation_tasks

    async def _collect_async(stream):
        return [event async for event in stream]

    asyncio.run(terminal_wins())
    asyncio.run(send_failure_wins())


def test_eof_cleanup_does_not_revoke_terminal_supersession_of_late_cancel_failure():
    class RetainedCancelWire(FakeWire):
        def __init__(self):
            super().__init__()
            self.incoming = asyncio.Queue()
            self.cancel_started = asyncio.Event()
            self.release_cancel = asyncio.Event()

        async def send_json(self, messages):
            messages = tuple(messages)
            self.sent.extend(messages)
            if messages[0]["type"] == "response.cancel":
                self.cancel_started.set()
                await self.release_cancel.wait()
                raise RuntimeError("late cancel secret-free failure")

        async def __anext__(self):
            event = await self.incoming.get()
            if event is None:
                raise core_rt.OpenAIWireEOF("")
            return event

        async def close(self):
            if not self.closed:
                self.closed = True
                self.incoming.put_nowait(None)

    async def scenario():
        harness = Harness()
        harness.wire = RetainedCancelWire()
        session = await harness.provider(token_factory=lambda: "token-1").open_session(setup())
        await session.start_response(response_request())
        stream = session.events()
        harness.wire.incoming.put_nowait(
            {
                "type": "response.created",
                "response": {"id": "resp-1", "metadata": {"correlation": "token-1"}},
            }
        )
        assert await anext(stream) == ResponseStarted(response_id="resp-1", turn_id="turn-41")
        waiter = asyncio.create_task(session.cancel_response("resp-1"))
        await harness.wire.cancel_started.wait()
        harness.wire.incoming.put_nowait(
            {
                "type": "response.done",
                "response": {
                    "id": "resp-1",
                    "status": "cancelled",
                    "metadata": {"correlation": "token-1"},
                    "output": [],
                    "status_details": {"type": "cancelled", "reason": "client_cancelled"},
                },
            }
        )
        assert await anext(stream) == Interruption(response_id="resp-1", turn_id="turn-41")
        harness.wire.incoming.put_nowait(None)
        assert await anext(stream) == SessionClosed()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        harness.wire.release_cancel.set()
        await waiter
        await asyncio.sleep(0)
        assert session._terminal_failure is None
        assert session._pending_send_failure is None
        assert not session._terminal_cancel_supersessions
        assert not session._in_flight_response_cancellations
        assert not session._response_cancellation_tasks
        assert not session._detached_close_tasks

    asyncio.run(scenario())


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
        ([{"type": "response.created", "response": {"id": "response-1"}}], "correlation"),
        (
            [{"type": "response.function_call_arguments.done", "call_id": "call-1"}],
            "function",
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
    assert failures[0].message == "provider protocol failure"
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
    assert received[0].message == "provider protocol failure"
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
