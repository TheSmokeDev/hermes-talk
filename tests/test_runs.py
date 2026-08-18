"""Run registry — lifecycle, eviction, and the durable history tail.

Ported from the proven Talk Mode suite. Workers here are plain callables —
no subprocesses, no network. The history tee is inert under pytest unless a
test opts in, so a suite that touches the registry transitively can never
write the operator's real Hermes home.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import talk_runs


@pytest.fixture(autouse=True)
def _clean_registry():
    talk_runs.reset_for_tests()
    # Runs are refused without a bound return route (hermes-talk#35), so the
    # suite attaches one. Tests that assert the REFUSAL detach it explicitly.
    _attach_test_owner()
    yield
    talk_runs.reset_for_tests()


def _attach_test_owner(**overrides) -> None:
    """Rebind the default test ticket owner.

    ``reset_for_tests`` detaches, so a test that resets mid-body has to
    rebind before it can dispatch again — the same fail-closed rule the
    product enforces on a real reconnect.
    """

    talk_runs.attach_owner(
        **{
            "talk_session_id": "ts-test",
            "generation_id": "gen-test",
            "hermes_session_id": "sess-test",
            "operator": "test",
            "profile": None,
            **overrides,
        }
    )


def _wait_terminal(run_id: int, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = talk_runs.get_run(run_id)
        if run and run["status"] in talk_runs.TERMINAL_STATUSES:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never reached a terminal status")


# --- lifecycle ---------------------------------------------------------------


@pytest.mark.parametrize("kind", talk_runs.RUN_KINDS)
def test_each_kind_runs_to_done(kind: str):
    run_id = talk_runs.start_run(kind, "label", lambda _rid: f"{kind} finished")

    run = _wait_terminal(run_id)
    assert run["status"] == "done"
    assert run["output"] == f"{kind} finished"
    assert run["kind"] == kind


def test_worker_exception_marks_failed_with_speakable_text():
    def boom(_run_id: int) -> str:
        raise RuntimeError("the lane died")

    run = _wait_terminal(talk_runs.start_run("agent", "doomed", boom))

    assert run["status"] == "failed"
    assert "RuntimeError: the lane died" in run["output"]


def test_unknown_kind_is_rejected_before_a_thread_starts():
    with pytest.raises(ValueError, match="unknown run kind"):
        talk_runs.start_run("telepathy", "nope", lambda _rid: "never")


def test_unknown_run_id_reads_none():
    assert talk_runs.get_run(4242) is None


def test_worker_can_annotate_while_running():
    release = threading.Event()

    def worker(run_id: int) -> str:
        talk_runs.annotate_run(run_id, pid=4242)
        release.wait(timeout=2)
        return "done"

    run_id = talk_runs.start_run("agent", "slow", worker)
    for _ in range(100):
        if (talk_runs.get_run(run_id)["meta"] or {}).get("pid"):
            break
        time.sleep(0.02)

    assert talk_runs.get_run(run_id)["meta"]["pid"] == 4242
    assert talk_runs.get_run(run_id)["status"] == "running"
    release.set()
    _wait_terminal(run_id)


def test_annotate_unknown_run_is_a_noop():
    talk_runs.annotate_run(999, pid=1)  # must not raise


def test_finish_run_is_idempotent_and_reports_the_transition():
    run_id = talk_runs.start_run("skill", "s", lambda _rid: "first")
    _wait_terminal(run_id)

    assert talk_runs.finish_run(run_id, "failed", "second") is False
    assert talk_runs.get_run(run_id)["output"] == "first"


def test_finish_run_rejects_a_non_terminal_status():
    with pytest.raises(ValueError, match="not a terminal status"):
        talk_runs.finish_run(1, "running", "x")


def test_get_run_returns_a_snapshot_not_a_live_handle():
    run_id = talk_runs.start_run("skill", "s", lambda _rid: "ok")
    _wait_terminal(run_id)

    snapshot = talk_runs.get_run(run_id)
    snapshot["status"] = "mutated"
    snapshot["meta"]["injected"] = True

    assert talk_runs.get_run(run_id)["status"] == "done"
    assert "injected" not in talk_runs.get_run(run_id)["meta"]


def test_list_runs_is_newest_first_and_capped():
    for index in range(5):
        _wait_terminal(talk_runs.start_run("skill", f"s{index}", lambda _rid: "ok"))

    listed = talk_runs.list_runs(3)

    assert [row["runId"] for row in listed] == [5, 4, 3]
    assert listed[0]["label"] == "s4"


def test_list_limit_is_clamped():
    run_id = talk_runs.start_run("skill", "one", lambda _rid: "x")
    _wait_terminal(run_id)

    assert len(talk_runs.list_runs(0)) == 1
    assert talk_runs.list_runs(10_000)


# --- eviction ----------------------------------------------------------------


def test_terminal_runs_older_than_the_ttl_are_evicted():
    stale_id = talk_runs.start_run("skill", "ancient", lambda _rid: "ok")
    _wait_terminal(stale_id)
    talk_runs._RUNS[stale_id]["updated"] = time.time() - (talk_runs._RUN_TTL_S + 60)

    _wait_terminal(talk_runs.start_run("skill", "fresh", lambda _rid: "ok"))

    assert talk_runs.get_run(stale_id) is None


def test_running_entries_are_never_evicted_by_age():
    release = threading.Event()
    long_id = talk_runs.start_run("agent", "slow", lambda _rid: release.wait(timeout=3) or "ok")
    for _ in range(50):
        if talk_runs.get_run(long_id):
            break
        time.sleep(0.02)
    talk_runs._RUNS[long_id]["updated"] = time.time() - (talk_runs._RUN_TTL_S + 60)

    _wait_terminal(talk_runs.start_run("skill", "fresh", lambda _rid: "ok"))

    assert talk_runs.get_run(long_id)["status"] == "running"
    release.set()


def test_registry_is_capped_at_max_runs(monkeypatch):
    monkeypatch.setattr(talk_runs, "_MAX_RUNS", 5)

    for index in range(12):
        _wait_terminal(talk_runs.start_run("skill", f"s{index}", lambda _rid: "ok"))

    assert len(talk_runs._RUNS) <= 5
    assert talk_runs.get_run(1) is None
    assert talk_runs.get_run(12) is not None


# --- the sentinel contract ---------------------------------------------------


def test_sentinel_carries_id_and_kind():
    sentinel = talk_runs.started_sentinel(12, "agent", "audit the site")

    assert sentinel == "WORK_STARTED #12 kind=agent (audit the site)"


# --- history tee -------------------------------------------------------------


@pytest.fixture
def history_env(tmp_path: Path, monkeypatch) -> Path:
    """Opt in to the tee (inert under pytest by default) on a tmp state dir."""

    state = tmp_path / "state"
    monkeypatch.setattr(talk_runs, "_history_path", lambda: state / "talk-runs.jsonl")
    monkeypatch.setattr(talk_runs, "_history_enabled", lambda: True)
    state.mkdir(parents=True, exist_ok=True)
    return state


def test_history_path_lives_under_the_hermes_state_dir(monkeypatch, tmp_path):
    """The one test that proves the real wiring the fixture stubs out."""

    import sys

    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    assert talk_runs._history_path() == tmp_path / "home" / "state" / "talk-runs.jsonl"


def _record(run_id: int, kind: str, status: str, **extra) -> str:
    """One serialized history row."""

    return json.dumps(
        {
            "runId": run_id,
            "kind": kind,
            "label": "old",
            "status": status,
            "ts": 1.0,
            "updated": 1.0,
            **extra,
        }
    )


def _history_records(path: Path) -> list[dict]:
    """Read the tail the way the PRODUCT reads it, not more strictly.

    ``talk_runs`` decodes with ``errors="replace"`` and parses line by line,
    so one torn line costs that line and nothing else. A test reader that
    decodes strictly would raise on a file the product handles fine — and it
    did, the moment a helper started polling this file mid-run instead of
    reading it once after compaction had already swept the bad bytes away.
    """

    file = path / talk_runs._HISTORY_FILENAME
    if not file.exists():
        return []
    records: list[dict] = []
    for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _wait_history_terminal(path: Path, run_id: int, timeout: float = 5.0) -> list[dict]:
    """Wait for the run's TERMINAL record to land on disk, then return its records.

    ``_wait_terminal`` polls the in-memory registry, which flips to a terminal
    status a moment BEFORE the history tee finishes writing. A test that
    asserts on the FILE has to wait on the file: waiting on the registry
    instead passed on one CI runner and failed on the other five.
    """

    deadline = time.time() + timeout
    while time.time() < deadline:
        mine = [r for r in _history_records(path) if r["runId"] == run_id]
        if mine and mine[-1]["status"] in talk_runs.TERMINAL_STATUSES:
            return mine
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never reached a terminal status in history")


def test_history_tee_on_start_and_finish(history_env: Path):
    run_id = talk_runs.start_run("agent", "audit", lambda _rid: "all good")
    mine = _wait_history_terminal(history_env, run_id)
    assert [r["status"] for r in mine] == ["running", "done"]
    assert mine[-1]["output"] == "all good"
    assert mine[-1]["kind"] == "agent"


def test_history_tee_is_inert_without_optin(tmp_path: Path, monkeypatch):
    """Suites that exercise the registry transitively must not write state."""

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(talk_runs, "_history_path", lambda: state / "talk-runs.jsonl")

    _wait_terminal(talk_runs.start_run("skill", "s", lambda _rid: "x"))

    assert _history_records(state) == []


def test_history_merge_marks_dead_process_runs_lost(history_env: Path):
    (history_env / talk_runs._HISTORY_FILENAME).write_text(
        json.dumps(
            {
                "runId": 6,
                "kind": "skill",
                "label": "done one",
                "status": "done",
                "output": "ok",
                "ts": 1.0,
                "updated": 2.0,
            }
        )
        + "\n"
        + json.dumps(
            {
                "runId": 7,
                "kind": "agent",
                "label": "died mid-flight",
                "status": "running",
                "ts": 3.0,
                "updated": 3.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    runs = {r["runId"]: r for r in talk_runs.list_runs(50, include_history=True)}

    # This process cannot know whether the detached child finished — saying
    # "running" would be a claim it has no evidence for.
    assert runs[7]["status"] == "lost"
    assert runs[7]["fromHistory"] is True
    assert runs[6]["status"] == "done"
    assert runs[6]["output"] == "ok"


def test_live_registry_wins_over_history(history_env: Path):
    run_id = talk_runs.start_run("agent", "live", lambda _rid: "fresh output")
    _wait_terminal(run_id)

    merged = [r for r in talk_runs.list_runs(50, include_history=True) if r["runId"] == run_id]

    assert len(merged) == 1
    assert merged[0]["status"] == "done"
    assert merged[0]["output"] == "fresh output"
    assert "fromHistory" not in merged[0]


def test_default_list_shape_is_unchanged(history_env: Path):
    (history_env / talk_runs._HISTORY_FILENAME).write_text(
        _record(99, "skill", "done") + "\n", encoding="utf-8"
    )
    run_id = talk_runs.start_run("skill", "s", lambda _rid: "seen")
    _wait_terminal(run_id)

    assert [r["runId"] for r in talk_runs.list_runs()] == [run_id]


def test_seq_seeds_past_history(history_env: Path):
    (history_env / talk_runs._HISTORY_FILENAME).write_text(
        _record(41, "agent", "done") + "\n", encoding="utf-8"
    )
    talk_runs.reset_for_tests()
    _attach_test_owner()

    run_id = talk_runs.start_run("agent", "new", lambda _rid: "x")
    _wait_terminal(run_id)

    assert run_id == 42


def test_history_compaction_keeps_newest(history_env: Path, monkeypatch):
    monkeypatch.setattr(talk_runs, "_HISTORY_MAX_BYTES", 400)
    monkeypatch.setattr(talk_runs, "_HISTORY_COMPACT_KEEP", 3)

    ids = []
    for i in range(8):
        rid = talk_runs.start_run("skill", f"run {i}", lambda _rid: "out")
        _wait_history_terminal(history_env, rid)
        ids.append(rid)

    kept_ids = {r["runId"] for r in _history_records(history_env)}
    assert len(kept_ids) <= 4  # keep cap plus at most the post-compact append
    assert ids[-1] in kept_ids
    assert ids[0] not in kept_ids


def test_acceptance_write_failure_refuses_the_run(history_env: Path, monkeypatch):
    """The one write that is fail-CLOSED (hermes-talk#35).

    A run whose acceptance record never reached disk has no durable return
    route, so accepting it would mean speaking WORK_STARTED over nothing.
    Nothing may be registered and no worker may start.
    """

    def _boom():
        raise OSError("disk gone")

    monkeypatch.setattr(talk_runs, "_history_path", _boom)
    before = talk_runs.list_runs(50)

    with pytest.raises(talk_runs.RoutingUnavailable) as caught:
        talk_runs.start_run("agent", "never accepted", lambda _rid: "unreachable")

    assert "durably" in str(caught.value)
    # No registry entry means no run id was ever handed back to speak about.
    assert talk_runs.list_runs(50) == before


def test_history_failure_after_acceptance_stays_fail_open(history_env: Path, monkeypatch):
    """Only ACCEPTANCE is fail-closed; the rest of the tee still degrades.

    The counterpart to the test above, and the reason the split is narrow:
    once a run is accepted the operator is owed its result, so a disk that
    breaks mid-run must not stop the run from terminating.
    """

    release = threading.Event()

    def worker(_run_id: int) -> str:
        release.wait(timeout=3.0)
        return "done anyway"

    run_id = talk_runs.start_run("agent", "accepted first", worker)

    def _boom():
        raise OSError("disk gone")

    monkeypatch.setattr(talk_runs, "_history_path", _boom)
    release.set()
    run = _wait_terminal(run_id)

    assert run["status"] == "done"
    assert run["output"] == "done anyway"


# --- history corruption: one torn byte must cost one line, not the file ------


def test_invalid_utf8_costs_one_line_not_the_file(history_env: Path):
    good_5 = json.dumps(
        {"runId": 5, "kind": "skill", "label": "a", "status": "done", "ts": 1.0, "updated": 1.0}
    )
    good_6 = json.dumps(
        {"runId": 6, "kind": "agent", "label": "b", "status": "done", "ts": 2.0, "updated": 2.0}
    )
    (history_env / talk_runs._HISTORY_FILENAME).write_bytes(
        good_5.encode() + b"\n\xff\xfe torn line \xba\n" + good_6.encode() + b"\n"
    )

    assert set(talk_runs._load_history().keys()) == {5, 6}
    assert {5, 6} <= {r["runId"] for r in talk_runs.list_runs(50, include_history=True)}


def test_seed_survives_invalid_utf8(history_env: Path):
    good = json.dumps(
        {"runId": 41, "kind": "agent", "label": "old", "status": "done", "ts": 1.0, "updated": 1.0}
    )
    (history_env / talk_runs._HISTORY_FILENAME).write_bytes(good.encode() + b"\n\xff\xfe torn\n")
    talk_runs.reset_for_tests()
    _attach_test_owner()

    run_id = talk_runs.start_run("agent", "new", lambda _rid: "x")
    _wait_terminal(run_id)

    assert run_id == 42


def test_seed_floor_when_history_unreadable(history_env: Path, monkeypatch):
    """A present-but-unreadable file must not seed colliding ids from zero."""

    # Only the READ is broken here. The append still works, because the
    # subject of this test is the SEED floor — and since hermes-talk#35 a
    # broken append is a refusal, which would mask the thing being measured.
    real = history_env / talk_runs._HISTORY_FILENAME

    class _UnreadablePath:
        parent = history_env
        name = talk_runs._HISTORY_FILENAME

        def exists(self) -> bool:
            return True

        def read_text(self, *a, **k) -> str:
            raise OSError("locked by another process")

        def open(self, *a, **k):
            return real.open(*a, **k)

        def stat(self):
            return real.stat()

    monkeypatch.setattr(talk_runs, "_history_path", lambda: _UnreadablePath())
    talk_runs.reset_for_tests()
    _attach_test_owner()

    run_id = talk_runs.start_run("agent", "still mints", lambda _rid: "x")
    run = _wait_terminal(run_id)

    assert run["status"] == "done"
    assert run_id > 1_000_000_000  # wall-clock floor, not a colliding small id


def test_compaction_survives_invalid_utf8(history_env: Path, monkeypatch):
    """A torn byte must not wedge compaction into unbounded growth."""

    monkeypatch.setattr(talk_runs, "_HISTORY_MAX_BYTES", 400)
    monkeypatch.setattr(talk_runs, "_HISTORY_COMPACT_KEEP", 3)
    (history_env / talk_runs._HISTORY_FILENAME).write_bytes(b"\xff\xfe torn seed line\n")

    ids = []
    for i in range(8):
        rid = talk_runs.start_run("skill", f"run {i}", lambda _rid: "out")
        _wait_history_terminal(history_env, rid)
        ids.append(rid)

    kept_ids = {r["runId"] for r in _history_records(history_env)}
    assert len(kept_ids) <= 4
    assert ids[-1] in kept_ids
    assert ids[0] not in kept_ids


def test_stop_receipt_survives_a_process_restart(history_env: Path):
    # Codex v0.6.1 finding 1: a receipt promised past the courtesy wait used
    # to live only in memory — hang up and it was gone, history reloads
    # rebuilt meta={}. tee=True persists it, and the history rebuild carries
    # it back, so a later session's check_work can still keep the promise.
    run_id = talk_runs.start_run("agent", "audit", lambda _rid: "all done")
    _wait_terminal(run_id)
    talk_runs.annotate_run(run_id, tee=True, stop_result="stop sent, receipt pending")

    # Simulate the restart: registry wiped, JSONL survives.
    with talk_runs._RUN_LOCK:
        talk_runs._RUNS.clear()

    runs = talk_runs.list_runs(include_history=True)
    reloaded = next(r for r in runs if r["runId"] == run_id)
    assert reloaded["fromHistory"] is True
    assert reloaded["meta"]["stop_result"] == "stop sent, receipt pending"


def test_annotate_without_tee_stays_in_memory(history_env: Path):
    run_id = talk_runs.start_run("agent", "audit", lambda _rid: "all done")
    _wait_terminal(run_id)
    talk_runs.annotate_run(run_id, stop_result="ephemeral")

    with talk_runs._RUN_LOCK:
        talk_runs._RUNS.clear()

    runs = talk_runs.list_runs(include_history=True)
    reloaded = next(r for r in runs if r["runId"] == run_id)
    assert reloaded["meta"] == {}  # default tee-less annotate: telemetry only


def test_terminal_tee_carries_meta_so_compaction_cannot_erase_it(history_env: Path):
    # The compactor keeps the NEWEST record per run — if the terminal tee
    # dropped meta, a receipt annotated before the finish would be erased.
    gate = threading.Event()
    run_id = talk_runs.start_run("agent", "audit", lambda _rid: gate.wait(5) or "all done")
    talk_runs.annotate_run(run_id, stop_result="accepted")  # while still running
    gate.set()
    _wait_terminal(run_id)

    records = _history_records(history_env)
    terminal = [r for r in records if r["runId"] == run_id and r["status"] == "done"]
    assert terminal and terminal[-1]["meta"].get("stop_result") == "accepted"


# --- the return-route ticket (hermes-talk#35) --------------------------------


def test_start_run_without_a_bound_route_is_refused():
    """Reject BEFORE execution when nothing can receive the result."""

    talk_runs.detach_owner()

    with pytest.raises(talk_runs.RoutingUnavailable) as caught:
        talk_runs.start_run("agent", "nowhere to send this", lambda _rid: "unreachable")

    assert "no Talk connection is bound" in str(caught.value)
    assert talk_runs.list_runs(50) == []


def test_the_ticket_binds_every_identity_the_issue_names():
    _attach_test_owner(
        talk_session_id="ts-1",
        generation_id="gen-7",
        hermes_session_id="sess-abc",
        operator="codex-oauth",
        profile="research",
    )

    run = _wait_terminal(talk_runs.start_run("agent", "bound", lambda _rid: "done"))
    ticket = run["ticket"]

    assert ticket["operator"] == "codex-oauth"
    assert ticket["profile"] == "research"
    assert ticket["hermesSessionId"] == "sess-abc"
    assert ticket["talkSessionId"] == "ts-1"
    assert ticket["generationId"] == "gen-7"
    assert ticket["requestId"].startswith("req-")
    assert run["delivery"] == talk_runs.DELIVERY_PENDING


def test_each_run_gets_its_own_request_id():
    first = talk_runs.get_run(talk_runs.start_run("agent", "a", lambda _rid: "x"))
    second = talk_runs.get_run(talk_runs.start_run("agent", "b", lambda _rid: "x"))

    assert first["ticket"]["requestId"] != second["ticket"]["requestId"]


def test_the_ticket_is_frozen_at_acceptance():
    """A later attach must not retroactively claim work it did not accept."""

    _attach_test_owner(hermes_session_id="sess-first", generation_id="gen-1")
    run_id = talk_runs.start_run("agent", "accepted by the first", lambda _rid: "done")
    _wait_terminal(run_id)

    _attach_test_owner(hermes_session_id="sess-second", generation_id="gen-2")

    ticket = talk_runs.get_run(run_id)["ticket"]
    assert ticket["hermesSessionId"] == "sess-first"
    assert ticket["generationId"] == "gen-1"
    assert talk_runs.list_undelivered_for_session("sess-second") == []


def test_mark_delivered_is_exact_once():
    run_id = talk_runs.start_run("agent", "once", lambda _rid: "done")
    _wait_terminal(run_id)

    assert talk_runs.mark_delivered(run_id) is True
    assert talk_runs.mark_delivered(run_id) is False
    assert talk_runs.get_run(run_id)["delivery"] == talk_runs.DELIVERED


def test_mark_delivered_ignores_an_unknown_run():
    assert talk_runs.mark_delivered(987654) is False


def test_undelivered_excludes_foreign_sessions_and_claimed_results():
    _attach_test_owner(hermes_session_id="sess-mine")
    mine = talk_runs.start_run("agent", "mine", lambda _rid: "done")
    _wait_terminal(mine)

    _attach_test_owner(hermes_session_id="sess-theirs")
    theirs = talk_runs.start_run("agent", "theirs", lambda _rid: "done")
    _wait_terminal(theirs)

    assert [r["runId"] for r in talk_runs.list_undelivered_for_session("sess-mine")] == [mine]
    assert [r["runId"] for r in talk_runs.list_undelivered_for_session("sess-theirs")] == [theirs]

    talk_runs.mark_delivered(mine)
    assert talk_runs.list_undelivered_for_session("sess-mine") == []


def test_undelivered_skips_runs_that_have_not_landed():
    release = threading.Event()
    run_id = talk_runs.start_run("agent", "still going", lambda _rid: release.wait(timeout=3.0))

    assert talk_runs.list_undelivered_for_session("sess-test") == []

    release.set()
    _wait_terminal(run_id)
    assert [r["runId"] for r in talk_runs.list_undelivered_for_session("sess-test")] == [run_id]


def test_undelivered_without_a_session_id_claims_nothing():
    """A tier-2/3-only connection has no durable id, so it adopts nothing."""

    _wait_terminal(talk_runs.start_run("agent", "landed", lambda _rid: "done"))

    assert talk_runs.list_undelivered_for_session(None) == []
    assert talk_runs.list_undelivered_for_session("") == []


def test_pre_fix_history_is_never_adopted(history_env: Path):
    """Records written before the ticket existed belong to nobody."""

    (history_env / talk_runs._HISTORY_FILENAME).write_text(
        _record(11, "agent", "done") + "\n", encoding="utf-8"
    )
    talk_runs.reset_for_tests()
    _attach_test_owner(hermes_session_id="sess-test")

    merged = {r["runId"]: r for r in talk_runs.list_runs(50, include_history=True)}

    assert merged[11]["ticket"] == {}
    assert merged[11]["delivery"] == talk_runs.DELIVERY_PENDING
    assert talk_runs.list_undelivered_for_session("sess-test") == []


def test_the_terminal_tee_carries_the_ticket_and_delivery(history_env: Path):
    """Compaction keeps the newest record, so the route must ride every tee."""

    _attach_test_owner(hermes_session_id="sess-restart")
    run_id = talk_runs.start_run("agent", "teed", lambda _rid: "done")
    records = _wait_history_terminal(history_env, run_id)

    assert records[-1]["ticket"]["hermesSessionId"] == "sess-restart"
    assert records[-1]["delivery"] == talk_runs.DELIVERY_PENDING


def test_a_reconnect_adopts_an_orphaned_result_exactly_once(history_env: Path):
    """The bug this issue is about, end to end across a simulated restart.

    The claim has to land on DISK, not just in the registry: the process that
    accepted the run is gone by definition, so an in-memory-only flip would
    let the same stale result be re-announced at every reconnect forever.
    """

    _attach_test_owner(hermes_session_id="sess-restart")
    run_id = talk_runs.start_run("agent", "orphaned", lambda _rid: "the answer")
    _wait_history_terminal(history_env, run_id)

    # The Talk process dies with the result unspoken; a new one reconnects
    # behind the same durable Hermes session.
    talk_runs.reset_for_tests()
    _attach_test_owner(hermes_session_id="sess-restart", generation_id="gen-after")

    owed = talk_runs.list_undelivered_for_session("sess-restart")
    assert [r["runId"] for r in owed] == [run_id]
    assert owed[0]["output"] == "the answer"

    assert talk_runs.mark_delivered(run_id) is True
    assert talk_runs.mark_delivered(run_id) is False
    assert talk_runs.list_undelivered_for_session("sess-restart") == []

    # And it stays claimed through the NEXT restart, which is the whole point.
    talk_runs.reset_for_tests()
    _attach_test_owner(hermes_session_id="sess-restart")
    assert talk_runs.list_undelivered_for_session("sess-restart") == []


def test_claiming_from_history_fails_closed_when_the_write_cannot_land(
    history_env: Path, monkeypatch
):
    """The delivery-claim counterpart to test_acceptance_write_failure_refuses_the_run.

    An unpersisted claim is not a claim (see _claim_delivery_in_history's own
    docstring) — a disk failure here must leave the run claimable, not silently
    grant the claim in memory only.
    """

    _attach_test_owner(hermes_session_id="sess-restart")
    run_id = talk_runs.start_run("agent", "orphaned", lambda _rid: "the answer")
    _wait_history_terminal(history_env, run_id)

    # The process that accepted it dies before speaking the result.
    talk_runs.reset_for_tests()
    _attach_test_owner(hermes_session_id="sess-restart")

    def _boom(_record):
        raise OSError("disk gone")

    monkeypatch.setattr(talk_runs, "_append_history_strict", _boom)

    assert talk_runs.mark_delivered(run_id) is False
    # Still owed — a failed claim must not remove the run from the adoption list.
    assert [
        r["runId"] for r in talk_runs.list_undelivered_for_session("sess-restart")
    ] == [run_id]


def test_a_reconnect_from_a_different_session_adopts_nothing(history_env: Path):
    _attach_test_owner(hermes_session_id="sess-restart")
    run_id = talk_runs.start_run("agent", "orphaned", lambda _rid: "the answer")
    _wait_history_terminal(history_env, run_id)

    talk_runs.reset_for_tests()
    _attach_test_owner(hermes_session_id="sess-stranger")

    assert talk_runs.list_undelivered_for_session("sess-stranger") == []
    # Still owed to its real owner, not silently consumed by the stranger.
    assert [r["runId"] for r in talk_runs.list_undelivered_for_session("sess-restart")] == [run_id]


def test_a_run_that_has_not_landed_cannot_be_claimed():
    """Claiming early would strand the result the reconnect was owed."""

    release = threading.Event()
    run_id = talk_runs.start_run("agent", "in flight", lambda _rid: release.wait(timeout=3.0))

    assert talk_runs.mark_delivered(run_id) is False

    release.set()
    _wait_terminal(run_id)

    # Still claimable — and still owed — once it actually lands.
    assert [r["runId"] for r in talk_runs.list_undelivered_for_session("sess-test")] == [run_id]
    assert talk_runs.mark_delivered(run_id) is True


def test_a_lost_history_run_is_not_claimable(history_env: Path):
    """`lost` means this process cannot know the outcome, not that it has one."""

    (history_env / talk_runs._HISTORY_FILENAME).write_text(
        _record(21, "agent", "running", ticket={"hermesSessionId": "sess-test"}) + "\n",
        encoding="utf-8",
    )
    talk_runs.reset_for_tests()
    _attach_test_owner()

    merged = {r["runId"]: r for r in talk_runs.list_runs(50, include_history=True)}
    assert merged[21]["status"] == "lost"
    assert talk_runs.mark_delivered(21) is False
    assert talk_runs.list_undelivered_for_session("sess-test") == []
