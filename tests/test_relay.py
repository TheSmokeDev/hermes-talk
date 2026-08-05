"""Relay — the event loop, against scripted transcripts. No network."""

from __future__ import annotations

import asyncio
import threading

import fake_realtime as fr

import talk_relay
from talk_tools import TalkToolError


def test_function_call_round_trip():
    def executor(name, arguments):
        assert name == "search_memory"
        assert arguments == {"query": "the deploy", "limit": 3}
        return "You shipped it Tuesday."

    recorder = fr.run_transcript(
        [fr.function_call("search_memory", '{"query": "the deploy", "limit": 3}')],
        tool_executor=executor,
    )

    assert recorder.sent_types == ["conversation.item.create", "response.create"]
    item = recorder.sent[0]["item"]
    assert item["type"] == "function_call_output"
    assert item["call_id"] == "call_1"
    assert item["output"] == "You shipped it Tuesday."


def test_async_tool_wait_is_bounded_and_answers_honestly(monkeypatch):
    release = threading.Event()

    def stuck_tool(_name, _arguments):
        release.wait()
        return "late result"

    async def scenario():
        relay = talk_relay.RealtimeRelay(tool_executor=stuck_tool)
        messages = await relay.handle_event_async(fr.function_call("slow_tool", "{}"))
        release.set()
        return messages

    monkeypatch.setattr(talk_relay, "TOOL_EXECUTION_WAIT_S", 0.01)
    messages = asyncio.run(scenario())

    assert "still running" in messages[0]["item"]["output"]
    assert "result won't return" in messages[0]["item"]["output"]
    assert len(messages) == 1


def test_async_tool_worker_cannot_hold_process_exit_open():
    worker_was_daemon: list[bool] = []

    def inspect_worker(_name, _arguments):
        worker_was_daemon.append(threading.current_thread().daemon)
        return "done"

    async def scenario():
        relay = talk_relay.RealtimeRelay(tool_executor=inspect_worker)
        await relay.handle_event_async(fr.function_call("inspect", "{}"))

    asyncio.run(scenario())

    assert worker_was_daemon == [True]


def test_timed_out_tools_have_bounded_worker_admission(monkeypatch):
    release = threading.Event()
    started = 0
    started_lock = threading.Lock()

    def stuck_tool(_name, _arguments):
        nonlocal started
        with started_lock:
            started += 1
        release.wait()
        return "late"

    pool = talk_relay._DaemonWorkerPool(max_workers=1, max_pending=1)
    monkeypatch.setattr(talk_relay, "_TOOL_POOL", pool)
    monkeypatch.setattr(talk_relay, "TOOL_EXECUTION_WAIT_S", 0.01)

    async def scenario():
        relays = [talk_relay.RealtimeRelay(tool_executor=stuck_tool) for _ in range(20)]
        return await asyncio.gather(
            *(relay.handle_event_async(fr.function_call("stuck", "{}")) for relay in relays)
        )

    messages = asyncio.run(scenario())
    release.set()

    assert pool.worker_count == 1
    assert started <= 1
    outputs = [result[0]["item"]["output"] for result in messages]
    running = sum(
        output.startswith("The stuck tool is still running") for output in outputs
    )
    assert running == started
    assert all(
        "still running" in output or "not started" in output or "did not start" in output
        for output in outputs
    )


def test_pending_tool_timeout_says_it_never_started(monkeypatch):
    release = threading.Event()
    pool = talk_relay._DaemonWorkerPool(max_workers=1, max_pending=2)
    monkeypatch.setattr(talk_relay, "_TOOL_POOL", pool)
    monkeypatch.setattr(talk_relay, "TOOL_EXECUTION_WAIT_S", 0.02)

    def blocked(_name, _arguments):
        release.wait()
        return "late"

    async def scenario():
        relay = talk_relay.RealtimeRelay(tool_executor=blocked)
        first = asyncio.create_task(
            relay.handle_event_async(fr.function_call("first", "{}", "call_first"))
        )
        await asyncio.sleep(0.005)
        second = await relay.handle_event_async(
            fr.function_call("second", "{}", "call_second")
        )
        release.set()
        await first
        return second

    messages = asyncio.run(scenario())
    output = messages[0]["item"]["output"]
    assert "did not start" in output
    assert "still running" not in output


def test_barge_in_cancels_and_drains():
    recorder = fr.run_transcript(
        [
            fr.response_created(),
            fr.audio_delta(b"\x01\x02"),
            fr.speech_started(),
            fr.audio_delta(b"\x03\x04"),
        ]
    )

    assert recorder.barge_ins == 1
    assert recorder.sent == [{"type": "response.cancel"}]
    # The callback fires so the caller can drop queued audio; the relay itself
    # never owns the playback buffer.
    assert recorder.audio == [b"\x01\x02", b"\x03\x04"]


def test_speech_while_idle_drains_but_cancels_nothing():
    # The operator starting a turn while the model is silent is the NORMAL
    # case — cancelling then earns "no active response found" on every turn
    # (live-session finding).
    recorder = fr.run_transcript([fr.speech_started()])

    assert recorder.barge_ins == 1
    assert recorder.sent == []


def test_response_done_rearms_the_idle_gate():
    recorder = fr.run_transcript(
        [fr.response_created(), fr.response_done(), fr.speech_started()]
    )

    assert recorder.barge_ins == 1
    assert recorder.sent == []


def test_benign_cancel_race_error_stays_silent():
    recorder = fr.run_transcript(
        [
            {
                "type": "error",
                "error": {"message": "Cancellation failed: no active response found"},
            }
        ]
    )

    assert recorder.errors == []


def test_audio_and_captions_reach_their_callbacks():
    recorder = fr.run_transcript(
        [
            fr.audio_delta(b"pcm-bytes", item_id="item_9"),
            fr.transcript_delta("hey "),
            fr.transcript_delta("there"),
        ]
    )

    assert recorder.audio == [b"pcm-bytes"]
    assert recorder.caption_text == "hey there"
    assert recorder.sent == []


def test_unknown_tool_answers_instead_of_crashing():
    def executor(name, arguments):
        raise TalkToolError(f"unknown talk tool: {name!r}")

    recorder = fr.run_transcript(
        [fr.function_call("launch_missiles", "{}")], tool_executor=executor
    )

    outputs = recorder.function_outputs()
    assert len(outputs) == 1
    assert "isn't available" in outputs[0]
    assert "launch_missiles" in outputs[0]
    assert recorder.sent_types[-1] == "response.create"


def test_malformed_arguments_get_a_speakable_answer():
    def executor(name, arguments):  # pragma: no cover - must never be reached
        raise AssertionError("executor ran on unparseable arguments")

    recorder = fr.run_transcript(
        [fr.function_call("search_memory", "{not json")], tool_executor=executor
    )

    assert recorder.function_outputs() == [talk_relay.UNPARSEABLE_ARGS_TEXT]


def test_non_object_arguments_get_a_speakable_answer():
    recorder = fr.run_transcript(
        [fr.function_call("search_memory", "[1, 2, 3]")],
        tool_executor=lambda *_: "should not run",
    )

    assert recorder.function_outputs() == [talk_relay.UNPARSEABLE_ARGS_TEXT]


def test_error_event_is_spoken_not_dumped():
    recorder = fr.run_transcript([fr.server_error("session expired")])

    assert len(recorder.errors) == 1
    assert "session expired" in recorder.errors[0]
    assert "Traceback" not in recorder.errors[0]
    assert recorder.sent == []


def test_session_id_is_noted_and_unknown_events_are_ignored():
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)

    assert relay.handle_event({"type": "session.created", "session": {"id": "sess_1"}}) == []
    assert relay.session_id == "sess_1"
    assert relay.handle_event({"type": "session.updated", "session": {"id": "sess_2"}}) == []
    assert relay.session_id == "sess_2"
    assert relay.handle_event({"type": "rate_limits.updated"}) == []
    assert relay.handle_event({}) == []


def test_response_done_clears_the_spoken_item():
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)

    relay.handle_event(fr.audio_delta(b"x", item_id="item_7"))
    assert relay.last_audio_item_id == "item_7"
    relay.handle_event({"type": "response.done"})
    assert relay.last_audio_item_id is None


def test_undecodable_audio_delta_is_reported_not_raised():
    recorder = fr.run_transcript(
        [{"type": "response.output_audio.delta", "item_id": "i", "delta": "!!!not-base64!!!"}]
    )

    assert recorder.audio == []
    assert len(recorder.errors) == 1
