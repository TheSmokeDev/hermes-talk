"""Relay — the event loop, against scripted transcripts. No network."""

from __future__ import annotations

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
