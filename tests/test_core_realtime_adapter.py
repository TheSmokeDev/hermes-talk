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
    InputTranscript,
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
    session, correlation, *, response_id="resp-1", item_id="item-1", transcript="words"
):
    for event in (
        {
            "type": "response.output_audio.delta",
            "response_id": response_id,
            "item_id": item_id,
            "delta": "cGNt",
        },
        {
            "type": "response.output_audio_transcript.done",
            "response_id": response_id,
            "item_id": item_id,
            "transcript": transcript,
        },
        {
            "type": "response.output_audio.done",
            "response_id": response_id,
            "item_id": item_id,
        },
    ):
        session._map_event(event)
    return session._map_event(
        response_done_event(
            correlation,
            response_id=response_id,
            item_id=item_id,
            transcript=transcript,
        )
    )


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
            "delta": "cGNt",
        },
        {
            "type": "response.output_audio_transcript.delta",
            "response_id": response_id,
            "item_id": item_id,
            "delta": transcript[:1],
        },
        {
            "type": "response.output_audio_transcript.done",
            "response_id": response_id,
            "item_id": item_id,
            "transcript": transcript,
        },
        {
            "type": "response.output_audio.done",
            "response_id": response_id,
            "item_id": item_id,
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
            "delta": "cGNt",
        },
        {
            "type": "response.output_audio_transcript.delta",
            "response_id": "resp-1",
            "item_id": "item-out-1",
            "delta": "Exact ",
        },
        {
            "type": "response.output_audio_transcript.done",
            "response_id": "resp-1",
            "item_id": "item-out-1",
            "transcript": "Exact words, exactly.",
        },
        {
            "type": "response.output_audio.done",
            "response_id": "resp-1",
            "item_id": "item-out-1",
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
            data=b"pcm", item_id="item-out-1", response_id="resp-1", turn_id="turn-41"
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
        events = [
            created,
            {
                "type": "response.output_audio.delta",
                "response_id": "resp-1",
                "item_id": "item-1",
                "delta": "cGNt",
            },
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
    assert match in received[-1].message
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
    for events, match in ((replay_events, "replayed"), (wrong_item_events, "identity")):
        harness = Harness(events)

        async def scenario(harness=harness, correlation=correlation):
            provider = harness.provider(token_factory=lambda: correlation)
            session = await provider.open_session(setup())
            await session.start_response(response_request())
            return [event async for event in session.events()]

        received = asyncio.run(scenario())
        assert isinstance(received[-1], SessionFailure)
        assert match in received[-1].message


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
            "message",
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
            "shape",
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
                "delta": "cGNt",
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
                "delta": "cGNt",
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


def test_valid_live_output_lifecycle_completes_exactly_once_and_cleans_terminal_state():
    correlation = "token-1"
    created = {
        "type": "response.created",
        "response": {"id": "resp-1", "metadata": {"correlation": correlation}},
    }
    harness = Harness([created, *valid_output_lifecycle_events(correlation)])

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
    assert not session._completed_responses


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
