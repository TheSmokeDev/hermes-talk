"""Gemini Live adapter tests with a scripted socket and no network.

Mirrors the Grok adapter tests against the same neutral contract, plus the
24k->16k input resampler, the key-in-URL redaction lanes, and the Gemini key
config lanes. Every key/token here is a fake short string — no realistic
payloads, no network, no real credentials.
"""

from __future__ import annotations

import array
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
import talk_gemini_realtime as gemini_rt
import talk_openai_realtime as openai_rt
import talk_realtime as rt

PROVIDER_ENV_NAMES = (
    "TALK_PROVIDER",
    "TALK_GEMINI_MODEL",
    "TALK_GEMINI_VOICE",
    "TALK_GEMINI_API_KEY",
    "GEMINI_API_KEY",
    "TALK_OPENAI_API_KEY",
    "OPENAI_API_KEY",
)

FAKE_KEY = "gemini-fake-key"


@pytest.fixture
def clean_provider_env(monkeypatch):
    for name in PROVIDER_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class _Message:
    def __init__(self, payload, *, message_type="text"):
        self.type = message_type
        if message_type == "binary":
            # The Google endpoint speaks its JSON in binary frames on some
            # connections; a binary payload is bytes, not str.
            self.data = (
                payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            )
        else:
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
    def __init__(self, socket=None, *, connect_error: Exception | None = None):
        self.socket = socket
        self.connect_error = connect_error
        self.exited = False
        self.connect_args = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.exited = True

    def ws_connect(self, *args, **kwargs):
        self.connect_args = (args, kwargs)
        if self.connect_error is not None:
            raise self.connect_error
        return _Context(self.socket, lambda: setattr(self.socket, "exited", True))


def _setup(*, automatic_response=True, turn_detection=None):
    kwargs = {}
    if turn_detection is not None:
        kwargs["turn_detection"] = turn_detection
    return rt.SessionSetup(
        model="gemini-live-test",
        voice="Puck",
        instructions="Be brief.",
        tools=(
            rt.ToolDefinition(
                name="search_memory",
                description="Search memory",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                },
            ),
        ),
        automatic_response=automatic_response,
        **kwargs,
    )


def _adapter(socket, *, key: str = FAKE_KEY):
    client = _Client(socket)
    aiohttp = types.SimpleNamespace(
        ClientSession=lambda: client,
        WSMsgType=types.SimpleNamespace(TEXT="text", BINARY="binary", ERROR="error"),
    )
    adapter = gemini_rt.GeminiRealtimeSession(
        auth_token=key,
        auth_source="test-auth",
        aiohttp_module=aiohttp,
    )
    return adapter, client


def _b64(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


# -- setup message and connect ---------------------------------------------------


def test_connect_sends_setup_with_key_in_url_and_models_prefix():
    async def scenario():
        socket = _Socket()
        adapter, client = _adapter(socket)
        await adapter.connect(_setup(automatic_response=True))
        await adapter.close()
        return socket, client, adapter

    socket, client, adapter = asyncio.run(scenario())

    args, kwargs = client.connect_args
    # The API key rides the URL query on this lane; there are no auth headers.
    assert args[0] == f"{gemini_rt.GEMINI_LIVE_WS_URL}?key={FAKE_KEY}"
    assert "headers" not in kwargs

    assert list(socket.sent[0]) == ["setup"]
    setup = socket.sent[0]["setup"]
    assert setup["model"] == "models/gemini-live-test"
    generation = setup["generationConfig"]
    assert generation["responseModalities"] == ["AUDIO"]
    voice = generation["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]
    assert voice == {"voiceName": "Puck"}
    assert setup["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    assert setup["outputAudioTranscription"] == {}
    assert setup["inputAudioTranscription"] == {}
    # Session resumption is enabled (record-only in v1), and context-window
    # compression rides the server defaults — without it audio-only sessions
    # hard-cap near 15 minutes.
    assert setup["sessionResumption"] == {}
    assert setup["contextWindowCompression"] == {"slidingWindow": {}}
    # Function-declaration schema types are UPPERCASE, recursively.
    declarations = setup["tools"][0]["functionDeclarations"]
    assert declarations == [
        {
            "name": "search_memory",
            "description": "Search memory",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "query": {"type": "STRING"},
                    "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
            },
        }
    ]
    assert socket.exited and client.exited
    assert adapter.state is rt.SessionState.CLOSED


def test_provider_native_turn_detection_keeps_realtime_input_config_omitted():
    message = gemini_rt.build_setup_message(
        _setup(
            turn_detection=rt.RealtimeTurnDetection(
                mode=rt.RealtimeTurnDetectionMode.PROVIDER_NATIVE
            )
        )
    )

    assert "realtimeInputConfig" not in message["setup"]


@pytest.mark.parametrize(
    "turn_detection",
    [
        rt.RealtimeTurnDetection(mode=rt.RealtimeTurnDetectionMode.SERVER_VAD),
        rt.RealtimeTurnDetection(
            mode=rt.RealtimeTurnDetectionMode.SEMANTIC_VAD,
            semantic_eagerness=rt.RealtimeSemanticEagerness.MEDIUM,
        ),
    ],
)
def test_explicit_non_native_turn_detection_is_refused_before_connection(
    turn_detection,
):
    socket = _Socket()
    adapter, client = _adapter(socket)

    with pytest.raises(rt.RealtimeSessionError, match="supports only provider-native"):
        asyncio.run(adapter.connect(_setup(turn_detection=turn_detection)))

    assert client.connect_args is None
    assert socket.sent == []
    assert adapter.state is rt.SessionState.FAILED


def test_wire_model_prefix_is_applied_exactly_once():
    assert gemini_rt._wire_model("gemini-live-test") == "models/gemini-live-test"
    assert gemini_rt._wire_model("models/gemini-live-test") == "models/gemini-live-test"


def test_no_tools_means_no_tool_fields():
    setup = rt.SessionSetup(model="gemini-live-test", voice="Kore", instructions="Hi.")
    payload = gemini_rt.build_setup_message(setup)["setup"]
    assert "tools" not in payload
    voice = payload["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]
    assert voice == {"voiceName": "Kore"}


def test_automatic_response_false_is_refused_not_degraded():
    async def scenario():
        socket = _Socket()
        adapter, client = _adapter(socket)
        with pytest.raises(rt.RealtimeSessionError, match="automatic_response"):
            await adapter.connect(_setup(automatic_response=False))
        return socket, client, adapter

    socket, client, adapter = asyncio.run(scenario())

    # The refusal happens before any network: no socket was opened, nothing
    # was sent, and the credential never left the factory shape.
    assert client.connect_args is None
    assert socket.sent == []
    assert adapter.state is rt.SessionState.FAILED


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
    assert FAKE_KEY not in snapshot
    assert "test-auth" not in snapshot
    with pytest.raises(rt.RealtimeSessionError, match="only run once"):
        asyncio.run(adapter.connect(_setup()))


# -- command encoding --------------------------------------------------------------


def test_commands_encode_the_live_vocabulary():
    async def scenario():
        socket = _Socket()
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        await adapter.send(
            (
                rt.AddContext(item_id="ctx-1", text="speaker context"),
                # 6 samples at 24kHz -> 4 samples at 16kHz.
                rt.AppendInputAudio(data=array.array("h", [1, 2, 4, 7, 8, 10]).tobytes()),
                rt.StartResponse(metadata={"speaker": "opaque"}),
                rt.StartResponse(allow_tools=False),
                rt.CancelResponse(),
                rt.TruncateOutput(item_id="item-1", audio_end_ms=75),
                rt.RemoveContext(item_id="ctx-1"),
            )
        )
        await adapter.close()
        return socket

    socket = asyncio.run(scenario())

    context, audio, trigger_a, trigger_b = socket.sent[1:]
    assert context == {
        "clientContent": {
            "turns": [{"role": "user", "parts": [{"text": "speaker context"}]}],
            "turnComplete": False,
        }
    }
    realtime = audio["realtimeInput"]["audio"]
    assert realtime["mimeType"] == gemini_rt.INPUT_AUDIO_MIME_TYPE
    pcm = array.array("h")
    pcm.frombytes(base64.b64decode(realtime["data"]))
    assert list(pcm) == [1, 3, 7, 9]
    assert trigger_a == {"clientContent": {"turnComplete": True}}
    assert trigger_b == {"clientContent": {"turnComplete": True}}
    # Cancel, truncate, and context-delete have no Live wire command: nothing
    # was sent for them, and no upstream call was faked.
    assert len(socket.sent) == 5


def test_start_response_is_dropped_when_a_tool_result_rides_the_batch():
    async def scenario():
        socket = _Socket(
            [
                {
                    "toolCall": {
                        "functionCalls": [
                            {"id": "fc-1", "name": "search_memory", "args": {"q": "x"}}
                        ]
                    }
                }
            ]
        )
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.send(
            (
                rt.SubmitToolResult(call_id="fc-1", output="done"),
                rt.StartResponse(),
            )
        )
        await adapter.close()
        return socket, events

    socket, events = asyncio.run(scenario())

    # The toolCall opens and closes a response around the call, mirroring the
    # GA shape the relay's tool coordinator flushes on.
    assert [type(event) for event in events] == [
        rt.ResponseStarted,
        rt.FunctionCall,
        rt.ResponseFinished,
        rt.SessionTerminated,
    ]
    # The toolResponse alone makes the model speak (probe-verified), so the
    # continuation trigger must not double the answer.
    assert socket.sent[1:] == [
        {
            "toolResponse": {
                "functionResponses": [
                    {"id": "fc-1", "name": "search_memory", "response": {"result": "done"}}
                ]
            }
        }
    ]


def test_tool_response_omits_an_unobserved_call_name():
    async def scenario():
        socket = _Socket()
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        await adapter.send((rt.SubmitToolResult(call_id="fc-9", output="late"),))
        await adapter.close()
        return socket

    socket = asyncio.run(scenario())

    responses = socket.sent[1]["toolResponse"]["functionResponses"]
    assert responses == [{"id": "fc-9", "response": {"result": "late"}}]


def test_sub_trio_audio_remainder_yields_no_frame_until_completed():
    async def scenario():
        socket = _Socket()
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        await adapter.send((rt.AppendInputAudio(data=array.array("h", [9, 9]).tobytes()),))
        await adapter.send((rt.AppendInputAudio(data=array.array("h", [9]).tobytes()),))
        await adapter.close()
        return socket

    socket = asyncio.run(scenario())

    # Two samples cannot make a trio; the third completes it and one 16kHz
    # frame carrying both outputs goes out.
    assert len(socket.sent) == 2
    pcm = array.array("h")
    pcm.frombytes(base64.b64decode(socket.sent[1]["realtimeInput"]["audio"]["data"]))
    assert list(pcm) == [9, 9]


def test_degrade_commands_log_one_receipt_each(caplog):
    async def scenario():
        socket = _Socket()
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        with caplog.at_level(logging.WARNING, logger="talk_gemini_realtime"):
            await adapter.send((rt.CancelResponse(),))
            await adapter.send((rt.TruncateOutput(item_id="i-1", audio_end_ms=10),))
            await adapter.send((rt.CancelResponse(),))
            await adapter.send((rt.RemoveContext(item_id="c-1"),))
            await adapter.send((rt.RemoveContext(item_id="c-2"),))
        await adapter.close()
        return socket

    socket = asyncio.run(scenario())

    assert len(socket.sent) == 1  # only the setup message went out
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "talk_gemini_realtime" and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 2
    assert any("cancel/truncate" in message for message in warnings)
    assert any("context delete" in message for message in warnings)


# -- event translation -----------------------------------------------------------


def test_server_events_translate_to_neutral_events():
    pcm = b"assistant pcm"

    async def scenario():
        wire_events = [
            {"setupComplete": {}},
            # Unknown and empty shapes arrive on this wire and are tolerated.
            {"usageOnlyFutureFrame": {"x": 1}},
            {"serverContent": {}},
            {"serverContent": {"usageMetadata": {"totalTokenCount": 12}}},
            {
                "sessionResumptionUpdate": {"newHandle": "handle-1", "resumable": True}
            },
            {"serverContent": {"inputTranscription": {"text": "hello "}}},
            {"serverContent": {"inputTranscription": {"text": "there"}}},
            {
                "serverContent": {
                    "modelTurn": {
                        "parts": [
                            {"text": "thinking out loud"},
                            {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": _b64(pcm)}},
                        ]
                    }
                }
            },
            {"serverContent": {"outputTranscription": {"text": "hi"}}},
            {
                "toolCall": {
                    "functionCalls": [
                        {"id": "fc-1", "name": "search_memory", "args": {"query": "x"}}
                    ]
                }
            },
            {
                "serverContent": {
                    "turnComplete": True,
                    "usageMetadata": {"totalTokenCount": 40},
                }
            },
        ]
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return adapter, events

    adapter, events = asyncio.run(scenario())

    assert events[0] == rt.SessionReady(session_id=adapter._session_id)
    assert events[0].session_id.startswith("gemini-live-")
    # The buffered input transcript flushes as ONE final user turn when the
    # model's answer opens, not chunk-at-a-time.
    assert events[1] == rt.Transcript(
        role=rt.TranscriptRole.USER,
        text="hello there",
        final=True,
        provenance=rt.TranscriptProvenance.INPUT_AUDIO,
    )
    assert isinstance(events[2], rt.ResponseStarted)
    assert events[2].response_id is None
    assert events[3] == rt.OutputAudio(data=pcm)
    assert events[4] == rt.Transcript(
        role=rt.TranscriptRole.ASSISTANT,
        text="hi",
        final=False,
        provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
    )
    # Tool args arrive as a parsed dict and are translated to the contract's
    # JSON string; the response closes so the tool coordinator can flush.
    assert events[5] == rt.FunctionCall(
        call_id="fc-1",
        name="search_memory",
        arguments=json.dumps({"query": "x"}),
    )
    assert events[6] == rt.ResponseFinished(response_id=None)
    function_calls = [event for event in events if isinstance(event, rt.FunctionCall)]
    assert len(function_calls) == 1
    finishes = [event for event in events if isinstance(event, rt.ResponseFinished)]
    assert finishes == [rt.ResponseFinished(response_id=None)]  # turnComplete reopened nothing
    assert isinstance(events[-1], rt.SessionTerminated)
    assert events[-1].state is rt.SessionState.CLOSED
    assert adapter.resumption_handle == "handle-1"


def test_turn_complete_after_audio_emits_final_transcript_then_finish():
    async def scenario():
        wire_events = [
            {"serverContent": {"modelTurn": {"parts": []}}},
            {"serverContent": {"outputTranscription": {"text": "done"}}},
            {"serverContent": {"generationComplete": True}},
            {"serverContent": {"turnComplete": True}},
        ]
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    assert [type(event) for event in events] == [
        rt.ResponseStarted,  # modelTurn opens the generation, even part-less
        rt.Transcript,  # assistant delta
        rt.Transcript,  # assistant final (empty text; relay folds its deltas)
        rt.ResponseFinished,
        rt.SessionTerminated,
    ]
    assert events[2].final is True and events[2].text == ""
    # generationComplete already closed the generation; the turnComplete that
    # follows must not emit a second finish.
    assert [e for e in events if isinstance(e, rt.ResponseFinished)] == [
        rt.ResponseFinished(response_id=None)
    ]


def test_model_turn_with_empty_parts_still_opens_a_generation():
    async def scenario():
        wire_events = [
            {"serverContent": {"modelTurn": {"parts": []}}},
            {"serverContent": {"turnComplete": True}},
        ]
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    assert isinstance(events[0], rt.ResponseStarted)
    assert events[1].final is True
    assert isinstance(events[2], rt.ResponseFinished)


def test_malformed_tool_call_identifiers_become_terminal_failures():
    async def scenario():
        wire_events = [
            {"toolCall": {"functionCalls": [{"id": "  ", "name": "tool", "args": {}}]}},
        ]
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    assert [type(event) for event in events] == [rt.ProviderFailure, rt.SessionTerminated]
    assert "identifier" in events[0].detail
    assert events[0].terminal is True
    assert events[1].state is rt.SessionState.FAILED


def test_malformed_audio_payload_is_a_non_terminal_failure():
    async def scenario():
        wire_events = [
            {
                "serverContent": {
                    "modelTurn": {
                        "parts": [{"inlineData": {"mimeType": "audio/pcm", "data": "!!!"}}]
                    }
                }
            },
        ]
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    failures = [event for event in events if isinstance(event, rt.ProviderFailure)]
    assert [failure.detail for failure in failures] == ["Provider sent a malformed audio payload"]
    assert failures[0].terminal is False
    assert isinstance(events[-1], rt.SessionTerminated)
    assert events[-1].state is rt.SessionState.CLOSED


def test_provider_error_frame_maps_to_a_failure_event():
    async def scenario():
        adapter, _client = _adapter(
            _Socket([{"error": {"code": 400, "message": "bad setup"}}])
        )
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    assert events[0] == rt.ProviderFailure(detail="bad setup")


# -- barge-in through the relay ----------------------------------------------------


def test_interrupted_drives_barge_in_and_fences_the_tail():
    heard, tail = b"heard", b"tail"

    async def scenario():
        wire_events = [
            {
                "serverContent": {
                    "modelTurn": {
                        "parts": [{"inlineData": {"mimeType": "audio/pcm", "data": _b64(heard)}}]
                    }
                }
            },
            # The interrupted frame still carries tail audio of the dead
            # generation; SpeechStarted must land first so the relay fences it.
            {
                "serverContent": {
                    "interrupted": True,
                    "modelTurn": {
                        "parts": [{"inlineData": {"mimeType": "audio/pcm", "data": _b64(tail)}}]
                    },
                }
            },
        ]
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    assert [type(event) for event in events[:-1]] == [
        rt.ResponseStarted,
        rt.OutputAudio,
        rt.SpeechStarted,
        rt.OutputAudio,  # present in the stream, fenced by the relay below
        rt.ResponseFinished,
    ]
    recorder = Recorder()
    relay = build_relay(recorder)
    play_neutral(relay, recorder, events[:-1])

    assert recorder.audio == [heard]  # the tail never reached playback
    assert recorder.barge_ins == 1
    assert recorder.command_types == ["CancelResponse"]


def test_interrupted_with_no_open_generation_is_tolerated():
    async def scenario():
        adapter, _client = _adapter(_Socket([{"serverContent": {"interrupted": True}}]))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    assert [type(event) for event in events] == [rt.SessionTerminated]


def test_cancel_response_after_interrupt_sends_nothing_upstream():
    async def scenario():
        socket = _Socket([{"serverContent": {"interrupted": True}}])
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        _events = [event async for event in adapter]
        # What the relay answered the barge-in with.
        await adapter.send((rt.CancelResponse(),))
        await adapter.close()
        return socket

    socket = asyncio.run(scenario())

    assert len(socket.sent) == 1  # setup only


# -- cancellation, resumption, goAway, and the trailing-output fence ----------------


def test_cancelled_tool_result_is_dropped_with_one_receipt_per_call(caplog):
    async def scenario():
        socket = _Socket(
            [
                {
                    "toolCall": {
                        "functionCalls": [
                            {"id": "fc-1", "name": "search_memory", "args": {}},
                            {"id": "fc-2", "name": "search_memory", "args": {}},
                        ]
                    }
                },
                # The operator barged in while fc-1 was pending; the server
                # discarded it.
                {"toolCallCancellation": {"ids": ["fc-1"]}},
            ]
        )
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        with caplog.at_level(logging.WARNING, logger="talk_gemini_realtime"):
            await adapter.send(
                (
                    rt.SubmitToolResult(call_id="fc-1", output="cancelled result"),
                    rt.SubmitToolResult(call_id="fc-2", output="live result"),
                    rt.StartResponse(),
                )
            )
            # A duplicate result for the same cancelled id: still dropped,
            # still just the one receipt.
            await adapter.send((rt.SubmitToolResult(call_id="fc-1", output="late"),))
        await adapter.close()
        return socket, events

    socket, events = asyncio.run(scenario())

    calls = [event for event in events if isinstance(event, rt.FunctionCall)]
    assert [call.call_id for call in calls] == ["fc-1", "fc-2"]
    # Only the live call's toolResponse went upstream. The cancelled result
    # was dropped, and — with a tool result in the batch — the trailing
    # StartResponse trigger was withheld as designed.
    assert socket.sent[1:] == [
        {
            "toolResponse": {
                "functionResponses": [
                    {
                        "id": "fc-2",
                        "name": "search_memory",
                        "response": {"result": "live result"},
                    }
                ]
            }
        }
    ]
    drops = [
        record.getMessage()
        for record in caplog.records
        if record.name == "talk_gemini_realtime"
        and record.levelno == logging.WARNING
        and "server-cancelled call" in record.getMessage()
    ]
    assert len(drops) == 1
    assert "fc-1" in drops[0]


def test_cancellation_of_unknown_ids_is_tolerated_silently(caplog):
    async def scenario():
        adapter, _client = _adapter(
            _Socket([{"toolCallCancellation": {"ids": ["fc-never-seen", 7, None]}}])
        )
        await adapter.connect(_setup())
        with caplog.at_level(logging.WARNING, logger="talk_gemini_realtime"):
            events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    # The retraction is now reported as an event so policy hears it, but the
    # malformed ids are still filtered out and nothing is logged.
    assert [type(event) for event in events] == [
        rt.ToolCallsCancelled,
        rt.SessionTerminated,
    ]
    assert events[0].call_ids == ("fc-never-seen",)
    assert not [r for r in caplog.records if r.name == "talk_gemini_realtime"]


def test_cancelled_id_cap_forgets_the_oldest():
    async def scenario():
        adapter, _client = _adapter(_Socket())
        await adapter.connect(_setup())
        adapter._note_cancelled_call_ids(
            [f"fc-{i}" for i in range(gemini_rt.MAX_CANCELLED_CALL_IDS + 76)]
        )
        await adapter.close()
        return adapter

    adapter = asyncio.run(scenario())

    assert len(adapter._cancelled_call_ids) == gemini_rt.MAX_CANCELLED_CALL_IDS
    assert "fc-0" not in adapter._cancelled_call_ids
    assert f"fc-{gemini_rt.MAX_CANCELLED_CALL_IDS + 75}" in adapter._cancelled_call_ids


def test_resumption_handle_confirmation_invalidation_and_preservation():
    adapter, _client = _adapter(_Socket())

    adapter._decode({"sessionResumptionUpdate": {"newHandle": "handle-1", "resumable": True}})
    assert adapter.resumption_handle == "handle-1"
    # No resumable opinion: the last CONFIRMED handle is preserved, and the
    # unconfirmed newHandle is not promoted.
    adapter._decode({"sessionResumptionUpdate": {"newHandle": "handle-2"}})
    assert adapter.resumption_handle == "handle-1"
    # resumable: false invalidates the cache — reusing an invalidated handle
    # would be silent data loss.
    adapter._decode(
        {"sessionResumptionUpdate": {"newHandle": "handle-3", "resumable": False}}
    )
    assert adapter.resumption_handle is None
    adapter._decode({"sessionResumptionUpdate": {"resumable": False}})
    assert adapter.resumption_handle is None


def test_goaway_surfaces_as_a_terminal_failure_with_scrubbed_detail():
    async def scenario():
        adapter, _client = _adapter(
            _Socket([{"goAway": {"timeLeft": f"30s key={FAKE_KEY}"}}])
        )
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return adapter, events

    adapter, events = asyncio.run(scenario())

    assert isinstance(events[0], rt.ProviderFailure)
    assert events[0].terminal is True
    assert "imminent server-side session termination" in events[0].detail
    assert "30s" in events[0].detail
    # goAway detail text is server-supplied; the key scrubber applies to it.
    assert FAKE_KEY not in events[0].detail
    assert "key=<redacted>" in events[0].detail
    assert adapter.state is rt.SessionState.FAILED
    assert isinstance(events[-1], rt.SessionTerminated)


def test_goaway_without_time_left_still_surfaces():
    adapter, _client = _adapter(_Socket())
    events = adapter._decode({"goAway": {}})
    assert events == [
        rt.ProviderFailure(
            detail="Provider announced imminent server-side session termination (goAway)",
            terminal=True,
        )
    ]


def test_one_frame_can_bundle_audio_parts_and_turn_complete():
    pcm = b"bundled audio"

    async def scenario():
        wire_events = [
            {
                "serverContent": {
                    "modelTurn": {
                        "parts": [
                            {"inlineData": {"mimeType": "audio/pcm", "data": _b64(pcm)}}
                        ]
                    },
                    "turnComplete": True,
                    "usageMetadata": {"totalTokenCount": 9},
                }
            },
        ]
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    # Every field of the bundled frame is processed before the terminal flag
    # is honored: the audio, then the final transcript, then the finish.
    assert events == [
        rt.ResponseStarted(response_id=None),
        rt.OutputAudio(data=pcm),
        rt.Transcript(
            role=rt.TranscriptRole.ASSISTANT,
            text="",
            final=True,
            provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
        ),
        rt.ResponseFinished(response_id=None),
        rt.SessionTerminated(state=rt.SessionState.CLOSED),
    ]


def test_trailing_output_after_generation_complete_is_dropped_and_warned_once(caplog):
    heard, straggler, answer = b"heard", b"straggler", b"answer"

    async def scenario():
        wire_events = [
            {
                "serverContent": {
                    "modelTurn": {
                        "parts": [{"inlineData": {"mimeType": "audio/pcm", "data": _b64(heard)}}]
                    }
                }
            },
            {"serverContent": {"generationComplete": True}},
            # Two trailing frames for the closed generation: both dropped,
            # one warning for the whole fenced window.
            {
                "serverContent": {
                    "modelTurn": {
                        "parts": [
                            {"inlineData": {"mimeType": "audio/pcm", "data": _b64(straggler)}}
                        ]
                    },
                    "outputTranscription": {"text": "late words"},
                }
            },
            {"serverContent": {"outputTranscription": {"text": "more late"}}},
            # A tool call continues the turn past generationComplete: it
            # disarms the fence, and its answer must not be eaten.
            {
                "toolCall": {
                    "functionCalls": [{"id": "fc-1", "name": "search_memory", "args": {}}]
                }
            },
            {
                "serverContent": {
                    "modelTurn": {
                        "parts": [{"inlineData": {"mimeType": "audio/pcm", "data": _b64(answer)}}]
                    }
                }
            },
        ]
        socket = _Socket(wire_events)
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        events = []
        with caplog.at_level(logging.WARNING, logger="talk_gemini_realtime"):
            async for event in adapter:
                events.append(event)
                if isinstance(event, rt.FunctionCall):
                    await adapter.send(
                        (rt.SubmitToolResult(call_id=event.call_id, output="done"),)
                    )
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    assert events == [
        rt.ResponseStarted(response_id=None),
        rt.OutputAudio(data=heard),
        rt.Transcript(
            role=rt.TranscriptRole.ASSISTANT,
            text="",
            final=True,
            provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
        ),
        rt.ResponseFinished(response_id=None),
        # The toolCall opens and closes around the call — nothing trailing
        # leaked in between.
        rt.ResponseStarted(response_id=None),
        rt.FunctionCall(call_id="fc-1", name="search_memory", arguments="{}"),
        rt.ResponseFinished(response_id=None),
        rt.ResponseStarted(response_id=None),
        rt.OutputAudio(data=answer),
        rt.SessionTerminated(state=rt.SessionState.CLOSED),
    ]
    drops = [
        record
        for record in caplog.records
        if record.name == "talk_gemini_realtime" and "trailing model output" in record.getMessage()
    ]
    assert len(drops) == 1


def test_operator_speech_disarms_the_trailing_fence():
    async def scenario():
        wire_events = [
            {"serverContent": {"modelTurn": {"parts": []}}},
            {"serverContent": {"generationComplete": True}},
            {"serverContent": {"inputTranscription": {"text": "next question"}}},
            {
                "serverContent": {
                    "modelTurn": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "audio/pcm",
                                    "data": _b64(b"fresh answer"),
                                }
                            }
                        ]
                    }
                }
            },
        ]
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    assert events == [
        rt.ResponseStarted(response_id=None),
        rt.Transcript(
            role=rt.TranscriptRole.ASSISTANT,
            text="",
            final=True,
            provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
        ),
        rt.ResponseFinished(response_id=None),
        rt.Transcript(
            role=rt.TranscriptRole.USER,
            text="next question",
            final=True,
            provenance=rt.TranscriptProvenance.INPUT_AUDIO,
        ),
        rt.ResponseStarted(response_id=None),
        rt.OutputAudio(data=b"fresh answer"),
        rt.SessionTerminated(state=rt.SessionState.CLOSED),
    ]


# -- binary frames (live-smoke finding, 2026-08-28) --------------------------------


def test_binary_frames_carry_the_full_session():
    pcm = b"binary pcm"

    async def scenario():
        wire_events = [
            _Message({"setupComplete": {}}, message_type="binary"),
            _Message(
                {
                    "serverContent": {
                        "modelTurn": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "audio/pcm;rate=24000",
                                        "data": _b64(pcm),
                                    }
                                }
                            ]
                        },
                        "turnComplete": True,
                    }
                },
                message_type="binary",
            ),
        ]
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    assert isinstance(events[0], rt.SessionReady)
    assert isinstance(events[1], rt.ResponseStarted)
    assert events[2] == rt.OutputAudio(data=pcm)
    assert events[3].final is True
    assert isinstance(events[4], rt.ResponseFinished)
    assert events[5] == rt.SessionTerminated(state=rt.SessionState.CLOSED)


def test_malformed_binary_frame_is_non_terminal_and_the_stream_continues():
    async def scenario():
        wire_events = [
            _Message(b"\xff\xfe not json", message_type="binary"),
            _Message("[]", message_type="binary"),  # valid UTF-8, not an object
            _Message({"setupComplete": {}}, message_type="binary"),
        ]
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return adapter, events

    adapter, events = asyncio.run(scenario())

    assert events == [
        rt.ProviderFailure(detail="Provider sent a malformed frame"),
        rt.ProviderFailure(detail="Provider sent a non-object frame"),
        rt.SessionReady(session_id=adapter._session_id),
        rt.SessionTerminated(state=rt.SessionState.CLOSED),
    ]
    assert all(not event.terminal for event in events[:2])
    assert adapter.state is rt.SessionState.CLOSED


# -- resampler ---------------------------------------------------------------------


def _feed(pcm: bytes) -> bytes:
    return gemini_rt.Pcm24To16Resampler().feed(pcm)


def test_resampler_length_ratio_and_streaming_equivalence():
    samples = list(range(-1500, 1500))  # 3000 samples -> 2000 out
    pcm = array.array("h", samples).tobytes()

    one_shot = _feed(pcm)

    chunked = gemini_rt.Pcm24To16Resampler()
    # 14-byte (7-sample) slices: odd sample counts exercise the carry, and
    # the byte alignment the feed contract requires is preserved.
    parts = [chunked.feed(pcm[i : i + 14]) for i in range(0, len(pcm), 14)]
    assert b"".join(parts) == one_shot

    out = array.array("h")
    out.frombytes(one_shot)
    assert len(out) == 2000


def test_resampler_passes_impulse_positions_and_step_levels():
    impulse = array.array("h", [0, 0, 0, 9000, 0, 0]).tobytes()
    out = array.array("h")
    out.frombytes(_feed(impulse))
    assert list(out) == [0, 0, 9000, 0]

    step = array.array("h", [1000] * 300).tobytes()
    out = array.array("h")
    out.frombytes(_feed(step))
    assert list(out) == [1000] * 200


def test_resampler_cannot_clip_at_int16_extremes():
    loud = array.array("h", [32767, -32768, 32767] * 100).tobytes()
    out = array.array("h")
    out.frombytes(_feed(loud))
    assert all(-32768 <= sample <= 32767 for sample in out)


def test_resampler_refuses_byte_misaligned_pcm():
    with pytest.raises(ValueError, match="byte-aligned"):
        _feed(b"\x01")


def test_resampler_empty_input_is_empty_output():
    assert _feed(b"") == b""


# -- key-in-URL redaction -----------------------------------------------------------


def test_connect_failure_never_leaks_the_keyed_url():
    keyed_url = f"{gemini_rt.GEMINI_LIVE_WS_URL}?key={FAKE_KEY}"
    client = _Client(connect_error=RuntimeError(f"handshake 401 for {keyed_url}"))
    aiohttp = types.SimpleNamespace(
        ClientSession=lambda: client,
        WSMsgType=types.SimpleNamespace(TEXT="text", ERROR="error"),
    )
    adapter = gemini_rt.GeminiRealtimeSession(
        auth_token=FAKE_KEY, auth_source="test-auth", aiohttp_module=aiohttp
    )

    async def scenario():
        with pytest.raises(rt.RealtimeSessionError) as captured:
            await adapter.connect(_setup())
        return str(captured.value), repr(captured.value.__context__), repr(
            captured.value.__cause__
        )

    detail, context, cause = asyncio.run(scenario())

    assert "key=<redacted>" in detail
    for text in (detail, context, cause):
        assert FAKE_KEY not in text
    assert adapter.state is rt.SessionState.FAILED


def test_no_log_line_or_failure_contains_the_key(caplog):
    async def scenario():
        socket = _Socket(
            [
                {"error": {"message": f"request rejected for key={FAKE_KEY}"}},
            ]
        )
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        with caplog.at_level(logging.WARNING, logger="talk_gemini_realtime"):
            events = [event async for event in adapter]
            # Clean EOF: the coordinator flush path may still send; a degrade
            # receipt must not leak anything either.
            await adapter.send((rt.CancelResponse(),))
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    details = "\n".join(getattr(event, "detail", "") for event in events)
    assert FAKE_KEY not in rendered
    assert FAKE_KEY not in details
    assert "key=<redacted>" in details
    assert events[-1] == rt.SessionTerminated(state=rt.SessionState.CLOSED)


def test_abnormal_close_detail_is_scrubbed():
    async def scenario():
        socket = _Socket(
            close_code=1008,
            socket_exception=RuntimeError(f"dying with key={FAKE_KEY}"),
        )
        adapter, _client = _adapter(socket)
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    assert events == [
        rt.SessionTerminated(
            state=rt.SessionState.FAILED,
            detail="Provider WebSocket closed abnormally "
            "(close code 1008; dying with key=<redacted>)",
        )
    ]


def test_scrub_strips_key_queries():
    assert (
        gemini_rt._scrub("failed: wss://host.example/path?key=abc123&x=1 end")
        == "failed: wss://host.example/path?key=<redacted>&x=1 end"
    )
    assert gemini_rt._scrub("no url here") == "no url here"


# -- provider selection, config, and auth ----------------------------------------


def test_provider_accepts_gemini_normalized(clean_provider_env):
    clean_provider_env.setenv("TALK_PROVIDER", "  GEMINI ")
    assert talk_config.talk_provider() == "gemini"


def test_gemini_model_and_voice_knobs(clean_provider_env):
    assert talk_config.talk_gemini_model() == talk_config.DEFAULT_GEMINI_MODEL
    assert talk_config.talk_gemini_voice() == talk_config.DEFAULT_GEMINI_VOICE
    clean_provider_env.setenv("TALK_GEMINI_MODEL", " gemini-9.9-live-test ")
    clean_provider_env.setenv("TALK_GEMINI_VOICE", " Fenrir ")
    assert talk_config.talk_gemini_model() == "gemini-9.9-live-test"
    assert talk_config.talk_gemini_voice() == "Fenrir"


def test_gemini_voice_is_case_sensitive_and_fail_closed(clean_provider_env):
    clean_provider_env.setenv("TALK_GEMINI_VOICE", "puck")
    with pytest.raises(talk_config.TalkConfigError, match="puck"):
        talk_config.talk_gemini_voice()
    clean_provider_env.setenv("TALK_GEMINI_VOICE", "Puck")
    assert talk_config.talk_gemini_voice() == "Puck"


def test_gemini_scoped_key_wins_over_shared(clean_provider_env):
    clean_provider_env.setenv("TALK_GEMINI_API_KEY", "gemini-scoped")
    clean_provider_env.setenv("GEMINI_API_KEY", "gemini-shared")
    assert talk_config.resolve_gemini_key() == "gemini-scoped"


def test_gemini_shared_key_used_when_scoped_unset(clean_provider_env):
    clean_provider_env.setenv("GEMINI_API_KEY", "  gemini-shared  ")
    assert talk_config.resolve_gemini_key() == "gemini-shared"


def test_gemini_scoped_key_set_but_empty_is_a_refusal(clean_provider_env):
    clean_provider_env.setenv("TALK_GEMINI_API_KEY", "   ")
    clean_provider_env.setenv("GEMINI_API_KEY", "gemini-shared")
    with pytest.raises(talk_config.TalkConfigError, match="TALK_GEMINI_API_KEY"):
        talk_config.resolve_gemini_key()


def test_gemini_shared_key_set_but_empty_is_a_refusal(clean_provider_env):
    clean_provider_env.setenv("GEMINI_API_KEY", "")
    with pytest.raises(talk_config.TalkConfigError, match="GEMINI_API_KEY"):
        talk_config.resolve_gemini_key()


def test_no_gemini_key_at_all_raises(clean_provider_env):
    with pytest.raises(talk_config.TalkConfigError, match="no Gemini key"):
        talk_config.resolve_gemini_key()


def test_both_keys_set_uses_talk_provider_not_key_presence(clean_provider_env):
    # The selection trap the knob exists to close: an operator holding keys
    # for BOTH providers must get the provider they named, never a switch
    # inferred from key presence.
    clean_provider_env.setenv("OPENAI_API_KEY", "openai-test")
    clean_provider_env.setenv("TALK_GEMINI_API_KEY", "gemini-scoped")
    auth = talk_auth.TalkAuth(token="openai-test", source="env", detail="test")
    assert isinstance(talk_cli._realtime_session(auth), openai_rt.OpenAIRealtimeSession)
    clean_provider_env.setenv("TALK_PROVIDER", "gemini")
    gemini_auth = talk_cli._gemini_auth()
    assert gemini_auth.token == "gemini-scoped"
    assert gemini_auth.source == talk_auth.SOURCE_CONFIGURED
    session = talk_cli._realtime_session(gemini_auth)
    assert isinstance(session, gemini_rt.GeminiRealtimeSession)
    assert session.auth_token == "gemini-scoped"


def test_gemini_auth_falls_back_to_the_shared_key(clean_provider_env):
    clean_provider_env.setenv("GEMINI_API_KEY", "gemini-shared")
    auth = talk_cli._gemini_auth()
    assert auth.token == "gemini-shared"
    assert auth.source == talk_auth.SOURCE_ENV
