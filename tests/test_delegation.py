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
import sys
import time

import pytest

import talk_config
import talk_host
import talk_runs

_NO_PARENT = json.dumps({"error": "delegate_task requires a parent agent context."})
_FAKE_HERMES = "/usr/local/bin/hermes"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    monkeypatch.delenv("TALK_AGENT_TIMEOUT_S", raising=False)
    monkeypatch.delenv("TALK_AGENT_PROFILE", raising=False)
    # Point HERMES_HOME at an EMPTY tmp dir and block the host resolver: no
    # test may read the operator's real ~/.hermes, and profile auto-detection
    # must never see their actual profiles.
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    yield
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()


def _write_home(root, *, root_model: bool, profiles: dict[str, bool]) -> None:
    """Build a Hermes home: root config, and profiles with/without a model."""

    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(
        "_config_version: 31\n"
        + ("model:\n  provider: openai-codex\n  default: gpt-5.6\n" if root_model else "")
        + "agent:\n  max_turns: 60\n",
        encoding="utf-8",
    )
    for name, has_model in profiles.items():
        directory = root / "profiles" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "config.yaml").write_text(
            ("model:\n  provider: openai-codex\n  default: gpt-5.5\n" if has_model else "")
            + "toolsets:\n  - hermes-cli\n",
            encoding="utf-8",
        )


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


class _FakePopen:
    """Popen-shaped: the worker retains the handle and calls communicate()."""

    def __init__(self, stdout="", stderr="", returncode=0, raises=None):
        self._out, self._err, self._raises = stdout, stderr, raises
        self.returncode = returncode
        self.killed = False

    def communicate(self, timeout=None):
        if self._raises is not None and not self.killed:
            raise self._raises
        return self._out, self._err

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self._raises = None
        self.returncode = -9

    def terminate(self):
        self.kill()


def _fake_completed(stdout="", stderr="", returncode=0):
    return _FakePopen(stdout=stdout, stderr=stderr, returncode=returncode)


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

    monkeypatch.setattr(talk_host.subprocess, "Popen", fake_run)
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
        proc = _fake_completed(stdout="ok")
        original = proc.communicate

        def communicate(timeout=None):
            # The budget moved from run(timeout=) to communicate(timeout=)
            # when the worker switched to a retained Popen handle.
            seen["timeout"] = timeout
            return original()

        proc.communicate = communicate
        return proc

    monkeypatch.setattr(talk_host.subprocess, "Popen", fake_run)

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
        talk_host.subprocess, "Popen", lambda *a, **k: _fake_completed(stdout="done")
    )

    assert "WORK_STARTED #" in talk_host.host().run_agent("go")


def test_a_nonzero_exit_lands_as_a_failed_run_with_the_reason(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)
    monkeypatch.setattr(
        talk_host.subprocess,
        "Popen",
        lambda *a, **k: _fake_completed(stderr="no provider configured", returncode=2),
    )

    run_id = int(talk_host.host().run_agent("go").split("#", 1)[1].split(" ", 1)[0])
    run = _wait_terminal(run_id)

    # `failed`, so check_work and the watcher both report it as such — and
    # the message is plain speakable text, with no exception type in front.
    assert run["status"] == "failed"
    assert run["output"].startswith("the agent exited 2")
    assert "no provider configured" in run["output"]


def test_a_timeout_lands_as_a_failed_run(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="hermes", timeout=1)

    monkeypatch.setattr(talk_host.subprocess, "Popen", timeout)

    run_id = int(talk_host.host().run_agent("go").split("#", 1)[1].split(" ", 1)[0])
    run = _wait_terminal(run_id)

    assert run["status"] == "failed"
    assert "TimeoutExpired" in run["output"]


def test_silent_child_still_says_something(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)
    monkeypatch.setattr(talk_host.subprocess, "Popen", lambda *a, **k: _fake_completed(stdout="  "))

    run_id = int(talk_host.host().run_agent("go").split("#", 1)[1].split(" ", 1)[0])

    assert "without printing anything" in _wait_terminal(run_id)["output"]


def test_output_is_capped(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)
    monkeypatch.setattr(
        talk_host.subprocess, "Popen", lambda *a, **k: _fake_completed(stdout="x" * 99_999)
    )

    run_id = int(talk_host.host().run_agent("go").split("#", 1)[1].split(" ", 1)[0])

    assert len(_wait_terminal(run_id)["output"]) == talk_runs.HISTORY_OUTPUT_CAP


def test_label_is_the_task_head(monkeypatch):
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)
    monkeypatch.setattr(talk_host.subprocess, "Popen", lambda *a, **k: _fake_completed(stdout="ok"))

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


# --- profile resolution: the flag that decides whether the child can run ----


def test_root_model_means_no_flag(tmp_path):
    _write_home(tmp_path / "home", root_model=True, profiles={"alpha": True})

    assert talk_config.detect_agent_profile() is None
    assert talk_config.agent_profile() is None


def test_root_without_a_model_and_one_usable_profile_is_detected(tmp_path):
    _write_home(tmp_path / "home", root_model=False, profiles={"devbox": True})

    assert talk_config.detect_agent_profile() == "devbox"


def test_two_usable_profiles_refuse_to_guess(tmp_path):
    _write_home(tmp_path / "home", root_model=False, profiles={"alpha": True, "beta": True})

    # Picking one would be invisible until the WRONG agent had already run.
    assert talk_config.detect_agent_profile() is None


def test_no_usable_profile_adds_no_flag(tmp_path):
    _write_home(tmp_path / "home", root_model=False, profiles={"alpha": False})

    assert talk_config.detect_agent_profile() is None


def test_profiles_without_a_model_are_not_candidates(tmp_path):
    _write_home(tmp_path / "home", root_model=False, profiles={"alpha": False, "beta": True})

    assert talk_config.detect_agent_profile() == "beta"


def test_missing_home_or_profiles_dir_is_not_an_error(tmp_path):
    assert talk_config.detect_agent_profile() is None  # nothing written at all

    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    assert talk_config.detect_agent_profile() is None


def test_a_commented_out_model_block_does_not_count(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "# model:\n#   default: gpt-5.6\nplugins:\n  enabled: []\n", encoding="utf-8"
    )
    (home / "profiles" / "alpha").mkdir(parents=True)
    (home / "profiles" / "alpha" / "config.yaml").write_text(
        "model:\n  # default: commented\n  default: gpt-5.5\n", encoding="utf-8"
    )

    assert talk_config.detect_agent_profile() == "alpha"


def test_an_empty_model_block_does_not_count(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    # `model:` present but with no default is exactly the shape that produces
    # "Invalid length for parameter modelId, value: 0".
    (home / "config.yaml").write_text("model:\nagent:\n  max_turns: 60\n", encoding="utf-8")
    (home / "profiles" / "alpha").mkdir(parents=True)
    (home / "profiles" / "alpha" / "config.yaml").write_text(
        "model:\n  provider: openai-codex\n  default: gpt-5.5\n", encoding="utf-8"
    )

    assert talk_config.detect_agent_profile() == "alpha"


def test_inline_model_mapping_counts(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("model: {default: gpt-5.6}\n", encoding="utf-8")

    assert talk_config.detect_agent_profile() is None


def test_the_knob_overrides_detection(monkeypatch, tmp_path):
    _write_home(tmp_path / "home", root_model=True, profiles={})
    monkeypatch.setenv("TALK_AGENT_PROFILE", "  chosen  ")

    assert talk_config.agent_profile() == "chosen"


def test_a_blank_knob_is_an_explicit_opt_out(monkeypatch, tmp_path):
    _write_home(tmp_path / "home", root_model=False, profiles={"alpha": True})
    monkeypatch.setenv("TALK_AGENT_PROFILE", "   ")

    # Detection WOULD have found alpha; the operator said no flag.
    assert talk_config.detect_agent_profile() == "alpha"
    assert talk_config.agent_profile() is None


# --- the argv the child actually gets ----------------------------------------


def test_argv_without_a_profile():
    assert talk_host.agent_argv("/bin/hermes", "do it", None) == ["/bin/hermes", "-z", "do it"]


def test_argv_puts_the_global_profile_flag_before_oneshot():
    argv = talk_host.agent_argv("/bin/hermes", "do it", "devbox")

    assert argv == ["/bin/hermes", "--profile", "devbox", "-z", "do it"]


def test_a_detected_profile_reaches_the_spawn(monkeypatch, tmp_path):
    _write_home(tmp_path / "home", root_model=False, profiles={"devbox": True})
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _fake_completed(stdout="ok")

    monkeypatch.setattr(talk_host.subprocess, "Popen", fake_run)

    run_id = int(talk_host.host().run_agent("go").split("#", 1)[1].split(" ", 1)[0])
    _wait_terminal(run_id)

    assert seen["argv"] == [_FAKE_HERMES, "--profile", "devbox", "-z", "go"]
    # Recorded on the run, so check_work can say which agent actually ran.
    assert talk_runs.get_run(run_id)["meta"]["profile"] == "devbox"


def test_no_profile_needed_means_a_bare_spawn(monkeypatch, tmp_path):
    _write_home(tmp_path / "home", root_model=True, profiles={"devbox": True})
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: _FAKE_HERMES)
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _fake_completed(stdout="ok")

    monkeypatch.setattr(talk_host.subprocess, "Popen", fake_run)

    run_id = int(talk_host.host().run_agent("go").split("#", 1)[1].split(" ", 1)[0])
    _wait_terminal(run_id)

    assert seen["argv"] == [_FAKE_HERMES, "-z", "go"]
    assert talk_runs.get_run(run_id)["meta"]["profile"] is None


def test_agent_timeout_default_and_override(monkeypatch):
    monkeypatch.delenv("TALK_AGENT_TIMEOUT_S", raising=False)
    assert talk_config.agent_timeout_s() == talk_config.DEFAULT_AGENT_TIMEOUT_S

    monkeypatch.setenv("TALK_AGENT_TIMEOUT_S", " 90 ")
    assert talk_config.agent_timeout_s() == 90

    for junk in ("nonsense", "0", "-5", ""):
        monkeypatch.setenv("TALK_AGENT_TIMEOUT_S", junk)
        assert talk_config.agent_timeout_s() == talk_config.DEFAULT_AGENT_TIMEOUT_S
