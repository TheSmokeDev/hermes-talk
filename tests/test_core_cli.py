"""Terminal runtime composition through Hermes's realtime coordinator."""

from __future__ import annotations

import asyncio
import io
import json

import pytest

pytest.importorskip("agent.realtime_voice", reason="Hermes #95147 contract is optional")
from agent.realtime_voice import RealtimeEvent, RealtimeEventType

import talk_core_cli

SUM_REQUEST = "Write a Python script that sums all numbers from 1 to 100 and run it."


class FakeAudio:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.queued = []
        self.drained = 0
        self.resets = 0
        self.played_ms = 360

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def read_input_chunk(self):
        return None

    def queue_playback(self, pcm):
        self.queued.append(pcm)

    def drain_playback(self):
        self.drained += 1

    def reset_played_ms(self):
        self.resets += 1


class FakeCapture:
    def __init__(self, _home):
        self.turns = []
        self.finished = False

    def append_turn(self, role, text):
        self.turns.append((role, text))

    def finish(self):
        self.finished = True


class FakeContext:
    def __init__(self):
        self.calls = []

    def dispatch_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return "tool result"


class FakeCoordinator:
    instance = None

    def __init__(self, provider, *, dispatch_tool, max_in_flight_tool_calls=16):
        self.provider = provider
        self.dispatch_tool = dispatch_tool
        self.max_in_flight_tool_calls = max_in_flight_tool_calls
        self.opened = None
        self.contexts = []
        self.heard = []
        self.cancelled = 0
        self.closed = False
        FakeCoordinator.instance = self

    async def open(self, **kwargs):
        self.opened = kwargs

    async def send_audio(self, _pcm):
        pass

    async def add_context(self, item_id, text):
        self.contexts.append((item_id, text))

    def report_audio_heard(self, event, *, audio_end_ms):
        self.heard.append((event, audio_end_ms))
        return True

    async def cancel_response(self):
        self.cancelled += 1

    async def events(self):
        yield RealtimeEvent(
            type=RealtimeEventType.SESSION_READY,
            session_id="provider-session",
        )
        await self.dispatch_tool("weather", {"city": "Paris"})
        yield RealtimeEvent.audio(b"speaker", item_id="item-1")
        yield RealtimeEvent(type=RealtimeEventType.TURN_ENDED, role="assistant")
        yield RealtimeEvent.transcript("hello", final=True, role="user")
        yield RealtimeEvent(type=RealtimeEventType.TURN_STARTED, role="user")

    async def close(self):
        self.closed = True


def configure_event_stream(monkeypatch, capture, coordinator=FakeCoordinator):
    monkeypatch.setenv(talk_core_cli.EVENT_STREAM_ENV, "jsonl")
    monkeypatch.setattr(talk_core_cli, "get_provider", lambda _name: object())
    monkeypatch.setattr(talk_core_cli, "RealtimeVoiceCoordinator", coordinator)
    monkeypatch.setattr(
        talk_core_cli.talk_host.host(), "identity_sections", lambda: {}
    )
    monkeypatch.setattr(
        talk_core_cli.talk_identity,
        "build_instructions",
        lambda *_args, **_kwargs: "identity rules",
    )
    monkeypatch.setattr(
        talk_core_cli.talk_config, "talk_model", lambda: "realtime-test"
    )
    monkeypatch.setattr(talk_core_cli.talk_config, "talk_voice", lambda: "cedar")
    monkeypatch.setattr(
        talk_core_cli.talk_config, "get_hermes_home", lambda: "/tmp/hermes"
    )
    monkeypatch.setattr(
        talk_core_cli.talk_transcript,
        "TranscriptCapture",
        lambda _home: capture,
    )
    monkeypatch.setattr(
        talk_core_cli.talk_transcript, "sweep_transcripts", lambda _home: None
    )

def test_core_runner_rejects_standalone_mode_before_starting_audio(
    monkeypatch, capsys
):
    audio = FakeAudio()
    monkeypatch.delenv(talk_core_cli.EVENT_STREAM_ENV, raising=False)

    result = asyncio.run(talk_core_cli.run_core_talk_session(audio))

    assert result == 1
    assert audio.started is False
    assert audio.stopped is False
    assert "reserved for the Hermes TUI" in capsys.readouterr().err


def test_tui_event_stream_delegates_to_text_agent_and_frames_transcripts(
    monkeypatch, capsys
):
    class BridgeCoordinator(FakeCoordinator):
        async def add_context(self, item_id, text):
            self.contexts.append((item_id, text))
            raise RuntimeError("provider rejected optional progress context")

        async def events(self):
            yield RealtimeEvent(
                type=RealtimeEventType.SESSION_READY,
                session_id="provider-session",
            )
            yield RealtimeEvent(
                type=RealtimeEventType.WARNING,
                text="provider recovered from one malformed frame",
            )
            self.result = await self.dispatch_tool(
                "client_delegate",
                {"request": SUM_REQUEST},
            )
            yield RealtimeEvent.audio(b"speaker-1", item_id="item-1")
            yield RealtimeEvent.audio(b"speaker-2", item_id="item-1")
            yield RealtimeEvent.audio(b"speaker-3", item_id="item-1")
            yield RealtimeEvent.transcript(
                SUM_REQUEST,
                final=True,
                role="user",
            )
            yield RealtimeEvent.transcript(
                "I found the issue.", final=True, role="assistant"
            )

    audio = FakeAudio()
    capture = FakeCapture(None)
    provider = object()
    monkeypatch.setenv(talk_core_cli.EVENT_STREAM_ENV, "jsonl")
    monkeypatch.setattr(talk_core_cli, "get_provider", lambda _name: provider)
    monkeypatch.setattr(talk_core_cli, "RealtimeVoiceCoordinator", BridgeCoordinator)
    monkeypatch.setattr(talk_core_cli.talk_host, "get_ctx", lambda: None)
    monkeypatch.setattr(
        talk_core_cli.talk_host.host(), "identity_sections", lambda: {}
    )
    monkeypatch.setattr(
        talk_core_cli.talk_identity,
        "build_instructions",
        lambda *_args, **_kwargs: "identity rules",
    )
    monkeypatch.setattr(talk_core_cli.talk_config, "talk_model", lambda: "realtime-test")
    monkeypatch.setattr(talk_core_cli.talk_config, "talk_voice", lambda: "cedar")
    monkeypatch.setattr(
        talk_core_cli.talk_config, "get_hermes_home", lambda: "/tmp/hermes"
    )
    monkeypatch.setattr(
        talk_core_cli.talk_transcript, "TranscriptCapture", lambda _home: capture
    )
    monkeypatch.setattr(
        talk_core_cli.talk_transcript, "sweep_transcripts", lambda _home: None
    )
    monkeypatch.setattr(
        talk_core_cli.uuid,
        "uuid4",
        lambda: type("Id", (), {"hex": "call-1"})(),
    )
    monkeypatch.setattr(
        talk_core_cli.sys,
        "stdin",
        io.StringIO(
            '{"type":"delegate.progress","id":"call-1","text":"Checked the tests."}\n'
            '{"type":"delegate.result","id":"call-1","output":"The script returned 5050."}\n'
        ),
    )

    result = asyncio.run(talk_core_cli.run_core_talk_session(audio))

    output = capsys.readouterr().out
    coordinator = FakeCoordinator.instance
    assert result == 1
    assert coordinator.max_in_flight_tool_calls == 1
    assert coordinator.contexts == [
        (
            "p1-call-1",
            "Silent Hermes text-agent progress:\n\nChecked the tests.",
        )
    ]
    assert coordinator.opened["tools"] == [talk_core_cli.DELEGATE_TOOL]
    assert talk_core_cli.DELEGATE_INSTRUCTIONS in coordinator.opened["instructions"]
    assert coordinator.result == '"Agent Final Message":\n\nThe script returned 5050.'
    frames = [
        json.loads(line.removeprefix(talk_core_cli.EVENT_PREFIX))
        for line in output.splitlines()
        if line.startswith(talk_core_cli.EVENT_PREFIX)
    ]
    assert {
        (frame["type"], frame.get("role"), frame.get("text"), frame.get("message"))
        for frame in frames
    } >= {
        ("delegate", None, None, None),
        ("transcript", "user", SUM_REQUEST, None),
        ("transcript", "assistant", "I found the issue.", None),
        ("warning", None, None, "provider recovered from one malformed frame"),
    }
    assert {frame["protocol_version"] for frame in frames} == {
        talk_core_cli.PROTOCOL_VERSION
    }
    assert {frame["surface_session_id"] for frame in frames} == {"call-1"}
    assert [frame["sequence"] for frame in frames] == list(range(1, len(frames) + 1))
    assert "talk: connected (realtime-test, voice cedar)" in output
    assert output.count("talk: state composing") == 1
    assert talk_core_cli.sys.stdin.closed is True


def test_progress_item_ids_stay_within_provider_wire_limit():
    request_id = "a" * 32

    first = talk_core_cli._progress_item_id(request_id, 1)
    second = talk_core_cli._progress_item_id(request_id, 2)

    assert first == f"p1-{request_id}"[:32]
    assert len(first) == talk_core_cli.MAX_PROVIDER_ITEM_ID_LENGTH
    assert first != second


def test_stdin_reader_consumes_prefetched_lines_without_deadlock():
    async def scenario():
        reader = talk_core_cli._StdinLineReader(io.StringIO("progress\nresult\n"))
        try:
            return (
                await asyncio.wait_for(reader.readline(), timeout=0.1),
                await asyncio.wait_for(reader.readline(), timeout=0.1),
            )
        finally:
            reader.close()

    assert asyncio.run(scenario()) == ("progress", "result")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("x" * (talk_core_cli.MAX_CONTROL_FRAME_CHARS + 1), "oversized"),
        (
            "\n".join(
                "x" for _ in range(talk_core_cli.MAX_PENDING_CONTROL_FRAMES + 1)
            ),
            "flooded",
        ),
    ],
)
def test_stdin_reader_bounds_parent_protocol(payload, message):
    async def scenario():
        reader = talk_core_cli._StdinLineReader(io.StringIO(f"{payload}\n"))
        idle = asyncio.Event()
        idle.set()
        try:
            with pytest.raises(RuntimeError, match=message):
                await asyncio.wait_for(
                    reader.wait_for_parent_close(idle),
                    timeout=0.1,
                )
        finally:
            reader.close()

    asyncio.run(scenario())


def test_stdin_reader_discards_queued_frames_after_parent_flood():
    async def scenario():
        payload = "\n".join(
            "x" for _ in range(talk_core_cli.MAX_PENDING_CONTROL_FRAMES + 1)
        )
        reader = talk_core_cli._StdinLineReader(io.StringIO(f"{payload}\n"))
        try:
            await asyncio.wait_for(reader._eof.wait(), timeout=0.1)
            with pytest.raises(RuntimeError, match="flooded"):
                await asyncio.wait_for(reader.readline(), timeout=0.1)
        finally:
            reader.close()

    asyncio.run(scenario())


def test_parent_stdin_eof_stops_an_idle_session(monkeypatch, capsys):
    class IdleCoordinator(FakeCoordinator):
        async def events(self):
            await asyncio.Event().wait()
            if False:  # pragma: no cover - make this an async iterator
                yield None

    audio = FakeAudio()
    capture = FakeCapture(None)
    configure_event_stream(monkeypatch, capture, IdleCoordinator)
    monkeypatch.setattr(talk_core_cli.sys, "stdin", io.StringIO(""))

    result = asyncio.run(talk_core_cli.run_core_talk_session(audio))

    assert result == 1
    assert "closed the live delegation channel" in capsys.readouterr().out
    assert audio.stopped is True
    assert capture.finished is True
    assert FakeCoordinator.instance.closed is True


def test_startup_cancellation_still_closes_every_owned_resource(monkeypatch):
    class CancelledOpenCoordinator(FakeCoordinator):
        async def open(self, **kwargs):
            self.opened = kwargs
            raise asyncio.CancelledError

    audio = FakeAudio()
    capture = FakeCapture(None)
    configure_event_stream(monkeypatch, capture, CancelledOpenCoordinator)
    monkeypatch.setattr(talk_core_cli.sys, "stdin", io.StringIO(""))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(talk_core_cli.run_core_talk_session(audio))

    assert audio.started is True
    assert audio.stopped is True
    assert capture.finished is True
    assert FakeCoordinator.instance.closed is True
