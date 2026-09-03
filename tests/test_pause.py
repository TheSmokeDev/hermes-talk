"""The microphone pause (hermes-talk#100).

What is being proved: a paused capture surface feeds the session nothing
while playback, the barge-in boundary and the announcement gate carry on;
both rooms (terminal microphone, Discord channel) honour the same flag;
the model's tool, the operator's key and the ``/talk`` command all flip
the one attached surface and get the receipt they were promised; and a
surface that is not attached refuses instead of arming a pause against
the next session.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import types

import pytest
from test_discord import _tone, _wired_host

import talk_audio
import talk_cli
import talk_discord
import talk_operator_auth
import talk_pause
import talk_realtime
import talk_tools


@pytest.fixture(autouse=True)
def _clean():
    talk_pause.reset_for_tests()
    talk_discord.reset_for_tests()
    yield
    talk_pause.reset_for_tests()
    talk_discord.reset_for_tests()


def _block() -> bytes:
    return b"\x10\x00" * talk_audio.BLOCKSIZE


# -- the terminal microphone --------------------------------------------------


def test_pause_stops_terminal_capture_and_resume_restores_it():
    audio = talk_audio.DuplexAudio()
    assert audio.input_paused is False

    audio._input_callback(_block(), talk_audio.BLOCKSIZE, None, None)
    assert audio.read_input_chunk() is not None

    audio.pause_input()
    assert audio.input_paused is True
    audio._input_callback(_block(), talk_audio.BLOCKSIZE, None, None)
    assert audio.read_input_chunk() is None, "paused capture reached the reader"
    # Dropped-while-paused is not an overflow: the counter reports capacity
    # trouble, and a pause is the operator's choice.
    assert audio.dropped_input_blocks == 0

    audio.resume_input()
    assert audio.input_paused is False
    audio._input_callback(_block(), talk_audio.BLOCKSIZE, None, None)
    assert audio.read_input_chunk() is not None


def test_pause_discards_what_was_queued_before_the_flag():
    """"Paused" means nothing the microphone heard reaches the wire —
    including the blocks the sender had not drained yet."""

    audio = talk_audio.DuplexAudio()
    for _ in range(3):
        audio._input_callback(_block(), talk_audio.BLOCKSIZE, None, None)

    audio.pause_input()
    audio.resume_input()

    assert audio.read_input_chunk() is None


def test_resume_drains_the_block_that_raced_the_flag():
    """The callback's flag check and its queue write are not one step: a
    block admitted just before the pause can land after the pause's drain.
    It is the stalest audio there is and must not lead the resume."""

    audio = talk_audio.DuplexAudio()
    audio.pause_input()
    audio._input.put_nowait(_block())  # landed after the drain, before the flag was seen

    assert audio.read_input_chunk() is None, "a paused reader must answer empty"
    audio.resume_input()
    assert audio.read_input_chunk() is None, "the raced block led the resume"


def test_pause_leaves_playback_and_the_announcement_gate_alone():
    """Only capture stops. The speaker, the drain signal the announcement
    pump gates on (hermes-talk#50), and the heard boundary are untouched —
    a paused session still speaks results and still knows what it said."""

    audio = talk_audio.DuplexAudio()
    audio.pause_input()

    audio.queue_playback(b"\x01\x02" * 480, "item-1")
    assert audio.playback_pending is True
    with audio._lock:
        audio._take_playback(talk_audio.FRAME_BYTES * 240)
    assert audio.played_ms == 10
    assert audio.drain_playback() == ("item-1", 10)
    assert audio.playback_pending is False
    assert audio.input_paused is True, "playback activity must not unpause"


# -- the Discord channel ------------------------------------------------------


def test_pause_drops_channel_audio_but_keeps_the_host_drained_and_armed(monkeypatch):
    """The host's buffers are still taken (they must not grow) and its
    inactivity timer still re-armed (it must not leave the channel); what
    stops is the audio reaching the session."""

    connection, receiver, _vc, adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    inline = types.SimpleNamespace(call_soon_threadsafe=lambda fn, *a: fn(*a))
    bridge._loop = inline
    bridge.start()
    bridge._loop = inline
    frame = _tone(1920, rate=48_000, stereo=True)

    connection.deliver(frame)
    assert bridge.read_input_chunk() not in (None, talk_discord.SESSION_SILENCE)

    bridge.pause_input()
    bridge._last_keepalive = 0.0  # past the keepalive throttle
    connection.deliver(frame)
    assert not receiver._buffers[1], "the host's buffer grew while paused"
    assert adapter.__dict__.get("timer_resets") == [7, 7]
    assert bridge.read_input_chunk() is None
    assert bridge.read_input_packet() is None, "silence must not be synthesized while paused"

    bridge.queue_playback(_tone(2400, rate=24_000))
    assert bridge.playback_pending is True, "a paused room must still play"

    bridge.resume_input()
    connection.deliver(frame)
    assert bridge.read_input_chunk() not in (None, talk_discord.SESSION_SILENCE)
    bridge.stop()


def test_resume_does_not_replay_the_pause_as_a_burst_of_silence(monkeypatch):
    """The pacing clock keeps up with the wall clock while paused. Otherwise
    a resume would owe the server the whole pause in catch-up silence frames,
    sent as fast as the pump can call — a minute of pause, three thousand
    frames at once."""

    _connection, _receiver, _vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()
    bridge.pause_input()
    bridge._audio_clock = time.monotonic() - 1.0  # a second of pause has passed

    assert bridge.read_input_packet() is None
    bridge.resume_input()

    burst = 0
    while bridge.read_input_packet() is not None and burst < 100:
        burst += 1
    assert burst <= 5, f"resume replayed {burst} catch-up frames"  # ~50 without the fix
    bridge.stop()


def test_a_bridge_pause_discards_the_frame_that_raced_the_flag(monkeypatch):
    _connection, _receiver, _vc, _adapter = _wired_host(monkeypatch)
    bridge = talk_discord.DiscordAudio(7)
    bridge.start()
    bridge.pause_input()
    bridge._inbound.put_nowait(
        talk_discord.InputAudioPacket(speaker=None, pcm=b"\x01\x02" * 480)
    )

    bridge.resume_input()
    packet = bridge.read_input_packet()
    assert packet is None or packet.pcm == talk_discord.SESSION_SILENCE
    bridge.stop()


# -- the registry -------------------------------------------------------------


class _Surface:
    def __init__(self):
        self.input_paused = False
        self.calls: list[str] = []

    def pause_input(self):
        self.calls.append("pause")
        self.input_paused = True

    def resume_input(self):
        self.calls.append("resume")
        self.input_paused = False


def test_nothing_attached_refuses_instead_of_arming_a_pause():
    assert talk_pause.is_paused() is None
    assert talk_pause.set_paused(True, source=talk_pause.SOURCE_TOOL) == talk_pause.NO_SESSION
    assert talk_pause.set_paused(False, source=talk_pause.SOURCE_TOOL) == talk_pause.NO_SESSION

    surface = _Surface()
    talk_pause.attach_session(surface)
    assert surface.input_paused is False, "a refused pause must not carry into the next attach"


def test_set_paused_flips_once_and_reports_no_ops():
    surface = _Surface()
    changes: list[tuple[bool, str]] = []
    talk_pause.attach_session(surface, lambda p, s: changes.append((p, s)))

    tool, key, command = (
        talk_pause.SOURCE_TOOL,
        talk_pause.SOURCE_KEYBOARD,
        talk_pause.SOURCE_COMMAND,
    )
    assert talk_pause.set_paused(True, source=tool) == talk_pause.PAUSED
    assert talk_pause.is_paused() is True
    assert talk_pause.set_paused(True, source=key) == talk_pause.ALREADY_PAUSED
    assert talk_pause.set_paused(False, source=command) == talk_pause.RESUMED
    assert talk_pause.set_paused(False, source=tool) == talk_pause.ALREADY_LISTENING

    assert surface.calls == ["pause", "resume"]
    # The receipt callback fires for the two ACTUAL flips only, tagged with
    # who made them — never for a no-op.
    assert changes == [(True, talk_pause.SOURCE_TOOL), (False, talk_pause.SOURCE_COMMAND)]


def test_a_raising_receipt_callback_never_undoes_the_flip():
    surface = _Surface()

    def boom(_paused, _source):
        raise RuntimeError("loop is gone")

    talk_pause.attach_session(surface, boom)
    assert talk_pause.set_paused(True, source=talk_pause.SOURCE_TOOL) == talk_pause.PAUSED
    assert surface.input_paused is True


def test_a_surface_without_the_flag_is_unsupported_not_crashed():
    talk_pause.attach_session(object())
    assert talk_pause.is_paused() is None
    assert talk_pause.set_paused(True, source=talk_pause.SOURCE_TOOL) == talk_pause.UNSUPPORTED


def test_an_unknown_source_is_a_caller_bug():
    talk_pause.attach_session(_Surface())
    with pytest.raises(ValueError, match="pause source"):
        talk_pause.set_paused(True, source="webhook")


def test_detach_only_drops_the_surface_it_names():
    first, second = _Surface(), _Surface()
    talk_pause.attach_session(first)
    talk_pause.attach_session(second)  # a later session took the slot
    talk_pause.detach_session(first)  # the earlier session tears down
    assert talk_pause.set_paused(True, source=talk_pause.SOURCE_TOOL) == talk_pause.PAUSED
    assert second.input_paused is True

    talk_pause.detach_session(second)
    assert talk_pause.is_paused() is None


def test_concurrent_controls_resolve_to_one_flip():
    """The model and a keypress racing for the same state produce one flip
    and one no-op, never two receipts for the same state."""

    surface = _Surface()
    changes: list = []
    talk_pause.attach_session(surface, lambda p, s: changes.append(s))
    outcomes: list[str] = []
    go = threading.Barrier(2)

    def press(source):
        go.wait()
        outcomes.append(talk_pause.set_paused(True, source=source))

    threads = [
        threading.Thread(target=press, args=(talk_pause.SOURCE_TOOL,)),
        threading.Thread(target=press, args=(talk_pause.SOURCE_KEYBOARD,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == [talk_pause.ALREADY_PAUSED, talk_pause.PAUSED]
    assert len(changes) == 1
    assert surface.calls == ["pause"]


# -- the tool -----------------------------------------------------------------


def test_the_tool_is_advertised_handled_and_classified_read_only():
    tools = talk_tools.default_talk_tools()
    schema = next(tool for tool in tools if tool["name"] == "pause_voice_input")
    assert schema["parameters"]["properties"]["paused"]["type"] == "boolean"
    assert "pause_voice_input" in talk_tools._HANDLERS
    # Read-only on purpose: it changes nothing outside the session and can
    # only NARROW what the session does. In a Discord room every speaker may
    # mute listening; nobody gains authority by it, and resume is the
    # operator's own control.
    assert "pause_voice_input" in talk_operator_auth.READ_ONLY_TALK_TOOLS
    assert "pause_voice_input" not in talk_operator_auth.MUTATING_TALK_TOOLS


def test_the_tool_is_offered_only_where_this_process_owns_the_microphone():
    """The dashboard tab's microphone lives in the browser; a pause tool
    there could only ever answer "nothing here to pause"."""

    def names(lane: str) -> list[str]:
        return [tool["name"] for tool in talk_tools.default_talk_tools(lane=lane)]

    assert "pause_voice_input" in names("cli")
    assert "pause_voice_input" in names("discord")
    assert "pause_voice_input" not in names("dashboard")
    assert "pause_voice_input" not in names("some-future-lane")
    assert names("dashboard") == names("cli")[:-1], "only the pause tool may differ by lane"


def test_the_tool_refuses_when_no_microphone_is_attached():
    receipt = talk_tools.execute_talk_tool("pause_voice_input", {})
    assert receipt == talk_tools.PAUSE_RECEIPTS[talk_pause.NO_SESSION]
    assert "browser owns the microphone" in receipt


def test_the_tool_pauses_says_how_to_resume_and_resumes():
    audio = talk_audio.DuplexAudio()
    talk_pause.attach_session(audio)

    paused = talk_tools.execute_talk_tool("pause_voice_input", {})
    assert paused == talk_tools.PAUSE_RECEIPTS[talk_pause.PAUSED]
    assert "Enter in the terminal" in paused and "/talk resume" in paused
    assert audio.input_paused is True

    again = talk_tools.execute_talk_tool("pause_voice_input", {"paused": True})
    assert again == talk_tools.PAUSE_RECEIPTS[talk_pause.ALREADY_PAUSED]

    resumed = talk_tools.execute_talk_tool("pause_voice_input", {"paused": False})
    assert resumed == talk_tools.PAUSE_RECEIPTS[talk_pause.RESUMED]
    assert audio.input_paused is False

    listening = talk_tools.execute_talk_tool("pause_voice_input", {"paused": False})
    assert listening == talk_tools.PAUSE_RECEIPTS[talk_pause.ALREADY_LISTENING]


@pytest.mark.parametrize("raw", ["false", "No", "0", "off", "resume"])
def test_a_provider_that_serializes_the_flag_as_text_still_resumes(raw):
    audio = talk_audio.DuplexAudio()
    audio.pause_input()
    talk_pause.attach_session(audio)

    assert talk_tools.execute_talk_tool("pause_voice_input", {"paused": raw}) == (
        talk_tools.PAUSE_RECEIPTS[talk_pause.RESUMED]
    )
    assert audio.input_paused is False


def test_every_outcome_has_a_receipt():
    outcomes = {
        talk_pause.PAUSED,
        talk_pause.RESUMED,
        talk_pause.ALREADY_PAUSED,
        talk_pause.ALREADY_LISTENING,
        talk_pause.NO_SESSION,
        talk_pause.UNSUPPORTED,
    }
    assert set(talk_tools.PAUSE_RECEIPTS) == outcomes
    assert set(talk_discord._PAUSE_COMMAND_RECEIPTS) == outcomes


# -- the operator's controls --------------------------------------------------


def test_operator_flips_are_announced_in_the_contained_shape_and_tool_flips_are_not():
    for source, control in (
        (talk_pause.SOURCE_KEYBOARD, "the keyboard"),
        (talk_pause.SOURCE_COMMAND, "a /talk command"),
    ):
        commands = talk_cli.input_pause_commands(True, source)
        assert [type(c) for c in commands] == [
            talk_realtime.AddContext,
            talk_realtime.StartResponse,
            talk_realtime.RemoveContext,
        ]
        assert f"paused your microphone from {control}" in commands[0].text
        assert commands[1].allow_tools is False
        assert commands[2].item_id == commands[0].item_id
        resumed = talk_cli.input_pause_commands(False, source)
        assert f"resumed your microphone from {control}" in resumed[0].text
    # The model speaks its own tool result; an announcement on top would be
    # the same receipt twice.
    assert talk_cli.input_pause_commands(True, talk_pause.SOURCE_TOOL) == []
    assert talk_cli.input_pause_commands(True, "webhook") == []


def test_the_keyboard_watcher_needs_a_terminal():
    piped = types.SimpleNamespace(isatty=lambda: False)
    assert talk_cli.start_keyboard_pause_control(piped) is None
    assert talk_cli.start_keyboard_pause_control(None) is None


def test_the_keyboard_watcher_flips_the_attached_surface_and_stops_on_request():
    surface = _Surface()
    sources: list[str] = []
    talk_pause.attach_session(surface, lambda _p, s: sources.append(s))
    keys = iter(["toggle", "toggle", "pause", "resume", "resume", "x", "pause"])
    seen = threading.Event()

    def read_key(_stdin, stop):
        try:
            key = next(keys)
        except StopIteration:
            seen.set()
            stop.wait(0.01)
            return None
        return key

    stop = talk_cli.start_keyboard_pause_control(
        types.SimpleNamespace(isatty=lambda: True), read_key=read_key
    )
    assert stop is not None
    assert seen.wait(5.0)
    stop()

    # toggle, toggle, pause, resume, resume (a no-op), x (unknown: ignored),
    # pause → the flips:
    assert surface.calls == ["pause", "resume", "pause", "resume", "pause"]
    assert set(sources) == {talk_pause.SOURCE_KEYBOARD}


def test_the_keyboard_watcher_survives_a_dead_console():
    def read_key(_stdin, _stop):
        raise OSError("console closed")

    stop = talk_cli.start_keyboard_pause_control(
        types.SimpleNamespace(isatty=lambda: True), read_key=read_key
    )
    assert stop is not None
    stop()


def test_discord_commands_route_to_the_live_session_and_refuse_without_one():
    assert talk_discord.pause_session() == "I'm not in a voice session right now."
    assert talk_discord.resume_session() == "I'm not in a voice session right now."

    class _LiveTask:
        def done(self):
            return False

    with talk_discord._SESSION_LOCK:
        talk_discord._SESSION.update({"task": _LiveTask(), "guild_id": 7, "mode": "legacy"})
    # The session is claimed but has not attached its surface yet.
    assert "isn't listening yet" in talk_discord.pause_session()

    audio = talk_audio.DuplexAudio()
    sources: list[str] = []
    talk_pause.attach_session(audio, lambda _p, s: sources.append(s))
    assert talk_discord.pause_session() == talk_discord._PAUSE_COMMAND_RECEIPTS[talk_pause.PAUSED]
    assert audio.input_paused is True
    assert "microphone is paused" in talk_discord.session_status()
    assert talk_discord.pause_session() == (
        talk_discord._PAUSE_COMMAND_RECEIPTS[talk_pause.ALREADY_PAUSED]
    )
    assert talk_discord.resume_session() == talk_discord._PAUSE_COMMAND_RECEIPTS[talk_pause.RESUMED]
    assert "microphone is paused" not in talk_discord.session_status()
    assert sources == [talk_pause.SOURCE_COMMAND, talk_pause.SOURCE_COMMAND]

    with talk_discord._SESSION_LOCK:
        talk_discord._SESSION["mode"] = "core"
    assert "doesn't pause" in talk_discord.pause_session()


# -- the live session ---------------------------------------------------------


class _PausableAudio:
    """A microphone that streams while listening and honours the flag."""

    played_ms = 0
    playback_pending = False

    def __init__(self):
        self.input_paused = False
        self.reads = 0

    def start(self):
        pass

    def stop(self):
        pass

    def pause_input(self):
        self.input_paused = True

    def resume_input(self):
        self.input_paused = False

    def read_input_chunk(self):
        # Mostly empty, like a real device: a chunk on EVERY read would keep
        # the sender from ever sleeping, and the fake wire's send never
        # yields, so the receiver would starve.
        self.reads += 1
        if self.input_paused or self.reads % 4:
            return None
        return b"\x00\x00" * 240

    def queue_playback(self, _pcm):
        pass

    def drain_playback(self):
        pass

    def reset_played_ms(self):
        pass


def _run_session_with_operator_pause(monkeypatch, *, keyboard: bool):
    sent: list[dict] = []
    marks: list[str] = []
    stopped: list[bool] = []

    class _Message:
        type = "text"

        def __init__(self, event):
            self.data = json.dumps(event)

    class _WS:
        def __init__(self):
            self.step = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.step += 1
            if self.step == 1:
                return _Message({"type": "response.created", "response": {"id": "r1"}})
            if self.step == 2:
                return _Message({"type": "response.done", "response": {"id": "r1"}})
            if self.step == 3:
                # Let the microphone stream first, then the operator pauses
                # it from their own control while the wire is idle.
                for _ in range(300):
                    if "append" in marks:
                        break
                    await asyncio.sleep(0.01)
                assert talk_pause.is_paused() is False, "the session never attached its audio"
                marks.append("PAUSED")
                outcome = talk_pause.set_paused(True, source=talk_pause.SOURCE_COMMAND)
                assert outcome == talk_pause.PAUSED
                for _ in range(500):
                    await asyncio.sleep(0.01)
                    if any("paused your microphone" in json.dumps(m) for m in sent):
                        break
                else:
                    raise AssertionError("the operator's pause was never announced")
                await asyncio.sleep(0.05)
                raise StopAsyncIteration
            raise StopAsyncIteration

        async def send_json(self, message):
            sent.append(message)
            if message.get("type") == "input_audio_buffer.append":
                marks.append("append")

    class _ClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def ws_connect(self, *_args, **_kwargs):
            return _WS()

    host = types.SimpleNamespace(
        resolve_auth=lambda: types.SimpleNamespace(token="token", source="test"),
        identity_sections=lambda: {},
    )
    monkeypatch.setattr(talk_cli.talk_host, "host", lambda: host)
    monkeypatch.setattr(
        talk_cli, "_mint_session", lambda *a, **k: types.SimpleNamespace(client_secret="x")
    )
    monkeypatch.setattr(
        talk_cli,
        "_import_aiohttp",
        lambda: types.SimpleNamespace(
            ClientSession=_ClientSession,
            WSMsgType=types.SimpleNamespace(TEXT="text"),
        ),
    )
    monkeypatch.setattr(
        talk_cli,
        "start_keyboard_pause_control",
        (lambda *a, **k: (lambda: stopped.append(True))) if keyboard else (lambda *a, **k: None),
    )
    audio = _PausableAudio()
    assert asyncio.run(talk_cli.run_talk_session(audio=audio)) == 0
    return sent, marks, stopped, audio


def test_the_session_attaches_its_microphone_and_speaks_an_operator_pause(monkeypatch, capsys):
    sent, marks, _stopped, audio = _run_session_with_operator_pause(monkeypatch, keyboard=False)

    assert "append" in marks[: marks.index("PAUSED")], "the microphone never streamed"
    assert "append" not in marks[marks.index("PAUSED") + 1 :], "audio flowed after the pause"
    assert audio.input_paused is True

    announcement = next(
        m for m in sent if "paused your microphone from a /talk command" in json.dumps(m)
    )
    assert announcement["item"]["role"] == "system"
    assert "playback and background work continue" in announcement["item"]["content"][0]["text"]
    # Detached at teardown: a pause must never be armed against the next call.
    assert talk_pause.is_paused() is None

    out = capsys.readouterr().out
    assert "talk: microphone paused" in out
    assert "Ctrl+C to hang up." in out and "Enter to pause" not in out


def test_the_terminal_control_is_offered_only_when_it_exists_and_is_stopped(monkeypatch, capsys):
    _sent, _marks, stopped, _audio = _run_session_with_operator_pause(monkeypatch, keyboard=True)

    out = capsys.readouterr().out
    assert "Ctrl+C to hang up, Enter to pause or resume the microphone." in out
    assert "talk: microphone paused (Enter to resume)" in out
    assert stopped == [True], "the keyboard watcher was never stopped"


def test_the_cli_lane_watches_the_real_stdin_only_when_it_is_a_terminal(monkeypatch):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    assert talk_cli.start_keyboard_pause_control() is None
