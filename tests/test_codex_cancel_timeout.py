"""Bounded waits against a scripted local peer; no models or credentials."""

import json
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_codex_worker import _Run, peer, run_worker, setup, wait_for

from talk_codex_wire import CodexWorkerError
from talk_codex_worker import CodexWorkerConfig


def reads(tmp_path):
    return [row for row in peer(tmp_path)["requests"] if row.get("method") == "thread/read"]


def one_owned_turn(tmp_path):
    record = peer(tmp_path)
    methods = [row.get("method") for row in record["requests"]]
    assert record["processes"] == 1
    assert methods.count("thread/start") == methods.count("turn/start") == 1
    assert "thread/resume" not in methods
    assert len(record["thread"]["turns"]) == 1
    assert all(
        row["params"] == {"threadId": "thread-owned", "includeTurns": False}
        for row in reads(tmp_path)
    )


@pytest.mark.parametrize("scenario", ["hold", "metadata_unavailable"])
def test_silent_responsive_turn_survives_repeated_liveness_reads(tmp_path, scenario):
    worker = setup(tmp_path, scenario, liveness_interval_s=0.05, liveness_timeout_s=0.5)
    with run_worker(worker) as running:
        wait_for(lambda: worker.snapshot()["state"] == "running")
        wait_for(lambda: len(reads(tmp_path)) >= 3)
        assert not running._done.is_set()
        assert worker.snapshot()["error"] is None
        worker.cancel_event.set()
        assert running.result(timeout=5)["state"] == "interrupted"
    one_owned_turn(tmp_path)


@pytest.mark.parametrize("scenario", ["approval", "approval_metadata_unavailable"])
def test_approval_wait_survives_repeated_metadata_reads_and_resolves_once(tmp_path, scenario):
    worker = setup(tmp_path, scenario, liveness_interval_s=0.05, liveness_timeout_s=0.5)
    with run_worker(worker) as running:
        wait_for(lambda: bool(worker.approvals()))
        original = worker.approvals()[0]
        wait_for(lambda: len(reads(tmp_path)) >= 3)
        assert worker.approvals() == [original]
        assert not running._done.is_set()
        assert worker.approve(original["request_id"], "once")["submitted"] is True
        assert running.result(timeout=5)["state"] == "completed"
    assert peer(tmp_path)["approval_replies"] == 1
    one_owned_turn(tmp_path)


def test_unanswered_probe_does_not_expire_a_current_approval(tmp_path):
    worker = setup(
        tmp_path,
        "unresponsive_approval",
        liveness_interval_s=0.05,
        liveness_timeout_s=0.05,
        wire_timeout=0.35,
    )
    with run_worker(worker) as running:
        wait_for(lambda: bool(worker.approvals()))
        original = worker.approvals()[0]
        # A second request proves the first real RPC waiter timed out and returned.
        wait_for(lambda: len(reads(tmp_path)) >= 2)
        assert not running._done.is_set()
        assert worker.approvals() == [original]
        worker.approve(original["request_id"], "once")
        assert running.result(timeout=5)["state"] == "completed"
    one_owned_turn(tmp_path)


@pytest.mark.parametrize("wire_timeout,probe_timeout", [(2.0, 0.05), (0.25, 1.0)])
def test_unanswered_probe_bounds_both_local_deadline_and_rpc_waiter(
    tmp_path,
    wire_timeout,
    probe_timeout,
):
    worker = setup(
        tmp_path,
        "unresponsive_read",
        liveness_interval_s=0.05,
        liveness_timeout_s=probe_timeout,
        wire_timeout=wire_timeout,
    )
    with run_worker(worker) as running:
        wait_for(lambda: worker.snapshot()["state"] == "running")
        started = time.monotonic()
        result = running.result(timeout=5)
        assert time.monotonic() - started < 4
    assert result["state"] == "unknown" and result["error"] == "outcome_unknown"
    assert len(reads(tmp_path)) == 1
    assert worker.wire.process.poll() is not None
    assert not worker.wire._pending
    one_owned_turn(tmp_path)


def test_cancellation_during_unanswered_probe_does_not_wait_for_probe_deadline(tmp_path):
    worker = setup(
        tmp_path,
        "unresponsive_read",
        liveness_interval_s=0.05,
        liveness_timeout_s=5.0,
        wire_timeout=5.0,
    )
    with run_worker(worker) as running:
        wait_for(lambda: worker.snapshot()["state"] == "running")
        wait_for(lambda: bool(reads(tmp_path)))
        started = time.monotonic()
        worker.cancel_event.set()
        result = running.result(timeout=3)
        assert time.monotonic() - started < 2
    assert result["state"] == "interrupted"
    assert worker.wire.process.poll() is not None
    assert not worker.wire._pending
    one_owned_turn(tmp_path)


def test_cancellation_keeps_its_deadline_when_probe_and_interrupt_are_unanswered(tmp_path):
    worker = setup(
        tmp_path,
        "unresponsive_read_interrupt",
        liveness_interval_s=0.05,
        liveness_timeout_s=5.0,
        wire_timeout=0.35,
    )
    worker.CANCEL_TIMEOUT_S = 0.1
    with run_worker(worker) as running:
        wait_for(lambda: worker.snapshot()["state"] == "running")
        wait_for(lambda: bool(reads(tmp_path)))
        worker.cancel_event.set()
        result = running.result(timeout=3)
    assert result["state"] == "unknown" and result["error"] == "cancellation_unconfirmed"
    assert worker.wire.process.poll() is not None
    assert not worker.wire._pending
    one_owned_turn(tmp_path)


def test_owned_events_keep_a_turn_live_when_a_metadata_rpc_stalls(tmp_path):
    worker = setup(
        tmp_path,
        "progress_on_steer",
        liveness_interval_s=0.05,
        liveness_timeout_s=0.4,
        wire_timeout=0.6,
    )
    with run_worker(worker) as running:
        wait_for(lambda: worker.snapshot()["state"] == "running")
        wait_for(lambda: bool(reads(tmp_path)))
        for index in range(12):
            action = f"steer-{index}"
            worker.control(action, text=action)
            wait_for(lambda action=action: worker.snapshot()["output"] == action)
            assert not running._done.wait(0.07)
        worker.cancel_event.set()
        assert running.result(timeout=5)["state"] == "interrupted"
    one_owned_turn(tmp_path)


def test_liveness_response_cannot_replace_owned_thread(tmp_path):
    worker = setup(tmp_path, "foreign_metadata", liveness_interval_s=0.05)
    with run_worker(worker) as running:
        result = running.result(timeout=5)
    assert result["state"] == "unknown" and result["error"] == "foreign_thread"
    assert result["thread_id"] == "thread-owned" and result["turn_id"] == "turn-owned"
    one_owned_turn(tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("liveness_interval_s", 0),
        ("liveness_interval_s", 3601),
        ("liveness_interval_s", float("nan")),
        ("liveness_interval_s", True),
        ("liveness_timeout_s", -1),
        ("liveness_timeout_s", 61),
        ("liveness_timeout_s", float("inf")),
        ("liveness_timeout_s", "5"),
    ],
)
def test_liveness_configuration_has_finite_bounds(tmp_path, field, value):
    config = CodexWorkerConfig(True, sys.executable, str(tmp_path), "explicit-model")
    with pytest.raises(CodexWorkerError, match="invalid_liveness_timeout"):
        replace(config, **{field: value}).validate()


def test_fixture_snapshots_stay_immutable_while_a_reader_holds_an_old_file(tmp_path):
    worker = setup(tmp_path, "hold")
    with run_worker(worker) as running:
        wait_for(lambda: worker.snapshot()["state"] == "running")
        first = sorted(tmp_path.glob("peer-*.json"))[0]
        original = first.read_bytes()
        with first.open("rb") as retained:
            for index in range(5):
                worker.control(f"snapshot-{index}", text="Keep the owned turn.")
            worker.cancel_event.set()
            assert running.result(timeout=5)["state"] == "interrupted"
            assert retained.read() == original
            assert first.read_bytes() == original
    paths = sorted(tmp_path.glob("peer-*.json"))
    sequences = [int(path.stem.rsplit("-", 1)[1]) for path in paths]
    assert sequences == list(range(1, len(paths) + 1))
    assert not list(tmp_path.glob("peer-*.tmp"))
    assert all(isinstance(json.loads(path.read_text()), dict) for path in paths)
    one_owned_turn(tmp_path)


@pytest.mark.parametrize("hang_close", [False, True])
def test_daemon_cleanup_cannot_wait_forever_for_run_or_close(hang_close):
    release = threading.Event()
    close_done = threading.Event()

    def close():
        try:
            if hang_close:
                release.wait()
        finally:
            close_done.set()

    worker = SimpleNamespace(run=release.wait, wire=SimpleNamespace(close=close))
    started = time.monotonic()
    running = None
    try:
        with (
            pytest.raises(AssertionError, match="wedged"),
            run_worker(worker, cleanup_timeout=0.05) as running,
        ):
            assert running.thread.daemon
        assert time.monotonic() - started < 1
    finally:
        release.set()
        assert running._done.wait(1)
        assert close_done.wait(1)


def test_daemon_waiter_preserves_worker_failure():
    def fail():
        raise ValueError("fixture failure")

    running = _Run(fail)
    with pytest.raises(ValueError, match="fixture failure"):
        running.result(timeout=1)


def test_accepted_approval_starts_a_fresh_quiet_interval(tmp_path):
    worker = setup(
        tmp_path,
        "approval_continue_unresponsive",
        liveness_interval_s=0.5,
        liveness_timeout_s=0.05,
        wire_timeout=2,
    )
    with run_worker(worker) as running:
        wait_for(lambda: bool(worker.approvals()))
        current = worker.approvals()[0]
        wait_for(lambda: bool(reads(tmp_path)))
        assert not running._done.wait(0.3)
        worker.approve(current["request_id"], "once")
        wait_for(lambda: peer(tmp_path).get("approval_replies") == 1)
        # No provider event follows the reply, and the old probe remains unanswered.
        assert not running._done.wait(0.3)
        assert worker.snapshot()["error"] is None
        worker.cancel_event.set()
        assert running.result(timeout=5)["state"] == "interrupted"
    one_owned_turn(tmp_path)
