"""Response completion stays ordered and reliable under playback backpressure."""

import asyncio

import pytest

import talk_cli
import talk_discord
import talk_realtime as rt

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
