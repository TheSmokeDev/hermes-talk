"""Run admission control — execution_mode + resource_keys (hermes-talk#101).

What is being proved: two live runs that share a resource key never
overlap; disjoint keys and undeclared runs are untouched; a read-only pair
may share a key only when the operator has opted into believing the
declaration; a refusal happens BEFORE acceptance (no run id burned, no
history row) and names the run in the way; the reservation closes the
check-then-accept gap between two tool-pool workers; and the declaration
rides delegate_task through the host adapter into the registry.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import talk_config
import talk_host
import talk_runs
import talk_tools

pytestmark = pytest.mark.usefixtures("_registry")


@pytest.fixture
def _registry(monkeypatch):
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    talk_runs.attach_owner(
        talk_session_id="ts-test",
        generation_id="gen-test",
        hermes_session_id="sess-test",
        operator="test",
        profile=None,
    )
    monkeypatch.delenv("TALK_TRUST_DECLARED_READ_ONLY", raising=False)
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: None)
    yield
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()


class _Gate:
    """Workers that stay running until released, so admission has something to hit."""

    def __init__(self):
        self.release = threading.Event()

    def worker(self, _run_id: int) -> str:
        self.release.wait(5.0)
        return "done"


def _wait_terminal(run_id: int, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = talk_runs.get_run(run_id)
        if run and run["status"] in talk_runs.TERMINAL_STATUSES:
            return run
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} never finished")


# -- the fence ----------------------------------------------------------------


def test_disjoint_keys_run_together():
    gate = _Gate()
    first = talk_runs.start_run("agent", "audit", gate.worker, resource_keys=["/repo/a"])
    second = talk_runs.start_run("agent", "deploy", gate.worker, resource_keys=["/repo/b"])

    assert talk_runs.get_run(first)["status"] == "running"
    assert talk_runs.get_run(second)["status"] == "running"
    gate.release.set()
    _wait_terminal(first)
    _wait_terminal(second)


def test_shared_keys_serialize_until_the_holder_finishes():
    gate = _Gate()
    first = talk_runs.start_run("agent", "audit the repo", gate.worker, resource_keys=["/repo/a"])

    with pytest.raises(talk_runs.AdmissionRefused) as refused:
        talk_runs.start_run("agent", "deploy", gate.worker, resource_keys=["/repo/b", "/repo/a"])
    assert refused.value.run_id == first
    assert refused.value.keys == ("/repo/a",)

    gate.release.set()
    _wait_terminal(first)
    # A terminal holder holds nothing: the same declaration is admitted now.
    second_gate = _Gate()
    second = talk_runs.start_run(
        "agent", "deploy", second_gate.worker, resource_keys=["/repo/a"]
    )
    assert talk_runs.get_run(second)["status"] == "running"
    second_gate.release.set()


def test_the_refusal_names_the_run_and_the_key_and_what_to_do():
    gate = _Gate()
    first = talk_runs.start_run("agent", "audit the repo", gate.worker, resource_keys=["/repo/a"])

    with pytest.raises(talk_runs.AdmissionRefused) as refused:
        talk_runs.start_run("agent", "deploy", gate.worker, resource_keys=["/repo/a"])

    message = str(refused.value)
    assert message.startswith(f"run {first} (audit the repo) is still running")
    assert "the same resource ('/repo/a')" in message
    assert "wait for it, stop it, or re-delegate without that key" in message
    gate.release.set()


def test_two_shared_keys_are_plural_in_the_refusal():
    gate = _Gate()
    talk_runs.start_run("agent", "a", gate.worker, resource_keys=["x", "y", "z"])
    # Named in the order the REFUSED run declared them — the model reads its
    # own declaration back, not the holder's.
    with pytest.raises(talk_runs.AdmissionRefused, match=r"resources \('z', 'x'\)"):
        talk_runs.start_run("agent", "b", gate.worker, resource_keys=["z", "x"])
    gate.release.set()


def test_read_only_pairs_overlap_only_with_the_knob_on(monkeypatch):
    gate = _Gate()
    talk_runs.start_run(
        "agent", "read a", gate.worker,
        execution_mode="parallel_read_only", resource_keys=["/repo/a"],
    )
    # Default: the declaration is not trusted, so the holder was admitted as
    # exclusive and the sibling is refused.
    with pytest.raises(talk_runs.AdmissionRefused):
        talk_runs.start_run(
            "agent", "read b", gate.worker,
            execution_mode="parallel_read_only", resource_keys=["/repo/a"],
        )
    gate.release.set()
    talk_runs.reset_for_tests()
    talk_runs.attach_owner(
        talk_session_id="ts-test", generation_id="g", hermes_session_id="s",
        operator="test", profile=None,
    )

    monkeypatch.setenv("TALK_TRUST_DECLARED_READ_ONLY", "true")
    gate = _Gate()
    first = talk_runs.start_run(
        "agent", "read a", gate.worker,
        execution_mode="parallel_read_only", resource_keys=["/repo/a"],
    )
    second = talk_runs.start_run(
        "agent", "read b", gate.worker,
        execution_mode="parallel_read_only", resource_keys=["/repo/a"],
    )
    assert talk_runs.get_run(first)["admission"]["mode"] == "parallel_read_only"
    assert talk_runs.get_run(second)["status"] == "running"
    # Read-only never overlaps a MUTATING holder of the same key, knob or not.
    with pytest.raises(talk_runs.AdmissionRefused):
        talk_runs.start_run("agent", "write a", gate.worker, resource_keys=["/repo/a"])
    gate.release.set()


def test_the_declaration_is_downgraded_not_dropped_when_untrusted():
    gate = _Gate()
    run_id = talk_runs.start_run(
        "agent", "read", gate.worker,
        execution_mode="parallel_read_only", resource_keys=["/repo/a"],
    )
    admission = talk_runs.get_run(run_id)["admission"]
    assert admission == {"mode": "exclusive", "keys": ["/repo/a"]}
    gate.release.set()


def test_turning_the_knob_off_closes_overlaps_it_had_allowed(monkeypatch):
    """A holder admitted as read-only under an earlier configuration is judged
    under the knob as it stands NOW — the safer default cannot be escaped by
    state minted before it was set."""

    monkeypatch.setenv("TALK_TRUST_DECLARED_READ_ONLY", "1")
    gate = _Gate()
    talk_runs.start_run(
        "agent", "read a", gate.worker,
        execution_mode="parallel_read_only", resource_keys=["/repo/a"],
    )
    monkeypatch.delenv("TALK_TRUST_DECLARED_READ_ONLY")
    with pytest.raises(talk_runs.AdmissionRefused):
        talk_runs.start_run(
            "agent", "read b", gate.worker,
            execution_mode="parallel_read_only", resource_keys=["/repo/a"],
        )
    gate.release.set()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("junk", False),
        ("1", True),
        ("true", True),
        ("YES", True),
        (" on ", True),
    ],
)
def test_the_trust_knob_is_off_unless_explicitly_on(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("TALK_TRUST_DECLARED_READ_ONLY", raising=False)
    else:
        monkeypatch.setenv("TALK_TRUST_DECLARED_READ_ONLY", raw)
    assert talk_config.trust_declared_read_only() is expected


# -- the declaration ----------------------------------------------------------


def test_keys_are_normalized_and_capped():
    assert talk_runs.normalize_resource_keys(None) == ()
    assert talk_runs.normalize_resource_keys("one key") == ("one key",)
    assert talk_runs.normalize_resource_keys(
        [" C:/Repo/A ", "c:/repo/a", "Deploy   Target", "", "  "]
    ) == ("c:/repo/a", "deploy target")

    assert talk_runs.normalize_resource_keys([f"k{i}" for i in range(8)]) == tuple(
        f"k{i}" for i in range(8)
    )
    with pytest.raises(ValueError, match="at most 8"):
        talk_runs.normalize_resource_keys([f"k{i}" for i in range(9)])
    with pytest.raises(ValueError, match="at most 200 characters"):
        talk_runs.normalize_resource_keys(["x" * 201])
    with pytest.raises(ValueError, match="list of short strings"):
        talk_runs.normalize_resource_keys([1, 2])
    with pytest.raises(ValueError, match="list of short strings"):
        talk_runs.normalize_resource_keys({"a": 1})


def test_case_folded_keys_collide_on_purpose():
    """Two spellings of one path are one key — the fence errs toward refusal."""

    gate = _Gate()
    talk_runs.start_run("agent", "a", gate.worker, resource_keys=["C:\\Repo"])
    with pytest.raises(talk_runs.AdmissionRefused, match="'c:\\\\repo'"):
        talk_runs.start_run("agent", "b", gate.worker, resource_keys=["c:\\REPO"])
    gate.release.set()


def test_an_unknown_mode_is_a_caller_bug():
    with pytest.raises(ValueError, match="execution_mode"):
        talk_runs.start_run("agent", "a", lambda _r: "x", execution_mode="yolo")
    assert talk_runs.list_runs() == []


# -- byte-for-byte compatibility ----------------------------------------------


def test_undeclared_runs_are_exactly_yesterdays_runs(monkeypatch, tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(talk_runs, "_history_path", lambda: state / "talk-runs.jsonl")
    monkeypatch.setattr(talk_runs, "_history_enabled", lambda: True)
    gate = _Gate()

    first = talk_runs.start_run("agent", "a", gate.worker)
    second = talk_runs.start_run("agent", "b", gate.worker)
    third = talk_runs.start_run("agent", "c", gate.worker, resource_keys=["/repo"])
    # Undeclared runs never fence and are never fenced: all three are live.
    assert all(talk_runs.get_run(rid)["status"] == "running" for rid in (first, second, third))
    assert "admission" not in talk_runs.get_run(first)
    assert "admission" not in talk_runs.get_run(second)

    records = [
        json.loads(line)
        for line in (state / "talk-runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    undeclared = next(rec for rec in records if rec["runId"] == first)
    assert set(undeclared) == {
        "runId", "kind", "label", "status", "output", "meta", "ticket", "delivery", "ts", "updated",
    }
    declared = next(rec for rec in records if rec["runId"] == third)
    assert declared["admission"] == {"mode": "exclusive", "keys": ["/repo"]}
    gate.release.set()


def test_a_refusal_burns_no_run_id_and_writes_no_history_row(monkeypatch, tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(talk_runs, "_history_path", lambda: state / "talk-runs.jsonl")
    monkeypatch.setattr(talk_runs, "_history_enabled", lambda: True)
    gate = _Gate()
    first = talk_runs.start_run("agent", "a", gate.worker, resource_keys=["/repo"])
    lines_before = (state / "talk-runs.jsonl").read_text(encoding="utf-8").splitlines()

    with pytest.raises(talk_runs.AdmissionRefused):
        talk_runs.start_run("agent", "b", gate.worker, resource_keys=["/repo"])

    assert (state / "talk-runs.jsonl").read_text(encoding="utf-8").splitlines() == lines_before
    gate.release.set()
    _wait_terminal(first)
    # The next accepted run takes the very next id — nothing was burned.
    assert talk_runs.start_run("agent", "c", gate.worker, resource_keys=["/repo"]) == first + 1
    gate.release.set()


# -- the reservation ----------------------------------------------------------


def test_the_reservation_closes_the_check_then_accept_gap(monkeypatch):
    """Two tool-pool workers admitted in the gap between the check and the
    registry insert would both start. The first holds its keys from the
    moment it passes the check, even while its acceptance write is in
    flight — and releases them if that write fails."""

    entered = threading.Event()
    proceed = threading.Event()
    real_accept = talk_runs._accept_run
    outcome: dict = {}

    def slow_accept(entry):
        if entry["label"] == "slow":
            entered.set()
            proceed.wait(5.0)
        return real_accept(entry)

    monkeypatch.setattr(talk_runs, "_accept_run", slow_accept)
    gate = _Gate()

    def start_slow():
        try:
            outcome["slow"] = talk_runs.start_run(
                "agent", "slow", gate.worker, resource_keys=["/repo"]
            )
        except Exception as exc:  # noqa: BLE001 — recorded for the assertion
            outcome["slow"] = exc

    slow = threading.Thread(target=start_slow)
    slow.start()
    assert entered.wait(2.0)
    # Mid-acceptance: not yet in the registry, but already holding the key.
    with pytest.raises(talk_runs.AdmissionRefused) as refused:
        talk_runs.start_run("agent", "fast", gate.worker, resource_keys=["/repo"])
    assert refused.value.run_id is None
    assert "a run just accepted (slow)" in str(refused.value)

    proceed.set()
    slow.join(2.0)
    assert isinstance(outcome["slow"], int)
    assert talk_runs._RESERVATIONS == {}
    gate.release.set()


def test_a_failed_acceptance_releases_its_reservation():
    def failing_accept(_entry):
        raise talk_runs.RoutingUnavailable("disk on fire")

    gate = _Gate()
    # Scoped so the real acceptance path is back for the second call.
    with pytest.MonkeyPatch.context() as failing:
        failing.setattr(talk_runs, "_accept_run", failing_accept)
        with pytest.raises(talk_runs.RoutingUnavailable):
            talk_runs.start_run("agent", "a", gate.worker, resource_keys=["/repo"])
        assert talk_runs._RESERVATIONS == {}

    # Nothing lingers: the same key is free for the next caller.
    run_id = talk_runs.start_run("agent", "b", gate.worker, resource_keys=["/repo"])
    assert talk_runs.get_run(run_id)["status"] == "running"
    gate.release.set()


def test_snapshots_carry_the_admission_and_check_work_reads_it_out():
    gate = _Gate()
    run_id = talk_runs.start_run("agent", "audit", gate.worker, resource_keys=["/repo", "prod"])

    listed = next(run for run in talk_runs.list_runs() if run["runId"] == run_id)
    assert listed["admission"] == {"mode": "exclusive", "keys": ["/repo", "prod"]}
    listed["admission"]["keys"].append("tampered")
    assert talk_runs.get_run(run_id)["admission"]["keys"] == ["/repo", "prod"]

    spoken = talk_tools.execute_talk_tool("check_work", {})
    assert f"run {run_id} (agent) running" in spoken
    assert "holding '/repo', 'prod'" in spoken
    gate.release.set()


# -- the tool and the host ----------------------------------------------------


def test_delegate_task_advertises_the_declaration():
    schema = next(t for t in talk_tools.default_talk_tools() if t["name"] == "delegate_task")
    properties = schema["parameters"]["properties"]
    assert properties["execution_mode"]["enum"] == ["exclusive", "parallel_read_only"]
    assert properties["resource_keys"]["maxItems"] == talk_runs.MAX_RESOURCE_KEYS
    assert properties["resource_keys"]["items"] == {"type": "string"}
    assert schema["parameters"]["required"] == ["task"]


def test_delegate_task_threads_the_declaration_into_the_host(monkeypatch):
    seen: dict = {}

    class _Host:
        def run_agent(self, task, background=True, *, execution_mode=None, resource_keys=None):
            seen.update(
                task=task, background=background, mode=execution_mode, keys=resource_keys
            )
            return "WORK_STARTED #1 kind=agent (x)"

    monkeypatch.setattr(talk_host, "host", lambda: _Host())
    result = talk_tools.execute_talk_tool(
        "delegate_task",
        {
            "task": "audit it",
            "execution_mode": "Parallel_Read_Only",
            "resource_keys": [" /Repo ", "/repo", "prod"],
        },
    )
    assert result.startswith("WORK_STARTED")
    assert seen == {
        "task": "audit it",
        "background": True,
        "mode": "parallel_read_only",
        "keys": ("/repo", "prod"),
    }

    # Absent: exactly the call the host always received.
    talk_tools.execute_talk_tool("delegate_task", {"task": "plain"})
    assert seen["mode"] is None and seen["keys"] == ()


def test_delegate_task_refuses_a_bad_declaration_before_any_lane_runs(monkeypatch):
    def never(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("a malformed declaration reached a backend")

    monkeypatch.setattr(talk_host, "host", lambda: type("H", (), {"run_agent": never})())

    assert "'exclusive' or 'parallel_read_only'" in talk_tools.execute_talk_tool(
        "delegate_task", {"task": "x", "execution_mode": "yolo"}
    )
    too_many = talk_tools.execute_talk_tool(
        "delegate_task", {"task": "x", "resource_keys": [f"k{i}" for i in range(9)]}
    )
    assert "at most 8" in too_many


class _StubCtx:
    def __init__(self, result):
        self.calls: list = []
        self.result = result

    def dispatch_tool(self, tool_name, args, **kwargs):
        self.calls.append((tool_name, args))
        return self.result


def test_the_registry_lanes_speak_the_refusal(monkeypatch):
    """Tier 2/3: the refusal is spoken in the same shape as a routing refusal —
    nothing was accepted, so nothing is in flight to check on."""

    monkeypatch.setattr(talk_host.talk_apiserver, "is_available", lambda: True)
    monkeypatch.setattr(
        talk_host, "_api_server_worker", lambda prompt, session_id=None: _Gate().worker
    )
    gate = _Gate()
    first = talk_runs.start_run("agent", "audit the repo", gate.worker, resource_keys=["/repo"])

    spoken = talk_host.host().run_agent("deploy the repo", resource_keys=["/repo"])

    assert spoken.startswith("I can't start that yet — ")
    assert f"run {first} (audit the repo)" in spoken
    assert "WORK_STARTED" not in spoken
    assert len(talk_runs.list_runs()) == 1
    gate.release.set()


def test_the_registry_lanes_admit_and_record_the_declaration(monkeypatch):
    monkeypatch.setattr(talk_host.talk_apiserver, "is_available", lambda: True)
    monkeypatch.setattr(
        talk_host, "_api_server_worker", lambda prompt, session_id=None: (lambda _r: "ok")
    )

    spoken = talk_host.host().run_agent(
        "read the repo", execution_mode="parallel_read_only", resource_keys=["/repo"]
    )

    assert spoken.startswith("WORK_STARTED #")
    run_id = int(spoken.split("#", 1)[1].split(" ", 1)[0])
    assert talk_runs.get_run(run_id)["admission"] == {"mode": "exclusive", "keys": ["/repo"]}
    _wait_terminal(run_id)


def test_the_host_loop_lane_is_checked_but_says_it_holds_nothing():
    """Tier 1 hands the child to Hermes's own delegation registry: it is never
    started on top of a registry holder, and the receipt says the fence does
    not hold it afterwards — never silently unfenced."""

    ctx = _StubCtx(json.dumps({"success": True, "result": "subagent 4 started"}))
    talk_host.bind_ctx(ctx)
    gate = _Gate()
    first = talk_runs.start_run("agent", "audit", gate.worker, resource_keys=["/repo"])

    refused = talk_host.host().run_agent("deploy", resource_keys=["/repo"])
    assert f"run {first} (audit)" in refused
    assert ctx.calls == []

    started = talk_host.host().run_agent("deploy elsewhere", resource_keys=["/other"])
    assert started.startswith("WORK_STARTED — subagent 4 started")
    assert started.endswith(talk_host.HOST_LOOP_ADMISSION_NOTE)
    assert ctx.calls == [("delegate_task", {"goal": "deploy elsewhere"})]

    plain = talk_host.host().run_agent("no keys at all")
    assert plain == "WORK_STARTED — subagent 4 started"
    gate.release.set()
