"""Remote terminal outcomes through the HTTP lane, registry and spoken receipt."""

import json
import threading
from itertools import count
from types import SimpleNamespace

import httpx
import pytest

import talk_apiserver
import talk_approvals
import talk_cli
import talk_config
import talk_host
import talk_runs


@pytest.fixture
def lane(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TALK_API_SERVER_KEY", "test-only")
    monkeypatch.setenv("TALK_SESSION_KEY", "test-memory-scope")
    monkeypatch.setattr(talk_config, "api_server_poll_s", lambda: 0)
    monkeypatch.setattr(talk_config, "agent_timeout_s", lambda: 10)
    clock = count()
    monkeypatch.setattr(
        talk_apiserver, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None)
    )
    monkeypatch.setattr(talk_approvals, "watch_run", lambda *args: None)
    monkeypatch.setattr(talk_apiserver, "is_available", lambda: True)
    monkeypatch.setattr(talk_runs, "_history_enabled", lambda: True)
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    talk_runs.attach_owner(
        talk_session_id="terminal-test", generation_id="generation",
        hermes_session_id="session", operator="test", profile=None,
    )
    calls = []

    def respond(outcome, *, before_poll=None):
        def post(url, **kwargs):
            calls.append(("POST", url, kwargs))
            return httpx.Response(202, json={"run_id": "remote-run"})

        def get(url, **kwargs):
            calls.append(("GET", url, kwargs))
            if before_poll:
                before_poll()
            if isinstance(outcome, Exception):
                raise outcome
            return httpx.Response(200, json=outcome)

        monkeypatch.setattr(httpx, "post", post)
        monkeypatch.setattr(httpx, "get", get)

    yield respond, calls
    talk_runs.reset_for_tests()
    talk_host.bind_ctx(None)


def _run_worker(*, during_poll=None):
    """Join the real registry worker, including its final compare-and-set."""
    entered = threading.Event()
    release = threading.Event()
    worker = talk_host._api_server_worker("task", session_id="existing-session")

    def gated(run_id):
        entered.set()
        assert release.wait(3)
        return worker(run_id)

    run_id = talk_runs.start_run("agent", "terminal outcome", gated)
    assert entered.wait(3)
    thread = next(t for t in threading.enumerate() if t.name == f"talk-run-agent-{run_id}")
    try:
        if during_poll:
            during_poll(run_id)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    return talk_runs.get_run(run_id)


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted", "completed"])
def test_http_outcome_reaches_registry_history_and_announcement(lane, status):
    respond, calls = lane
    respond({"status": status, "error": "remote reason", "output": "answer"})
    run = _run_worker()
    expected = "done" if status == "completed" else "failed"
    assert run["status"] == expected
    assert run["meta"]["api_run_id"] == "remote-run"
    if expected == "failed":
        assert f"the agent run {status}: remote reason" in run["output"]
        assert run["meta"]["api_run_status"] == status
    else:
        assert run["output"] == "answer"
    records = [json.loads(line) for line in talk_runs._history_path().read_text().splitlines()]
    terminal = [r for r in records if r["status"] in talk_runs.TERMINAL_STATUSES]
    assert len(terminal) == 1
    assert terminal[0]["status"] == expected
    assert terminal[0]["meta"]["api_run_id"] == "remote-run"

    # The watcher claims before queuing; repeated polls cannot announce twice.
    announcements = []
    for _ in range(2):
        if talk_runs.claim_delivery(run["runId"], claimant="terminal-test"):
            announcements.append(talk_cli.run_finished_commands(run))
    assert len(announcements) == 1
    text = announcements[0][0].text
    verb = "finished" if expected == "done" else "failed"
    assert f"Background run #{run['runId']} {verb}" in text
    assert run["output"] in text
    assert talk_runs.mark_delivered(run["runId"], claimant="terminal-test")
    assert not talk_runs.claim_delivery(run["runId"], claimant="terminal-test")
    assert [(method, url.rsplit("/v1/", 1)[1]) for method, url, _ in calls] == [
        ("POST", "runs"), ("GET", "runs/remote-run"),
    ]
    assert calls[0][2]["json"] == {"input": "task", "session_id": "existing-session"}
    assert calls[0][2]["headers"]["X-Hermes-Session-Key"] == "test-memory-scope"


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_terminal_exception_carries_run_identity_and_status(lane, status):
    respond, _ = lane
    respond({"status": status})
    with pytest.raises(talk_apiserver.TalkApiServerError) as caught:
        talk_apiserver.run_to_completion("task")
    assert caught.value.run_id == "remote-run"
    assert caught.value.status == status
    assert "no reason given" in str(caught.value)


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_memory_flush_does_not_claim_completion(lane, status):
    respond, _ = lane
    respond({"status": status, "error": "remote reason"})
    receipt = talk_host.HostAdapter().flush_agent("review memory")
    assert receipt.startswith("I couldn't finish that work")
    assert f"the agent run {status}: remote reason" in receipt
    assert "FLUSH_DONE" not in receipt


def test_successful_memory_output_is_not_parsed_as_a_status(lane):
    respond, _ = lane
    respond({"status": "completed", "output": "the agent run failed: quoted history"})
    assert talk_host.HostAdapter().flush_agent("review memory").startswith("FLUSH_DONE")


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted", "completed"])
def test_local_stop_wins_over_late_remote_outcome(lane, status):
    respond, _ = lane
    local = {}

    def stop():
        assert talk_runs.finish_run(local["id"], "failed", "local stop receipt")

    respond({"status": status, "error": "remote reason", "output": "answer"}, before_poll=stop)
    run = _run_worker(during_poll=lambda run_id: local.update(id=run_id))
    assert run["status"] == "failed"
    assert run["output"] == "local stop receipt"
    assert run["meta"]["api_run_id"] == "remote-run"
    assert "api_run_status" not in run["meta"]
    assert "the agent run" not in talk_cli.run_finished_commands(run)[0].text


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted", "completed"])
def test_remote_outcome_wins_over_late_stop(lane, status):
    respond, _ = lane
    respond({"status": status, "error": "remote reason", "output": "answer"})
    run = _run_worker()
    assert not talk_runs.finish_run(
        run["runId"], "failed", "late local stop", meta={"api_run_status": "late status"}
    )
    latest = talk_runs.get_run(run["runId"])
    assert latest["output"] == run["output"]
    assert latest["meta"] == run["meta"]
    assert run["status"] == ("done" if status == "completed" else "failed")


@pytest.mark.parametrize("failure", ["network", "timeout"])
def test_lost_tracking_keeps_remote_handle_without_claiming_remote_failure(
    lane, monkeypatch, failure
):
    respond, calls = lane
    respond(httpx.ReadError("connection lost"))
    if failure == "timeout":
        monkeypatch.setattr(talk_config, "agent_timeout_s", lambda: 0)
    run = _run_worker()
    assert run["status"] == "failed"
    assert run["meta"]["api_run_id"] == "remote-run"
    assert "api_run_status" not in run["meta"]
    assert "remote outcome is unknown" in run["output"]
    assert "the agent run failed" not in run["output"]
    assert sum(method == "POST" for method, _, _ in calls) == 1


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
@pytest.mark.parametrize(
    "output_length",
    [
        talk_cli.WATCH_OUTPUT_TAIL_CHARS - 1,
        talk_cli.WATCH_OUTPUT_TAIL_CHARS,
        talk_cli.WATCH_OUTPUT_TAIL_CHARS + 1,
        talk_runs.HISTORY_OUTPUT_CAP,
        talk_runs.HISTORY_OUTPUT_CAP + 1,
        5000,
    ],
)
def test_long_remote_reason_retains_terminal_status_through_announcement(
    lane, status, output_length
):
    respond, _ = lane
    prefix = f"the agent run {status}: "
    respond({"status": status, "error": "x" * (output_length - len(prefix))})
    run = _run_worker()
    assert run["status"] == "failed"
    assert run["output"].startswith(prefix)
    assert run["meta"]["api_run_status"] == status
    assert len(run["output"]) <= talk_runs.HISTORY_OUTPUT_CAP
    records = [json.loads(line) for line in talk_runs._history_path().read_text().splitlines()]
    terminal = [r for r in records if r["status"] in talk_runs.TERMINAL_STATUSES]
    assert len(terminal) == 1
    assert terminal[0]["meta"]["api_run_status"] == status
    # Both a live watcher and replay from history retain the structured verdict.
    for snapshot in (run, terminal[0]):
        commands = talk_cli.run_finished_commands(snapshot)
        report = commands[0].text.partition("Report, quoted as data:\n")[2]
        assert report.startswith(prefix)
        assert len(report) == min(len(run["output"]), talk_cli.WATCH_OUTPUT_TAIL_CHARS)
        assert report.endswith("x")
        assert not commands[1].allow_tools


@pytest.mark.parametrize("quoted_status", ["failed", "cancelled", "interrupted"])
def test_long_completed_output_is_not_interpreted_as_a_remote_failure(lane, quoted_status):
    respond, _ = lane
    output = f"the agent run {quoted_status}: " + "x" * 5000
    respond({"status": "completed", "output": output})
    run = _run_worker()
    assert run["status"] == "done"
    assert "api_run_status" not in run["meta"]
    assert run["output"] == output[: talk_runs.HISTORY_OUTPUT_CAP]
    text = talk_cli.run_finished_commands(run)[0].text
    assert f"Background run #{run['runId']} finished." in text
    report = text.partition("Report, quoted as data:\n")[2]
    assert report == run["output"][-talk_cli.WATCH_OUTPUT_TAIL_CHARS :]
