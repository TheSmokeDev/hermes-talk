"""Terminal runtime composition through Hermes's realtime coordinator."""

from __future__ import annotations

import asyncio
import io

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
        await self.dispatch_tool("weather", {"city": "Paris"})
        yield RealtimeEvent.audio(b"speaker", item_id="item-1")
        yield RealtimeEvent.transcript("hello", final=True, role="user")
        yield RealtimeEvent(type=RealtimeEventType.TURN_STARTED, role="user")

    async def close(self):
        self.closed = True


def test_terminal_runtime_composes_audio_provider_and_hermes_authority(monkeypatch):
    audio = FakeAudio()
    capture = FakeCapture(None)
    ctx = FakeContext()
    provider = object()
    monkeypatch.setattr(talk_core_cli, "get_provider", lambda _name: provider)
    monkeypatch.setattr(talk_core_cli, "RealtimeVoiceCoordinator", FakeCoordinator)
    monkeypatch.setattr(talk_core_cli.talk_host, "get_ctx", lambda: ctx)
    monkeypatch.setattr(
        talk_core_cli.talk_host.host(),
        "identity_sections",
        lambda: {},
    )
    monkeypatch.setattr(
        talk_core_cli,
        "get_tool_definitions",
        lambda **_kwargs: [{"name": "weather"}],
    )
    monkeypatch.setattr(
        talk_core_cli.talk_identity,
        "build_instructions",
        lambda *_args, **_kwargs: "rules",
    )
    monkeypatch.setattr(talk_core_cli.talk_config, "talk_model", lambda: "realtime-test")
    monkeypatch.setattr(talk_core_cli.talk_config, "talk_voice", lambda: "cedar")
    monkeypatch.setattr(talk_core_cli.talk_config, "get_hermes_home", lambda: "/tmp/hermes")
    monkeypatch.setattr(talk_core_cli.talk_transcript, "TranscriptCapture", lambda _home: capture)
    monkeypatch.setattr(talk_core_cli.talk_transcript, "sweep_transcripts", lambda _home: None)

    result = asyncio.run(talk_core_cli.run_core_talk_session(audio))

    coordinator = FakeCoordinator.instance
    assert result == 0
    assert audio.started is True
    assert audio.stopped is True
    assert audio.queued == [b"speaker"]
    assert audio.drained == 1
    assert coordinator.provider is provider
    assert coordinator.opened == {
        "instructions": "rules",
        "tools": [{"name": "weather"}],
        "voice": "cedar",
    }
    assert coordinator.heard[0][1] == 360
    assert coordinator.cancelled == 1
    assert coordinator.max_in_flight_tool_calls == 16
    assert coordinator.closed is True
    assert ctx.calls == [("weather", {"city": "Paris"})]
    assert capture.turns == [("user", "hello")]
    assert capture.finished is True


def test_tui_event_stream_delegates_to_text_agent_and_frames_transcripts(
    monkeypatch, capsys
):
    class BridgeCoordinator(FakeCoordinator):
        async def add_context(self, item_id, text):
            self.contexts.append((item_id, text))
            raise RuntimeError("provider rejected optional progress context")

        async def events(self):
            self.result = await self.dispatch_tool(
                "client_delegate",
                {"request": SUM_REQUEST},
            )
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
    assert result == 0
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
    assert (
        'talk: event {"type":"delegate","id":"call-1",'
        f'"request":"{SUM_REQUEST}"}}'
    ) in output
    assert (
        'talk: event {"type":"transcript","role":"user",'
        f'"text":"{SUM_REQUEST}","final":true}}'
    ) in output
    assert (
        'talk: event {"type":"transcript","role":"assistant",'
        '"text":"I found the issue.","final":true}'
    ) in output


def test_progress_item_ids_stay_within_provider_wire_limit():
    request_id = "a" * 32

    first = talk_core_cli._progress_item_id(request_id, 1)
    second = talk_core_cli._progress_item_id(request_id, 2)

    assert first == f"p1-{request_id}"[:32]
    assert len(first) == talk_core_cli.MAX_PROVIDER_ITEM_ID_LENGTH
    assert first != second
