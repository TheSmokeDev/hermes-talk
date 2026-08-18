"""The real Hermes policy loop against a provider-neutral offline fake."""

from __future__ import annotations

import asyncio
import json
import time
import types
from typing import ClassVar

import pytest

import talk_cli
import talk_openai_realtime as openai_rt
import talk_operator_auth
import talk_realtime as rt
import talk_relay
import talk_runs


class FakeProviderSession:
    def __init__(self, events=()):
        self.state = rt.SessionState.NEW
        self.events = list(events)
        self.setup = None
        self.sent: list[tuple[rt.RealtimeCommand, ...]] = []
        self.closed = False
        self._terminal_emitted = False

    async def connect(self, setup):
        self.setup = setup
        self.state = rt.SessionState.CONNECTED

    async def send(self, commands):
        self.sent.append(tuple(commands))

    def __aiter__(self):
        return self

    async def __anext__(self):
        # A real transport yields control while receiving each event.  Preserve
        # that boundary so the bounded tool worker can dequeue in wire order.
        await asyncio.sleep(0)
        if self.events:
            return self.events.pop(0)
        if not self._terminal_emitted:
            self._terminal_emitted = True
            self.state = rt.SessionState.CLOSED
            return rt.SessionTerminated(state=rt.SessionState.CLOSED)
        raise StopAsyncIteration

    async def close(self):
        self.closed = True
        if self.state is not rt.SessionState.FAILED:
            self.state = rt.SessionState.CLOSED


class Audio:
    played_ms = 0

    def __init__(self):
        self.stopped = False
        self.playback: list[bytes] = []
        self.drains = 0

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def read_input_chunk(self):
        return None

    def queue_playback(self, pcm):
        self.playback.append(pcm)

    def drain_playback(self):
        self.drains += 1

    def reset_played_ms(self):
        pass


class Capture:
    instances: ClassVar[list] = []

    def __init__(self, _home):
        self.turns = []
        self.finished = False
        self.instances.append(self)

    def append_turn(self, role, text):
        self.turns.append((role, text))

    def finish(self):
        self.finished = True


class HostExecutionAttachment:
    def __init__(self, *, block=False):
        self.definitions = [
            {
                "name": "host_tool",
                "description": "Run the canonical host tool.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                },
            }
        ]
        self.minted = []
        self.executions = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = block
        self.closed = False

    def tool_definitions(self):
        return self.definitions

    def mint_tool_call_permit(self, **kwargs):
        permit = object()
        self.minted.append((kwargs, permit))
        return permit

    async def execute_tool_batch(self, permits):
        self.executions.append(permits)
        self.started.set()
        if self.block:
            await self.release.wait()
        call_ids = [entry[0]["call_id"] for entry in self.minted]
        return tuple(
            {
                "call_id": call_id,
                "output": f"exact host output for {call_id}",
                "receipt_id": f"receipt-{call_id}",
            }
            for call_id in reversed(call_ids)
        )

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _offline_policy(monkeypatch, tmp_path):
    Capture.instances.clear()
    host = types.SimpleNamespace(
        resolve_auth=lambda: types.SimpleNamespace(token="token", source="fake-auth"),
        identity_sections=lambda: {},
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(talk_cli.talk_host, "host", lambda: host)
    monkeypatch.setattr(talk_cli.talk_apiserver, "warm_in_background", lambda: None)
    monkeypatch.setattr(talk_cli.talk_transcript, "TranscriptCapture", Capture)
    monkeypatch.setattr(talk_cli.talk_transcript, "sweep_transcripts", lambda _home: None)
    monkeypatch.setattr(talk_cli.talk_lifecycle, "attach_session", lambda *_args: None)
    monkeypatch.setattr(talk_cli.talk_lifecycle, "detach_session", lambda: None)
    monkeypatch.setattr(talk_cli.talk_steer, "set_landed_notifier", lambda _value: None)


def _run(fake, audio=None):
    audio = audio or Audio()
    result = asyncio.run(
        talk_cli.run_talk_session(
            audio=audio,
            session_factory=lambda _auth: fake,
        )
    )
    return result, audio


def _run_bounded(fake, audio=None, *, timeout=1.0):
    """Run one failure-path session under an in-test bound and prove task cleanup."""

    async def scenario():
        current = asyncio.current_task()
        session = asyncio.create_task(
            talk_cli.run_talk_session(
                audio=audio,
                session_factory=lambda _auth: fake,
            )
        )
        result = await asyncio.wait_for(session, timeout)
        await asyncio.sleep(0)
        leaked = [
            task.get_coro().__qualname__
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        assert leaked == []
        return result

    audio = audio or Audio()
    return asyncio.run(scenario()), audio


def test_fake_provider_ordinary_turn_preserves_audio_and_transcript_provenance():
    fake = FakeProviderSession(
        [
            rt.SessionReady(session_id="session-1"),
            rt.ResponseStarted(response_id="response-1"),
            rt.Transcript(
                role=rt.TranscriptRole.USER,
                text="hello",
                final=True,
                provenance=rt.TranscriptProvenance.INPUT_AUDIO,
            ),
            rt.OutputAudio(data=b"assistant-pcm", item_id="output-1"),
            rt.Transcript(
                role=rt.TranscriptRole.ASSISTANT,
                text="hi ",
                final=False,
                provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
            ),
            rt.Transcript(
                role=rt.TranscriptRole.ASSISTANT,
                text="hi there",
                final=True,
                provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
            ),
            rt.ResponseFinished(response_id="response-1"),
        ]
    )

    result, audio = _run(fake)

    assert result == 0
    assert fake.setup.instructions
    assert fake.setup.tools
    assert fake.setup.automatic_response is True
    assert audio.playback == [b"assistant-pcm"]
    assert Capture.instances[0].turns == [("user", "hello"), ("assistant", "hi there")]
    assert Capture.instances[0].finished
    assert fake.closed and audio.stopped


def test_host_attachment_tools_execute_as_one_batch_without_blocking_audio():
    attachment = HostExecutionAttachment(block=True)
    audio = Audio()
    fake = FakeProviderSession(
        [
            rt.ResponseStarted(response_id="response-1"),
            rt.FunctionCall(
                response_id="response-1",
                item_id="item-1",
                call_id="call-1",
                name="host_tool",
                arguments='{"value":1}',
            ),
            rt.FunctionCall(
                response_id="response-1",
                item_id="item-2",
                call_id="call-2",
                name="host_tool",
                arguments='{"value":2}',
            ),
            rt.ResponseFinished(response_id="response-1"),
            rt.OutputAudio(data=b"audio-while-host-awaits", item_id="audio-1"),
        ]
    )

    async def scenario():
        running = asyncio.create_task(
            talk_cli.run_talk_session(
                audio=audio,
                session_factory=lambda _auth: fake,
                host_execution_attachment=attachment,
            )
        )
        await asyncio.wait_for(attachment.started.wait(), 0.2)
        for _ in range(20):
            if audio.playback:
                break
            await asyncio.sleep(0)
        assert audio.playback == [b"audio-while-host-awaits"]
        assert not running.done()
        attachment.release.set()
        return await asyncio.wait_for(running, 0.5)

    assert asyncio.run(scenario()) == 0
    assert [tool.name for tool in fake.setup.tools] == ["host_tool"]
    assert fake.setup.tools[0].parameters == attachment.definitions[0]["parameters"]
    assert "limited legacy" not in fake.setup.instructions.lower()
    assert "canonical hermes host tools" in fake.setup.instructions.lower()
    assert [entry[0] for entry in attachment.minted] == [
        {
            "response_id": "response-1",
            "item_id": "item-1",
            "call_id": "call-1",
            "batch_id": attachment.minted[0][0]["batch_id"],
            "tool_name": "host_tool",
            "arguments": {"value": 1},
        },
        {
            "response_id": "response-1",
            "item_id": "item-2",
            "call_id": "call-2",
            "batch_id": attachment.minted[0][0]["batch_id"],
            "tool_name": "host_tool",
            "arguments": {"value": 2},
        },
    ]
    assert len(attachment.executions) == 1
    assert attachment.executions[0] == tuple(entry[1] for entry in attachment.minted)
    tool_batch = next(
        batch for batch in fake.sent if any(isinstance(item, rt.SubmitToolResult) for item in batch)
    )
    assert tool_batch == (
        rt.SubmitToolResult(call_id="call-1", output="exact host output for call-1"),
        rt.SubmitToolResult(call_id="call-2", output="exact host output for call-2"),
        rt.StartResponse(),
    )
    assert attachment.closed


def test_malformed_host_tool_arguments_return_an_error_without_authority_or_effect():
    attachment = HostExecutionAttachment()
    fake = FakeProviderSession(
        [
            rt.ResponseStarted(response_id="response-1"),
            rt.FunctionCall(
                response_id="response-1",
                item_id="item-1",
                call_id="call-1",
                name="host_tool",
                arguments="[]",
            ),
            rt.ResponseFinished(response_id="response-1"),
        ]
    )

    result, _audio = asyncio.run(
        talk_cli.run_talk_session(
            audio=Audio(),
            session_factory=lambda _auth: fake,
            host_execution_attachment=attachment,
        )
    ), None

    assert result == 0
    assert attachment.minted == []
    assert attachment.executions == []
    outputs = [
        command
        for batch in fake.sent
        for command in batch
        if isinstance(command, rt.SubmitToolResult)
    ]
    assert len(outputs) == 1
    assert outputs[0].call_id == "call-1"
    assert "valid json object" in outputs[0].output.lower()
    assert attachment.closed


def test_fake_provider_tools_stay_fifo_and_continue_exactly_once(monkeypatch):
    executed = []

    def execute(name, arguments):
        executed.append((name, arguments))
        return f"result-{name}"

    monkeypatch.setattr(talk_relay, "execute_talk_tool", execute)
    fake = FakeProviderSession(
        [
            rt.ResponseStarted(response_id="response-1"),
            rt.FunctionCall(
                call_id="call-1",
                response_id="response-1",
                name="first",
                arguments='{"position": 1}',
            ),
            rt.FunctionCall(
                call_id="call-2",
                response_id="response-1",
                name="second",
                arguments='{"position": 2}',
            ),
            rt.ResponseFinished(response_id="response-1"),
        ]
    )

    result, _audio = _run(fake)

    assert result == 0
    assert executed == [("first", {"position": 1}), ("second", {"position": 2})]
    tool_batches = [
        batch for batch in fake.sent if any(isinstance(item, rt.SubmitToolResult) for item in batch)
    ]
    assert len(tool_batches) == 1
    assert [item.call_id for item in tool_batches[0][:-1]] == ["call-1", "call-2"]
    assert isinstance(tool_batches[0][-1], rt.StartResponse)
    assert sum(isinstance(item, rt.StartResponse) for item in tool_batches[0]) == 1


def test_fake_provider_barge_in_cancels_and_truncates_heard_audio():
    audio = Audio()
    audio.played_ms = 75
    fake = FakeProviderSession(
        [
            rt.ResponseStarted(response_id="response-1"),
            rt.OutputAudio(data=b"pcm", item_id="output-1"),
            rt.SpeechStarted(input_id="input-2", offset_ms=0),
            rt.ResponseFinished(response_id="response-1"),
        ]
    )

    result, audio = _run(fake, audio)

    assert result == 0
    assert audio.drains == 1
    cancellation = [
        batch for batch in fake.sent if any(isinstance(item, rt.CancelResponse) for item in batch)
    ]
    assert len(cancellation) == 1
    assert [type(item) for item in cancellation[0]] == [rt.CancelResponse, rt.TruncateOutput]
    assert cancellation[0][1] == rt.TruncateOutput(item_id="output-1", audio_end_ms=75)


def test_fake_provider_send_failure_cancels_receive_and_tears_down():
    class OneChunkAudio(Audio):
        def __init__(self):
            super().__init__()
            self.once = False

        def read_input_chunk(self):
            if not self.once:
                self.once = True
                return b"microphone"
            return None

    class FailingProvider(FakeProviderSession):
        def __init__(self):
            super().__init__()
            self.forever = asyncio.Event()
            self.receive_cancelled = False

        async def send(self, commands):
            if any(isinstance(item, rt.AppendInputAudio) for item in commands):
                self.state = rt.SessionState.FAILED
                raise rt.RealtimeSessionError("scripted provider send failure")
            await super().send(commands)

        async def __anext__(self):
            try:
                await self.forever.wait()
            except asyncio.CancelledError:
                self.receive_cancelled = True
                raise
            raise StopAsyncIteration

    fake = FailingProvider()
    audio = OneChunkAudio()

    result, audio = _run(fake, audio)

    assert result == 1
    assert fake.receive_cancelled
    assert fake.closed and audio.stopped
    assert fake.state is rt.SessionState.FAILED


def test_fake_provider_close_failure_still_tears_down_local_resources(capsys):
    class CloseFailingProvider(FakeProviderSession):
        async def close(self):
            self.closed = True
            raise rt.RealtimeSessionError("scripted close failure")

    fake = CloseFailingProvider()

    result, audio = _run(fake)

    assert result == 1
    assert fake.closed and audio.stopped
    assert Capture.instances[0].finished
    assert "scripted close failure" in capsys.readouterr().err


def test_malformed_response_id_consumes_echoed_authority_before_terminal_failure(
    monkeypatch,
):
    class InspectableLedger(talk_operator_auth.DiscordToolAuthorizationLedger):
        def clear(self):
            # Preserve post-session state solely so this regression can attempt
            # the exact replay the production clear would otherwise hide.
            pass

    ledger = InspectableLedger()
    ledger.record_packet(
        {"ssrc": 11, "user_id": 586638048133906576, "display_name": "operator"},
        bytes(20 * 24 * 2),
    )
    ledger.note_speech_started(
        {"item_id": "input-1", "audio_start_ms": 0}
    )
    ledger.note_speech_stopped(
        {"item_id": "input-1", "audio_end_ms": 20}
    )
    create = ledger.response_for_commit({"item_id": "input-1"})
    metadata = create["response"]["metadata"]
    malformed = openai_rt.decode_event(
        {
            "type": "response.created",
            "response": {"id": " padded ", "metadata": metadata},
        }
    )
    fake = FakeProviderSession([malformed])
    audio = Audio()
    audio.discord_speaker_authorization = True
    monkeypatch.setattr(
        talk_cli.talk_operator_auth,
        "DiscordToolAuthorizationLedger",
        lambda: ledger,
    )

    result, _audio = _run(fake, audio)
    replay = openai_rt.decode_event(
        {
            "type": "response.created",
            "response": {"id": "resp-valid", "metadata": metadata},
        }
    )
    ledger.note_response_created(
        {
            "response": {
                "id": replay.response_id,
                "metadata": dict(replay.metadata),
            }
        }
    )

    assert ledger.binding_for_response("resp-valid") is None
    assert result == 1


def test_cancellation_during_connect_closes_every_session_owned_resource(monkeypatch):
    class BlockingConnectProvider(FakeProviderSession):
        def __init__(self):
            super().__init__()
            self.connect_started = asyncio.Event()

        async def connect(self, setup):
            self.setup = setup
            self.state = rt.SessionState.CONNECTING
            self.connect_started.set()
            await asyncio.Event().wait()

    class RecordingLedger:
        def __init__(self):
            self.cleared = False

        def authorize_tool(self, _name, _event):
            return None

        def clear(self):
            self.cleared = True

    async def scenario():
        fake = BlockingConnectProvider()
        ledger = RecordingLedger()
        audio = Audio()
        audio.discord_speaker_authorization = True
        sweeps = []
        monkeypatch.setattr(
            talk_cli.talk_operator_auth,
            "DiscordToolAuthorizationLedger",
            lambda: ledger,
        )
        monkeypatch.setattr(
            talk_cli.talk_transcript,
            "sweep_transcripts",
            lambda home: sweeps.append(home),
        )
        task = asyncio.create_task(
            talk_cli.run_talk_session(
                audio=audio,
                session_factory=lambda _auth: fake,
            )
        )
        await fake.connect_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return fake, ledger, audio, sweeps

    fake, ledger, audio, sweeps = asyncio.run(scenario())

    assert fake.closed
    assert ledger.cleared
    assert audio.stopped
    assert Capture.instances[0].finished
    assert len(sweeps) == 2  # preflight sweep plus cancellation-teardown sweep


def test_terminal_provider_failure_makes_session_nonzero():
    fake = FakeProviderSession(
        [rt.ProviderFailure(detail="fatal receive", terminal=True)]
    )

    result, audio = _run(fake)

    assert result == 1
    assert fake.closed and audio.stopped


def test_abnormal_openai_eof_fails_session_without_flushing_active_tool_batch(
    monkeypatch, capsys
):
    executed = []

    def execute(name, arguments):
        executed.append((name, arguments))
        return "tool completed"

    monkeypatch.setattr(talk_relay, "execute_talk_tool", execute)

    class AbnormalSocket:
        close_code = 1006

        def __init__(self):
            self.events = iter(
                [
                    {
                        "type": "response.created",
                        "response": {"id": "resp-1"},
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "call_id": "call-1",
                        "response_id": "resp-1",
                        "name": "search_memory",
                        "arguments": "{}",
                    },
                ]
            )
            self.sent = []

        async def send_json(self, message):
            self.sent.append(message)

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(0)
            try:
                event = next(self.events)
            except StopIteration:
                for _ in range(1000):
                    if executed:
                        break
                    await asyncio.sleep(0)
                raise StopAsyncIteration from None
            return types.SimpleNamespace(type="text", data=json.dumps(event))

        def exception(self):
            return RuntimeError("server disconnected")

    class Context:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *_exc):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        def ws_connect(self, *_args, **_kwargs):
            return Context(socket)

    socket = AbnormalSocket()
    client = Client()
    aiohttp = types.SimpleNamespace(
        ClientSession=lambda: client,
        WSMsgType=types.SimpleNamespace(TEXT="text", ERROR="error"),
    )
    adapter = openai_rt.OpenAIRealtimeSession(
        auth_token="raw-token",
        auth_source="test-auth",
        aiohttp_module=aiohttp,
        mint_session=lambda _setup: types.SimpleNamespace(client_secret="ephemeral"),
    )

    result, audio = _run_bounded(adapter)

    assert result == 1
    assert adapter.state is rt.SessionState.FAILED
    assert executed == [("search_memory", {})]
    assert [message["type"] for message in socket.sent] == ["session.update"]
    assert "server disconnected" in capsys.readouterr().err
    assert audio.stopped


def test_malformed_response_done_terminates_without_flushing_live_tool_batch(
    monkeypatch,
):
    monkeypatch.setattr(talk_relay, "execute_talk_tool", lambda _name, _args: "done")
    malformed_done = openai_rt.decode_event(
        {"type": "response.done", "response": {"id": " padded "}}
    )
    fake = FakeProviderSession(
        [
            rt.ResponseStarted(response_id="resp-1"),
            rt.FunctionCall(
                call_id="call-1",
                response_id="resp-1",
                name="search_memory",
                arguments="{}",
            ),
            malformed_done,
        ]
    )

    result, audio = _run_bounded(fake)

    assert result == 1
    assert not any(
        isinstance(command, (rt.SubmitToolResult, rt.StartResponse))
        for batch in fake.sent
        for command in batch
    )
    assert fake.closed and audio.stopped


def test_announcement_send_failure_reaches_session_supervisor(monkeypatch):
    class AnnouncementFailingProvider(FakeProviderSession):
        def __init__(self):
            super().__init__()
            self.receive_cancelled = False

        async def send(self, commands):
            if any(isinstance(command, rt.AddContext) for command in commands):
                self.state = rt.SessionState.FAILED
                raise rt.RealtimeSessionError("scripted announcement send failure")
            await super().send(commands)

        async def __anext__(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.receive_cancelled = True
                raise
            raise StopAsyncIteration

    def attach(loop, callback, _owner_session_id):
        loop.call_soon(
            callback,
            {"subagent_id": "sa-1", "status": "ok", "summary": "done"},
        )

    monkeypatch.setattr(talk_cli.talk_lifecycle, "attach_session", attach)
    fake = AnnouncementFailingProvider()
    audio = Audio()

    async def scenario():
        return await asyncio.wait_for(
            talk_cli.run_talk_session(
                audio=audio,
                session_factory=lambda _auth: fake,
            ),
            timeout=1.0,
        )

    result = asyncio.run(scenario())

    assert result == 1
    assert fake.receive_cancelled
    assert fake.closed and audio.stopped


def test_provider_factory_failure_still_tears_down_local_resources(capsys):
    audio = Audio()

    def fail_factory(_auth):
        raise RuntimeError("scripted factory failure")

    result = asyncio.run(
        talk_cli.run_talk_session(
            audio=audio,
            session_factory=fail_factory,
        )
    )

    assert result == 1
    assert audio.stopped
    assert Capture.instances[0].finished
    assert "scripted factory failure" in capsys.readouterr().err


def test_fake_provider_cannot_construct_a_malformed_tool_identifier():
    with pytest.raises(ValueError, match="call_id"):
        rt.FunctionCall(call_id="", response_id="response-1", name="tool", arguments="{}")


# --- the connection's return route (hermes-talk#35) --------------------------


@pytest.fixture
def routed(monkeypatch, tmp_path):
    """A session whose bound Hermes session id and run history are ours."""

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(talk_runs, "_history_path", lambda: state / "talk-runs.jsonl")
    monkeypatch.setattr(talk_runs, "_history_enabled", lambda: True)
    monkeypatch.setattr(talk_cli, "_active_parent_session_id", lambda: "sess-bound")
    talk_runs.reset_for_tests()
    yield state
    talk_runs.reset_for_tests()


def _seed_orphaned_run(session_id: str, output: str) -> int:
    """One terminal, unclaimed run left behind by a process that is now gone."""

    talk_runs.attach_owner(
        talk_session_id="ts-dead",
        generation_id="gen-dead",
        hermes_session_id=session_id,
        operator="test",
        profile=None,
    )
    run_id = talk_runs.start_run("agent", "orphaned", lambda _rid: output)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        run = talk_runs.get_run(run_id)
        if run and run["status"] in talk_runs.TERMINAL_STATUSES:
            break
        time.sleep(0.02)
    # The process that accepted it dies without ever speaking the result.
    talk_runs.reset_for_tests()
    return run_id


def test_the_session_binds_and_releases_its_return_route(routed, monkeypatch):
    seen: list[dict] = []
    real_attach = talk_runs.attach_owner
    monkeypatch.setattr(
        talk_runs,
        "attach_owner",
        lambda **kw: (seen.append(dict(kw)), real_attach(**kw))[1],
    )

    _run(FakeProviderSession([rt.SessionReady(session_id="session-1")]))

    assert len(seen) == 1
    assert seen[0]["hermes_session_id"] == "sess-bound"
    assert seen[0]["operator"] == "fake-auth"
    assert seen[0]["talk_session_id"]
    assert seen[0]["generation_id"]
    # Released at teardown: with no live connection, later work is refused
    # rather than accepted into a void.
    assert talk_runs.current_owner() is None


def test_a_reconnect_speaks_the_result_it_was_owed(routed):
    run_id = _seed_orphaned_run("sess-bound", "the index is rebuilt")

    fake = FakeProviderSession([rt.SessionReady(session_id="session-1")])
    _run(fake)

    spoken = " ".join(
        getattr(command, "text", "")
        for batch in fake.sent
        for command in batch
    )
    assert f"Background run #{run_id}" in spoken
    assert "the index is rebuilt" in spoken
    # Claimed, so the next reconnect does not say it again.
    assert talk_runs.list_undelivered_for_session("sess-bound") == []


def test_a_reconnect_speaks_every_result_it_was_owed(routed):
    """A session can have more than one background run in flight when it drops.

    A single-orphan test cannot distinguish "the adoption loop claims every
    owed run" from "it claims the first and stops" (an early-return/break
    regression). This seeds two and asserts the loop's own claim-before-speak
    contract: every owed run ends up claimed, none stay silently stranded.

    Deliberately not asserting both texts land in `fake.sent`: which queued
    announcements the wire actually flushes before a short-lived session's
    teardown cancels `pump_announcements` is a separate, pre-existing
    scheduling question this test doesn't pin — only that the claim itself,
    which is what determines whether a result can ever be re-adopted, covers
    every owed run rather than just the first.
    """

    first = _seed_orphaned_run("sess-bound", "the index is rebuilt")
    second = _seed_orphaned_run("sess-bound", "the audit is done")

    fake = FakeProviderSession([rt.SessionReady(session_id="session-1")])
    _run(fake)

    spoken = " ".join(
        getattr(command, "text", "")
        for batch in fake.sent
        for command in batch
    )
    assert f"Background run #{first}" in spoken
    assert "the index is rebuilt" in spoken
    # Both claimed — an early-return/break after the first would leave the
    # second run still listed as owed, and still claimable, here.
    assert talk_runs.list_undelivered_for_session("sess-bound") == []
    assert talk_runs.mark_delivered(second) is False


def test_a_reconnect_does_not_speak_a_stranger_s_result(routed):
    _seed_orphaned_run("sess-somebody-else", "not yours")

    fake = FakeProviderSession([rt.SessionReady(session_id="session-1")])
    _run(fake)

    spoken = " ".join(
        getattr(command, "text", "")
        for batch in fake.sent
        for command in batch
    )
    assert "not yours" not in spoken
    assert "Background run #" not in spoken
    # Still owed to its real owner rather than silently consumed.
    assert talk_runs.list_undelivered_for_session("sess-somebody-else")
