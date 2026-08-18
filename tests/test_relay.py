"""Relay — the event loop, against scripted transcripts. No network."""

from __future__ import annotations

import asyncio
import threading

import fake_realtime as fr
import pytest

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


def test_authorization_denial_is_returned_without_executing_the_tool():
    executed = []
    authorized = []

    def executor(name, arguments):  # pragma: no cover - denial must stop here
        executed.append((name, arguments))
        return "mutated"

    def authorizer(name, event):
        authorized.append((name, event["response_id"]))
        return "That state-changing tool was not run."

    relay = talk_relay.RealtimeRelay(
        tool_executor=executor,
        tool_authorizer=authorizer,
    )
    messages = relay.handle_event(
        {
            **fr.function_call("delegate_task", '{"task": "ship it"}'),
            "response_id": "resp_operator_turn",
        }
    )

    assert executed == []
    assert authorized == [("delegate_task", "resp_operator_turn")]
    assert messages[0]["item"]["output"] == "That state-changing tool was not run."


def test_authorizer_allow_reaches_the_tool_executor():
    relay = talk_relay.RealtimeRelay(
        tool_executor=lambda name, _arguments: f"ran {name}",
        tool_authorizer=lambda _name, _event: None,
    )

    messages = relay.handle_event(fr.function_call("search_memory", '{}'))

    assert messages[0]["item"]["output"] == "ran search_memory"


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
    # never owns the playback buffer. The cancelled response's own tail
    # (even unnamed) is fenced, so only what played before the barge-in reaches
    # the speaker — see test_cancelled_unnamed_response_tail_audio_never_
    # reaches_the_speaker for the dedicated regression test.
    assert recorder.audio == [b"\x01\x02"]


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


def test_completed_input_transcription_fires_turn_callback():
    turns = []
    relay = talk_relay.RealtimeRelay(
        on_transcript_turn=lambda role, text: turns.append((role, text))
    )

    relay.handle_event(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "  remember that the deploy is Friday  ",
        }
    )

    assert turns == [("user", "remember that the deploy is Friday")]


def test_assistant_deltas_are_emitted_once_at_done_boundary():
    turns = []
    relay = talk_relay.RealtimeRelay(
        on_transcript_turn=lambda role, text: turns.append((role, text))
    )

    relay.handle_event(fr.transcript_delta("The deploy "))
    relay.handle_event(fr.transcript_delta("is Friday."))
    assert turns == []

    relay.handle_event({"type": "response.output_audio_transcript.done"})

    assert turns == [("assistant", "The deploy is Friday.")]


def test_assistant_done_payload_wins_over_incomplete_deltas_and_resets_buffer():
    turns = []
    relay = talk_relay.RealtimeRelay(
        on_transcript_turn=lambda role, text: turns.append((role, text))
    )

    relay.handle_event(fr.transcript_delta("partial"))
    relay.handle_event(
        {"type": "response.output_audio_transcript.done", "transcript": "complete answer"}
    )
    relay.handle_event(fr.transcript_delta("next"))
    relay.handle_event({"type": "response.output_audio_transcript.done"})

    assert turns == [("assistant", "complete answer"), ("assistant", "next")]


def test_undecodable_audio_delta_is_reported_not_raised():
    recorder = fr.run_transcript(
        [{"type": "response.output_audio.delta", "item_id": "i", "delta": "!!!not-base64!!!"}]
    )

    assert recorder.audio == []
    assert len(recorder.errors) == 1


# -- response identity --------------------------------------------------------
#
# OpenAI keeps sending a cancelled response's audio and transcript deltas after
# response.cancel, and can start the next response before the old one's terminal
# event lands. Everything below drives handle_realtime_event, the dispatch path
# a real CLI or Discord call actually runs.


def test_cancelled_response_tail_audio_never_reaches_the_speaker():
    """The reported bug: barge-in, and the dead response keeps talking."""

    recorder = fr.run_neutral_transcript(
        [
            fr.rt_response_started("resp_A"),
            fr.rt_audio(b"\x01", response_id="resp_A"),
            fr.rt_speech_started(),
            fr.rt_audio(b"\x02", response_id="resp_A"),
            fr.rt_audio(b"\x03", response_id="resp_A"),
        ]
    )

    assert recorder.audio == [b"\x01"]
    assert recorder.command_types == ["CancelResponse"]


def test_the_next_response_speaks_while_the_cancelled_one_stays_silent():
    recorder = fr.run_neutral_transcript(
        [
            fr.rt_response_started("resp_A"),
            fr.rt_audio(b"\x01", response_id="resp_A"),
            fr.rt_speech_started(),
            fr.rt_response_finished("resp_A"),
            fr.rt_response_started("resp_B"),
            fr.rt_audio(b"\x02", response_id="resp_A"),  # tail, arriving late
            fr.rt_audio(b"\x03", response_id="resp_B"),
        ]
    )

    assert recorder.audio == [b"\x01", b"\x03"]


def test_stale_tail_audio_cannot_redirect_the_next_barge_in_truncate():
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)

    fr.play_neutral(
        relay,
        recorder,
        [
            fr.rt_response_started("resp_A"),
            fr.rt_audio(b"\x01", item_id="item_A", response_id="resp_A"),
            fr.rt_speech_started(),
            fr.rt_response_finished("resp_A"),
            fr.rt_response_started("resp_B"),
            fr.rt_audio(b"\x02", item_id="item_B", response_id="resp_B"),
            fr.rt_audio(b"\x03", item_id="item_A", response_id="resp_A"),
        ],
    )

    # talk_cli.on_barge_in truncates whatever this names. A dead response must
    # not be able to point the next truncate back at its own item.
    assert relay.last_audio_item_id == "item_B"


def test_interrupted_text_is_not_folded_into_the_next_answer():
    recorder = fr.run_neutral_transcript(
        [
            fr.rt_response_started("resp_A"),
            fr.rt_transcript_delta("The deploy is on ", response_id="resp_A"),
            fr.rt_speech_started(),
            # The cancelled response still reports the sentence it never got to
            # finish. Recording it would put words in the transcript that the
            # operator never heard.
            fr.rt_transcript_done("The deploy is on Friday.", response_id="resp_A"),
            fr.rt_response_finished("resp_A"),
            fr.rt_response_started("resp_B"),
            fr.rt_transcript_delta("Tuesday.", response_id="resp_B"),
            fr.rt_transcript_done(response_id="resp_B"),
        ]
    )

    assert recorder.turns == [("assistant", "Tuesday.")]


def test_a_terminal_for_a_dead_response_cannot_end_the_live_one():
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)

    fr.play_neutral(
        relay,
        recorder,
        [
            fr.rt_response_started("resp_A"),
            fr.rt_response_finished("resp_A"),
            fr.rt_response_started("resp_B"),
            fr.rt_audio(b"\x01", item_id="item_B", response_id="resp_B"),
            fr.rt_response_finished("resp_A"),  # replayed terminal
        ],
    )

    assert relay.response_active is True
    assert relay.last_audio_item_id == "item_B"

    fr.play_neutral(relay, recorder, [fr.rt_response_finished("resp_B")])

    assert relay.response_active is False
    assert relay.last_audio_item_id is None


def test_a_duplicate_terminal_does_not_replay_the_response_it_ends():
    recorder = fr.run_neutral_transcript(
        [
            fr.rt_response_started("resp_1"),
            fr.rt_audio(b"\x01", response_id="resp_1"),
            fr.rt_transcript_done("done once", response_id="resp_1"),
            fr.rt_response_finished("resp_1"),
            fr.rt_response_finished("resp_1"),
            fr.rt_transcript_done("done once", response_id="resp_1"),
            fr.rt_audio(b"\x01", response_id="resp_1"),
        ]
    )

    assert recorder.audio == [b"\x01"]
    assert recorder.turns == [("assistant", "done once")]


def test_a_replayed_start_cannot_hand_the_speaker_back_to_a_settled_response():
    recorder = fr.run_neutral_transcript(
        [
            fr.rt_response_started("resp_A"),
            fr.rt_response_finished("resp_A"),
            fr.rt_response_started("resp_B"),
            fr.rt_response_started("resp_A"),  # replayed start
            fr.rt_audio(b"\x01", response_id="resp_B"),
            fr.rt_audio(b"\x02", response_id="resp_A"),
        ]
    )

    assert recorder.audio == [b"\x01"]


def test_barge_in_cancels_once_per_response_but_always_drains_playback():
    recorder = fr.run_neutral_transcript(
        [
            fr.rt_response_started("resp_A"),
            fr.rt_audio(b"\x01", response_id="resp_A"),
            fr.rt_speech_started(),
            fr.rt_speech_started(),
            fr.rt_speech_started(),
        ]
    )

    assert recorder.command_types == ["CancelResponse"]
    assert recorder.barge_ins == 3


def test_a_cancelled_response_still_blocks_announcements_until_it_terminates():
    # response.cancel is a request, not an acknowledgement: the server still
    # owns the response until its terminal event, and announcing into that
    # window is what earns "conversation already has an active response".
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)

    fr.play_neutral(
        relay, recorder, [fr.rt_response_started("resp_A"), fr.rt_speech_started()]
    )
    assert relay.response_active is True

    fr.play_neutral(relay, recorder, [fr.rt_response_finished("resp_A")])
    assert relay.response_active is False


def test_events_the_provider_did_not_stamp_still_reach_the_speaker():
    # Fail open. An unnamed response is ambiguous, and muting a real answer is
    # a worse failure than replaying a stale one.
    recorder = fr.run_neutral_transcript(
        [
            fr.rt_response_started(None),
            fr.rt_audio(b"\x01", response_id=None),
            fr.rt_transcript_done("still spoken", response_id=None),
            fr.rt_response_finished(None),
        ]
    )

    assert recorder.audio == [b"\x01"]
    assert recorder.turns == [("assistant", "still spoken")]


def test_the_settled_ledger_stays_bounded_over_a_long_call():
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)
    turns = talk_relay.MAX_SETTLED_RESPONSES * 3

    for index in range(turns):
        fr.play_neutral(
            relay,
            recorder,
            [
                fr.rt_response_started(f"resp_{index}"),
                fr.rt_response_finished(f"resp_{index}"),
            ],
        )

    assert len(relay._settled_response_ids) == talk_relay.MAX_SETTLED_RESPONSES
    # Eviction takes the oldest, so the responses whose tails could still be in
    # flight are exactly the ones still fenced.
    fr.play_neutral(
        relay, recorder, [fr.rt_audio(b"\x01", response_id=f"resp_{turns - 1}")]
    )
    assert recorder.audio == []


def test_a_fresh_relay_carries_no_residual_response_state():
    # A reconnect builds a new relay, so close has to leave nothing behind.
    relay = fr.build_relay(fr.Recorder())

    assert relay.response_active is False
    assert relay.last_audio_item_id is None
    assert relay._settled_response_ids == {}


def test_a_resumed_session_drops_the_previous_responses_tail():
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)

    fr.play_neutral(
        relay, recorder, [fr.rt_response_started("resp_before"), fr.rt_speech_started()]
    )
    # The call resumes and the server names a new response. Anything still
    # carrying the old id is a tail, not an answer.
    fr.play_neutral(
        relay,
        recorder,
        [
            fr.rt_response_started("resp_after"),
            fr.rt_audio(b"\x01", response_id="resp_before"),
            fr.rt_audio(b"\x02", response_id="resp_after"),
        ],
    )

    assert recorder.audio == [b"\x02"]


def test_response_active_cannot_be_set_behind_the_ledgers_back():
    relay = fr.build_relay(fr.Recorder())

    with pytest.raises(AttributeError):
        relay.response_active = True


def test_dict_dispatch_fences_stale_events_like_the_live_path():
    # Both dispatch paths share one fence so they cannot drift apart again —
    # which is how _DISPATCH ended up carrying a bug the live path never had.
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)

    for event in [
        fr.response_created("resp_A"),
        fr.audio_delta(b"\x01", item_id="item_A", response_id="resp_A"),
        fr.transcript_delta("interrupted ", response_id="resp_A"),
        fr.speech_started(),
        fr.audio_delta(b"\x02", item_id="item_A", response_id="resp_A"),
        {
            "type": "response.output_audio_transcript.done",
            "transcript": "interrupted mid-sentence.",
            "response_id": "resp_A",
        },
        fr.response_done("resp_A"),
    ]:
        recorder.sent.extend(relay.handle_event(event))

    assert recorder.audio == [b"\x01"]
    assert recorder.sent_types == ["response.cancel"]
    assert recorder.turns == []
    assert relay.response_active is False


def test_cancelled_unnamed_response_tail_audio_never_reaches_the_speaker():
    # Same bug as test_cancelled_response_tail_audio_never_reaches_the_speaker,
    # for a response the provider never named on the wire (a documented, real
    # OpenAI behaviour, not a hypothetical).
    recorder = fr.run_neutral_transcript(
        [
            fr.rt_response_started(None),
            fr.rt_audio(b"\x01", response_id=None),
            fr.rt_speech_started(),
            fr.rt_audio(b"\x02", response_id=None),
            fr.rt_audio(b"\x03", response_id=None),
        ]
    )

    assert recorder.audio == [b"\x01"]
    assert recorder.command_types == ["CancelResponse"]


def test_an_unnamed_start_cannot_un_name_an_already_identified_response():
    # A degraded/duplicate response.created with no id must not blind the
    # ledger to a named response that is still open — that would silently
    # disable both fencing and settle-on-cancel for it.
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)

    fr.play_neutral(
        relay,
        recorder,
        [
            fr.rt_response_started("resp_A"),
            fr.rt_response_started(None),  # spurious unnamed start, mid-turn
            fr.rt_speech_started(),
            fr.rt_audio(b"\x01", response_id="resp_A"),  # tail after cancel
        ],
    )

    assert recorder.audio == []


def test_repeated_barge_in_on_an_unnamed_response_cancels_only_once():
    recorder = fr.run_neutral_transcript(
        [
            fr.rt_response_started(None),
            fr.rt_speech_started(),
            fr.rt_speech_started(),
            fr.rt_speech_started(),
        ]
    )

    assert recorder.command_types == ["CancelResponse"]
    assert recorder.barge_ins == 3


def test_a_second_barge_in_on_a_lost_terminal_recovers_the_ledger():
    # A response whose terminal event never arrives after its cancel would
    # hold response_active true forever — announcements defer indefinitely.
    # A SECOND barge-in on that same settled response proves the terminal is
    # lost (the cancel went out a full user-turn ago), so the ledger releases
    # it: still no second cancel, but no more waiting for a ghost.
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)

    fr.play_neutral(
        relay, recorder, [fr.rt_response_started("resp_A"), fr.rt_speech_started()]
    )
    assert recorder.command_types == ["CancelResponse"]
    assert relay.response_active is True  # cancel sent; terminal still owed

    fr.play_neutral(relay, recorder, [fr.rt_speech_started()])
    assert recorder.command_types == ["CancelResponse"]  # one cancel per response
    assert relay.response_active is False  # the ledger recovered

    # The next response starts cleanly, speaks, and the dead response's late
    # tail stays fenced by its settled id.
    fr.play_neutral(
        relay,
        recorder,
        [
            fr.rt_response_started("resp_B"),
            fr.rt_audio(b"\x01", response_id="resp_A"),
            fr.rt_audio(b"\x02", response_id="resp_B"),
        ],
    )
    assert relay.response_active is True
    assert recorder.audio == [b"\x02"]


def test_a_second_barge_in_on_a_lost_unnamed_terminal_recovers_the_ledger():
    # Same lost-terminal recovery for a response the provider never named:
    # the second barge-in releases the in-flight state without a second
    # cancel, and a fresh response then starts on a closed ledger.
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)

    fr.play_neutral(
        relay, recorder, [fr.rt_response_started(None), fr.rt_speech_started()]
    )
    assert recorder.command_types == ["CancelResponse"]
    assert relay.response_active is True

    fr.play_neutral(relay, recorder, [fr.rt_speech_started()])
    assert recorder.command_types == ["CancelResponse"]
    assert relay.response_active is False

    fr.play_neutral(
        relay,
        recorder,
        [fr.rt_response_started("resp_B"), fr.rt_audio(b"\x01", response_id="resp_B")],
    )
    assert relay.response_active is True
    assert recorder.audio == [b"\x01"]


def test_an_unstamped_terminal_still_closes_a_named_active_response():
    # _finish_response's stated invariant: an unnamed terminal never wedges
    # the session open, even when the response it's closing had a name.
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)

    fr.play_neutral(relay, recorder, [fr.rt_response_started("resp_A")])
    assert relay.response_active is True

    fr.play_neutral(relay, recorder, [fr.rt_response_finished(None)])

    assert relay.response_active is False
    assert relay._active_response_id is None
    assert relay.last_audio_item_id is None


def test_dict_dispatch_replayed_start_cannot_hand_back_a_settled_response():
    # Neutral-path equivalent: test_a_replayed_start_cannot_hand_the_speaker_
    # back_to_a_settled_response. The dict path extracts response ids from raw
    # keys (_nested_response_id/_event_response_id) instead of typed events, so
    # it needs its own coverage of the same scenario.
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)

    for event in [
        fr.response_created("resp_A"),
        fr.response_done("resp_A"),
        fr.response_created("resp_B"),
        fr.response_created("resp_A"),  # replayed start
        fr.audio_delta(b"\x01", response_id="resp_B"),
        fr.audio_delta(b"\x02", response_id="resp_A"),
    ]:
        recorder.sent.extend(relay.handle_event(event))

    assert recorder.audio == [b"\x01"]


def test_dict_dispatch_settled_ledger_stays_bounded_over_a_long_call():
    # Neutral-path equivalent: test_the_settled_ledger_stays_bounded_over_a_
    # long_call, driven through handle_event instead of handle_realtime_event.
    recorder = fr.Recorder()
    relay = fr.build_relay(recorder)
    turns = talk_relay.MAX_SETTLED_RESPONSES * 3

    for index in range(turns):
        for event in [
            fr.response_created(f"resp_{index}"),
            fr.response_done(f"resp_{index}"),
        ]:
            recorder.sent.extend(relay.handle_event(event))

    assert len(relay._settled_response_ids) == talk_relay.MAX_SETTLED_RESPONSES
    recorder.sent.extend(
        relay.handle_event(fr.audio_delta(b"\x01", response_id=f"resp_{turns - 1}"))
    )
    assert recorder.audio == []
