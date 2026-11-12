"""The Echo surface: Wyoming framing, resampling, paced playback, and one wake word end to end."""

from __future__ import annotations

import array
import asyncio
import json

import pytest

import talk_echo
from talk_echo import EchoAudio, Upsampler, encode_event, read_event


def _reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def _pcm(samples) -> bytes:
    return array.array("h", samples).tobytes()


def _samples(pcm: bytes) -> list[int]:
    out = array.array("h")
    out.frombytes(pcm)
    return list(out)


# -- framing -------------------------------------------------------------------


def test_reads_hassmics_own_framing():
    # Byte for byte what hassmic's wy_write sends for a microphone chunk.
    data = b'{"rate":16000,"width":2,"channels":1,"timestamp":5}'
    raw = (
        b'{"type":"audio-chunk","version":"1.5.4","data_length":%d,"payload_length":4}\n'
        % len(data)
        + data
        + b"\x01\x00\x02\x00"
    )

    async def go():
        reader = _reader(raw)
        first = await read_event(reader)
        return first, await read_event(reader)

    event, after = asyncio.run(go())
    assert event.type == "audio-chunk"
    assert event.data["rate"] == 16000
    assert event.payload == b"\x01\x00\x02\x00"
    assert after is None


def test_encoded_events_read_back_and_inline_data_is_accepted():
    raw = encode_event("audio-start", {"rate": 24000}) + b'{"type":"ping","data":{"text":"x"}}\n'

    async def go():
        reader = _reader(raw)
        return await read_event(reader), await read_event(reader)

    start, ping = asyncio.run(go())
    assert (start.type, start.data, start.payload) == ("audio-start", {"rate": 24000}, b"")
    assert (ping.type, ping.data) == ("ping", {"text": "x"})


@pytest.mark.parametrize(
    "raw",
    [b"not json\n", b'{"type":"x","payload_length":-1}\n', b'{"type":"x","payload_length":9}\nab'],
)
def test_malformed_events_are_refused(raw):
    async def go():
        return await read_event(_reader(raw))

    with pytest.raises(talk_echo.WyomingError):
        asyncio.run(go())


# -- resampling -------------------------------------------------------------------


def test_upsampler_makes_three_samples_of_two_and_interpolates():
    up = Upsampler()
    out = _samples(up.feed(_pcm([0, 300, 600, 900, 1200])))
    assert out == [0, 200, 400, 600, 800, 1000]
    assert _samples(up.feed(_pcm([1500, 1800]))) == [1200, 1400, 1600]


def test_upsampler_is_continuous_across_any_chunking():
    ramp = _pcm(range(0, 3000, 3))
    whole = Upsampler().feed(ramp)
    up = Upsampler()
    pieces = b"".join(up.feed(ramp[i : i + 7]) for i in range(0, len(ramp), 7))
    assert pieces == whole


# -- paced playback -----------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _sent(raw: list[bytes]) -> list[talk_echo.WyomingEvent]:
    async def go():
        reader = _reader(b"".join(raw))
        events = []
        while (event := await read_event(reader)) is not None:
            events.append(event)
        return events

    return asyncio.run(go())


def _ms(n: float) -> bytes:
    return b"\x00\x01" * int(talk_echo.RATE * n / 1000)


def test_playback_goes_out_at_the_rate_it_plays_plus_the_lead():
    clock, wire = Clock(), []
    audio = EchoAudio(wire.append, clock=clock)
    audio.queue_playback(_ms(1000), "item")
    audio.pace_once()
    events = _sent(wire)
    assert [e.type for e in events] == ["audio-start", "audio-chunk"]
    assert events[0].data == {"rate": 24000, "width": 2, "channels": 1}
    assert len(events[1].payload) == len(_ms(talk_echo.PLAYBACK_LEAD_S * 1000))

    wire.clear()
    audio.pace_once()  # no time passed: nothing more is due
    assert wire == []
    clock.now += 0.1
    audio.pace_once()
    (chunk,) = _sent(wire)
    assert chunk.type == "audio-chunk" and len(chunk.payload) == len(_ms(100))
    assert audio.playback_pending


def test_barge_in_discards_the_unsent_and_reports_what_was_sent():
    clock, wire = Clock(), []
    audio = EchoAudio(wire.append, clock=clock)
    audio.queue_playback(_ms(2000), "item")
    audio.pace_once()
    for _ in range(35):  # the pacing task's ticks, 10 ms apart
        clock.now += 0.01
        audio.pace_once()
    item, heard_ms = audio.drain_playback()
    assert item == "item" and abs(heard_ms - 500) <= 1  # 150 ms lead + 350 ms played
    assert not audio.playback_pending
    wire.clear()
    clock.now += 1
    audio.pace_once()
    assert wire == []
    assert audio.end_event() == encode_event("audio-stop")


def test_a_session_that_never_spoke_ends_quietly():
    audio = EchoAudio(lambda _: None, clock=Clock())
    event = _sent([audio.end_event()])[0]
    assert (event.type, event.data["code"]) == ("error", talk_echo.QUIET_END_CODE)
    assert _sent([audio.end_event(failed=True)])[0].data["code"] == "talk-session-failed"


def test_quiet_timer_counts_speech_and_playback_not_silence():
    clock = Clock()
    audio = EchoAudio(lambda _: None, clock=clock)
    clock.now += 5
    audio.feed_microphone(_pcm([0] * 1600))
    assert audio.quiet_s() == 5
    audio.feed_microphone(_pcm([8000, -8000] * 800))
    assert audio.quiet_s() == 0
    clock.now += 3
    audio.queue_playback(_ms(100))
    assert audio.quiet_s() == 0


def test_microphone_is_held_until_read_and_dropped_while_paused():
    audio = EchoAudio(lambda _: None, clock=Clock())
    audio.feed_microphone(_pcm([0] * 1001))
    assert (
        len(audio.read_input_chunk()) == 3000
    )  # 1001 samples at 16 kHz: 1500 at 24 kHz, one carried
    assert audio.read_input_chunk() is None
    audio.pause_input()
    audio.feed_microphone(_pcm([0] * 1000))
    audio.resume_input()
    assert audio.read_input_chunk() is None


def test_microphone_backlog_keeps_the_newest_audio():
    audio = EchoAudio(lambda _: None, clock=Clock())
    for _ in range(100):
        audio.feed_microphone(_pcm([0] * 1600))  # 100 ms each
    assert audio.dropped_input_blocks > 0
    held = 0
    while (chunk := audio.read_input_chunk()) is not None:
        held += len(chunk)
    assert held <= talk_echo.MAX_INPUT_BYTES


# -- one wake word, end to end ---------------------------------------------------------


def test_wake_word_runs_a_session_on_the_echo_and_hands_it_back_when_quiet(monkeypatch):
    """A fake Echo speaking hassmic's side of Wyoming over a real socket."""

    monkeypatch.setattr(talk_echo, "PACE_TICK_S", 0.005)
    heard: list[bytes] = []

    async def session(*, audio, lane):
        assert lane == "echo"
        audio.start()
        try:
            while sum(map(len, heard)) < 3000:
                if (chunk := audio.read_input_chunk()) is not None:
                    heard.append(chunk)
                await asyncio.sleep(0.005)
            audio.queue_playback(_ms(50), "reply")
            await asyncio.Event().wait()
        finally:
            audio.stop()

    async def go():
        received: list[talk_echo.WyomingEvent] = []
        done = asyncio.Event()

        async def echo(reader, writer):
            while (event := await read_event(reader)) is not None:
                received.append(event)
                if event.type == "run-satellite":
                    writer.write(
                        encode_event("run-pipeline", {"start_stage": "asr", "end_stage": "tts"})
                    )
                    for _ in range(2):
                        writer.write(encode_event("audio-chunk", {"rate": 16000}, _pcm([0] * 1024)))
                    await writer.drain()
                if event.type == "audio-stop":
                    done.set()

        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        surface = asyncio.create_task(
            talk_echo.serve("127.0.0.1", port, idle_s=0.3, runner=session)
        )
        try:
            await asyncio.wait_for(done.wait(), 5)
        finally:
            surface.cancel()
            await asyncio.gather(surface, return_exceptions=True)
            server.close()
        return received

    received = asyncio.run(go())
    types = [e.type for e in received]
    assert types[:2] == ["describe", "run-satellite"]
    assert types.index("audio-start") < types.index("audio-chunk") < types.index("audio-stop")
    assert sum(len(e.payload) for e in received if e.type == "audio-chunk") == len(_ms(50))
    assert json.loads(json.dumps(received[types.index("audio-start")].data))["rate"] == 24000


def test_while_the_echo_speaks_only_a_voice_louder_than_its_echo_gets_through():
    """The echo canceller's leftover of the reply must not reach the session as the operator."""

    clock = Clock()
    audio = EchoAudio(lambda _: None, clock=clock, barge_in_level=0.02)
    leftover = _pcm([300, -300] * 501)  # about -40 dBFS
    voice = _pcm([4000, -4000] * 501)  # about -18 dBFS
    audio.queue_playback(_ms(1000), "reply")
    audio.pace_once()

    audio.feed_microphone(leftover)
    assert set(audio.read_input_chunk()) == {0}
    audio.feed_microphone(voice)
    assert set(audio.read_input_chunk()) != {0}

    clock.now += talk_echo.PLAYBACK_LEAD_S + talk_echo.ECHO_TAIL_S + 0.01
    audio.drain_playback()
    audio.feed_microphone(leftover)
    assert set(audio.read_input_chunk()) != {0}
    assert "1 gated under 0.02" in audio.level_report()
