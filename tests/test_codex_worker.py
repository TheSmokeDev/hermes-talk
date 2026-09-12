"""Real subprocess/SQLite integration with a scripted app-server, no model or tool execution."""

import json
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from talk_codex_store import CodexJobs
from talk_codex_wire import CodexAppServer, CodexWorkerError
from talk_codex_worker import CodexWorker, CodexWorkerConfig
from talk_outbox import HistoryOutbox
from talk_passive import HistoryOwner, digest

SCRIPT = Path(__file__).parent / "fixtures" / "codex_app_server.py"


def setup(
    tmp_path,
    scenario="complete",
    *,
    enabled=True,
    jobs=None,
    context="Reference context",
    wire_timeout=2,
    **config_options,
):
    owner = HistoryOwner(digest("host"), "default", digest("principal"), "parent")
    config = CodexWorkerConfig(
        enabled, sys.executable, str(tmp_path), "explicit-model", **config_options
    )
    request = {
        "parent_session_id": "parent",
        "child_session_id": "child",
        "origin_turn_id": "origin",
        "action_id": "action",
        "goal": "Derived worker goal",
        "context": context,
    }
    jobs = jobs or CodexJobs(HistoryOutbox(tmp_path, profile="default"))

    def wire():
        return CodexAppServer(
            (sys.executable, "-u", str(SCRIPT), str(tmp_path / "peer.json"), scenario),
            cwd=str(tmp_path),
            timeout=wire_timeout,
            version_command=(sys.executable, str(SCRIPT), "--version"),
        )

    worker = CodexWorker(jobs, owner, "hermes-job", request, config, wire_factory=wire)
    return worker


def peer(tmp_path):
    return json.loads(sorted(tmp_path.glob("peer-*.json"))[-1].read_text(encoding="utf-8"))


def peer_wrote_nothing(tmp_path):
    return not list(tmp_path.glob("peer-*.json"))


def wait_for(check):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("Scripted worker did not reach the expected state")


class _Run:
    """A worker operation in flight, joined with a deadline instead of forever."""

    def __init__(self, call):
        self._done = threading.Event()
        self._value: Any = None
        self._error: BaseException | None = None
        self.thread = threading.Thread(target=self._call, args=(call,), daemon=True)
        self.thread.start()

    def _call(self, call):
        try:
            self._value = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised by result()
            self._error = exc
        finally:
            self._done.set()

    def result(self, timeout=10) -> Any:
        if not self._done.wait(timeout):
            raise AssertionError("worker operation never returned; the worker is wedged")
        if self._error is not None:
            raise self._error
        return self._value


@contextmanager
def run_worker(worker, *, cleanup_timeout=10):
    """Run the worker off-thread and bound the join.

    A plain executor context manager calls ``shutdown(wait=True)`` on exit, so
    one wedged worker thread hangs the whole process forever — which on a slow
    Windows runner is indistinguishable from a very long test. The thread here
    is a daemon and the join is bounded, so a stuck worker fails the test
    loudly and cannot hold interpreter exit open.
    """

    running = _Run(worker.run)
    try:
        yield running
    finally:
        deadline = time.monotonic() + cleanup_timeout
        if worker.wire is not None:
            _Run(worker.wire.close).result(timeout=cleanup_timeout)
        assert running._done.wait(max(0, deadline - time.monotonic())), (
            "worker.run() never returned; the worker is wedged"
        )


def test_disabled_worker_starts_no_process_or_job(tmp_path):
    with pytest.raises(CodexWorkerError, match="worker_disabled"):
        setup(tmp_path, enabled=False)
    assert peer_wrote_nothing(tmp_path)
    jobs = CodexJobs(HistoryOutbox(tmp_path, profile="default"))
    with jobs.outbox._db() as db:
        assert db.execute("SELECT count(*) FROM codex_jobs").fetchone()[0] == 0


def test_full_result_and_same_job_recovery_do_not_launch_again(tmp_path):
    worker = setup(tmp_path)
    result = worker.run()
    assert result["state"] == "completed", result["error"]
    assert result["thread_id"] == "thread-owned" and result["turn_id"] == "turn-owned"
    expected = "First complete section\n" * 800 + "\n\nSecond section [artifact](result.md)"
    assert result["output"] == expected
    assert result["items"][-1]["changes"][0]["diff"] == "+full artifact"
    assert worker.wire.process.poll() is not None
    assert setup(tmp_path).run()["output"] == result["output"]
    assert peer(tmp_path)["processes"] == 1
    methods = [row.get("method") for row in peer(tmp_path)["requests"]]
    assert methods.count("thread/start") == methods.count("turn/start") == 1


def test_lost_turn_response_recovers_by_original_client_message_without_relaunch(tmp_path):
    first = setup(tmp_path, "drop_turn").run()
    assert first["state"] == "unknown" and first["thread_id"] == "thread-owned"
    restored = setup(tmp_path).run()
    assert restored["state"] == "completed"
    assert restored["turn_id"] == "turn-owned"
    methods = [row.get("method") for row in peer(tmp_path)["requests"]]
    assert methods.count("turn/start") == 1 and methods.count("thread/start") == 1
    assert "thread/read" in methods


def test_lost_thread_response_never_guesses_or_creates_replacement(tmp_path):
    first = setup(tmp_path, "drop_thread").run()
    assert first["state"] == "unknown" and first["thread_id"] is None
    assert setup(tmp_path).run()["state"] == "unknown"
    assert peer(tmp_path)["processes"] == 1


@pytest.mark.parametrize(
    "scenario,error", [("bad_policy", "policy_mismatch"), ("foreign", "foreign_thread")]
)
def test_policy_or_foreign_thread_refusal_closes_only_the_owned_process(tmp_path, scenario, error):
    worker = setup(tmp_path, scenario)
    assert worker.run()["error"] == error
    assert worker.wire.process.poll() is not None
    if scenario == "bad_policy":
        assert not any(row.get("method") == "turn/start" for row in peer(tmp_path)["requests"])


def test_version_mismatch_starts_no_app_server(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_VERSION", "0.999.0")
    worker = setup(tmp_path)
    assert worker.run()["error"] == "unsupported_version"
    assert peer_wrote_nothing(tmp_path)


def test_exact_steering_and_cancellation_keep_original_thread_and_turn(tmp_path):
    worker = setup(tmp_path, "hold")
    with run_worker(worker) as running:
        wait_for(lambda: worker.snapshot()["state"] == "running")
        original = "  Keep the prefix.\n  Add this correction exactly.  "
        receipt = worker.control("steer-one", text=original)
        assert receipt["state"] == "queued"
        assert worker.control("steer-one", text=original) == receipt
        with pytest.raises(CodexWorkerError, match="event_conflict"):
            worker.control("steer-one", text="changed")
        assert worker.control("cancel-one", cancel=True)["state"] == "cancel_requested"
        assert running.result(timeout=5)["state"] == "interrupted"
    steering = [row for row in peer(tmp_path)["requests"] if row.get("method") == "turn/steer"]
    assert len(steering) == 1 and steering[0]["params"]["input"][0]["text"] == original
    assert steering[0]["params"]["expectedTurnId"] == "turn-owned"
    assert len(peer(tmp_path)["thread"]["turns"]) == 1


def test_current_approval_once_and_replay_cannot_authorize_a_new_request(tmp_path):
    worker = setup(tmp_path, "approval_replay")
    with run_worker(worker) as running:
        wait_for(lambda: bool(worker.approvals()))
        current = worker.approvals()[0]
        with pytest.raises(CodexWorkerError, match="approval_unavailable"):
            worker.approve("foreign", "once")
        assert worker.approve(current["request_id"], "once")["evidence"] == "transport_handoff"
        wait_for(lambda: peer(tmp_path).get("approval_replies") == 1)
        with pytest.raises(CodexWorkerError, match="approval_unavailable"):
            worker.approve(current["request_id"], "once")
        assert worker.approvals() == []
        worker.control("stop-approved-job", cancel=True)
        assert running.result(timeout=5)["state"] == "interrupted"
    assert peer(tmp_path)["approval_replies"] == 1


def test_foreign_owner_and_changed_original_request_are_refused(tmp_path):
    worker = setup(tmp_path)
    other = replace(worker.owner, principal=digest("another-principal"))
    with pytest.raises(CodexWorkerError, match="job_unavailable"):
        worker.jobs.read(other, worker.job_id)
    with pytest.raises(CodexWorkerError, match="event_conflict"):
        CodexWorker(
            worker.jobs,
            worker.owner,
            worker.job_id,
            {**worker.request, "goal": "different"},
            worker.config,
        )
    assert peer_wrote_nothing(tmp_path)


def test_stalled_stdin_cannot_hold_worker_shutdown_forever(tmp_path):
    worker = setup(tmp_path, "blocked_writer", context="context " * 3900)
    started = time.monotonic()
    result = worker.run()
    assert result["state"] == "unknown"
    assert time.monotonic() - started < 10
    assert worker.wire.process.poll() is not None


def test_owner_revoked_after_thread_creation_prevents_turn_execution(tmp_path):
    worker = setup(tmp_path)
    allowed = [True]
    worker.authorize = lambda: allowed[0]

    def revoke(record):
        if record["state"] == "thread_ready":
            allowed[0] = False

    worker.on_change = revoke
    result = worker.run()
    assert result["state"] == "unknown" and result["error"] == "owner_retired"
    assert not any(row.get("method") == "turn/start" for row in peer(tmp_path)["requests"])


@pytest.mark.parametrize(
    "field,value", [("model", 7), ("workspace", []), ("sandbox", {}), ("approval_policy", [])]
)
def test_malformed_configuration_is_a_fixed_refusal(tmp_path, field, value):
    config = CodexWorkerConfig(True, sys.executable, str(tmp_path), "explicit-model")
    with pytest.raises(CodexWorkerError):
        replace(config, **{field: value}).validate()


@pytest.mark.parametrize("scenario", ["ignore_interrupt", "ack_no_terminal"])
def test_unconfirmed_cancellation_exits_with_partial_result_and_no_replacement(tmp_path, scenario):
    worker = setup(tmp_path, scenario)
    worker.CANCEL_TIMEOUT_S = 0.25
    with run_worker(worker) as running:
        wait_for(lambda: worker.snapshot()["output"] == "Partial work before stop")
        worker.cancel_event.set()
        result = running.result(timeout=8)
    assert result["state"] == "unknown" and result["error"] == "cancellation_unconfirmed"
    assert result["output"] == "Partial work before stop"
    assert worker.approvals() == []
    assert worker.wire.process.poll() is not None
    assert sum(row.get("method") == "turn/interrupt" for row in peer(tmp_path)["requests"]) == 1
    assert peer(tmp_path)["processes"] == 1
