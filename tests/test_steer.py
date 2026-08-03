"""``steer_run`` — redirecting a live child, and refusing when a lane can't.

The shape being proved: only the ATTACHED lane can carry a steer. The other
two are not failures to be swallowed, they are answers — a registry run says
which lane it is stuck in and offers the one thing that lane CAN do (stop it).

The registry bridge is exercised against a fake ``tools.delegate_tool`` module
rather than a Hermes install, so every branch runs on a bare checkout. That is
deliberate: the bridge reaches into host-private state, and the tests are what
say out loud which shape it depends on.
"""

from __future__ import annotations

import json
import sys
import threading
import types

import pytest

import talk_host
import talk_runs
import talk_tools

_UNKNOWN_STEER = json.dumps({"error": "unknown tool: steer_subagent"})
_NO_PARENT = json.dumps({"error": "delegate_task requires a parent agent context."})


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    yield
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()


class _StubCtx:
    """A plugin context whose dispatch returns (or raises) one canned value."""

    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    def dispatch_tool(self, tool_name, args, **kwargs):
        self.calls.append((tool_name, args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _FakeAgent:
    """Stands in for AIAgent — only ``steer`` is reached."""

    def __init__(self, accepted=True, raises=None):
        self.accepted = accepted
        self.raises = raises
        self.steered: list[str] = []

    def steer(self, text: str) -> bool:
        if self.raises is not None:
            raise self.raises
        self.steered.append(text)
        return self.accepted


def _install_registry(monkeypatch, registry: dict | None, *, with_lock: bool = True):
    """Inject a fake ``tools.delegate_tool`` carrying ``registry``.

    ``registry=None`` installs the module WITHOUT the attribute, which is the
    "host shape changed underneath us" branch.
    """

    module = types.ModuleType("tools.delegate_tool")
    if registry is not None:
        module._active_subagents = registry
        if with_lock:
            module._active_subagents_lock = threading.Lock()
    package = types.ModuleType("tools")
    package.delegate_tool = module
    monkeypatch.setitem(sys.modules, "tools", package)
    monkeypatch.setitem(sys.modules, "tools.delegate_tool", module)
    return module


def _finished_run() -> int:
    run_id = talk_runs.start_run("agent", "audit the auth module", lambda _rid: "done")
    deadline = 0
    while talk_runs.get_run(run_id)["status"] not in talk_runs.TERMINAL_STATUSES:
        deadline += 1
        if deadline > 500:  # pragma: no cover - worker thread never settled
            raise AssertionError("run never reached a terminal status")
        threading.Event().wait(0.01)
    return run_id


# -- argument guards ----------------------------------------------------------


def test_empty_text_is_refused_before_any_lane_is_touched():
    talk_host.bind_ctx(_StubCtx("should not be reached"))
    out = talk_host.host().steer_run("child-1", "   ")
    assert "need something to tell it" in out.lower()


def test_tool_layer_refuses_a_missing_target():
    out = talk_tools.execute_talk_tool("steer_run", {"text": "focus on pricing"})
    assert "which job" in out.lower()


def test_tool_layer_refuses_missing_text():
    out = talk_tools.execute_talk_tool("steer_run", {"target": "child-1"})
    assert "something to tell it" in out.lower()


# -- registry runs: the lanes that cannot steer -------------------------------


def test_an_api_server_run_names_its_lane_and_offers_stop(monkeypatch):
    hung = threading.Event()
    run_id = talk_runs.start_run("agent", "audit", lambda _rid: hung.wait(5) or "x")
    talk_runs.annotate_run(run_id, lane=talk_host.LANE_API_SERVER)
    try:
        out = talk_host.host().steer_run(str(run_id), "focus on pricing")
    finally:
        hung.set()
    assert "api server" in out.lower()
    assert "stop" in out.lower()


def test_a_detached_run_says_there_is_no_channel_to_reach_it():
    hung = threading.Event()
    run_id = talk_runs.start_run("agent", "audit", lambda _rid: hung.wait(5) or "x")
    try:
        out = talk_host.host().steer_run(str(run_id), "focus on pricing")
    finally:
        hung.set()
    assert "detached" in out.lower()
    assert "stop" in out.lower()


def test_a_finished_run_says_so_rather_than_naming_a_lane():
    run_id = _finished_run()
    out = talk_host.host().steer_run(str(run_id), "focus on pricing")
    assert "already finished" in out.lower()


def test_a_number_that_is_not_a_run_falls_through_to_the_subagent_path(monkeypatch):
    """An unknown NUMBER is not a registry run, so it is tried as an id."""

    _install_registry(monkeypatch, {})
    talk_host.bind_ctx(_StubCtx(_UNKNOWN_STEER))
    out = talk_host.host().steer_run("4321", "focus on pricing")
    assert "nothing is running" in out.lower()


# -- no host ------------------------------------------------------------------


def test_no_plugin_context_refuses_and_says_why():
    out = talk_host.host().steer_run("child-1", "focus on pricing")
    assert "outside a hermes agent" in out.lower()


# -- the host's own steer_subagent tool ---------------------------------------


def test_the_host_tool_is_preferred_when_it_exists():
    ctx = _StubCtx(json.dumps({"success": True, "result": "queued"}))
    talk_host.bind_ctx(ctx)
    out = talk_host.host().steer_run("child-1", "focus on pricing")
    assert ctx.calls == [
        ("steer_subagent", {"subagent_id": "child-1", "text": "focus on pricing"})
    ]
    assert "passed it along" in out.lower()


def test_a_raising_dispatch_is_spoken_not_routed_around(monkeypatch):
    _install_registry(monkeypatch, {"child-1": {"agent": _FakeAgent()}})
    talk_host.bind_ctx(_StubCtx(RuntimeError("registry offline")))
    out = talk_host.host().steer_run("child-1", "focus on pricing")
    assert "RuntimeError" in out
    # A real dispatch error is the host's decision — it must NOT silently
    # fall through to the bridge and steer the child anyway.
    assert "passed it along" not in out.lower()


def test_a_real_tool_error_is_spoken_not_treated_as_a_missing_tool():
    talk_host.bind_ctx(_StubCtx(json.dumps({"error": "delegation is paused"})))
    out = talk_host.host().steer_run("child-1", "focus on pricing")
    assert "paused" in out.lower()


# -- the registry bridge ------------------------------------------------------


def test_unknown_tool_falls_through_to_the_registry_and_steers(monkeypatch):
    agent = _FakeAgent(accepted=True)
    _install_registry(monkeypatch, {"child-1": {"agent": agent}})
    talk_host.bind_ctx(_StubCtx(_UNKNOWN_STEER))
    out = talk_host.host().steer_run("child-1", "focus on pricing")
    assert agent.steered == ["focus on pricing"]
    assert "passed it along" in out.lower()
    # Queued is not delivered — the reply must not promise the agent acted.
    assert "after its next step" in out.lower()


def test_the_bridge_works_without_the_lock_attribute(monkeypatch):
    agent = _FakeAgent(accepted=True)
    _install_registry(monkeypatch, {"child-1": {"agent": agent}}, with_lock=False)
    talk_host.bind_ctx(_StubCtx(_UNKNOWN_STEER))
    out = talk_host.host().steer_run("child-1", "focus on pricing")
    assert agent.steered == ["focus on pricing"]
    assert "passed it along" in out.lower()


def test_a_rejected_steer_says_the_child_is_probably_done(monkeypatch):
    agent = _FakeAgent(accepted=False)
    _install_registry(monkeypatch, {"child-1": {"agent": agent}})
    talk_host.bind_ctx(_StubCtx(_UNKNOWN_STEER))
    out = talk_host.host().steer_run("child-1", "focus on pricing")
    assert "didn't take it" in out.lower()
    assert "last tool call" in out.lower()


def test_a_raising_steer_is_spoken(monkeypatch):
    agent = _FakeAgent(raises=RuntimeError("child died"))
    _install_registry(monkeypatch, {"child-1": {"agent": agent}})
    talk_host.bind_ctx(_StubCtx(_UNKNOWN_STEER))
    out = talk_host.host().steer_run("child-1", "focus on pricing")
    assert "RuntimeError" in out


def test_an_unknown_id_lists_what_is_actually_running(monkeypatch):
    _install_registry(
        monkeypatch,
        {"child-1": {"agent": _FakeAgent()}, "child-2": {"agent": _FakeAgent()}},
    )
    talk_host.bind_ctx(_StubCtx(_UNKNOWN_STEER))
    out = talk_host.host().steer_run("child-9", "focus on pricing")
    assert "child-1" in out and "child-2" in out


def test_an_empty_registry_says_nothing_is_running(monkeypatch):
    _install_registry(monkeypatch, {})
    talk_host.bind_ctx(_StubCtx(_UNKNOWN_STEER))
    out = talk_host.host().steer_run("child-1", "focus on pricing")
    assert "nothing is running" in out.lower()


def test_a_record_with_no_live_agent_refuses(monkeypatch):
    _install_registry(monkeypatch, {"child-1": {"agent": None}})
    talk_host.bind_ctx(_StubCtx(_UNKNOWN_STEER))
    out = talk_host.host().steer_run("child-1", "focus on pricing")
    assert "no live agent" in out.lower()


def test_a_registry_of_the_wrong_shape_refuses_instead_of_raising(monkeypatch):
    _install_registry(monkeypatch, None)
    talk_host.bind_ctx(_StubCtx(_UNKNOWN_STEER))
    out = talk_host.host().steer_run("child-1", "focus on pricing")
    assert "isn't in the shape" in out.lower()


def test_no_importable_tools_package_refuses(monkeypatch):
    monkeypatch.setitem(sys.modules, "tools", None)
    monkeypatch.setitem(sys.modules, "tools.delegate_tool", None)
    talk_host.bind_ctx(_StubCtx(_UNKNOWN_STEER))
    out = talk_host.host().steer_run("child-1", "focus on pricing")
    assert "no" in out.lower() and "steer_subagent" in out


# -- the advertised surface ---------------------------------------------------


def test_steer_run_is_advertised_to_the_session():
    names = [tool["name"] for tool in talk_tools.default_talk_tools()]
    assert "steer_run" in names


def test_the_schema_requires_both_arguments():
    schema = next(
        tool for tool in talk_tools.default_talk_tools() if tool["name"] == "steer_run"
    )
    assert set(schema["parameters"]["required"]) == {"target", "text"}
    assert schema["parameters"]["additionalProperties"] is False


def test_the_description_tells_the_model_it_is_not_a_stop():
    schema = next(
        tool for tool in talk_tools.default_talk_tools() if tool["name"] == "steer_run"
    )
    assert "never cancels" in schema["description"].lower()
