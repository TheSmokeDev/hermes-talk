"""Response completion stays ordered and reliable under playback backpressure."""

import asyncio
import types

import fake_realtime as fr
import pytest
from test_discord import _wired_host
from test_fake_provider_session import (
    AnnouncementAnsweringProvider,
    _delivered_in_history,
    _owed_to,
    _seed_orphaned_run,
)

import talk_cli
import talk_discord
import talk_realtime as rt
import talk_runs

FRAME = talk_discord.DISCORD_FRAME_BYTES


def _finish(audio):
    # Also runnable on main to expose the retained PCM, before the new hook exists.
    finish = getattr(audio, "finish_playback", None)
    if finish is not None:
        finish()


def _audio(capacity=2):
    audio = talk_discord.DiscordAudio()
    audio._outbound.maxsize = capacity
    audio._source = talk_discord._RealtimeSource(audio._outbound)
    return audio


def test_full_queue_preserves_audio_and_completion_without_extra_capacity():
    audio = _audio()
    first = b"a" * FRAME
    tail = b"b" * 20
    audio._outbound.put_nowait(first)
    audio._outbound.put_nowait(tail)
    _finish(audio)
    _finish(audio)  # duplicate notifications do not grow a full queue
    assert audio._outbound.qsize() == 2
    assert audio._source.read() == first
    assert audio._source.read() == tail + bytes(FRAME - len(tail))
    assert not audio.playback_pending
    assert audio._source.read() == talk_discord.SILENCE_FRAME


@pytest.mark.parametrize("length", [20, FRAME, FRAME + 20, FRAME * 3])
def test_completed_chunk_drains_without_merging_the_next_response(length):
    audio = _audio()
    first = b"a" * length
    second = b"b" * FRAME
    audio._outbound.put_nowait(first)
    _finish(audio)
    audio._outbound.put_nowait(second)
    expected = first + bytes(-length % FRAME)
    actual = b"".join(audio._source.read() for _ in range(len(expected) // FRAME))
    assert actual == expected
    assert audio._source.read() == second
    assert not audio.playback_pending


def test_completion_after_player_took_partial_chunk_flushes_its_carry():
    audio = _audio()
    tail = b"a" * 20
    audio._outbound.put_nowait(tail)
    assert audio._source.read() == talk_discord.SILENCE_FRAME
    _finish(audio)
    assert audio._source.read() == tail + bytes(FRAME - len(tail))
    assert not audio.playback_pending


def test_barge_in_discards_old_boundary_and_does_not_flush_next_response_early():
    audio = _audio()
    audio._outbound.put_nowait(b"a" * (FRAME + 20))
    _finish(audio)
    assert audio._source.read() == b"a" * FRAME
    audio.drain_playback()
    assert not audio.playback_pending
    audio._outbound.put_nowait(b"b" * 20)
    assert audio._source.read() == talk_discord.SILENCE_FRAME
    _finish(audio)
    assert audio._source.read() == b"b" * 20 + bytes(FRAME - 20)
    assert not audio.playback_pending


def test_capture_only_completion_never_enqueues_playback():
    audio = talk_discord.DiscordAudio(capture_only=True)
    _finish(audio)
    assert audio._outbound.empty()
    assert not audio.playback_pending


def test_overflow_marks_last_admitted_chunk_and_keeps_drop_accounting():
    audio = _audio(capacity=1)
    pcm = b"\x01\x00" * 5
    audio.queue_playback(pcm)
    audio.queue_playback(pcm)  # the second chunk cannot be admitted
    _finish(audio)
    assert audio._outbound.qsize() == 1
    converted, _ = talk_discord.session_to_discord(pcm)
    assert audio._source.read() == converted + bytes(FRAME - len(converted))
    assert audio.playback_progress.discarded_bytes == len(converted)
    assert not audio.playback_progress.pending


def test_unfinished_response_retains_partial_frame_until_its_own_boundary():
    audio = _audio()
    audio._outbound.put_nowait(b"a" * 20)
    for _ in range(3):
        assert audio._source.read() == talk_discord.SILENCE_FRAME
        assert audio.playback_pending
    _finish(audio)
    assert audio._source.read() == b"a" * 20 + bytes(FRAME - 20)
    assert not audio.playback_pending


def test_cleanup_clears_a_boundary_held_with_multi_frame_audio():
    audio = _audio()
    audio._outbound.put_nowait(b"a" * (FRAME + 20))
    _finish(audio)
    assert audio._source.read() == b"a" * FRAME
    audio._source.cleanup()
    audio._outbound.put_nowait(b"b" * 20)
    assert audio._source.read() == talk_discord.SILENCE_FRAME
    assert audio.playback_pending
    _finish(audio)
    assert audio._source.read() == b"b" * 20 + bytes(FRAME - 20)


def test_empty_and_duplicate_completion_stays_bounded_and_player_stays_alive():
    audio = _audio(capacity=1)
    for _ in range(3):
        _finish(audio)
    assert audio._outbound.qsize() <= 1
    assert audio._source.read() == talk_discord.SILENCE_FRAME
    assert not audio.playback_pending
    audio._outbound.put_nowait(b"a" * FRAME)
    assert audio._source.read() == b"a" * FRAME


def test_final_fragment_releases_a_deferred_announcement(monkeypatch):
    monkeypatch.setattr(talk_cli, "ANNOUNCE_IDLE_POLL_S", 0)

    async def scenario():
        audio = _audio()
        audio._outbound.put_nowait(b"a" * (FRAME + 20))
        _finish(audio)
        assert audio._source.read() == b"a" * FRAME
        queue = asyncio.Queue()
        queue.put_nowait([rt.AddContext(text="Work finished.", item_id="announcement")])
        sent = asyncio.Event()

        async def send_batch(batch, *, is_announcement=False):
            assert is_announcement and not audio.playback_pending
            sent.set()
            return True

        relay = type("IdleRelay", (), {"response_active": False})()
        pump = asyncio.create_task(talk_cli.pump_announcements(
            queue, relay, None, send_batch, lambda: audio.playback_pending
        ))
        try:
            await asyncio.sleep(0)
            assert not sent.is_set()
            assert audio._source.read() == b"a" * 20 + bytes(FRAME - 20)
            await asyncio.wait_for(sent.wait(), 1)
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)

    asyncio.run(scenario())


@pytest.fixture
def owed_discord_call(monkeypatch, tmp_path):
    """An offline Discord session that is owed one finished background result."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    for name in ("TALK_VOICE_MODE", "TALK_CASCADE_TTS", "TALK_PROVIDER"):
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
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(talk_runs, "_history_path", lambda: state / "talk-runs.jsonl")
    monkeypatch.setattr(talk_runs, "_history_enabled", lambda: True)
    monkeypatch.setattr(talk_cli, "_active_parent_session_id", lambda: "sess-bound")
    talk_runs.reset_for_tests()
    yield
    talk_runs.reset_for_tests()
    talk_discord.reset_for_tests()


class _ReplyThenIdle(AnnouncementAnsweringProvider):
    """Speak one reply, then report when the session loop has fully handled it."""

    def __init__(self, events, *, until):
        super().__init__(events, until=until)
        self.reply_handled = asyncio.Event()
        self._reply_finished = False

    async def __anext__(self):
        if self._reply_finished:
            # The loop asks for the next event only after it handled this one.
            self.reply_handled.set()
        event = await super().__anext__()
        if isinstance(event, rt.ResponseFinished) and event.response_id == "resp-1":
            self._reply_finished = True
        return event


def test_owed_result_is_announced_once_after_the_final_partial_frame_plays(
    owed_discord_call, monkeypatch
):
    """Issue #239 end to end: the real Discord lane, pump and delivery claim.

    The reply's audio ends 40 bytes into a 20 ms frame. Before the fix the
    source held that fragment forever, ``playback_pending`` never cleared and
    the owed result waited for a safe opening that never came.
    """

    monkeypatch.setattr(talk_cli, "ANNOUNCE_IDLE_POLL_S", 0.001)
    run_id = _seed_orphaned_run("sess-bound", "the index is rebuilt")
    pcm = b"\x01\x00" * 5  # 40 Discord bytes after conversion
    fake = _ReplyThenIdle(
        [
            rt.SessionReady(session_id="session-1"),
            rt.ResponseStarted(response_id="resp-1"),
            fr.rt_audio(pcm, response_id="resp-1"),
            rt.ResponseFinished(response_id="resp-1"),
        ],
        until=lambda: _delivered_in_history(run_id),
    )

    def announced():
        return [
            batch
            for batch in fake.sent
            if any(f"Background run #{run_id}" in getattr(c, "text", "") for c in batch)
        ]

    real_pump = talk_cli.pump_announcements

    async def pump_after_the_reply(*args, **kwargs):
        # The owed result reaches the pump while the reply's tail is unplayed.
        await fake.reply_handled.wait()
        await real_pump(*args, **kwargs)

    monkeypatch.setattr(talk_cli, "pump_announcements", pump_after_the_reply)

    async def scenario():
        _connection, _receiver, vc, _adapter = _wired_host(monkeypatch)
        audio = talk_discord.DiscordAudio(7)
        session = asyncio.create_task(
            talk_cli.run_talk_session(
                audio=audio, session_factory=lambda _auth: fake, lane="discord"
            )
        )
        try:
            await asyncio.wait_for(fake.reply_handled.wait(), 3)
            source = vc.playing
            for _ in range(20):
                await asyncio.sleep(0)
            assert audio.playback_pending  # the tail has not reached the room
            assert announced() == []  # so the result waits rather than overlap it

            # The host's player takes its next 20 ms frame: the padded tail.
            converted, _ = talk_discord.session_to_discord(pcm)
            assert source.read() == converted + bytes(FRAME - len(converted))
            assert not audio.playback_pending
            assert await asyncio.wait_for(session, 6) == 0
        finally:
            if not session.done():
                session.cancel()
                await asyncio.gather(session, return_exceptions=True)

    asyncio.run(scenario())

    assert len(announced()) == 1
    assert "the index is rebuilt" in " ".join(
        getattr(command, "text", "") for command in announced()[0]
    )
    assert _delivered_in_history(run_id)
    assert _owed_to("sess-bound") == []
    assert talk_runs.claim_delivery(run_id, claimant="ts-probe") is False
