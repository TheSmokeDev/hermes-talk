"""Grok (xAI) adapter tests with a scripted socket and no network.

Mirrors the OpenAI adapter tests against the same neutral contract, plus the
provider-selection and xAI key config lanes. Every key/token here is a fake
short string — no realistic payloads, no network, no real credentials.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import types

import pytest
from fake_realtime import Recorder, build_relay, play_neutral

import talk_auth
import talk_cli
import talk_config
import talk_grok_realtime as grok_rt
import talk_openai_realtime as openai_rt
import talk_realtime as rt

PROVIDER_ENV_NAMES = (
    "TALK_PROVIDER",
    "TALK_GROK_MODEL",
    "TALK_GROK_VOICE",
    "TALK_XAI_API_KEY",
    "XAI_API_KEY",
    "TALK_PREFER_XAI_OAUTH",
    "TALK_OPENAI_API_KEY",
    "OPENAI_API_KEY",
)


@pytest.fixture
def clean_provider_env(monkeypatch):
    for name in PROVIDER_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class _Message:
    def __init__(self, payload, *, message_type="text"):
        self.type = message_type
        self.data = payload if isinstance(payload, str) else json.dumps(payload)


class _Socket:
    def __init__(
        self, events=(), *, fail_send_at=None, close_code=None, socket_exception=None
    ):
        self.events = iter(events)
        self.sent: list[dict] = []
        self.fail_send_at = fail_send_at
        self.close_code = close_code
        self.socket_exception = socket_exception
        self.exited = False

    async def send_json(self, message):
        if self.fail_send_at == len(self.sent):
            raise RuntimeError("scripted send failure")
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            event = next(self.events)
        except StopIteration:
            raise StopAsyncIteration from None
        return event if isinstance(event, _Message) else _Message(event)

    def exception(self):
        return self.socket_exception


class _Context:
    def __init__(self, value, on_exit):
        self.value = value
        self.on_exit = on_exit

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_exc):
        self.on_exit()


class _Client:
    def __init__(self, socket):
        self.socket = socket
        self.exited = False
        self.connect_args = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.exited = True

    def ws_connect(self, *args, **kwargs):
        self.connect_args = (args, kwargs)
        return _Context(self.socket, lambda: setattr(self.socket, "exited", True))


def _setup(*, automatic_response=True):
    return rt.SessionSetup(
        model="grok-voice-test",
        voice="ara",
        instructions="Be brief.",
        tools=(
            rt.ToolDefinition(
                name="search_memory",
                description="Search memory",
                parameters={"type": "object", "properties": {}},
            ),
        ),
        automatic_response=automatic_response,
    )


def _adapter(socket):
    client = _Client(socket)
    aiohttp = types.SimpleNamespace(
        ClientSession=lambda: client,
        WSMsgType=types.SimpleNamespace(TEXT="text", ERROR="error"),
    )
    adapter = grok_rt.GrokRealtimeSession(
        auth_token="raw-token",
        auth_source="test-auth",
        aiohttp_module=aiohttp,
    )
    return adapter, client


# -- session payload and commands ----------------------------------------------


def test_connect_sends_grok_session_update_with_prefixed_voice_and_url_model():
    async def scenario():
        socket = _Socket()
        adapter, client = _adapter(socket)
        await adapter.connect(_setup(automatic_response=False))
        await adapter.close()
        return socket, client, adapter

    socket, client, adapter = asyncio.run(scenario())

    args, kwargs = client.connect_args
    # The model rides the URL query; there is no ephemeral mint — the raw xAI
    # key itself is the bearer, straight from the resolved auth lane.
    assert args[0] == f"{grok_rt.XAI_REALTIME_WS_URL}?model=grok-voice-test"
    assert kwargs["headers"] == {"Authorization": "Bearer raw-token"}

    assert socket.sent[0]["type"] == "session.update"
    session = socket.sent[0]["session"]
    assert session["type"] == "realtime"
    assert "model" not in session
    assert session["instructions"] == "Be brief."
    assert session["output_modalities"] == ["audio"]
    audio = session["audio"]
    assert audio["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert audio["input"]["turn_detection"] == {
        "type": "server_vad",
        "create_response": False,
        "interrupt_response": True,
    }
    assert audio["input"]["transcription"] == {"model": grok_rt.GROK_TRANSCRIPTION_MODEL}
    assert audio["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert audio["output"]["voice"] == "xai_ara"
    assert session["tools"] == [
        {
            "type": "function",
            "name": "search_memory",
            "description": "Search memory",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    assert session["tool_choice"] == "auto"
    assert socket.exited and client.exited
    assert adapter.state is rt.SessionState.CLOSED


def test_wire_voice_prefix_is_applied_exactly_once():
    assert grok_rt._wire_voice("ara") == "xai_ara"
    assert grok_rt._wire_voice("xai_eve") == "xai_eve"


def test_no_tools_means_no_tool_fields():
    setup = rt.SessionSetup(model="grok-voice-test", voice="leo", instructions="Hi.")
    session = grok_rt.build_session_update(setup)["session"]
    assert "tools" not in session
    assert "tool_choice" not in session
    assert session["audio"]["output"]["voice"] == "xai_leo"
    assert session["audio"]["input"]["turn_detection"]["create_response"] is True


def test_commands_encode_the_shared_ga_vocabulary():
    async def scenario():
        socket = _Socket()
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        await adapter.send(
            (
                rt.AddContext(item_id="ctx-1", text="speaker context"),
                rt.AppendInputAudio(data=b"mic"),
                rt.StartResponse(metadata={"speaker": "opaque"}),
                rt.StartResponse(allow_tools=False),
                rt.SubmitToolResult(call_id="call-1", output="done"),
                rt.CancelResponse(),
                rt.TruncateOutput(item_id="item-1", audio_end_ms=75),
                rt.RemoveContext(item_id="ctx-1"),
            )
        )
        await adapter.close()
        return socket

    socket = asyncio.run(scenario())

    assert [message["type"] for message in socket.sent[1:]] == [
        "conversation.item.create",
        "input_audio_buffer.append",
        "response.create",
        "response.create",
        "conversation.item.create",
        "response.cancel",
        "conversation.item.truncate",
        "conversation.item.delete",
    ]
    assert socket.sent[1]["item"]["role"] == "system"
    assert base64.b64decode(socket.sent[2]["audio"]) == b"mic"
    assert socket.sent[3]["response"]["metadata"] == {"speaker": "opaque"}
    assert socket.sent[4]["response"]["tool_choice"] == "none"
    tool_output = socket.sent[5]["item"]
    assert tool_output == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "done",
    }
    assert socket.sent[7] == {
        "type": "conversation.item.truncate",
        "item_id": "item-1",
        "content_index": 0,
        "audio_end_ms": 75,
    }


def test_wire_credentials_are_cleared_after_connect_and_connect_is_one_shot():
    async def scenario():
        socket = _Socket()
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        snapshot = repr(vars(adapter._wire))
        await adapter.close()
        return adapter, snapshot

    adapter, snapshot = asyncio.run(scenario())

    assert adapter._wire._auth_token is None
    assert adapter._wire._auth_source is None
    assert "raw-token" not in snapshot
    assert "test-auth" not in snapshot
    with pytest.raises(rt.RealtimeSessionError, match="only run once"):
        asyncio.run(adapter.connect(_setup()))


# -- event translation -----------------------------------------------------------


def test_server_events_translate_to_neutral_events():
    async def scenario():
        pcm = b"assistant pcm"
        wire_events = [
            {"type": "session.created", "session": {"id": "sess-1"}},
            # Application-level pings and item scaffolding arrive on this wire
            # and must be tolerated without emitting anything.
            {"type": "ping", "timestamp": 123},
            {"type": "conversation.created"},
            {"type": "conversation.item.added", "item": {"id": "input-1"}},
            {"type": "conversation.item.created", "item": {"id": "input-1"}},
            {
                "type": "input_audio_buffer.speech_started",
                "item_id": "input-1",
                "audio_start_ms": 1,
            },
            {
                "type": "input_audio_buffer.speech_stopped",
                "item_id": "input-1",
                "audio_end_ms": 9,
            },
            {"type": "input_audio_buffer.committed", "item_id": "input-1"},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "hello",
            },
            {
                "type": "response.created",
                "response": {"id": "resp-1", "metadata": {"speaker": "opaque"}},
            },
            {"type": "response.output_item.added", "response_id": "resp-1"},
            {
                "type": "response.output_audio.delta",
                "item_id": "out-1",
                "response_id": "resp-1",
                "delta": base64.b64encode(pcm).decode("ascii"),
            },
            {
                "type": "response.output_audio_transcript.delta",
                "response_id": "resp-1",
                "delta": "hi",
            },
            {
                "type": "response.output_audio_transcript.done",
                "response_id": "resp-1",
                "transcript": "hi there",
            },
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call-1",
                "item_id": "item-1",
                "response_id": "resp-1",
                "output_index": 0,
                "name": "search_memory",
                "arguments": "{}",
            },
            {
                "type": "response.done",
                # xAI can ship the literal string "unimplemented" here — an
                # unknown field value must never choke the decoder.
                "response": {"id": "resp-1", "status_details": "unimplemented"},
            },
        ]
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events, pcm

    events, pcm = asyncio.run(scenario())

    assert isinstance(events[0], rt.SessionReady)
    assert events[0].session_id == "sess-1"
    assert isinstance(events[1], rt.SpeechStarted)
    assert events[1].offset_ms == 1
    assert isinstance(events[2], rt.SpeechStopped)
    assert events[2].offset_ms == 9
    assert isinstance(events[3], rt.InputAudioCommitted)
    assert events[4] == rt.Transcript(
        role=rt.TranscriptRole.USER,
        text="hello",
        final=True,
        provenance=rt.TranscriptProvenance.INPUT_AUDIO,
    )
    assert isinstance(events[5], rt.ResponseStarted)
    assert events[5].metadata == {"speaker": "opaque"}
    assert events[6] == rt.OutputAudio(data=pcm, item_id="out-1", response_id="resp-1")
    assert events[7].provenance is rt.TranscriptProvenance.OUTPUT_AUDIO
    assert events[7].final is False
    assert events[8].final is True
    assert events[9] == rt.FunctionCall(
        call_id="call-1",
        item_id="item-1",
        response_id="resp-1",
        name="search_memory",
        arguments="{}",
    )
    assert events[10] == rt.ResponseFinished(response_id="resp-1")
    assert events[11] == rt.SessionTerminated(state=rt.SessionState.CLOSED)
    assert len(events) == 12


def test_cumulative_input_transcripts_dedupe_to_one_final_per_utterance():
    # Live smoke against the real xAI API, 2026-08-28: one spoken utterance
    # produced SIX final user transcripts in the terminal because xAI streams
    # CUMULATIVE input-transcription snapshots (each repeats the full text so
    # far, sometimes repeating an identical snapshot) and can emit the
    # terminal ``.completed`` more than once per input item — including one
    # last copy after ``input_audio_buffer.committed``. The neutral contract
    # is live non-final partials plus exactly ONE final per utterance.
    full = "Hey Grok, what is the weather in Paris right now?"
    wire_events = [
        {"type": "session.created", "session": {"id": "sess-1"}},
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "input-1",
            "audio_start_ms": 0,
        },
        {
            "type": "conversation.item.input_audio_transcription.updated",
            "item_id": "input-1",
            "transcript": "Hey Grok.",
        },
        # Identical repeated snapshot: suppressed, not re-emitted.
        {
            "type": "conversation.item.input_audio_transcription.updated",
            "item_id": "input-1",
            "transcript": "Hey Grok.",
        },
        {
            "type": "conversation.item.input_audio_transcription.updated",
            "item_id": "input-1",
            "transcript": "Hey Grok, what is the weather in",
        },
        # Pre-commit completions are still cumulative snapshots, not finals.
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "input-1",
            "transcript": full,
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "input-1",
            "transcript": full,
        },
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "input-1",
            "audio_end_ms": 2400,
        },
        {"type": "input_audio_buffer.committed", "item_id": "input-1"},
        # The true completion: exactly one final, after the commit.
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "input-1",
            "transcript": full,
        },
        # A second post-commit copy must not print twice.
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "input-1",
            "transcript": full,
        },
        # A new utterance with IDENTICAL text is a new turn, not a dupe.
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "input-2",
            "audio_start_ms": 5000,
        },
        {"type": "input_audio_buffer.committed", "item_id": "input-2"},
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "input-2",
            "transcript": full,
        },
    ]

    async def scenario():
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    transcripts = [
        event
        for event in events
        if isinstance(event, rt.Transcript)
        and event.provenance is rt.TranscriptProvenance.INPUT_AUDIO
    ]
    assert [(event.text, event.final) for event in transcripts] == [
        ("Hey Grok.", False),
        ("Hey Grok, what is the weather in", False),
        (full, False),
        (full, True),
        (full, True),
    ]
    assert [event.text for event in transcripts if event.final] == [full, full]


def test_session_updated_echo_is_a_receipt_not_authority():
    # The normalized echo carries turn_detection/tools at the session root and
    # may omit the id; it is never parsed for authority and never fails.
    normalized_echo = {
        "type": "session.updated",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "tools": [{"type": "function", "function": {"name": "search_memory"}}],
        },
    }
    assert grok_rt.decode_event(normalized_echo) is None
    with_id = {
        "type": "session.updated",
        "session": {"id": "sess-9", "turn_detection": {"type": "server_vad"}},
    }
    assert grok_rt.decode_event(with_id) == rt.SessionReady(session_id="sess-9")


def test_malformed_wire_identifiers_become_neutral_failures():
    async def scenario():
        adapter, _client = _adapter(
            _Socket(
                [
                    {"type": "response.created", "response": {"id": " padded "}},
                    {
                        "type": "response.function_call_arguments.done",
                        "call_id": "",
                        "name": "tool",
                        "arguments": "{}",
                    },
                ]
            )
        )
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    assert [type(event) for event in events] == [
        rt.ProviderFailure,
        rt.ProviderFailure,
        rt.SessionTerminated,
    ]
    assert all("identifier" in event.detail for event in events[:2])


def test_barge_in_speech_started_drives_cancel_through_the_relay():
    # The full live path: wire speech_started decodes to SpeechStarted, and a
    # relay holding an active response answers with CancelResponse before any
    # truncate — the ordering barge-in correctness depends on.
    async def scenario():
        wire_events = [
            {"type": "response.created", "response": {"id": "resp-1"}},
            {"type": "input_audio_buffer.speech_started", "item_id": "input-1"},
        ]
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    recorder = Recorder()
    relay = build_relay(recorder)
    play_neutral(relay, recorder, events[:-1])  # drop the terminal EOF event

    assert [type(event) for event in events[:-1]] == [rt.ResponseStarted, rt.SpeechStarted]
    assert recorder.command_types == ["CancelResponse"]
    assert recorder.barge_ins == 1
    assert grok_rt.encode_command(recorder.commands[0]) == {"type": "response.cancel"}


# -- truncation degrade path -----------------------------------------------------


def test_truncate_degrades_to_cancel_only_after_a_wire_refusal(caplog):
    refusal = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "Unknown event type 'conversation.item.truncate' (unimplemented)",
        },
    }

    async def scenario():
        socket = _Socket([refusal])
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        with caplog.at_level(logging.WARNING, logger="talk_grok_realtime"):
            # First truncate goes out as a real truncation attempt.
            await adapter.send((rt.TruncateOutput(item_id="item-1", audio_end_ms=75),))
            # The refusal is consumed as a degrade receipt, not a failure.
            observed = [await adapter.__anext__()]
            # Every later truncate degrades to cancel-only.
            await adapter.send((rt.TruncateOutput(item_id="item-2", audio_end_ms=10),))
            await adapter.send((rt.CancelResponse(),))
        await adapter.close()
        return socket, observed

    socket, observed = asyncio.run(scenario())

    assert socket.sent[1]["type"] == "conversation.item.truncate"
    assert observed == [rt.SessionTerminated(state=rt.SessionState.CLOSED)]
    assert [message["type"] for message in socket.sent[2:]] == [
        "response.cancel",
        "response.cancel",
    ]
    warnings = [
        record
        for record in caplog.records
        if record.name == "talk_grok_realtime" and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "cancel-only" in warnings[0].getMessage()


def test_generic_errors_surface_and_do_not_flip_the_truncate_flag():
    async def scenario():
        socket = _Socket(
            [
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "conversation item item-9 not found",
                    },
                }
            ]
        )
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        failure = await adapter.__anext__()
        # A truncation failure that does NOT name the event as unimplemented
        # keeps the truncation lane armed.
        await adapter.send((rt.TruncateOutput(item_id="item-1", audio_end_ms=75),))
        await adapter.close()
        return socket, failure

    socket, failure = asyncio.run(scenario())

    assert failure == rt.ProviderFailure(detail="conversation item item-9 not found")
    assert socket.sent[1]["type"] == "conversation.item.truncate"


# -- provider selection, config, and auth ----------------------------------------


def test_provider_defaults_to_openai_and_normalizes_case(clean_provider_env):
    assert talk_config.talk_provider() == "openai"
    clean_provider_env.setenv("TALK_PROVIDER", "  GROK ")
    assert talk_config.talk_provider() == "grok"


def test_unknown_provider_refuses_closed(clean_provider_env):
    clean_provider_env.setenv("TALK_PROVIDER", "grok-voice-latest")
    with pytest.raises(talk_config.TalkConfigError, match="TALK_PROVIDER"):
        talk_config.talk_provider()


def test_grok_model_and_voice_knobs(clean_provider_env):
    assert talk_config.talk_grok_model() == talk_config.DEFAULT_GROK_MODEL
    assert talk_config.talk_grok_voice() == "ara"
    clean_provider_env.setenv("TALK_GROK_MODEL", " grok-voice-think-fast-9.9 ")
    clean_provider_env.setenv("TALK_GROK_VOICE", " EVE ")
    assert talk_config.talk_grok_model() == "grok-voice-think-fast-9.9"
    assert talk_config.talk_grok_voice() == "eve"


def test_unknown_grok_voice_refuses_closed(clean_provider_env):
    clean_provider_env.setenv("TALK_GROK_VOICE", "morgan-freeman")
    with pytest.raises(talk_config.TalkConfigError, match="morgan-freeman"):
        talk_config.talk_grok_voice()


def test_xai_scoped_key_wins_over_shared(clean_provider_env):
    clean_provider_env.setenv("TALK_XAI_API_KEY", "xai-scoped")
    clean_provider_env.setenv("XAI_API_KEY", "xai-shared")
    assert talk_config.resolve_xai_key() == "xai-scoped"


def test_xai_shared_key_used_when_scoped_unset(clean_provider_env):
    clean_provider_env.setenv("XAI_API_KEY", "  xai-shared  ")
    assert talk_config.resolve_xai_key() == "xai-shared"


def test_xai_scoped_key_set_but_empty_is_a_refusal(clean_provider_env):
    clean_provider_env.setenv("TALK_XAI_API_KEY", "   ")
    clean_provider_env.setenv("XAI_API_KEY", "xai-shared")
    with pytest.raises(talk_config.TalkConfigError, match="TALK_XAI_API_KEY"):
        talk_config.resolve_xai_key()


def test_xai_shared_key_set_but_empty_is_a_refusal(clean_provider_env):
    clean_provider_env.setenv("XAI_API_KEY", "")
    with pytest.raises(talk_config.TalkConfigError, match="XAI_API_KEY"):
        talk_config.resolve_xai_key()


def test_no_xai_key_at_all_raises(clean_provider_env):
    with pytest.raises(talk_config.TalkConfigError, match="no xAI key"):
        talk_config.resolve_xai_key()


def test_both_keys_set_uses_talk_provider_not_key_presence(clean_provider_env):
    # The selection trap the knob exists to close: an operator holding keys
    # for BOTH providers must get the provider they named, never a switch
    # inferred from key presence.
    clean_provider_env.setenv("OPENAI_API_KEY", "openai-test")
    clean_provider_env.setenv("TALK_XAI_API_KEY", "xai-scoped")
    auth = talk_auth.TalkAuth(token="openai-test", source="env", detail="test")
    assert isinstance(talk_cli._realtime_session(auth), openai_rt.OpenAIRealtimeSession)
    clean_provider_env.setenv("TALK_PROVIDER", "grok")
    grok_auth = talk_cli._grok_auth()
    assert grok_auth.token == "xai-scoped"
    assert grok_auth.source == talk_auth.SOURCE_CONFIGURED
    session = talk_cli._realtime_session(grok_auth)
    assert isinstance(session, grok_rt.GrokRealtimeSession)
    assert session.auth_token == "xai-scoped"


def test_factory_propagates_the_unknown_provider_refusal(clean_provider_env):
    clean_provider_env.setenv("TALK_PROVIDER", "alexa")
    auth = talk_auth.TalkAuth(token="unit-test", source="env", detail="test")
    with pytest.raises(talk_config.TalkConfigError, match="TALK_PROVIDER"):
        talk_cli._realtime_session(auth)


def test_grok_auth_falls_back_to_the_shared_key(clean_provider_env):
    clean_provider_env.setenv("XAI_API_KEY", "xai-shared")
    auth = talk_cli._grok_auth()
    assert auth.token == "xai-shared"
    assert auth.source == talk_auth.SOURCE_ENV


def test_grok_auth_delegates_to_the_grok_auth_resolver(clean_provider_env, monkeypatch):
    sentinel = talk_auth.TalkAuth(token="sentinel", source="xai-oauth", detail="test")
    monkeypatch.setattr(talk_cli.talk_grok_auth, "resolve_grok_auth", lambda: sentinel)
    assert talk_cli._grok_auth() is sentinel


# -- handshake remediation -----------------------------------------------------


class WSServerHandshakeError(Exception):
    """Same class NAME as aiohttp's; the helper matches by name + status."""

    def __init__(self, status):
        super().__init__(f"{status}, message='Invalid response status'")
        self.status = status


@pytest.mark.parametrize(
    ("status", "source", "expected"),
    [
        (401, "xai-oauth", "xAI OAuth token rejected — run `hermes auth add xai-oauth`"),
        (401, "env", "xAI API key rejected (401)"),
        (401, "configured", "xAI API key rejected (401)"),
        (
            403,
            "xai-oauth",
            "your xAI subscription tier does not include realtime API access; "
            "set `XAI_API_KEY` for Grok voice",
        ),
        (403, "env", "xAI refused this key for realtime (403)"),
    ],
)
def test_handshake_remediation_names_the_lane(status, source, expected):
    exc = WSServerHandshakeError(status)
    assert grok_rt.handshake_remediation(exc, auth_source=source) == expected


def test_handshake_remediation_leaves_other_failures_alone():
    for exc in (WSServerHandshakeError(500), WSServerHandshakeError("401"), OSError("reset")):
        assert grok_rt.handshake_remediation(exc, auth_source="xai-oauth") is None


class _RejectingContext:
    def __init__(self, exc):
        self.exc = exc

    async def __aenter__(self):
        raise self.exc

    async def __aexit__(self, *_exc):
        return False


class _RejectingClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def ws_connect(self, *_args, **_kwargs):
        return _RejectingContext(WSServerHandshakeError(401))


def test_connect_turns_an_oauth_401_into_the_relogin_remediation():
    aiohttp = types.SimpleNamespace(
        ClientSession=lambda: _RejectingClient(),
        WSMsgType=types.SimpleNamespace(TEXT="text", ERROR="error"),
    )
    adapter = grok_rt.GrokRealtimeSession(
        auth_token="oauth-canary", auth_source="xai-oauth", aiohttp_module=aiohttp
    )

    with pytest.raises(rt.RealtimeSessionError) as info:
        asyncio.run(adapter.connect(_setup()))

    assert str(info.value) == "xAI OAuth token rejected — run `hermes auth add xai-oauth`"
    assert "oauth-canary" not in str(info.value)
    assert adapter.state is rt.SessionState.FAILED
