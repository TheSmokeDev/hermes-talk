"""Exact result presentation over the existing durable task-event receipts."""

from dataclasses import replace

import pytest
from test_task_events import bind, poll, scope

from talk_attachment import CaptureToken
from talk_passive import HistoryError
from talk_task_events import TaskEvents
from talk_task_sources import TaskEventError


def completed(tmp_path, *, clock=None):
    store, box, token = scope(tmp_path, clock=clock)
    bind(store, token)
    source = store.open_source(
        token, "job", mode="api_poll", source_session="worker", run_id=7
    )
    store.observe_poll(token, source, 7, poll(), live=True)
    event = store.page(token)["events"][0]["event_id"]
    return store, box, token, source, event


def claim(store, token, event, **kwargs):
    return store.queue_speech(token, event, presentation_protocol=1, **kwargs)


def test_result_and_playback_are_distinct_exact_idempotent_facts(tmp_path):
    store, _, token, _, event = completed(tmp_path)
    ready = store.presentation(token, event)
    assert ready["state"] == "result_ready" and ready["result_ready"]
    assert ready["operation_id"] == "request-one" and ready["run_id"] == 7
    attempt = claim(store, token, event, playback_supported=True)
    store.acknowledge_speech(token, attempt, "submitting")
    store.acknowledge_speech(token, attempt, "context_submitted")
    assert not store.presentation(token, event)["playback_started"]
    with pytest.raises(TaskEventError, match="invalid_delivery"):
        store.acknowledge_speech(token, attempt, "playback_finished", response_id="response-a")
    for _ in range(2):
        store.acknowledge_speech(token, attempt, "playback_started", response_id="response-a")
    with pytest.raises(TaskEventError, match="invalid_delivery"):
        store.acknowledge_speech(token, attempt, "playback_finished", response_id="response-b")
    store.acknowledge_speech(token, attempt, "playback_finished", response_id="response-a")
    view = store.presentation(token, event)
    assert view["context_submitted"] and view["playback_started"] and view["playback_finished"]
    assert view["state"] == "playback_finished" and not view["unknown"]
    assert store.page(token)["events"][0]["presentation"] == view


def test_dispatch_fence_recovers_only_proven_unsent_claims(tmp_path):
    now = [100.0]
    store, _, token, _, event = completed(tmp_path, clock=lambda: now[0])
    first = claim(store, token, event)
    now[0] += 31
    assert store.speech_candidates(token)[0]["event_id"] == event
    second = claim(store, token, event)
    with pytest.raises(TaskEventError, match="invalid_delivery"):
        store.acknowledge_speech(token, first, "submitting")
    store.acknowledge_speech(token, second, "submitting")
    now[0] += 31
    assert store.speech_candidates(token) == []
    assert store.presentation(token, event)["state"] == "unknown"


def test_deferred_backoff_and_renewal_do_not_hot_loop(tmp_path):
    now = [100.0]
    store, _, token, _, event = completed(tmp_path, clock=lambda: now[0])
    first = claim(store, token, event)
    now[0] += 29
    store.acknowledge_speech(token, first, "renewed")
    now[0] += 2
    assert store.speech_candidates(token) == []
    store.defer_speech(token, first)
    assert store.speech_candidates(token) == []
    now[0] += 0.25
    second = claim(store, token, event)
    store.defer_speech(token, second)
    assert store.presentation(token, event)["retry_at"] == now[0] + 0.5


@pytest.mark.parametrize("state", ["interrupted", "unknown"])
def test_explicit_replay_survives_reconnect_without_reexecuting(tmp_path, state):
    store, box, token, _, event = completed(tmp_path)
    first = claim(store, token, event)
    store.acknowledge_speech(token, first, "submitting")
    store.acknowledge_speech(token, first, "context_submitted")
    store.acknowledge_speech(token, first, state)
    view = store.presentation(token, event)
    assert view["context_submitted"] and view[state] and view["replay_eligible"]
    fresh = CaptureToken(
        token.owner, token.connection_id, box.begin(token.owner, token.connection_id)
    )
    reopened = TaskEvents(box, fresh)
    assert reopened.speech_candidates(fresh) == []
    replay = claim(reopened, fresh, event, replay=True)
    assert replay.attempt_id != first.attempt_id
    with pytest.raises(HistoryError, match="stale_generation"):
        store.acknowledge_speech(token, first, "unknown")
    with pytest.raises(TaskEventError, match="invalid_delivery"):
        reopened.acknowledge_speech(fresh, replace(first, capture=fresh), "unknown")


def test_uncorrelated_output_cannot_finish_and_repeated_notice_is_consumed_once(tmp_path):
    store, _, token, source, event = completed(tmp_path)
    attempt = claim(store, token, event)
    store.acknowledge_speech(token, attempt, "submitting")
    store.acknowledge_speech(token, attempt, "context_submitted")
    with pytest.raises(TaskEventError, match="invalid_delivery"):
        store.acknowledge_speech(token, attempt, "playback_started", response_id="response-a")
    store.observe_poll(token, source, 7, poll(2), live=True)
    assert store.speech_candidates(token) == []


def test_full_result_view_does_not_truncate_and_never_speaks(tmp_path):
    from test_task_events import run_record

    store, _, token, _, _ = completed(tmp_path)
    output = "Full output " * 1000
    result = store.result_view(token, 7, resolve_run=lambda _: run_record(output=output))
    assert result["output"] == output and result["speak"] is False


@pytest.mark.parametrize("state", ["interrupted", "unknown"])
def test_late_handoff_preserves_the_retired_state(tmp_path, state):
    store, _, token, _, event = completed(tmp_path)
    attempt = claim(store, token, event)
    store.acknowledge_speech(token, attempt, "submitting")
    store.acknowledge_speech(token, attempt, state)
    store.acknowledge_speech(token, attempt, "context_submitted")
    presentation = store.presentation(token, event)
    assert presentation["state"] == state and presentation["context_submitted"]
    assert not presentation["playback_finished"]


def test_expired_handoff_can_record_late_interrupt_but_cannot_finish(tmp_path):
    now = [100.0]
    store, _, token, _, event = completed(tmp_path, clock=lambda: now[0])
    attempt = claim(store, token, event, playback_supported=True)
    store.acknowledge_speech(token, attempt, "submitting")
    now[0] += 31
    store.acknowledge_speech(token, attempt, "context_submitted")
    store.acknowledge_speech(token, attempt, "interrupted")
    view = store.presentation(token, event)
    assert view["unknown"] and view["interrupted"] and view["context_submitted"]
    with pytest.raises(TaskEventError, match="invalid_delivery"):
        store.acknowledge_speech(token, attempt, "playback_finished", response_id="late")


def test_candidate_batch_and_concurrent_claims_are_bounded(tmp_path):
    from test_task_events import run_record

    store, _, token = scope(tmp_path)
    for index in range(1, 11):
        store.bind_run(
            token, {**run_record(request=f"request-{index}"), "runId": index},
            operator="operator", worker_session_id="worker", api_run_id=f"remote-{index}",
        )
        source = store.open_source(
            token, f"job-{index}", mode="api_poll", source_session="worker", run_id=index,
        )
        store.observe_poll(token, source, index, {**poll(), "run_id": f"remote-{index}"}, live=True)
    candidates = store.speech_candidates(token)
    assert len(candidates) == 8
    claim(store, token, candidates[0]["event_id"])
    with pytest.raises(TaskEventError, match="delivery_exists"):
        claim(store, token, candidates[1]["event_id"])


def test_replay_of_approval_reference_is_not_speakable(tmp_path):
    from test_task_events import approval_event

    store, _, token = scope(tmp_path)
    event = approval_event(store, token)
    with pytest.raises(TaskEventError, match="replay_not_speakable"):
        claim(store, token, event, replay=True)


def test_replaying_a_duplicate_observation_projects_the_newest_attempt(tmp_path):
    store, _, token, source, event = completed(tmp_path)
    first = claim(store, token, event)
    store.acknowledge_speech(token, first, "submitting")
    store.acknowledge_speech(token, first, "unknown")
    store.observe_poll(token, source, 7, poll(2), live=True)
    newest = store.page(token)["events"][-1]["event_id"]
    replay = claim(store, token, newest, replay=True)
    assert store.job_presentations(token)[7]["attempt_id"] == replay.attempt_id
    assert store.job_presentations(token)[7]["state"] == "claimed"
    store.acknowledge_speech(token, replay, "submitting")
    store.acknowledge_speech(token, replay, "unknown")
    original_replay = claim(store, token, event, replay=True)
    assert store.job_presentations(token)[7]["attempt_id"] == original_replay.attempt_id
