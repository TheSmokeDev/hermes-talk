"""OpenAI adapter mapping tests with a scripted socket and no network."""

from __future__ import annotations

import asyncio
import base64
import json
import types

import pytest

import talk_openai_realtime as openai_rt
import talk_realtime as rt


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
        model="gpt-realtime-test",
        voice="cedar",
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
    descriptor = types.SimpleNamespace(client_secret="ephemeral")
    adapter = openai_rt.OpenAIRealtimeSession(
        auth_token="raw-token",
        auth_source="test-auth",
        aiohttp_module=aiohttp,
        mint_session=lambda _setup: descriptor,
    )
    return adapter, client


def test_connect_configures_the_session_and_commands_map_to_openai_wire():
    async def scenario():
        socket = _Socket()
        adapter, client = _adapter(socket)
        await adapter.connect(_setup(automatic_response=False))
        await adapter.send(
            (
                rt.AddContext(item_id="ctx-1", text="speaker context"),
                rt.AppendInputAudio(data=b"mic"),
                rt.StartResponse(metadata={"speaker": "opaque"}),
                rt.SubmitToolResult(call_id="call-1", output="done"),
                rt.CancelResponse(),
                rt.TruncateOutput(item_id="item-1", audio_end_ms=75),
                rt.RemoveContext(item_id="ctx-1"),
            )
        )
        await adapter.close()
        return socket, client, adapter

    socket, client, adapter = asyncio.run(scenario())

    assert socket.sent[0]["type"] == "session.update"
    session = socket.sent[0]["session"]
    assert session["type"] == "realtime"
    assert "model" not in session
    assert session["audio"]["input"]["turn_detection"]["create_response"] is False
    assert session["tools"][0]["name"] == "search_memory"
    assert [message["type"] for message in socket.sent[1:]] == [
        "conversation.item.create",
        "input_audio_buffer.append",
        "response.create",
        "conversation.item.create",
        "response.cancel",
        "conversation.item.truncate",
        "conversation.item.delete",
    ]
    assert base64.b64decode(socket.sent[2]["audio"]) == b"mic"
    assert socket.sent[3]["response"]["metadata"] == {"speaker": "opaque"}
    assert socket.sent[4]["item"]["type"] == "function_call_output"
    assert client.connect_args[1]["headers"] == {"Authorization": "Bearer ephemeral"}
    assert socket.exited and client.exited
    assert adapter.state is rt.SessionState.CLOSED


def test_server_events_map_to_neutral_events_with_transcript_provenance():
    async def scenario():
        pcm = b"assistant pcm"
        wire_events = [
            {"type": "session.created", "session": {"id": "sess-1"}},
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
            {
                "type": "response.output_audio.delta",
                "item_id": "out-1",
                "delta": base64.b64encode(pcm).decode("ascii"),
            },
            {"type": "response.output_audio_transcript.delta", "delta": "hi"},
            {"type": "response.output_audio_transcript.done", "transcript": "hi there"},
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call-1",
                "response_id": "resp-1",
                "name": "search_memory",
                "arguments": "{}",
            },
            {"type": "response.done", "response": {"id": "resp-1"}},
        ]
        adapter, _client = _adapter(_Socket(wire_events))
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events, pcm

    events, pcm = asyncio.run(scenario())

    assert isinstance(events[0], rt.SessionReady)
    assert isinstance(events[1], rt.SpeechStarted)
    assert isinstance(events[2], rt.SpeechStopped)
    assert isinstance(events[3], rt.InputAudioCommitted)
    assert events[4] == rt.Transcript(
        role=rt.TranscriptRole.USER,
        text="hello",
        final=True,
        provenance=rt.TranscriptProvenance.INPUT_AUDIO,
    )
    assert events[5].metadata == {"speaker": "opaque"}
    assert events[6] == rt.OutputAudio(data=pcm, item_id="out-1")
    assert events[7].provenance is rt.TranscriptProvenance.OUTPUT_AUDIO
    assert events[7].final is False
    assert events[8].final is True
    assert events[9] == rt.FunctionCall(
        call_id="call-1",
        response_id="resp-1",
        name="search_memory",
        arguments="{}",
    )
    assert events[10] == rt.ResponseFinished(response_id="resp-1")
    assert events[11] == rt.SessionTerminated(state=rt.SessionState.CLOSED)


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


def test_websocket_error_frame_fails_adapter_instead_of_reporting_clean_eof():
    async def scenario():
        adapter, _client = _adapter(
            _Socket([_Message("receive exploded", message_type="error")])
        )
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        state_before_close = adapter.state
        await adapter.close()
        return events, state_before_close

    events, state_before_close = asyncio.run(scenario())

    assert events[0] == rt.ProviderFailure(
        detail="receive exploded",
        terminal=True,
    )
    assert events[1] == rt.SessionTerminated(state=rt.SessionState.FAILED)
    assert state_before_close is rt.SessionState.FAILED


@pytest.mark.parametrize(
    ("close_code", "socket_exception", "detail"),
    [
        (1006, None, "1006"),
        (1000, RuntimeError("server disconnected"), "server disconnected"),
    ],
)
def test_abnormal_iterator_eof_emits_synchronized_terminal_failure(
    close_code, socket_exception, detail
):
    async def scenario():
        adapter, _client = _adapter(
            _Socket(close_code=close_code, socket_exception=socket_exception)
        )
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        state_before_close = adapter.state
        await adapter.close()
        return events, state_before_close

    events, state_before_close = asyncio.run(scenario())

    assert len(events) == 1
    assert events[0].state is rt.SessionState.FAILED
    assert detail in events[0].detail
    assert state_before_close is rt.SessionState.FAILED


@pytest.mark.parametrize("close_code", [1000, 1001])
def test_normal_iterator_eof_preserves_clean_close(close_code):
    async def scenario():
        adapter, _client = _adapter(_Socket(close_code=close_code))
        await adapter.connect(_setup())
        terminal = await adapter.__anext__()
        state_before_close = adapter.state
        await adapter.close()
        return terminal, state_before_close

    terminal, state_before_close = asyncio.run(scenario())

    assert terminal == rt.SessionTerminated(state=rt.SessionState.CLOSED)
    assert state_before_close is rt.SessionState.CLOSED


def test_send_failure_is_terminal_and_teardown_remains_idempotent():
    async def scenario():
        socket = _Socket(fail_send_at=1)  # setup succeeds; first policy command fails
        adapter, client = _adapter(socket)
        await adapter.connect(_setup())
        with pytest.raises(rt.RealtimeSessionError, match="scripted send failure"):
            await adapter.send((rt.AppendInputAudio(data=b"mic"),))
        assert adapter.state is rt.SessionState.FAILED
        await adapter.close()
        await adapter.close()
        return socket, client, adapter

    socket, client, adapter = asyncio.run(scenario())

    assert socket.exited and client.exited
    assert adapter.state is rt.SessionState.FAILED


def test_connect_cancellation_unwinds_the_partial_http_context():
    async def scenario():
        started = asyncio.Event()
        socket = _Socket()
        client = _Client(socket)

        class BlockingSocketContext:
            async def __aenter__(self):
                started.set()
                await asyncio.Event().wait()

            async def __aexit__(self, *_exc):
                socket.exited = True

        client.ws_connect = lambda *_args, **_kwargs: BlockingSocketContext()
        aiohttp = types.SimpleNamespace(
            ClientSession=lambda: client,
            WSMsgType=types.SimpleNamespace(TEXT="text"),
        )
        adapter = openai_rt.OpenAIRealtimeSession(
            auth_token="raw-token",
            auth_source="test-auth",
            aiohttp_module=aiohttp,
            mint_session=lambda _setup: types.SimpleNamespace(client_secret="ephemeral"),
        )
        task = asyncio.create_task(adapter.connect(_setup()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return adapter, client, socket

    adapter, client, socket = asyncio.run(scenario())

    assert adapter.state is rt.SessionState.CLOSED
    assert client.exited
    assert not socket.exited  # its context was never entered
