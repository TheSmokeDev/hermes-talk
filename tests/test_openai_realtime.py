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


def test_core_response_builder_is_exact_and_does_not_change_legacy_encoding():
    response = openai_rt.build_core_response_create(
        canonical_text="Exact words, exactly.",
        correlation="opaque-correlation",
        voice="cedar",
        event_id="evt-opaque",
    )

    assert response == {
        "type": "response.create",
        "event_id": "evt-opaque",
        "response": {
            "conversation": "none",
            "metadata": {"correlation": "opaque-correlation"},
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Exact words, exactly."}
                    ],
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
    assert openai_rt.encode_command(
        rt.StartResponse(metadata={"speaker": "opaque"})
    ) == {
        "type": "response.create",
        "response": {"metadata": {"speaker": "opaque"}},
    }


def test_core_response_cancel_builder_is_exact_and_legacy_cancel_is_unchanged():
    assert openai_rt.build_core_response_cancel(
        response_id="resp-exact", event_id="evt-unpredictable"
    ) == {
        "type": "response.cancel",
        "event_id": "evt-unpredictable",
        "response_id": "resp-exact",
    }
    assert openai_rt.encode_command(rt.CancelResponse()) == {"type": "response.cancel"}


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


def test_wire_credentials_are_private_one_shot_and_cleared_on_every_terminal_path(
    monkeypatch,
):
    token = "unit-test-token"
    source = "unit-test-source"
    descriptor = types.SimpleNamespace(client_secret="ephemeral")
    configuration = {
        "model": "gpt-realtime-test",
        "voice": "cedar",
        "instructions": "Be brief.",
        "tools": None,
        "automatic_response": False,
        "session_update": {"type": "session.update", "session": {}},
    }

    def aiohttp_for(client):
        return types.SimpleNamespace(
            ClientSession=lambda: client,
            WSMsgType=types.SimpleNamespace(TEXT="text", ERROR="error"),
        )

    def assert_private_and_cleared(wire):
        assert not hasattr(wire, "auth_token")
        assert not hasattr(wire, "auth_source")
        assert wire._auth_token is None
        assert wire._auth_source is None
        snapshot = repr(vars(wire))
        assert token not in snapshot
        assert source not in snapshot

    async def scenario():
        minted = []

        def mint_ephemeral_session(**kwargs):
            minted.append((kwargs["auth_token"], kwargs["model"]))
            return descriptor

        monkeypatch.setattr(
            openai_rt.talk_wire,
            "mint_ephemeral_session",
            mint_ephemeral_session,
        )
        success_client = _Client(_Socket())
        success = openai_rt._OpenAIWireSession(
            auth_token=token,
            auth_source=source,
            aiohttp_module=aiohttp_for(success_client),
        )
        await success.connect(**configuration)
        assert minted == [(token, "gpt-realtime-test")]
        assert_private_and_cleared(success)
        with pytest.raises(openai_rt.OpenAIWireError, match="only run once"):
            await success.connect(**configuration)
        await success.close()
        assert_private_and_cleared(success)

        def fail_mint(**_kwargs):
            raise RuntimeError("mint failed")

        mint_failure = openai_rt._OpenAIWireSession(
            auth_token=token,
            auth_source=source,
            mint_session=fail_mint,
        )
        with pytest.raises(RuntimeError, match="mint failed"):
            await mint_failure.connect(**configuration)
        assert_private_and_cleared(mint_failure)
        with pytest.raises(openai_rt.OpenAIWireError, match="only run once"):
            await mint_failure.connect(**configuration)
        await mint_failure.close()
        assert_private_and_cleared(mint_failure)

        class FailingSocketContext:
            async def __aenter__(self):
                raise RuntimeError("socket connect failed")

            async def __aexit__(self, *_exc):
                return None

        connect_client = _Client(_Socket())
        connect_client.ws_connect = lambda *_args, **_kwargs: FailingSocketContext()
        connect_failure = openai_rt._OpenAIWireSession(
            auth_token=token,
            auth_source=source,
            aiohttp_module=aiohttp_for(connect_client),
            mint_session=lambda **_configuration: descriptor,
        )
        with pytest.raises(RuntimeError, match="socket connect failed"):
            await connect_failure.connect(**configuration)
        assert connect_client.exited is True
        assert_private_and_cleared(connect_failure)
        with pytest.raises(openai_rt.OpenAIWireError, match="only run once"):
            await connect_failure.connect(**configuration)
        await connect_failure.close()
        assert_private_and_cleared(connect_failure)

        close_before_connect = openai_rt._OpenAIWireSession(
            auth_token=token,
            auth_source=source,
            mint_session=lambda **_configuration: descriptor,
        )
        await close_before_connect.close()
        assert_private_and_cleared(close_before_connect)
        with pytest.raises(openai_rt.OpenAIWireError, match="only run once"):
            await close_before_connect.connect(**configuration)

    asyncio.run(scenario())


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
    assert events[6] == rt.OutputAudio(data=pcm, item_id="out-1", response_id="resp-1")
    assert events[7].provenance is rt.TranscriptProvenance.OUTPUT_AUDIO
    assert events[7].final is False
    assert events[7].response_id == "resp-1"
    assert events[8].final is True
    assert events[8].response_id == "resp-1"
    assert events[9] == rt.FunctionCall(
        call_id="call-1",
        item_id="item-1",
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


def test_binary_websocket_frame_fails_adapter_instead_of_reporting_clean_eof():
    async def scenario():
        adapter, _client = _adapter(
            _Socket(
                [_Message("not-json", message_type="binary")],
                close_code=1000,
            )
        )
        await adapter.connect(_setup())
        events = [event async for event in adapter]
        await adapter.close()
        return events

    events = asyncio.run(scenario())

    assert events[0] == rt.ProviderFailure(
        detail="Provider sent unsupported WebSocket frame type: binary",
        terminal=True,
    )
    assert events[1] == rt.SessionTerminated(state=rt.SessionState.FAILED)


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


def test_cancelled_close_waiter_does_not_retain_completed_cleanup_task():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingStack:
            closed = False

            async def aclose(self):
                started.set()
                await release.wait()
                self.closed = True

        stack = BlockingStack()
        wire = openai_rt._OpenAIWireSession(auth_token="token", auth_source="test")
        wire._stack = stack
        close_waiter = asyncio.create_task(wire.close())
        await started.wait()
        close_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_waiter
        release.set()
        for _ in range(100):
            if stack.closed and wire._close_task is None:
                break
            await asyncio.sleep(0)
        return stack, wire

    stack, wire = asyncio.run(scenario())

    assert stack.closed is True
    assert wire._closed is True
    assert wire._close_task is None


def test_cancelled_sole_close_waiter_retains_late_cleanup_failure_for_retry():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        unhandled = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

        class FailsOnceStack:
            calls = 0
            closed = False

            async def aclose(self):
                self.calls += 1
                if self.calls == 1:
                    started.set()
                    await release.wait()
                    raise RuntimeError("cleanup exploded")
                self.closed = True

        try:
            stack = FailsOnceStack()
            wire = openai_rt._OpenAIWireSession(auth_token="token", auth_source="test")
            wire._stack = stack
            close_waiter = asyncio.create_task(wire.close())
            await started.wait()
            close_waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close_waiter
            release.set()
            for _ in range(100):
                if wire._close_task is None:
                    break
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            retained = wire._close_failure
            await wire.close()
            await asyncio.sleep(0)
            return stack, wire, retained, unhandled
        finally:
            loop.set_exception_handler(previous_handler)

    stack, wire, retained, unhandled = asyncio.run(scenario())

    assert isinstance(retained, RuntimeError)
    assert str(retained) == "cleanup exploded"
    assert unhandled == []
    assert stack.calls == 2
    assert stack.closed is True
    assert wire._closed is True
    assert wire._close_task is None
    assert wire._close_failure is None


def test_output_events_carry_the_response_that_produced_them():
    # A cancelled response keeps emitting deltas. The relay can only fence them
    # if the decoder stops throwing the id away.
    audio = openai_rt.decode_event(
        {
            "type": "response.output_audio.delta",
            "item_id": "out-1",
            "response_id": "resp-9",
            "delta": base64.b64encode(b"pcm").decode("ascii"),
        }
    )
    delta = openai_rt.decode_event(
        {
            "type": "response.output_audio_transcript.delta",
            "response_id": "resp-9",
            "delta": "hi",
        }
    )
    done = openai_rt.decode_event(
        {
            "type": "response.output_audio_transcript.done",
            "response_id": "resp-9",
            "transcript": "hi there",
        }
    )

    assert (audio.response_id, delta.response_id, done.response_id) == (
        "resp-9",
        "resp-9",
        "resp-9",
    )


def test_output_events_without_a_response_id_decode_to_none():
    # A provider build that omits the field degrades to "unattributed", which
    # the relay treats as speakable rather than raising or muting.
    audio = openai_rt.decode_event(
        {
            "type": "response.output_audio.delta",
            "item_id": "out-1",
            "delta": base64.b64encode(b"pcm").decode("ascii"),
        }
    )

    assert audio.response_id is None
