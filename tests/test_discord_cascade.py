"""Discord lane cascade wiring — the voice channel speaks ElevenLabs PCM.

The Discord lane enters the SAME ``run_talk_session`` the terminal uses
(``talk_discord.start_session`` passes ``audio=DiscordAudio(...),
lane="discord"``), so the cascade wiring shipped for the terminal — fail-closed
config, ``SessionSetup(text_output=True)``, observe-before-relay, ``aclose()``
at teardown — already covers the room by construction. What could still break
silently is the ROOM side, and that is what this file proves end to end:

- cascade PCM24k lands on the host's player source through the real
  ``DiscordAudio.queue_playback`` conversion (24k mono -> 48k stereo),
- a barge-in aborts the in-flight TTS stream and drains the channel's queue,
- teardown leaves no cascade task running,
- a non-OpenAI provider refuses before the voice channel is touched, and
- native mode never dials ElevenLabs.

No network, no real keys: the ElevenLabs leg is a scripted fake socket behind
the ``talk_cascade_voice._import_aiohttp`` seam, the provider is the
provider-neutral offline fake, and the voice channel is the wired fake host
from ``test_discord``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import types

import aiohttp
import fake_realtime as fr
import pytest
from test_discord import _tone, _wired_host
from test_fake_provider_session import FakeProviderSession

import talk_cascade_voice
import talk_cli
import talk_discord
import talk_realtime as rt

FAKE_KEY = "fake-elevenlabs-key-for-tests"
FAKE_VOICE_ID = "fakeVoiceId0001"


@pytest.fixture(autouse=True)
def _offline_discord(monkeypatch, tmp_path):
    """Offline session plumbing plus a hermetic cascade env on a keyed box."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    for name in (
        "TALK_ELEVENLABS_API_KEY",
        "ELEVENLABS_API_KEY",
        "TALK_ELEVENLABS_VOICE_ID",
        "TALK_VOICE_MODE",
        "TALK_CASCADE_TTS",
        "TALK_ELEVENLABS_MODEL",
        "TALK_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    host = types.SimpleNamespace(
        resolve_auth=lambda: types.SimpleNamespace(token="token", source="fake-auth"),
        identity_sections=lambda: {},
    )
    monkeypatch.setattr(talk_cli.talk_host, "host", lambda: host)
    monkeypatch.setattr(talk_cli.talk_apiserver, "warm_in_background", lambda: None)
    monkeypatch.setattr(talk_cli.talk_lifecycle, "attach_session", lambda *_args: None)
    monkeypatch.setattr(talk_cli.talk_lifecycle, "detach_session", lambda: None)
    monkeypatch.setattr(talk_cli.talk_progress, "attach_session", lambda *_args: None)
    monkeypatch.setattr(talk_cli.talk_progress, "detach_session", lambda: None)
    monkeypatch.setattr(talk_cli.talk_steer, "set_landed_notifier", lambda _value: None)
    yield
    talk_discord.reset_for_tests()


def _cascade_env(monkeypatch) -> None:
    monkeypatch.setenv("TALK_VOICE_MODE", "cascade")
    monkeypatch.setenv("TALK_ELEVENLABS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TALK_ELEVENLABS_VOICE_ID", FAKE_VOICE_ID)


async def _wait_for(condition, timeout: float = 2.0) -> None:
    """Poll the loop until ``condition`` holds; fail the test if it never does."""

    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0)


class _FakeTtsWs:
    """A scripted ElevenLabs stream-input socket: records sends, yields frames."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True

    # -- test scripting -------------------------------------------------------

    def feed(self, frame: dict) -> None:
        message = types.SimpleNamespace(
            type=aiohttp.WSMsgType.TEXT, data=json.dumps(frame)
        )
        self.incoming.put_nowait(message)

    def feed_audio(self, pcm: bytes) -> None:
        self.feed({"audio": base64.b64encode(pcm).decode("ascii")})

    def feed_final(self) -> None:
        self.feed({"isFinal": True})


class _FakeTtsModule:
    """The two aiohttp members the cascade touches, with scripted sockets.

    ``WSMsgType`` stays the REAL enum so the reader's frame-type checks run
    unchanged; ``ClientSession`` hands out fake sockets and records each dial.
    """

    WSMsgType = aiohttp.WSMsgType

    def __init__(self) -> None:
        self.dials: list[dict] = []
        self.sockets: list[_FakeTtsWs] = []

    def ClientSession(self):
        module = self

        class _Session:
            async def ws_connect(self, url, headers=None, timeout=None):
                ws = _FakeTtsWs()
                module.dials.append({"url": url, "headers": headers})
                module.sockets.append(ws)
                return ws

            async def close(self) -> None:
                pass

        return _Session()


class _HeldProvider(FakeProviderSession):
    """Terminate only once ``until`` says the cascade leg has settled.

    A plain FakeProviderSession ends the moment its scripted events drain,
    which would tear the session down before the TTS stream finished speaking.
    The wait is bounded (~5s), so a broken cascade fails the assertions instead
    of hanging the suite.
    """

    def __init__(self, events, *, until) -> None:
        super().__init__(events)
        self._until = until

    async def __anext__(self):
        if not self.events and not self._terminal_emitted and not self._until():
            for _ in range(500):
                await asyncio.sleep(0.01)
                if self.events or self._until():
                    break
        return await super().__anext__()


def _no_leaked_tasks() -> None:
    current = asyncio.current_task()
    leaked = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
    assert leaked == [], f"session teardown left tasks running: {leaked}"


def test_cascade_pcm_reaches_the_voice_channel_through_real_discord_audio(monkeypatch):
    async def scenario():
        _connection, _receiver, vc, _adapter = _wired_host(monkeypatch)
        _cascade_env(monkeypatch)
        tts = _FakeTtsModule()
        monkeypatch.setattr(talk_cascade_voice, "_import_aiohttp", lambda: tts)

        audio = talk_discord.DiscordAudio(7)
        fake = _HeldProvider(
            [
                rt.SessionReady(session_id="session-1"),
                rt.ResponseStarted(response_id="resp-1"),
                fr.rt_transcript_delta("Hello Discord. ", response_id="resp-1"),
                fr.rt_transcript_done("Hello Discord.", response_id="resp-1"),
                rt.ResponseFinished(response_id="resp-1"),
            ],
            until=lambda: bool(tts.sockets) and tts.sockets[0].closed,
        )
        task = asyncio.create_task(
            talk_cli.run_talk_session(
                audio=audio, session_factory=lambda _auth: fake, lane="discord"
            )
        )
        await _wait_for(lambda: vc.playing is not None)
        source = vc.playing  # the host's player source; survives audio.stop()
        await _wait_for(lambda: len(tts.sockets) == 1)
        ws = tts.sockets[0]
        await _wait_for(lambda: len(ws.sent) == 3)
        # BOS, the one sentence chunk, EOS — and the key rode only the header.
        assert ws.sent[1] == {"text": "Hello Discord."}
        assert ws.sent[2] == {"text": ""}
        assert tts.dials[0]["headers"] == {"xi-api-key": FAKE_KEY}
        assert FAKE_KEY not in tts.dials[0]["url"]

        # One 20 ms session-rate frame (480 samples of a 440 Hz tone).
        pcm24 = _tone(480, rate=24_000)
        ws.feed_audio(pcm24)
        ws.feed_final()
        result = await asyncio.wait_for(task, 3)

        assert result == 0
        # The provider session opened in text-output mode....
        assert fake.setup.text_output is True
        # ...and the cascade's PCM took the relay's exact path into the room:
        # the same 24k mono -> 48k stereo conversion, one Discord frame out.
        frame = source.read()
        expected, _carry = talk_discord.session_to_discord(pcm24)
        assert frame == expected
        assert len(frame) == talk_discord.DISCORD_FRAME_BYTES
        assert frame != talk_discord.SILENCE_FRAME
        # Nothing else was ever queued — the next read is synthesized silence.
        assert source.read() == talk_discord.SILENCE_FRAME
        _no_leaked_tasks()  # aclose() stopped the cascade worker at teardown

    asyncio.run(scenario())


def test_barge_in_aborts_the_tts_stream_and_drains_the_channel(monkeypatch):
    async def scenario():
        _connection, _receiver, vc, _adapter = _wired_host(monkeypatch)
        _cascade_env(monkeypatch)
        tts = _FakeTtsModule()
        monkeypatch.setattr(talk_cascade_voice, "_import_aiohttp", lambda: tts)

        audio = talk_discord.DiscordAudio(7)
        fake = _HeldProvider(
            [
                rt.SessionReady(session_id="session-1"),
                rt.ResponseStarted(response_id="resp-1"),
                fr.rt_transcript_delta("A long answer begins. ", response_id="resp-1"),
            ],
            until=lambda: bool(tts.sockets) and tts.sockets[0].closed,
        )
        task = asyncio.create_task(
            talk_cli.run_talk_session(
                audio=audio, session_factory=lambda _auth: fake, lane="discord"
            )
        )
        await _wait_for(lambda: vc.playing is not None)
        source = vc.playing
        await _wait_for(lambda: len(tts.sockets) == 1)
        ws = tts.sockets[0]
        await _wait_for(lambda: len(ws.sent) == 2)  # BOS + first chunk, no EOS

        # Cascade audio is queued for the room when the operator starts talking.
        ws.feed_audio(_tone(480, rate=24_000))
        await _wait_for(lambda: source._frames.qsize() >= 1)  # the player's queue
        fake.events.append(rt.SpeechStarted(input_id="in-1", offset_ms=0))
        fake.events.append(rt.ResponseFinished(response_id="resp-1"))
        result = await asyncio.wait_for(task, 3)

        assert result == 0
        # The in-flight TTS stream died: no EOS went out, and nothing more may
        # arrive from it (a cancelled sentence never speaks).
        assert ws.closed
        assert len(ws.sent) == 2
        # The relay drained the channel in the same step: the queued frame is
        # gone and the room hears only synthesized silence.
        assert source.read() == talk_discord.SILENCE_FRAME
        _no_leaked_tasks()

    asyncio.run(scenario())


def test_cascade_on_discord_refuses_a_non_openai_provider_before_the_channel(
    monkeypatch, capsys
):
    async def scenario():
        connection, _receiver, vc, _adapter = _wired_host(monkeypatch)
        original = list(connection.callbacks)
        monkeypatch.setenv("TALK_PROVIDER", "grok")
        monkeypatch.setenv("XAI_API_KEY", "fake-xai-key")
        _cascade_env(monkeypatch)
        tts = _FakeTtsModule()
        monkeypatch.setattr(talk_cascade_voice, "_import_aiohttp", lambda: tts)

        audio = talk_discord.DiscordAudio(7)
        result = await talk_cli.run_talk_session(
            audio=audio, session_factory=lambda _auth: FakeProviderSession(), lane="discord"
        )

        assert result == 1
        err = capsys.readouterr().err
        assert "grok" in err
        assert "openai" in err
        # The refusal fired before a secret was spent or the room was touched:
        # no ElevenLabs dial, no tapped receiver, no player takeover.
        assert tts.dials == []
        assert list(connection.callbacks) == original
        assert vc.playing is None
        _no_leaked_tasks()

    asyncio.run(scenario())


def test_native_mode_discord_never_dials_elevenlabs(monkeypatch):
    """TALK_VOICE_MODE unset: the room behaves exactly as before the cascade."""

    async def scenario():
        _connection, _receiver, vc, _adapter = _wired_host(monkeypatch)
        tts = _FakeTtsModule()
        monkeypatch.setattr(talk_cascade_voice, "_import_aiohttp", lambda: tts)
        # Capture the player source at handoff — teardown clears vc.playing.
        played_sources = []
        real_play = vc.play
        vc.play = lambda source: (played_sources.append(source), real_play(source))[1]

        audio = talk_discord.DiscordAudio(7)
        pcm24 = _tone(480, rate=24_000)
        fake = FakeProviderSession(
            [
                rt.SessionReady(session_id="session-1"),
                rt.ResponseStarted(response_id="resp-1"),
                fr.rt_audio(pcm24, response_id="resp-1"),
                fr.rt_transcript_done("Hello Discord.", response_id="resp-1"),
                rt.ResponseFinished(response_id="resp-1"),
            ]
        )
        result = await talk_cli.run_talk_session(
            audio=audio, session_factory=lambda _auth: fake, lane="discord"
        )

        assert result == 0
        assert fake.setup.text_output is False
        # Provider audio took the same conversion path, and no TTS socket opened.
        # (The relay queues provider audio synchronously inside event handling.)
        expected, _carry = talk_discord.session_to_discord(pcm24)
        assert played_sources, "the room never got a player source"
        assert played_sources[0].read() == expected
        assert tts.dials == []
        _no_leaked_tasks()

    asyncio.run(scenario())
