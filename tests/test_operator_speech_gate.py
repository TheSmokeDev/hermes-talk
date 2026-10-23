"""Synthetic reproduction for #90; no provider, microphone, or real socket."""

import asyncio
from contextlib import suppress

import pytest

import talk_cli
import talk_realtime as rt
import talk_relay


def test_announcement_waits_while_operator_speech_is_in_flight():
    async def scenario():
        relay = talk_relay.RealtimeRelay()
        relay.handle_realtime_event(rt.SpeechStarted(input_id="synthetic-turn"))
        sent = []

        class Socket:
            async def send_json(self, message):
                sent.append(message)

        queue = asyncio.Queue()
        queue.put_nowait([{"type": "synthetic-announcement"}])
        pump = asyncio.create_task(talk_cli.pump_announcements(queue, relay, Socket()))
        try:
            # Let the pump consume the queued batch once; no wall-clock delay.
            await asyncio.sleep(0)
            assert sent == [], "announcement was sent while the operator is speaking"
        finally:
            pump.cancel()
            with suppress(asyncio.CancelledError):
                await pump

    asyncio.run(scenario())


@pytest.mark.parametrize("wire", [False, True])
def test_matching_stop_releases_speech_but_stale_stop_does_not(wire):
    relay = talk_relay.RealtimeRelay()

    def emit(start, name):
        if wire:
            relay.handle_event({
                "type": "input_audio_buffer.speech_started" if start else
                        "input_audio_buffer.speech_stopped", "item_id": name,
            })
        else:
            cls = rt.SpeechStarted if start else rt.SpeechStopped
            relay.handle_realtime_event(cls(input_id=name))

    assert not relay.operator_speaking
    emit(True, "first")
    emit(True, "second")
    emit(False, "first")
    assert relay.operator_speaking
    emit(False, "second")
    assert not relay.operator_speaking


def test_missing_stop_expires_and_duplicate_start_does_not_extend_deadline():
    now = [0.0]
    relay = talk_relay.RealtimeRelay(speech_clock=lambda: now[0])
    relay.handle_realtime_event(rt.SpeechStarted(input_id="synthetic"))
    now[0] = talk_relay.OPERATOR_SPEECH_STALE_S - 1
    relay.handle_realtime_event(rt.SpeechStarted(input_id="synthetic"))
    assert relay.operator_speaking
    now[0] += 1
    assert not relay.operator_speaking
    relay.handle_realtime_event(rt.SpeechStarted(input_id="next"))
    assert relay.operator_speaking


def test_provider_batch_is_released_after_operator_stops(monkeypatch):
    monkeypatch.setattr(talk_cli, "ANNOUNCE_IDLE_POLL_S", 0)

    async def scenario():
        relay = talk_relay.RealtimeRelay()
        relay.handle_realtime_event(rt.SpeechStarted())
        queue = asyncio.Queue()
        queue.put_nowait(["synthetic"])
        sent = []

        async def send_batch(batch, *, is_announcement):
            assert is_announcement
            sent.extend(batch)
            return True

        pump = asyncio.create_task(talk_cli.pump_announcements(
            queue, relay, None, send_batch=send_batch, response_busy=lambda: False,
        ))
        try:
            await asyncio.sleep(0)
            assert not sent
            relay.handle_realtime_event(rt.SpeechStopped())
            await asyncio.sleep(0)
            assert sent == ["synthetic"]
        finally:
            pump.cancel()
            with suppress(asyncio.CancelledError):
                await pump

    asyncio.run(scenario())


def test_unnamed_stop_and_new_session_clear_speech_state():
    relay = talk_relay.RealtimeRelay()
    relay.handle_realtime_event(rt.SpeechStarted(input_id="named"))
    relay.handle_realtime_event(rt.SpeechStopped())
    assert not relay.operator_speaking
    relay.handle_event({"type": "input_audio_buffer.speech_started", "item_id": "named"})
    relay.handle_event({"type": "session.created", "session": {"id": "new"}})
    assert not relay.operator_speaking


def test_the_production_busy_and_atomic_checks_include_operator_speech():
    import inspect

    source = inspect.getsource(talk_cli.run_talk_session)
    start = source.index("pump = asyncio.create_task(")
    predicate = source[start:source.index("tool_worker = asyncio.create_task(", start)]
    assert "relay.operator_speaking" in predicate
    lock_check = source[
        source.index("async def send_outgoing"):source.index("async def send_microphone")
    ]
    assert "relay.operator_speaking" in lock_check


def test_missing_stop_event_cannot_hold_an_announcement_forever(monkeypatch):
    monkeypatch.setattr(talk_cli, "ANNOUNCE_IDLE_POLL_S", 0)

    async def scenario():
        now = [0.0]
        relay = talk_relay.RealtimeRelay(speech_clock=lambda: now[0])
        relay.handle_realtime_event(rt.SpeechStarted(input_id="lost-stop"))
        queue = asyncio.Queue()
        queue.put_nowait(["synthetic"])
        sent = []

        async def send_batch(batch, *, is_announcement):
            sent.extend(batch)
            return True

        pump = asyncio.create_task(talk_cli.pump_announcements(
            queue, relay, None, send_batch=send_batch, response_busy=lambda: False,
        ))
        try:
            await asyncio.sleep(0)
            assert not sent
            now[0] = talk_relay.OPERATOR_SPEECH_STALE_S
            await asyncio.sleep(0)
            assert sent == ["synthetic"]
        finally:
            pump.cancel()
            with suppress(asyncio.CancelledError):
                await pump

    asyncio.run(scenario())


@pytest.mark.parametrize("wire", [False, True])
def test_gemini_shaped_stream_releases_speech_when_the_next_response_starts(wire):
    """Start, no stop, then a response: the missing stop cannot outlast the turn."""
    now = [0.0]
    relay = talk_relay.RealtimeRelay(speech_clock=lambda: now[0])
    if wire:
        relay.handle_event({"type": "input_audio_buffer.speech_started", "item_id": "turn-1"})
    else:
        relay.handle_realtime_event(rt.SpeechStarted(input_id="turn-1"))
    assert relay.operator_speaking
    now[0] = 5.0  # well inside the stale window: only the response start can release it
    if wire:
        relay.handle_event({"type": "response.created", "response": {"id": "resp-1"}})
    else:
        relay.handle_realtime_event(rt.ResponseStarted(response_id="resp-1"))
    assert not relay.operator_speaking


def test_replayed_settled_response_does_not_release_operator_speech():
    relay = talk_relay.RealtimeRelay()
    relay.handle_realtime_event(rt.ResponseStarted(response_id="old"))
    relay.handle_realtime_event(rt.SpeechStarted(input_id="turn"))  # barge-in cancels "old"
    assert relay.operator_speaking
    relay.handle_realtime_event(rt.ResponseStarted(response_id="old"))  # replayed start
    assert relay.operator_speaking

