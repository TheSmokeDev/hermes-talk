"""Shared replay/observation behavior over real durable P2a storage, with no providers."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

import talk_apiserver
import talk_runs
from talk_attachment import CaptureToken, HistoryDelivery
from talk_outbox import HistoryOutbox, PendingHistory
from talk_passive import HistoryError, HistoryMessage, HistoryOwner, HistoryReceipt, digest
from talk_task_events import ApprovalReader, TaskEvents
from talk_task_sources import TaskEventError


def scope(tmp_path, *, parent="parent", tab="tab", clock=None, max_events=256, ttl=86400):
    owner = HistoryOwner(digest("host"), "default", digest("principal"), parent)
    opts = {"clock": clock} if clock else {}
    box = HistoryOutbox(tmp_path, profile="default", **opts)
    token = CaptureToken(owner, tab, box.begin(owner, tab))
    store = TaskEvents(box, token, max_events=max_events, ttl_s=ttl, **opts)
    return store, box, token


def rpc(store, token, *, epoch="epoch-a", source="rpc", session="parent"):
    return store.open_source(token, source, mode="rpc", source_session=session, epoch=epoch)


def frame(seq, *, kind="tool.start", name="terminal", session="parent", **payload):
    return {
        "session_id": session,
        "seq": seq,
        "type": kind,
        "payload": {"name": name, "tool_id": f"call-{seq}", **payload},
    }


def replay(*frames, epoch="epoch-a", latest=None, truncated=False):
    return {
        "events": list(frames),
        "epoch": epoch,
        "count": len(frames),
        "latest_seq": latest
        if latest is not None
        else max((row["seq"] for row in frames), default=0),
        "truncated": truncated,
    }


def run_record(*, parent="parent", request="request-one", status="done", output="Completed work"):
    return {
        "runId": 7,
        "status": status,
        "output": output,
        "ticket": {
            "hermesSessionId": parent,
            "operator": "operator",
            "profile": "default",
            "requestId": request,
            "talkSessionId": "old-call",
            "generationId": "old-gen",
        },
    }


def bind(store, token):
    return store.bind_run(
        token,
        run_record(),
        operator="operator",
        worker_session_id="worker",
        api_run_id="remote-seven",
        origin_turn_id="origin-work",
    )


def poll(updated=1.0, *, status="completed", **extra):
    return {
        "run_id": "remote-seven",
        "session_id": "worker",
        "status": status,
        "updated_at": updated,
        "last_event": "run.completed",
        **extra,
    }


def test_rpc_duplicate_out_of_order_and_contiguous_cursor(tmp_path):
    store, _, token = scope(tmp_path)
    lease = rpc(store, token)
    store.observe_rpc(token, lease, replay(frame(3), frame(1), latest=3))
    page = store.page(token)
    assert page["sources"][0]["cursor"] == 1
    assert page["sources"][0]["gap"] == "missing_sequence"
    assert [row["source_seq"] for row in page["events"]] == [3, 1]
    assert all(row["canonical_revision"] is None for row in page["events"])
    store.observe_rpc(token, lease, replay(frame(2), latest=3))
    store.observe_rpc(token, lease, replay(frame(1), frame(2), frame(3)))
    page = store.page(token)
    assert len(page["events"]) == 3
    assert page["sources"][0]["cursor"] == 3
    assert page["sources"][0]["gap"] == "none"
    assert page["speak"] is False


def test_conflicting_replay_rolls_back_events_and_cursor_together(tmp_path):
    store, _, token = scope(tmp_path)
    lease = rpc(store, token)
    store.observe_rpc(token, lease, replay(frame(1)))
    before = store.page(token)
    with pytest.raises(TaskEventError, match="event_conflict"):
        store.observe_rpc(token, lease, replay(frame(2), frame(1, name="read_file")))
    assert store.page(token) == before


def test_epoch_reset_and_truncated_replay_require_refetch(tmp_path):
    store, _, token = scope(tmp_path)
    old = rpc(store, token)
    store.observe_rpc(token, old, replay(frame(10), truncated=True))
    assert store.page(token)["sources"][0]["gap"] == "truncated"
    fresh = store.open_source(
        token, "rpc", mode="rpc", source_session="parent", epoch="epoch-b", previous=old
    )
    store.observe_rpc(token, fresh, replay(frame(1), epoch="epoch-b"))
    page = store.page(token)
    assert page["sources"][0]["cursor"] == 1
    assert page["sources"][0]["gap"] == "epoch_reset"
    assert page["snapshot_refetch_required"]
    assert [row["source_epoch"] for row in page["events"]] == ["epoch-a", "epoch-b"]
    with pytest.raises(TaskEventError, match="stale_source"):
        store.observe_rpc(token, old, replay(frame(11)))
    with pytest.raises(TaskEventError, match="stale_source"):
        store.open_source(
            token, "rpc", mode="rpc", source_session="parent", epoch="epoch-c", previous=old
        )


def test_rpc_transcript_fragments_and_raw_tool_payloads_are_not_stored(tmp_path):
    store, _, token = scope(tmp_path)
    lease = rpc(store, token)
    store.observe_rpc(
        token,
        lease,
        replay(
            frame(1, kind="message.delta", text="secret-transcript"),
            frame(2, args={"password": "secret-credential"}, command="secret-command"),
            frame(3, kind="message.complete", text="secret-final-text"),
        ),
    )
    page = store.page(token)
    assert [row["kind"] for row in page["events"]] == ["tool_started", "response_completed"]
    assert page["sources"][0]["cursor"] == 3
    raw = (tmp_path / "state" / "talk-history-outbox.sqlite3").read_bytes()
    assert all(
        value not in raw
        for value in (
            b"secret-transcript",
            b"secret-credential",
            b"secret-command",
            b"secret-final-text",
        )
    )


def test_api_poll_is_snapshot_only_and_never_opens_single_consumer_stream(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not subscribe, execute, or submit an approval")

    monkeypatch.setattr(talk_apiserver, "stream_run_events", forbidden)
    monkeypatch.setattr(talk_apiserver, "respond_to_approval", forbidden)
    monkeypatch.setattr(talk_runs, "start_run", forbidden)
    store, _, token = scope(tmp_path)
    bind(store, token)
    lease = store.open_source(
        token, "poll-seven", mode="api_poll", source_session="worker", run_id=7
    )
    store.source_unavailable(token, lease)
    assert store.page(token)["sources"][0]["availability"] == "unavailable"
    assert store.page(token)["sources"][0]["gap"] == "snapshot_only"
    store.observe_poll(token, lease, 7, poll(4, output="not copied"))
    store.observe_poll(token, lease, 7, poll(4, output="not copied"))
    assert store.observe_poll(token, lease, 7, poll(2, status="running")) is None
    page = store.page(token)
    assert len(page["events"]) == 1
    assert page["events"][0]["state"] == "completed"
    assert page["events"][0]["source_seq"] is None
    assert page["events"][0]["source_epoch"] is None
    assert page["events"][0]["origin_turn_id"] == "origin-work"
    assert page["sources"][0]["gap"] == "snapshot_only"


def test_poll_and_hook_must_match_immutable_accepted_run(tmp_path):
    store, _, token = scope(tmp_path)
    original = bind(store, token)
    with pytest.raises(TaskEventError, match="event_conflict"):
        store.bind_run(
            token, run_record(), operator="operator", worker_session_id="different-worker"
        )
    with pytest.raises(TaskEventError, match="foreign_owner"):
        store.bind_run(
            token, run_record(parent="other"), operator="operator", worker_session_id="worker"
        )
    lease = store.open_source(token, "hook-seven", mode="hook", source_session="worker", run_id=7)
    store.observe_hook(
        token,
        lease,
        7,
        "post_tool_call",
        {"session_id": "worker", "tool_name": "read_file"},
        observation_id="hook-once",
    )
    store.observe_hook(
        token,
        lease,
        7,
        "post_tool_call",
        {"session_id": "worker", "tool_name": "read_file"},
        observation_id="hook-once",
    )
    with pytest.raises(TaskEventError, match="foreign_owner"):
        store.observe_hook(
            token, lease, 7, "post_tool_call", {"session_id": "foreign"}, observation_id="foreign"
        )
    assert original.origin_turn_id == store.page(token)["events"][0]["origin_turn_id"]
    assert len(store.page(token)["events"]) == 1
    assert store.page(token)["sources"][0]["gap"] == "unsequenced"


def test_result_replay_reads_current_run_record_without_delivery_or_authority(
    tmp_path, monkeypatch
):
    store, _, token = scope(tmp_path)
    bind(store, token)
    lease = store.open_source(
        token, "poll-seven", mode="api_poll", source_session="worker", run_id=7
    )
    store.observe_poll(token, lease, 7, poll())
    monkeypatch.setattr(
        talk_runs, "get_run", lambda run_id: run_record(output="authoritative-result")
    )
    assert store.result_view(token, 7)["output"] == "authoritative-result"
    assert store.result_view(token, 7)["speak"] is False
    assert store.page(token)["events"][0]["delivery"] == "unclaimed"
    raw = (tmp_path / "state" / "talk-history-outbox.sqlite3").read_bytes()
    assert b"authoritative-result" not in raw
    with pytest.raises(TaskEventError, match="foreign_owner"):
        store.result_view(token, 7, resolve_run=lambda _: run_record(parent="foreign"))


@pytest.mark.parametrize("before_crash", ["queued", "sent", "playback_acknowledged"])
def test_speech_crash_states_do_not_requeue_on_replay(tmp_path, before_crash):
    store, box, token = scope(tmp_path)
    lease = rpc(store, token)
    store.observe_rpc(token, lease, replay(frame(1)), live=True)
    event = store.page(token)["events"][0]["event_id"]
    attempt = store.queue_speech(token, event, playback_supported=True)
    if before_crash != "queued":
        store.acknowledge_speech(token, attempt, "sent")
    if before_crash == "playback_acknowledged":
        store.acknowledge_speech(token, attempt, before_crash)
    fresh = CaptureToken(
        token.owner, token.connection_id, box.begin(token.owner, token.connection_id)
    )
    reopened = TaskEvents(box, fresh)
    delivery = reopened.page(fresh)["events"][0]["delivery"]
    assert delivery == (
        "playback_acknowledged" if before_crash == "playback_acknowledged" else "unknown"
    )
    with pytest.raises(TaskEventError, match="replay_not_speakable"):
        reopened.queue_speech(fresh, event, playback_supported=True)
    with pytest.raises(HistoryError, match="stale_generation"):
        store.acknowledge_speech(token, attempt, "unknown")


def test_observation_is_not_ack_and_replayed_events_are_never_speech_candidates(tmp_path):
    store, _, token = scope(tmp_path)
    lease = rpc(store, token)
    store.observe_rpc(token, lease, replay(frame(1)))
    event = store.page(token)["events"][0]["event_id"]
    assert store.page(token)["events"][0]["delivery"] == "unclaimed"
    with pytest.raises(TaskEventError, match="replay_not_speakable"):
        store.queue_speech(token, event)
    store.observe_rpc(token, lease, replay(frame(2)), live=True)
    event = store.page(token)["events"][1]["event_id"]
    attempt = store.queue_speech(token, event)
    with pytest.raises(TaskEventError, match="delivery_exists"):
        store.queue_speech(token, event)
    store.acknowledge_speech(token, attempt, "sent")
    with pytest.raises(TaskEventError, match="invalid_delivery"):
        store.acknowledge_speech(token, attempt, "playback_acknowledged")


def approval_event(store, token):
    lease = rpc(store, token)
    store.observe_rpc(
        token,
        lease,
        replay(
            frame(
                1,
                kind="approval.request",
                request_id="approval-one",
                command="secret command",
                choices=["always"],
            )
        ),
    )
    return store.page(token)["events"][0]["event_id"]


def test_approval_view_rereads_authoritative_pending_and_never_uses_cached_authority(tmp_path):
    store, _, token = scope(tmp_path)
    event = approval_event(store, token)
    pending = [{"request_id": "approval-one", "choices": ["once", "session", "always", "deny"]}]
    reader = ApprovalReader(token.owner, "parent", lambda: list(pending))
    assert store.approval_view(token, event, reader) == {
        "state": "pending_observed",
        "approval_id": "approval-one",
        "choices": ["once", "session", "deny"],
        "actionable": False,
        "submit_boundary": "existing_host_resolver",
    }
    pending.clear()  # authoritative resolver resolved/revoked/expired it meanwhile
    assert store.approval_view(token, event, reader) == {"state": "gone", "actionable": False}
    assert store.approval_view(token, event, None)["state"] == "unsupported"
    assert store.page(token)["events"][0]["approval_id"] == "approval-one"
    with pytest.raises(TaskEventError, match="foreign_owner"):
        store.approval_view(
            token, event, replace(reader, owner=replace(token.owner, session_id="foreign"))
        )


def test_approval_reader_failure_cannot_fall_back_to_old_event(tmp_path):
    store, _, token = scope(tmp_path)
    event = approval_event(store, token)

    def failed():
        raise RuntimeError("secret-host-path and credentials")

    with pytest.raises(TaskEventError, match=r"^unavailable$"):
        store.approval_view(token, event, ApprovalReader(token.owner, "parent", failed))


def test_approval_response_after_new_generation_is_refused(tmp_path):
    store, box, token = scope(tmp_path)
    event = approval_event(store, token)
    entered, release = threading.Event(), threading.Event()

    def read():
        entered.set()
        assert release.wait(5)
        return [{"request_id": "approval-one", "choices": ["once"]}]

    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            store.approval_view, token, event, ApprovalReader(token.owner, "parent", read)
        )
        assert entered.wait(5)
        box.begin(token.owner, token.connection_id)
        release.set()
        with pytest.raises(HistoryError, match="stale_generation"):
            future.result(timeout=5)


def test_stale_generation_cannot_advance_cursor_or_ack(tmp_path):
    store, box, token = scope(tmp_path)
    lease = rpc(store, token)
    fresh = CaptureToken(
        token.owner, token.connection_id, box.begin(token.owner, token.connection_id)
    )
    with pytest.raises(HistoryError, match="stale_generation"):
        store.observe_rpc(token, lease, replay(frame(1)))
    reopened = TaskEvents(box, fresh)
    assert reopened.page(fresh)["sources"][0]["cursor"] == 0


def test_retention_reports_gap_and_old_sequence_cannot_reappear(tmp_path):
    store, _, token = scope(tmp_path, max_events=2)
    lease = rpc(store, token)
    store.observe_rpc(token, lease, replay(frame(1), frame(2), frame(3)))
    page = store.page(token)
    assert page["retention_gap"] is True
    assert [row["source_seq"] for row in page["events"]] == [2, 3]
    store.observe_rpc(token, lease, replay(frame(1)), live=True)
    assert [row["source_seq"] for row in store.page(token)["events"]] == [2, 3]


def test_ttl_clears_event_refs_and_source_lease_cannot_revive(tmp_path):
    now = [1000.0]
    store, _, token = scope(tmp_path, clock=lambda: now[0], ttl=10)
    old = rpc(store, token)
    store.observe_rpc(token, old, replay(frame(1)))
    now[0] += 11
    assert store.page(token)["events"] == []
    assert store.page(token)["snapshot_refetch_required"]
    fresh = rpc(store, token)
    assert fresh.revision > old.revision
    with pytest.raises(TaskEventError, match="stale_source"):
        store.observe_rpc(token, old, replay(frame(2)))


def test_confirmed_owner_deletion_cascades_only_authorized_scope(tmp_path):
    store, _, token = scope(tmp_path)
    lease = rpc(store, token)
    store.observe_rpc(token, lease, replay(frame(1)), live=True)
    store.queue_speech(token, store.page(token)["events"][0]["event_id"])
    foreign, _, other = scope(tmp_path, parent="other", tab="other-tab")
    foreign.observe_rpc(
        other, rpc(foreign, other, session="other"), replay(frame(1, session="other"))
    )
    with pytest.raises(TaskEventError, match="foreign_owner"):
        store.delete_owner(other)
    store.delete_owner(token)
    with pytest.raises(HistoryError, match="stale_generation"):
        store.page(token)
    assert len(foreign.page(other)["events"]) == 1
    with sqlite3.connect(tmp_path / "state" / "talk-history-outbox.sqlite3") as db:
        for table in ("task_event_owners", "task_event_sources", "task_events", "task_event_runs"):
            assert (
                db.execute(
                    f"SELECT count(*) FROM {table} WHERE owner=?", (token.owner.key,)
                ).fetchone()[0]
                == 0
            )
        assert db.execute("SELECT count(*) FROM task_event_speech").fetchone()[0] == 0


def test_delayed_verified_commit_links_origin_without_rewriting_observation_order(tmp_path):
    store, box, token = scope(tmp_path)
    lease = store.open_source(token, "history", mode="hook", source_session="parent")
    pending = PendingHistory(
        "voice-event",
        "voice-origin",
        token.owner,
        "root",
        token.connection_id,
        token.generation,
        (HistoryMessage("user", "not copied"),),
        "pending",
        0,
        "",
    )
    box.add(pending)
    receipt = HistoryReceipt("voice-event", "voice-origin", "root", "compressed-tip", (9,), 17)
    delivery = HistoryDelivery("saved", receipt=receipt)
    with pytest.raises(TaskEventError, match="missing_reference"):
        store.observe_commit(token, lease, delivery)
    source = rpc(store, token)
    store.observe_rpc(token, source, replay(frame(4, kind="message.complete"), truncated=True))
    first = store.page(token)["events"][0]
    box.mark(
        token.owner,
        "voice-event",
        connection_id=token.connection_id,
        generation=token.generation,
        state="saved",
    )
    store.observe_commit(token, lease, delivery)
    rows = store.page(token)["events"]
    assert rows[0] == first
    assert rows[1]["origin_turn_id"] == "voice-origin"
    assert rows[1]["canonical_revision"] == 17
    assert rows[1]["source_seq"] is None and rows[1]["occurred_at"] is None
    assert rows[1]["observed_index"] > rows[0]["observed_index"]
    assert b"not copied" not in (tmp_path / "state" / "talk-history-outbox.sqlite3").read_bytes()
    box.invalidate(
        token.owner,
        code="retired",
        event_id="voice-event",
        connection_id=token.connection_id,
        generation=token.generation,
    )
    assert store.page(token)["events"] == [first]
    with pytest.raises(TaskEventError, match="missing_reference"):
        store.observe_commit(token, lease, delivery)


def test_redacted_diagnostics_contain_no_owner_payload_or_results(tmp_path):
    store, _, token = scope(tmp_path, parent="private-parent")
    lease = rpc(store, token, session="private-parent")
    store.observe_rpc(
        token, lease, replay(frame(1, session="private-parent", args={"key": "secret"}))
    )
    data = json.dumps(store.diagnostics(token))
    assert "private-parent" not in data and "secret" not in data and token.owner.key not in data
    assert json.loads(data)["stream_consumers"] == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"events": "not a replay", "epoch": "epoch-a"},
        replay({"seq": 1, "session_id": "parent", "type": {}, "payload": {}}),
        {**replay(frame(1)), "count": True},
        replay(frame(True)),
    ],
)
def test_malformed_source_never_changes_cursor_or_events(tmp_path, bad):
    store, _, token = scope(tmp_path)
    lease = rpc(store, token)
    before = store.page(token)
    with pytest.raises(TaskEventError, match="invalid_event"):
        store.observe_rpc(token, lease, bad)
    assert store.page(token) == before


def test_source_payload_caps_and_no_unknown_transport_modes(tmp_path):
    store, _, token = scope(tmp_path)
    lease = rpc(store, token)
    with pytest.raises(TaskEventError, match="invalid_event"):
        store.observe_rpc(token, lease, replay(*(frame(seq) for seq in range(1, 514))))
    with pytest.raises(TaskEventError, match="unsupported"):
        store.open_source(token, "unimplemented", mode="codex_remote", source_session="parent")
    assert store.page(token)["events"] == []


def test_poll_source_cannot_be_reused_for_another_run_or_foreign_lease(tmp_path):
    store, _, token = scope(tmp_path)
    bind(store, token)
    second = {**run_record(request="request-two"), "runId": 8}
    store.bind_run(
        token, second, operator="operator", worker_session_id="worker", api_run_id="remote-eight"
    )
    lease = store.open_source(token, "poll", mode="api_poll", source_session="worker", run_id=7)
    with pytest.raises(TaskEventError, match="unsupported"):
        store.observe_poll(token, lease, 8, {**poll(), "run_id": "remote-eight"})
    with pytest.raises(TaskEventError, match="foreign_owner"):
        store.observe_poll(token, replace(lease, owner_key=digest("foreign")), 7, poll())
    store.observe_poll(token, lease, 7, poll(1))
    assert store.observe_poll(token, lease, 7, poll(2, status="running")) is None
    assert store.page(token)["events"][0]["state"] == "completed"


def test_retention_does_not_evict_foreign_owner_to_make_room(tmp_path):
    store, _, token = scope(tmp_path, max_events=1)
    lease = rpc(store, token)
    store.observe_rpc(token, lease, replay(frame(1)))
    foreign, _, other = scope(tmp_path, parent="other", tab="other-tab", max_events=1)
    foreign.observe_rpc(
        other, rpc(foreign, other, session="other"), replay(frame(1, session="other"))
    )
    store.observe_rpc(token, lease, replay(frame(2)))
    assert len(foreign.page(other)["events"]) == 1
    assert [row["source_seq"] for row in store.page(token)["events"]] == [2]


def test_reconnect_resolves_durable_result_beyond_ui_listing_cap(tmp_path, monkeypatch):
    store, _, token = scope(tmp_path)
    bind(store, token)
    history = tmp_path / "worker-history.jsonl"
    target = run_record(output="durable worker output")
    records = [target, *({"runId": index, "status": "done"} for index in range(100, 220))]
    history.write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
    monkeypatch.setattr(talk_runs, "_history_path", lambda: history)
    monkeypatch.setattr(talk_runs, "_history_enabled", lambda: True)
    monkeypatch.setattr(talk_runs, "get_run", lambda _: None)
    before = history.read_bytes()
    assert store.result_view(token, 7)["output"] == "durable worker output"
    assert history.read_bytes() == before
    assert talk_runs.resolve_run_record(7)["fromHistory"] is True
    history.write_text(json.dumps({**target, "status": "running"}), encoding="utf-8")
    assert store.result_view(token, 7)["status"] == "lost"
    with pytest.raises(TaskEventError, match="unavailable"):
        store.result_view(token, 7, resolve_run=lambda _: None)


@pytest.mark.parametrize("integrity_gap", ["truncated", "epoch_reset"])
def test_replay_gap_survives_outage_and_ordinary_recovery(tmp_path, integrity_gap):
    store, _, token = scope(tmp_path)
    lease = rpc(store, token)
    if integrity_gap == "truncated":
        store.observe_rpc(token, lease, replay(frame(10), truncated=True))
        recovery = replay(frame(11))
    else:
        store.observe_rpc(token, lease, replay(frame(1)))
        lease = store.open_source(
            token, "rpc", mode="rpc", source_session="parent", epoch="epoch-b", previous=lease
        )
        store.observe_rpc(token, lease, replay(frame(1), epoch="epoch-b"))
        recovery = replay(frame(2), epoch="epoch-b")
    store.source_unavailable(token, lease)
    assert store.page(token)["sources"][0]["gap"] == integrity_gap
    store.observe_rpc(token, lease, recovery)
    page = store.page(token)
    assert page["sources"][0]["gap"] == integrity_gap
    assert page["sources"][0]["availability"] == "available"
    assert page["snapshot_refetch_required"] is True


def test_global_capacity_reclaims_32_abandoned_expired_owners(tmp_path):
    now = [1000.0]
    for index in range(32):
        store, _, token = scope(
            tmp_path, parent=f"old-{index}", tab=f"old-tab-{index}", clock=lambda: now[0], ttl=10
        )
        rpc(store, token, session=f"old-{index}")
    now[0] = 1011.0
    fresh, _, token = scope(tmp_path, parent="fresh", clock=lambda: now[0], ttl=10)
    assert fresh.diagnostics(token)["sources"] == 0
    with sqlite3.connect(tmp_path / "state" / "talk-history-outbox.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM task_event_owners").fetchone()[0] == 1


def test_global_reclamation_preserves_unexpired_foreign_state(tmp_path):
    now = [1000.0]
    for index in range(31):
        scope(tmp_path, parent=f"old-{index}", tab=f"old-tab-{index}", clock=lambda: now[0], ttl=10)
    now[0] = 1005.0
    live, _, live_token = scope(
        tmp_path, parent="live", tab="live-tab", clock=lambda: now[0], ttl=100
    )
    lease = rpc(live, live_token, session="live")
    live.observe_rpc(live_token, lease, replay(frame(1, session="live")))
    live.bind_run(
        live_token, run_record(parent="live"), operator="operator", worker_session_id="worker"
    )
    before = live.page(live_token)
    now[0] = 1011.0
    scope(tmp_path, parent="fresh", clock=lambda: now[0], ttl=10)
    assert live.page(live_token) == before
    assert (
        live.result_view(live_token, 7, resolve_run=lambda _: run_record(parent="live"))["status"]
        == "done"
    )


def test_run_binding_retention_is_independent_of_active_owner(tmp_path):
    now = [1000.0]
    store, _, token = scope(tmp_path, clock=lambda: now[0], ttl=10)
    for index in range(1, 129):
        store.bind_run(
            token,
            {**run_record(request=f"request-{index}"), "runId": index},
            operator="operator",
            worker_session_id=f"worker-{index}",
        )
    heartbeat = rpc(store, token)
    now[0] = 1009.0
    store.observe_rpc(token, heartbeat, replay(frame(1)))
    now[0] = 1011.0
    store.bind_run(
        token,
        {**run_record(request="new-request"), "runId": 999},
        operator="operator",
        worker_session_id="new-worker",
    )
    assert store.page(token)["events"][0]["source_seq"] == 1
    with sqlite3.connect(tmp_path / "state" / "talk-history-outbox.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM task_event_runs").fetchone()[0] == 1


def test_source_retention_is_independent_of_active_owner(tmp_path):
    now = [1000.0]
    store, _, token = scope(tmp_path, clock=lambda: now[0], ttl=10)
    for index in range(63):
        rpc(store, token, source=f"old-source-{index}")
    now[0] = 1009.0
    active = rpc(store, token, source="active-source")
    store.observe_rpc(token, active, replay(frame(1)))
    now[0] = 1011.0
    rpc(store, token, source="new-source")
    assert {row["source_id"] for row in store.page(token)["sources"]} == {
        "active-source",
        "new-source",
    }


def test_active_source_renews_only_its_associated_run(tmp_path):
    now = [1000.0]
    store, _, token = scope(tmp_path, clock=lambda: now[0], ttl=10)
    bind(store, token)
    store.bind_run(
        token,
        {**run_record(request="request-eight"), "runId": 8},
        operator="operator",
        worker_session_id="worker-eight",
    )
    now[0] = 1009.0
    lease = store.open_source(
        token, "active-poll", mode="api_poll", source_session="worker", run_id=7
    )
    now[0] = 1011.0
    store.observe_poll(token, lease, 7, poll(status="running"))
    with sqlite3.connect(tmp_path / "state" / "talk-history-outbox.sqlite3") as db:
        assert [row[0] for row in db.execute("SELECT run_id FROM task_event_runs")] == [7]


def test_preferences_survive_event_expiry_and_surface_reconnect(tmp_path):
    now = [1.0]
    store, box, token = scope(tmp_path, clock=lambda: now[0], ttl=10)
    assert store.preferences(token) == {"update_mode": "important"}
    store.set_update_preference(token, "frequent")
    store.observe_rpc(token, rpc(store, token), replay(frame(1)))
    now[0] = 12.0
    assert store.page(token)["events"] == []
    terminal_token = CaptureToken(token.owner, "terminal", box.begin(token.owner, "terminal"))
    resumed = TaskEvents(box, terminal_token, clock=lambda: now[0])
    assert resumed.preferences(terminal_token) == {"update_mode": "frequent"}
    resumed.set_update_preference(terminal_token, "completion")
    assert store.preferences(token) == {"update_mode": "completion"}


def test_preferences_fence_owner_generation_and_canonical_deletion(tmp_path):
    store, box, token = scope(tmp_path)
    other, _, foreign = scope(tmp_path, parent="other", tab="other-tab")
    store.set_update_preference(token, "completion")
    other.set_update_preference(foreign, "frequent")
    with pytest.raises(TaskEventError, match="foreign_owner"):
        store.preferences(foreign)
    with pytest.raises(TaskEventError, match="foreign_owner"):
        store.set_update_preference(foreign, "important")
    fresh = CaptureToken(
        token.owner, token.connection_id, box.begin(token.owner, token.connection_id)
    )
    with pytest.raises(HistoryError):
        store.set_update_preference(token, "important")
    with pytest.raises(HistoryError):
        store.preferences(token)
    assert store.preferences(fresh)["update_mode"] == "completion"
    box.invalidate(token.owner, code="target_missing", connection_id=fresh.connection_id,
                   generation=fresh.generation)
    with pytest.raises(HistoryError):
        store.preferences(fresh)
    with box._db() as db:
        assert db.execute("SELECT count(*) FROM task_preferences WHERE owner=?",
                          (token.owner.key,)).fetchone()[0] == 0
    assert other.preferences(foreign)["update_mode"] == "frequent"


@pytest.mark.parametrize("mode", [None, "quiet", [], {}, True])
def test_invalid_preferences_leave_existing_setting_unchanged(tmp_path, mode):
    store, _, token = scope(tmp_path)
    store.set_update_preference(token, "completion")
    with pytest.raises(TaskEventError, match="invalid_event"):
        store.set_update_preference(token, mode)
    assert store.preferences(token)["update_mode"] == "completion"


def test_preference_capacity_does_not_evict_other_owners(tmp_path):
    store, box, token = scope(tmp_path)
    with box._db() as db:
        db.executemany("INSERT INTO task_preferences VALUES (?,?)",
                       [(f"owner-{i}", "frequent") for i in range(256)])
    with pytest.raises(TaskEventError, match="capacity"):
        store.set_update_preference(token, "completion")
    assert store.preferences(token)["update_mode"] == "important"
    with box._db() as db:
        assert db.execute("SELECT count(*) FROM task_preferences").fetchone()[0] == 256
        db.execute("UPDATE task_preferences SET owner=? WHERE owner='owner-0'", (token.owner.key,))
    assert store.set_update_preference(token, "completion")["update_mode"] == "completion"


def test_only_unsent_speech_can_be_deferred_without_losing_the_result(tmp_path):
    store, _, token = scope(tmp_path)
    bind(store, token)
    lease = store.open_source(token, "job", mode="api_poll", source_session="worker", run_id=7)
    store.observe_poll(token, lease, 7, poll(), live=True)
    event_id = store.page(token)["events"][0]["event_id"]
    first = store.queue_speech(token, event_id)
    store.defer_speech(token, first)
    second = store.queue_speech(token, event_id)
    assert second.attempt_id != first.attempt_id
    with pytest.raises(TaskEventError, match="invalid_delivery"):
        store.acknowledge_speech(token, first, "sent")
    store.acknowledge_speech(token, second, "sent")
    with pytest.raises(TaskEventError, match="invalid_delivery"):
        store.defer_speech(token, second)
    store.acknowledge_speech(token, second, "unknown")
    with pytest.raises(TaskEventError, match="invalid_delivery"):
        store.defer_speech(token, second)
    assert store.page(token)["events"][0]["state"] == "completed"
