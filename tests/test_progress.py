"""Bounded progress phases for background work (hermes-talk#33).

What is being proved: host events map to the bounded phase vocabulary and
NOWHERE else; the only job-specific detail that can leave the module is a
safe tool label; projections key on exact correlators (never recency), so
concurrent jobs cannot cross-route; terminal phases are durable receipts
that never flip registry authority; the watcher speaks on phase CHANGE only
plus bounded heartbeats; and replaying history after a restart still reads
the phase off meta.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import fixture_data
import pytest

import talk_apiserver
import talk_cli
import talk_host
import talk_lifecycle
import talk_progress
import talk_runs


@pytest.fixture(autouse=True)
def _clean():
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    talk_lifecycle.reset_for_tests()
    talk_progress.reset_for_tests()
    # Runs are refused without a bound return route (hermes-talk#35), so the
    # suite attaches one. Tests that assert the owner gate detach explicitly.
    talk_runs.attach_owner(
        talk_session_id="ts-test",
        generation_id="gen-test",
        hermes_session_id="sess-test",
        operator="test",
        profile=None,
    )
    yield
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    talk_lifecycle.reset_for_tests()
    talk_progress.reset_for_tests()


def _start_live_run(label: str = "audit the auth module") -> tuple[int, threading.Event]:
    """A run whose worker blocks until released, so phases land on a LIVE run."""

    release = threading.Event()

    def worker(_run_id: int) -> str:
        release.wait(timeout=5.0)
        return "done"

    run_id = talk_runs.start_run("agent", label, worker)
    return run_id, release


def _wait_terminal(run_id: int, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = talk_runs.get_run(run_id)
        if run and run["status"] in talk_runs.TERMINAL_STATUSES:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")


class _StubLoop:
    """Records the marshal and runs the callback inline, as the loop would."""

    def __init__(self) -> None:
        self.marshalled = 0

    def call_soon_threadsafe(self, callback, *args):
        self.marshalled += 1
        callback(*args)


class _Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _run_snapshot(run_id: int, phase: str | None = None, detail: str = "") -> dict:
    meta = {}
    if phase is not None:
        meta = {"phase": phase, "phase_detail": detail}
    return {"runId": run_id, "status": "running", "label": "audit", "meta": meta}


def _spoken_text(commands) -> str:
    return " ".join(getattr(command, "text", "") for command in commands)


# -- event -> phase mapping ------------------------------------------------------


def test_api_event_mapping_is_exactly_the_bounded_set():
    assert talk_progress.phase_for_api_event("run.queued") == "accepted"
    assert talk_progress.phase_for_api_event("run.started") == "accepted"
    assert talk_progress.phase_for_api_event("tool.started") == "executing"
    assert talk_progress.phase_for_api_event("approval.request") == "blocked"
    assert talk_progress.phase_for_api_event("run.completed") == "complete"
    assert talk_progress.phase_for_api_event("run.failed") == "failed"
    assert talk_progress.phase_for_api_event("run.cancelled") == "stopped"


def test_unmapped_and_dropped_events_are_not_phases():
    """Unknown events leave the last evidenced phase alone — no guessing."""
    for event in (
        None,
        "",
        "approval.responded",
        "run.steered",
        "run.stopping",
        "message.delta",
        "tool.completed",
        "inspecting",  # no host evidence exists for this phase
        "verifying",
        "RUN.COMPLETED",  # case-sensitive: the host's strings, not near-misses
    ):
        assert talk_progress.phase_for_api_event(event) is None


# -- redaction --------------------------------------------------------------------


def test_tool_labels_come_only_from_the_table():
    assert talk_progress.tool_label("read_file") == "Reading files"
    assert talk_progress.tool_label("terminal") == "Running commands"
    # The browser family is prefix-mapped: one label for every browser_* verb.
    assert talk_progress.tool_label("browser_navigate") == "Browsing"
    # Unknown tools degrade to the generic label — the name itself never
    # surfaces, because a name can carry a path or a URL.
    assert talk_progress.tool_label("mcp_evil_read_secrets") == "Working"
    assert talk_progress.tool_label("/home/operator/.ssh/id_rsa") == "Working"
    assert talk_progress.tool_label(None) == "Working"
    assert talk_progress.tool_label(42) == "Working"


def test_hook_args_and_output_never_reach_the_phase_event():
    """Redaction is positional: there is no field for args to ride in on."""

    events: list[dict] = []
    talk_progress.attach_session(_StubLoop(), events.append, "parent-sess")
    talk_lifecycle.on_subagent_start(
        parent_session_id="parent-sess",
        child_session_id="cs-1",
        child_subagent_id="sa-0-aaaa",
        child_role="researcher",
        child_goal="audit the auth module",
    )
    talk_progress.on_post_tool_call(
        session_id="cs-1",
        tool_name="terminal",
        args={"command": fixture_data.payload("adversarial/redact-aws-curl.fixture")},
        result="AKIA-SECRET-OUTPUT",
    )

    phase_events = [e for e in events if e["kind"] == "subagent_phase"]
    assert phase_events[-1]["phase"] == "executing"
    assert phase_events[-1]["detail"] == "Running commands"
    blob = json.dumps(events)
    for leaked in ("credentials", "evil.example", "AKIA-SECRET-OUTPUT"):
        assert leaked not in blob


def test_approval_waits_are_visible_without_the_command():
    events: list[dict] = []
    talk_progress.attach_session(_StubLoop(), events.append, "parent-sess")
    talk_lifecycle.on_subagent_start(
        parent_session_id="parent-sess",
        child_session_id="cs-1",
        child_subagent_id="sa-0-aaaa",
        child_role="researcher",
        child_goal="audit",
    )
    talk_progress.on_pre_approval_request(
        session_id="cs-1",
        command=fixture_data.payload("adversarial/redact-rm-rf.fixture"),
        description="delete everything",
    )

    phase_events = [e for e in events if e["kind"] == "subagent_phase"]
    assert phase_events[-1]["phase"] == "blocked"
    blob = json.dumps(phase_events)
    assert "rm -rf" not in blob
    assert "delete everything" not in blob


def test_the_spoken_milestone_is_contained_and_carries_no_routing_metadata():
    """Same containment as the result announcement, minus the quoted report."""

    run = _run_snapshot(7, "executing", "Reading files")
    run["ticket"] = {
        "talkSessionId": "ts-secret",
        "generationId": "gen-secret",
        "hermesSessionId": "sess-secret",
        "operator": "codex-oauth",
        "profile": "research",
        "requestId": "req-secret",
    }

    create, respond, delete = talk_cli.run_phase_messages(run, "executing")
    assert create["item"]["role"] == "system"
    assert respond == {"type": "response.create", "response": {"tool_choice": "none"}}
    assert delete == {
        "type": "conversation.item.delete",
        "item_id": create["item"]["id"],
    }
    text = create["item"]["content"][0]["text"]
    assert "Background run #7" in text
    assert "Reading files" in text
    for leaked in ("ts-secret", "gen-secret", "sess-secret", "req-secret", "codex-oauth"):
        assert leaked not in text


def test_heartbeat_and_blocked_and_accepted_speech():
    run = _run_snapshot(9)
    texts = {
        kind: _spoken_text(talk_cli.run_phase_commands(run, kind))
        for kind in ("heartbeat", "accepted", "blocked")
    }
    assert "still working" in texts["heartbeat"]
    assert "was accepted" in texts["accepted"]
    assert "waiting on an approval" in texts["blocked"]
    assert "#9" in texts["heartbeat"]


def test_terminal_phases_build_no_milestone_speech():
    """The outcome sentence belongs to the terminal announcement, not a phase."""
    for phase in ("complete", "failed", "stopped"):
        assert talk_cli.run_phase_commands(_run_snapshot(7, phase), phase) == []
        assert (
            talk_cli.subagent_phase_commands({"subagent_id": "sa-0-x", "phase": phase}) == []
        )


# -- run projection --------------------------------------------------------------


def test_poll_projection_annotates_on_change_only(monkeypatch):
    calls: list[dict] = []
    real_annotate = talk_runs.annotate_run
    monkeypatch.setattr(
        talk_runs,
        "annotate_run",
        lambda *args, **kwargs: real_annotate(*args, **kwargs) or calls.append(kwargs),
    )
    run_id, release = _start_live_run()
    try:
        assert talk_progress.project_api_poll(run_id, {"last_event": "run.started"}) is None
        assert talk_runs.get_run(run_id)["meta"]["phase"] == "accepted"
        assert len(calls) == 1

        # Same event again: no rewrite, no meta churn.
        talk_progress.project_api_poll(run_id, {"last_event": "run.started"})
        assert len(calls) == 1

        talk_progress.project_api_poll(run_id, {"last_event": "tool.started"})
        assert talk_runs.get_run(run_id)["meta"]["phase"] == "executing"
        assert len(calls) == 2
        # An unmappable event is a dropped event: the phase holds.
        talk_progress.project_api_poll(run_id, {"last_event": "run.steered"})
        assert talk_runs.get_run(run_id)["meta"]["phase"] == "executing"
        assert len(calls) == 2
    finally:
        release.set()
    _wait_terminal(run_id)


def test_the_projection_never_marks_a_run_finished():
    """Telemetry is not authority: meta says complete, the registry still runs."""

    run_id, release = _start_live_run()
    try:
        talk_progress.project_api_poll(
            run_id, {"last_event": "run.completed", "session_id": "sess-remote-1"}
        )
        run = talk_runs.get_run(run_id)
        assert run["meta"]["phase"] == "complete"
        # ...but nothing about the RUN's authority moved: not terminal, and
        # the result is still unclaimable because there is no result yet.
        assert run["status"] == "running"
        assert run["delivery"] == talk_runs.DELIVERY_PENDING
        assert talk_runs.claim_delivery(run_id, claimant="ts-test") is False
    finally:
        release.set()
    terminal = _wait_terminal(run_id)
    # Only the worker's own finish flipped the status — and NOW it is claimable.
    assert terminal["status"] == "done"
    assert terminal["meta"]["phase"] == "complete"
    assert talk_runs.claim_delivery(run_id, claimant="ts-test") is True


def test_a_late_event_cannot_rephase_a_terminal_run_or_a_terminal_phase():
    run_id, release = _start_live_run()
    talk_progress.project_api_poll(run_id, {"last_event": "run.completed"})
    # A straggler tool event in the annotate->finish gap must not reopen the
    # receipt the terminal artifact just wrote.
    assert talk_progress.set_run_phase(run_id, "executing", detail="Working") is False
    release.set()
    _wait_terminal(run_id)
    assert talk_progress.project_api_poll(run_id, {"last_event": "tool.started"}) is None
    run = talk_runs.get_run(run_id)
    assert run["status"] == "done"
    assert run["meta"]["phase"] == "complete"


def test_projection_keys_on_correlators_never_recency():
    """Two concurrent runs: an event addressed to one never lands on the other."""

    run_a, release_a = _start_live_run("job A")
    run_b, release_b = _start_live_run("job B")
    try:
        talk_progress.project_api_poll(run_a, {"session_id": "sess-A"})
        talk_progress.project_api_poll(run_b, {"session_id": "sess-B"})

        # The most recent projection was B's; the hook event is A's. Recency
        # would route it to B — the correlator routes it to A.
        talk_progress.on_post_tool_call(session_id="sess-A", tool_name="read_file")

        assert talk_runs.get_run(run_a)["meta"]["phase"] == "executing"
        assert talk_runs.get_run(run_a)["meta"]["phase_detail"] == "Reading files"
        assert "phase" not in talk_runs.get_run(run_b)["meta"]

        # An event naming neither run goes nowhere.
        talk_progress.on_pre_approval_request(session_id="sess-nobody")
        assert "phase" not in talk_runs.get_run(run_b)["meta"]
    finally:
        release_a.set()
        release_b.set()
    _wait_terminal(run_a)
    _wait_terminal(run_b)


def test_the_poll_does_not_erase_a_finer_hook_supplied_detail():
    run_id, release = _start_live_run()
    try:
        talk_progress.project_api_poll(run_id, {"session_id": "sess-A"})
        talk_progress.on_post_tool_call(session_id="sess-A", tool_name="web_search")
        assert talk_runs.get_run(run_id)["meta"]["phase_detail"] == "Searching the web"
        # The tier-2 poll carries no tool name; its re-projection of the SAME
        # phase must not blank the label the hook supplied.
        talk_progress.project_api_poll(run_id, {"last_event": "tool.started"})
        meta = talk_runs.get_run(run_id)["meta"]
        assert meta["phase"] == "executing"
        assert meta["phase_detail"] == "Searching the web"
        # A phase CHANGE does clear the stale label.
        talk_progress.project_api_poll(run_id, {"last_event": "approval.request"})
        meta = talk_runs.get_run(run_id)["meta"]
        assert meta["phase"] == "blocked"
        assert meta["phase_detail"] == ""
    finally:
        release.set()
    _wait_terminal(run_id)


def test_the_api_worker_projects_onto_its_own_run(monkeypatch):
    """The production wiring, end to end: worker -> on_event -> run meta."""

    payloads = [
        {"status": "running", "last_event": "run.started", "session_id": "sess-r1"},
        {"status": "running", "last_event": "tool.started"},
        {"status": "completed", "last_event": "run.completed", "output": "done"},
    ]

    def fake_run_to_completion(
        _task, *, session_id=None, session_key=None, on_start=None, on_event=None
    ):
        if on_start is not None:
            on_start("run_remote_1")
        for payload in payloads:
            on_event(payload)
        return "audit complete"

    monkeypatch.setattr(talk_apiserver, "run_to_completion", fake_run_to_completion)

    run_id = talk_runs.start_run(
        "agent", "audit", talk_host._api_server_worker("audit", session_id=None)
    )
    run = _wait_terminal(run_id)

    assert run["output"] == "audit complete"
    assert run["meta"]["api_run_id"] == "run_remote_1"
    assert run["meta"]["phase"] == "complete"
    # The recorded correlator self-cleared with the terminal transition.
    talk_progress.on_post_tool_call(session_id="sess-r1", tool_name="read_file")
    assert talk_runs.get_run(run_id)["meta"]["phase"] == "complete"


def test_run_to_completion_taps_every_poll_including_the_terminal_one(monkeypatch):
    payloads = iter(
        [
            {"status": "running", "last_event": "run.started", "session_id": "s-1"},
            {"status": "running", "last_event": "tool.started"},
            {"status": "completed", "last_event": "run.completed", "output": "the answer"},
        ]
    )
    seen: list[dict] = []
    monkeypatch.setattr(talk_apiserver, "start_run", lambda *a, **k: "run_abc")
    monkeypatch.setattr(talk_apiserver, "get_run", lambda _rid: next(payloads))
    monkeypatch.setenv("TALK_API_SERVER_POLL_S", "0.01")

    out = talk_apiserver.run_to_completion("go", on_event=seen.append)

    assert out == "the answer"
    assert [p.get("last_event") for p in seen] == [
        "run.started",
        "tool.started",
        "run.completed",
    ]


def test_an_exploding_progress_tap_never_breaks_the_run(monkeypatch):
    monkeypatch.setattr(talk_apiserver, "start_run", lambda *a, **k: "run_abc")
    monkeypatch.setattr(
        talk_apiserver,
        "get_run",
        lambda _rid: {"status": "completed", "output": "fine"},
    )
    monkeypatch.setenv("TALK_API_SERVER_POLL_S", "0.01")

    def boom(_payload):
        raise RuntimeError("progress is on fire")

    assert talk_apiserver.run_to_completion("go", on_event=boom) == "fine"


# -- attached-lane children (tier 1) ----------------------------------------------

PARENT = "parent-sess"


def _attach(events: list[dict]) -> None:
    talk_progress.attach_session(_StubLoop(), events.append, PARENT)


def _child_start(csid: str = "cs-1", sid: str = "sa-0-aaaa", **overrides) -> None:
    kwargs = {
        "parent_session_id": PARENT,
        "child_session_id": csid,
        "child_subagent_id": sid,
        "child_role": "researcher",
        "child_goal": "audit",
        "parent_subagent_id": None,
    }
    kwargs.update(overrides)
    # The registered hook entry point — the delegation to talk_progress is
    # what is under test, not a private shortcut.
    talk_lifecycle.on_subagent_start(**kwargs)


def test_attached_child_progress_flows_through_the_registered_hook():
    events: list[dict] = []
    _attach(events)

    _child_start()
    talk_progress.on_post_tool_call(session_id="cs-1", tool_name="web_search")
    # Same phase, finer detail: no second speech.
    talk_progress.on_post_tool_call(session_id="cs-1", tool_name="read_file")
    talk_progress.on_pre_approval_request(session_id="cs-1")

    phases = [e["phase"] for e in events if e["kind"] == "subagent_phase"]
    assert phases == ["accepted", "executing", "blocked"]
    executing = next(e for e in events if e["phase"] == "executing")
    assert executing["subagent_id"] == "sa-0-aaaa"
    assert executing["detail"] == "Searching the web"

    # The stop pops the subject; a straggler tool event goes nowhere.
    talk_lifecycle.on_subagent_stop(
        child_session_id="cs-1", child_status="ok", child_summary="done"
    )
    talk_progress.on_post_tool_call(session_id="cs-1", tool_name="read_file")
    phases = [e["phase"] for e in events if e["kind"] == "subagent_phase"]
    assert phases == ["accepted", "executing", "blocked"]


def test_a_foreign_parents_child_is_never_tracked():
    events: list[dict] = []
    _attach(events)

    _child_start(parent_session_id="someone-elses-session")
    talk_progress.on_post_tool_call(session_id="cs-1", tool_name="read_file")

    assert events == []


def test_no_attached_session_means_no_progress_at_all():
    _child_start()
    talk_progress.on_post_tool_call(session_id="cs-1", tool_name="read_file")
    # Nothing tracked, nothing raised, nothing spoken.
    assert talk_progress._CHILDREN == {}


def test_nested_grandchildren_are_tracked_but_not_spoken():
    events: list[dict] = []
    _attach(events)

    _child_start(csid="cs-parent", sid="sa-0-aaaa")
    _child_start(csid="cs-nested", sid="sa-1-bbbb", parent_subagent_id="sa-0-aaaa")
    talk_progress.on_post_tool_call(session_id="cs-nested", tool_name="read_file")

    phases = [e["phase"] for e in events if e["kind"] == "subagent_phase"]
    assert phases == ["accepted"]  # the parent's; the grandchild stays silent


def test_the_notify_marshal_is_fail_open():
    class ClosedLoop:
        def call_soon_threadsafe(self, callback, *args):
            raise RuntimeError("loop is closed")

    talk_progress.attach_session(ClosedLoop(), lambda event: None, PARENT)
    _child_start()  # must not raise


def test_hooks_never_raise_into_the_host():
    talk_progress.on_post_tool_call(session_id=object())  # unstringable-ish
    talk_progress.on_pre_approval_request(session_id=None)
    talk_progress.on_post_tool_call()  # nothing at all


# -- the watcher's speech state ----------------------------------------------------


def test_the_watch_speaks_on_phase_change_only():
    clock = _Clock()
    watch = talk_progress.RunProgressWatch(now=clock)

    assert watch.poll(_run_snapshot(1, "accepted")) == "accepted"
    assert watch.poll(_run_snapshot(1, "accepted")) is None
    assert watch.poll(_run_snapshot(1, "executing", "Reading files")) == "executing"
    assert watch.poll(_run_snapshot(1, "executing", "Running tests")) is None
    assert watch.poll(_run_snapshot(1, "blocked")) == "blocked"


def test_the_watch_never_speaks_a_terminal_phase():
    clock = _Clock()
    watch = talk_progress.RunProgressWatch(now=clock)

    # The annotate->finish gap: meta already says complete, the registry not
    # yet. The watch records the phase but leaves the sentence to the
    # terminal branch.
    assert watch.poll(_run_snapshot(1, "complete")) is None
    assert watch.poll(_run_snapshot(1, "complete")) is None


def test_the_watch_ignores_a_phase_it_cannot_vouch_for():
    clock = _Clock()
    watch = talk_progress.RunProgressWatch(now=clock)

    assert watch.poll(_run_snapshot(1, "inspecting")) is None
    assert watch.poll(_run_snapshot(1, "garbage")) is None
    # ...and a real phase afterwards still announces.
    assert watch.poll(_run_snapshot(1, "executing")) == "executing"


def test_heartbeats_are_bounded_to_one_per_interval():
    clock = _Clock()
    watch = talk_progress.RunProgressWatch(heartbeat_s=60.0, now=clock)

    assert watch.poll(_run_snapshot(1)) is None
    clock.advance(59.9)
    assert watch.poll(_run_snapshot(1)) is None
    clock.advance(0.1)
    assert watch.poll(_run_snapshot(1)) == "heartbeat"
    clock.advance(30)
    assert watch.poll(_run_snapshot(1)) is None
    clock.advance(30)
    assert watch.poll(_run_snapshot(1)) == "heartbeat"


def test_milestone_speech_resets_the_heartbeat_clock():
    clock = _Clock()
    watch = talk_progress.RunProgressWatch(heartbeat_s=60.0, now=clock)

    clock.advance(59)
    assert watch.poll(_run_snapshot(1, "executing")) == "executing"
    # The milestone just spoke; a heartbeat one second later would be noise.
    clock.advance(59)
    assert watch.poll(_run_snapshot(1, "executing")) is None
    clock.advance(1)
    assert watch.poll(_run_snapshot(1, "executing")) == "heartbeat"


def test_interleaved_jobs_keep_their_own_milestone_order():
    """Two watchers, interleaved polls: per-run order is each run's own."""

    clock = _Clock()
    watch_a = talk_progress.RunProgressWatch(now=clock)
    watch_b = talk_progress.RunProgressWatch(now=clock)
    feed_a = ["accepted", "executing", "blocked"]
    feed_b = ["accepted", "blocked"]
    heard_a: list[str] = []
    heard_b: list[str] = []

    for step in range(3):
        if step < len(feed_a):
            milestone = watch_a.poll(_run_snapshot(1, feed_a[step]))
            if milestone:
                heard_a.append(milestone)
        if step < len(feed_b):
            milestone = watch_b.poll(_run_snapshot(2, feed_b[step]))
            if milestone:
                heard_b.append(milestone)

    assert heard_a == feed_a
    assert heard_b == feed_b


# -- durability and replay ----------------------------------------------------------


@pytest.fixture
def history_env(tmp_path: Path, monkeypatch) -> Path:
    """Opt in to the tee (inert under pytest by default) on a tmp state dir."""

    state = tmp_path / "state"
    monkeypatch.setattr(talk_runs, "_history_path", lambda: state / "talk-runs.jsonl")
    monkeypatch.setattr(talk_runs, "_history_enabled", lambda: True)
    state.mkdir(parents=True, exist_ok=True)
    return state


def _history_records(state: Path) -> list[dict]:
    file = state / "talk-runs.jsonl"
    if not file.exists():
        return []
    return [json.loads(line) for line in file.read_text().splitlines() if line.strip()]


def test_only_the_terminal_phase_is_durable(history_env):
    state = history_env
    run_id, release = _start_live_run()
    try:
        talk_progress.project_api_poll(run_id, {"last_event": "run.started"})
        talk_progress.project_api_poll(run_id, {"last_event": "tool.started"})
        # Acceptance record only: in-flight phases are speech state, not
        # durable claims.
        assert len(_history_records(state)) == 1

        talk_progress.project_api_poll(run_id, {"last_event": "run.completed"})
        records = _history_records(state)
        assert len(records) == 2
        assert records[-1]["meta"]["phase"] == "complete"
        assert records[-1]["status"] == "running"  # a receipt, not an outcome
    finally:
        release.set()
    _wait_terminal(run_id)


def test_the_phase_replays_off_history_after_a_restart(history_env):
    """A process that dies mid-run leaves the phase readable off the record."""

    run_id, release = _start_live_run()
    talk_progress.project_api_poll(run_id, {"last_event": "run.started"})
    talk_progress.project_api_poll(run_id, {"last_event": "run.completed"})
    release.set()
    _wait_terminal(run_id)

    # Simulate the restart: the registry is process memory and dies with it.
    with talk_runs._RUN_LOCK:
        talk_runs._RUNS.clear()

    listed = talk_runs.list_runs(limit=10, include_history=True)
    mine = [run for run in listed if run["runId"] == run_id]
    assert mine
    assert mine[0]["fromHistory"] is True
    assert mine[0]["status"] == "done"
    assert mine[0]["meta"]["phase"] == "complete"


# -- eviction caps + the same-phase detail-only run path (hermes-talk#60) ---------


def test_run_session_eviction_drops_the_oldest_not_the_newest():
    """FIFO, not LIFO: at the cap, the FIRST id recorded is the one that goes.

    Popping the wrong end would evict the id that just arrived — so the run
    actually executing right now would stop being findable by its session id
    and go silent under load, which is the precise failure the cap exists to
    prevent.
    """

    cap = talk_progress._MAX_RUN_SESSIONS
    for index in range(cap):
        talk_progress.note_run_session(index, f"sess-{index:04d}")
    assert len(talk_progress._RUN_BY_SESSION) == cap

    talk_progress.note_run_session(cap, "sess-newest")

    assert len(talk_progress._RUN_BY_SESSION) == cap
    assert "sess-0000" not in talk_progress._RUN_BY_SESSION  # oldest, evicted
    assert talk_progress._RUN_BY_SESSION["sess-newest"] == cap  # newest, kept
    assert talk_progress._RUN_BY_SESSION["sess-0001"] == 1  # runner-up, kept


def test_re_noting_a_session_does_not_refresh_its_eviction_position():
    """The cap is insertion-ordered, not least-recently-used.

    Re-noting an id already in the index rebinds its run in place; it must not
    move the entry to the back of the queue, or a chatty session could hold a
    slot forever while quieter live runs are evicted around it.
    """

    cap = talk_progress._MAX_RUN_SESSIONS
    for index in range(cap):
        talk_progress.note_run_session(index, f"sess-{index:04d}")

    talk_progress.note_run_session(999, "sess-0000")
    assert talk_progress._RUN_BY_SESSION["sess-0000"] == 999  # rebound

    talk_progress.note_run_session(cap, "sess-newest")
    assert "sess-0000" not in talk_progress._RUN_BY_SESSION  # still first out


def test_child_eviction_drops_the_oldest_not_the_newest():
    """Same FIFO property for the attached-child index, and its consequence.

    An evicted child stops being a progress subject, so its later tool call
    finds no entry and speaks nothing — which is why evicting the NEWEST would
    silence the child that just started.
    """

    events: list[dict] = []
    _attach(events)
    cap = talk_progress._MAX_CHILDREN
    for index in range(cap):
        _child_start(csid=f"cs-{index:04d}", sid=f"sa-0-{index:04d}")
    assert len(talk_progress._CHILDREN) == cap

    _child_start(csid="cs-newest", sid="sa-0-newest")

    assert len(talk_progress._CHILDREN) == cap
    assert "cs-0000" not in talk_progress._CHILDREN
    assert "cs-newest" in talk_progress._CHILDREN

    spoken = len(events)
    talk_progress.on_post_tool_call(session_id="cs-0000", tool_name="read_file")
    assert len(events) == spoken  # the evicted child has no subject to phase
    talk_progress.on_post_tool_call(session_id="cs-newest", tool_name="read_file")
    assert len(events) == spoken + 1  # the newest one still speaks


def test_the_same_phase_detail_only_update_rides_the_run_path(monkeypatch):
    """The run path's detail-only branch, exercised directly on a run.

    The child path already covers "finer detail, same phase, no second
    speech"; this is the same branch in ``set_run_phase``. The evidence is the
    written FIELD SET, not the resulting meta: within a phase only
    ``phase_detail`` may be rewritten, so ``phase_at`` keeps marking when the
    PHASE changed instead of being pushed forward by every tool label.
    Asserting on ``phase_at``'s value instead would prove nothing here - two
    ``time.time()`` calls inside one phase can return the same float.
    """

    calls: list[dict] = []
    real_annotate = talk_runs.annotate_run
    monkeypatch.setattr(
        talk_runs,
        "annotate_run",
        lambda *args, **kwargs: real_annotate(*args, **kwargs) or calls.append(kwargs),
    )
    run_id, release = _start_live_run()
    try:
        assert talk_progress.set_run_phase(run_id, "executing", detail="Reading files")
        meta = talk_runs.get_run(run_id)["meta"]
        assert (meta["phase"], meta["phase_detail"]) == ("executing", "Reading files")
        assert set(calls[0]) == {"durable", "phase", "phase_at", "phase_detail"}

        # Same phase, different detail: the label is replaced, and it is the
        # ONLY field the write carries.
        assert talk_progress.set_run_phase(run_id, "executing", detail="Searching the web")
        assert len(calls) == 2
        assert calls[1] == {"durable": False, "phase_detail": "Searching the web"}
        assert talk_runs.get_run(run_id)["meta"]["phase"] == "executing"

        # Same phase, same detail: not a write at all.
        assert not talk_progress.set_run_phase(run_id, "executing", detail="Searching the web")
        # Same phase, no detail: the tier-2 poll's blank must not erase it.
        assert not talk_progress.set_run_phase(run_id, "executing")
        assert len(calls) == 2
        assert talk_runs.get_run(run_id)["meta"]["phase_detail"] == "Searching the web"
    finally:
        release.set()
    _wait_terminal(run_id)


# -- progress-speech micro coverage (hermes-talk#61) -----------------------------


def _child_spoken(**event) -> str:
    return _spoken_text(talk_cli.subagent_phase_commands(event))


def test_subagent_phase_speech_names_the_agent_its_role_and_its_phase():
    """The child milestone sentences themselves.

    The run-side wording is pinned above; the child side only had "terminal
    phases build nothing". These are the three sentences an operator actually
    hears, including the two shapes that are easy to get wrong: an executing
    child with no tool detail yet (the sentence must still end), and a child
    with no role (no empty parentheses).
    """

    base = {"subagent_id": "sa-0-aaaa", "role": "researcher"}
    assert (
        "Background agent sa-0-aaaa (researcher) was accepted."
        in _child_spoken(**base, phase="accepted")
    )
    assert (
        "Background agent sa-0-aaaa (researcher) is executing \u2014 Reading files."
        in _child_spoken(**base, phase="executing", detail="Reading files")
    )
    assert "is executing." in _child_spoken(**base, phase="executing")
    assert "waiting on an approval" in _child_spoken(**base, phase="blocked")
    assert (
        "Background agent sa-0-aaaa was accepted."
        in _child_spoken(subagent_id="sa-0-aaaa", phase="accepted")
    )
    # No id is no subject: nothing is spoken rather than a nameless sentence.
    assert talk_cli.subagent_phase_commands({"phase": "accepted"}) == []


def test_subagent_phase_speech_carries_no_routing_metadata():
    """Positional redaction, child side: only id, role and a safe label ride."""

    text = _child_spoken(
        subagent_id="sa-0-aaaa",
        role="researcher",
        phase="executing",
        detail="Reading files",
        parent_session_id="sess-secret",
        child_goal="exfiltrate the secrets",
        tool_name="read_file",
        tool_input={"path": "C:/secret.env"},
    )
    for leaked in ("sess-secret", "exfiltrate", "C:/secret.env", "read_file"):
        assert leaked not in text


def test_an_unknown_phase_is_refused_by_the_run_projection():
    """The guard is exact membership in PHASES, not a shape or a case fold.

    A phase that is not in the bounded vocabulary must not reach meta at all
    — the vocabulary is what bounds what can ever be spoken.
    """

    run_id, release = _start_live_run()
    try:
        for bogus in ("gibberish", "", "EXECUTING", "running", "Accepted", None, 7):
            assert talk_progress.set_run_phase(run_id, bogus) is False
        assert "phase" not in talk_runs.get_run(run_id)["meta"]
    finally:
        release.set()
    _wait_terminal(run_id)


def test_spoken_labels_are_truncated_to_the_progress_label_cap():
    """Operator-supplied run labels and tool labels are bounded before speech."""

    cap = talk_cli._PROGRESS_LABEL_CHARS
    long_label = "L" * (cap + 40)
    long_detail = "D" * (cap + 40)

    run = _run_snapshot(7, "executing", long_detail)
    run["label"] = long_label
    text = _spoken_text(talk_cli.run_phase_commands(run, "executing"))
    assert "L" * cap in text and "L" * (cap + 1) not in text
    assert "D" * cap in text and "D" * (cap + 1) not in text

    # The heartbeat carries the label too, on the same cap.
    beat = _spoken_text(talk_cli.run_phase_commands(run, "heartbeat"))
    assert "L" * cap in beat and "L" * (cap + 1) not in beat

    child = _child_spoken(subagent_id="sa-0-aaaa", phase="executing", detail=long_detail)
    assert "D" * cap in child and "D" * (cap + 1) not in child


def test_concurrent_producers_never_corrupt_the_progress_indexes():
    """Both indexes are written from the poll loop AND the host's hook threads.

    The caps evict while other threads insert, so this drives every producer
    at once past both caps and asserts the invariants that a torn write would
    break: bounded size, whole entries, and a phase still inside the bounded
    vocabulary.
    """

    events: list[dict] = []
    _attach(events)
    workers = 6
    errors: list[Exception] = []
    barrier = threading.Barrier(workers)

    def hammer(worker: int) -> None:
        try:
            barrier.wait(timeout=10.0)
            for index in range(60):
                key = f"{worker}-{index}"
                talk_progress.note_run_session(worker * 1000 + index, f"sess-{key}")
                _child_start(csid=f"cs-{key}", sid=f"sa-{worker}-{index}")
                talk_progress.on_post_tool_call(session_id=f"cs-{key}", tool_name="read_file")
                talk_progress.on_pre_approval_request(session_id=f"cs-{key}")
                if index % 3 == 0:
                    talk_lifecycle.on_subagent_stop(
                        child_session_id=f"cs-{key}", child_status="ok", child_summary="done"
                    )
        except Exception as exc:  # noqa: BLE001 - the thread's failure IS the assertion
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(worker,)) for worker in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)

    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert len(talk_progress._RUN_BY_SESSION) <= talk_progress._MAX_RUN_SESSIONS
    assert len(talk_progress._CHILDREN) <= talk_progress._MAX_CHILDREN
    for entry in list(talk_progress._CHILDREN.values()):
        assert set(entry) == {
            "subagent_id",
            "parent_session_id",
            "role",
            "top_level",
            "phase",
            "detail",
        }
        assert entry["phase"] in talk_progress.PHASES
    for phase in [event.get("phase") for event in list(events)]:
        assert phase in talk_progress.PHASES
