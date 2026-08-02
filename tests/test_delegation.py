"""The run_agent backend chain — host agent loop, detached Hermes, refusal.

The live failure this closes: in a standalone ``hermes talk`` the plugin
context IS bound, but Hermes has no parent agent to delegate into, so every
``delegate_task`` came back as an honest refusal. Tier 2 makes it real.

No process is ever spawned here — ``shutil.which`` and ``subprocess.run`` are
both replaced, so the suite proves the argv, the env inheritance, and the
receipt without a Hermes install.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest

import talk_config
import talk_host
import talk_runs

_NO_PARENT = json.dumps({"error": "delegate_task requires a parent agent context."})
_FAKE_HERMES = "/usr/local/bin/hermes"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    monkeypatch.delenv("TALK_AGENT_TIMEOUT_S", raising=False)
    yield
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()


class _StubCtx:
    def __init__(self, result):
        self.calls: list[tuple[str, dict]] = []
        self.result = result

    def dispatch_tool(self, tool_name, args, **kwargs):
        self.calls.append((tool_name, args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _wait_terminal(run_id: int, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = talk_runs.get_run(run_id)
        if run and run["status"] in talk_runs.TERMINAL_STATUSES:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")


def _fake_completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# --- the degradation detector ------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        _NO_PARENT,
        json.dumps({"error": "Unknown tool: delegate_task"}),
        json.dumps({"error": "DELEGATE_TASK REQUIRES A PARENT AGENT CONTEXT."}),
    ],
)
def test_agent_loop_absent_is_recognised(raw):
    assert talk_host._agent_loop_absent(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({"success": True, "result": "subagent 4 started"}),
        # An operator PAUSED delegation, a depth limit, a genuine crash: the
        # host's decision. Routing around these would be the plugin overruling
        # a deliberate setting.
        json.dumps({"error": "Delegation spawning is paused. Clear the pause via the TUI"}),
        json.dumps({"error": "max_spawn_depth exceeded"}),
        "not json at all",
        json.dumps(["a", "list"]),
    ],
)
def test_other_results_are_not_a_fall_through(raw):
    assert talk_host._agent_loop_absent(raw) is False


# --- tier 1: the host's own agent loop ---------------------------------------


def test_a_working_agent_loop_wins_and_never_spawns(monkeypatch):
    def never(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("spawned a detached agent despite a live agent loop")

    monkeypatch.setattr(talk_host, "hermes_binary", never)
    ctx = _StubCtx(json.dumps({"success": True, "result": "subagent 4 started"}))
    talk_host.bind_ctx(ctx)

    result = talk_host.host().run_agent("rebuild the index")

    assert ctx.calls == [("delegate_task", {"goal": "rebuild the index"})]
    assert result.startswith("WORK_STARTED")
    assert "subagent 4 started" in result


def test_a_real_dispatch_error_is_spoken_not_routed_around(monkeypatch):
    def never(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("fell through on an error that was not a missing agent loop")

    monkeypatch.setattr(talk_host, "hermes_binary", never)
    talk_host.bind_ctx(_StubCtx(json.dumps({"error": "Delegation spawning is paused."})))

    assert "paused" in talk_host.host().run_agent("do the thing")


def test_a_raising_dispatch_is_spoken(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: None)
    talk_host.bind_ctx(_StubCtx(RuntimeError("registry offline")))

    result = talk_host.host().run_agent("do the thing")

    assert "couldn't start that work" in result
    assert "registry offline" in result


# --- tier 2: the detached headless Hermes ------------------------------------


def test_missing_agent_loop_falls_through_to_a_detached_run(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _fake_completed(stdout="  the index is rebuilt  \n")

    monkeypatch.setattr(talk_host.subprocess, "run", fake_run)
    talk_host.bind_ctx(_StubCtx(_NO_PARENT))

    result = talk_host.host().run_agent("rebuild the index")

    assert result.startswith("WORK_STARTED #")
    assert "kind=agent" in result
    assert "detached Hermes agent" in result  # the fall-through is announced

    run_id = int(result.split("#", 1)[1].split(" ", 1)[0])
    assert _wait_terminal(run_id)["output"] == "the index is rebuilt"
    assert seen["argv"] == [_FAKE_HERMES, "-z", "rebuild the index"]


def test_detached_run_inherits_the_environment_and_bounds_itself(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)
    monkeypatch.setenv("TALK_AGENT_TIMEOUT_S", "42")
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return _fake_completed(stdout="ok")

    monkeypatch.setattr(talk_host.subprocess, "run", fake_run)

    run_id = int(talk_host.host().run_agent("go").split("#", 1)[1].split(" ", 1)[0])
    _wait_terminal(run_id)

    # No env= at all: the child inherits HERMES_HOME (and everything else)
    # from this process, so it reads the same home the voice session uses.
    assert "env" not in seen
    assert seen["timeout"] == 42
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"


def test_no_ctx_at_all_goes_straight_to_the_detached_backend(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)
    monkeypatch.setattr(
        talk_host.subprocess, "run", lambda *a, **k: _fake_completed(stdout="done")
    )

    assert "WORK_STARTED #" in talk_host.host().run_agent("go")


def test_a_nonzero_exit_lands_as_a_failed_run_with_the_reason(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)
    monkeypatch.setattr(
        talk_host.subprocess,
        "run",
        lambda *a, **k: _fake_completed(stderr="no provider configured", returncode=2),
    )

    run_id = int(talk_host.host().run_agent("go").split("#", 1)[1].split(" ", 1)[0])
    run = _wait_terminal(run_id)

    # The run itself completed — the CHILD failed, and its reason is speakable.
    assert run["status"] == "done"
    assert "exited 2" in run["output"]
    assert "no provider configured" in run["output"]


def test_a_timeout_lands_as_a_failed_run(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="hermes", timeout=1)

    monkeypatch.setattr(talk_host.subprocess, "run", timeout)

    run_id = int(talk_host.host().run_agent("go").split("#", 1)[1].split(" ", 1)[0])
    run = _wait_terminal(run_id)

    assert run["status"] == "failed"
    assert "TimeoutExpired" in run["output"]


def test_silent_child_still_says_something(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)
    monkeypatch.setattr(talk_host.subprocess, "run", lambda *a, **k: _fake_completed(stdout="  "))

    run_id = int(talk_host.host().run_agent("go").split("#", 1)[1].split(" ", 1)[0])

    assert "without printing anything" in _wait_terminal(run_id)["output"]


def test_output_is_capped(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)
    monkeypatch.setattr(
        talk_host.subprocess, "run", lambda *a, **k: _fake_completed(stdout="x" * 99_999)
    )

    run_id = int(talk_host.host().run_agent("go").split("#", 1)[1].split(" ", 1)[0])

    assert len(_wait_terminal(run_id)["output"]) == talk_runs.HISTORY_OUTPUT_CAP


def test_label_is_the_task_head(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)
    monkeypatch.setattr(talk_host.subprocess, "run", lambda *a, **k: _fake_completed(stdout="ok"))

    task = "audit " + "the site " * 40
    run_id = int(talk_host.host().run_agent(task).split("#", 1)[1].split(" ", 1)[0])

    assert talk_runs.get_run(run_id)["label"] == task.strip()[:60]


# --- tier 3: nothing available -----------------------------------------------


def test_no_agent_loop_and_no_binary_refuses_honestly(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: None)
    talk_host.bind_ctx(_StubCtx(_NO_PARENT))

    result = talk_host.host().run_agent("rebuild the index")

    assert "no Hermes agent attached" in result
    assert "no `hermes` command on the PATH" in result
    assert "WORK_STARTED" not in result


def test_binary_is_resolved_through_which(monkeypatch):
    monkeypatch.setattr(talk_host.shutil, "which", lambda name: f"C:/npm/{name}.cmd")

    # shutil.which, not a bare "hermes": on Windows the installed entry point
    # is an npm .cmd shim and CreateProcess does no PATHEXT resolution.
    assert talk_host.hermes_binary() == "C:/npm/hermes.cmd"


def test_agent_timeout_default_and_override(monkeypatch):
    monkeypatch.delenv("TALK_AGENT_TIMEOUT_S", raising=False)
    assert talk_config.agent_timeout_s() == talk_config.DEFAULT_AGENT_TIMEOUT_S

    monkeypatch.setenv("TALK_AGENT_TIMEOUT_S", " 90 ")
    assert talk_config.agent_timeout_s() == 90

    for junk in ("nonsense", "0", "-5", ""):
        monkeypatch.setenv("TALK_AGENT_TIMEOUT_S", junk)
        assert talk_config.agent_timeout_s() == talk_config.DEFAULT_AGENT_TIMEOUT_S
