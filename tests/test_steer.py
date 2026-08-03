"""steer_agent / list_agents / stop_work — the run-control surface.

The contract under test: a steer is QUEUED, never claimed delivered; every
lane that cannot steer refuses with the one thing it CAN do (a stop that is
now real); and the ladder prefers the host's public ``steer_subagent`` when
present, bridging to the registry only when it is not.

All host state is faked through ``sys.modules`` injection — no Hermes
install, no processes, no network.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

import talk_apiserver
import talk_host
import talk_runs
import talk_steer
import talk_tools


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    talk_steer.reset_for_tests()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    yield
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    talk_steer.reset_for_tests()


class _FakeAgent:
    def __init__(self, accepted=True, raises=None):
        self.accepted = accepted
        self.raises = raises
        self.steered: list[str] = []

    def steer(self, text: str) -> bool:
        if self.raises is not None:
            raise self.raises
        self.steered.append(text)
        return self.accepted


def _install_host(
    monkeypatch,
    registry: dict | None = None,
    *,
    steer_subagent=None,
    interrupt_subagent=None,
    list_children=None,
    absent: bool = False,
):
    """Inject a fake ``tools.delegate_tool``; ``absent=True`` removes it."""

    if absent:
        monkeypatch.setitem(sys.modules, "tools", None)
        monkeypatch.setitem(sys.modules, "tools.delegate_tool", None)
        return None
    module = types.ModuleType("tools.delegate_tool")
    if registry is not None:
        module._active_subagents = registry
        module._active_subagents_lock = threading.Lock()
    if steer_subagent is not None:
        module.steer_subagent = steer_subagent
    if interrupt_subagent is not None:
        module.interrupt_subagent = interrupt_subagent
    module.list_active_subagents = list_children or (lambda: [])
    package = types.ModuleType("tools")
    package.delegate_tool = module
    monkeypatch.setitem(sys.modules, "tools", package)
    monkeypatch.setitem(sys.modules, "tools.delegate_tool", module)
    return module


def _running_run(lane: str | None = None, **meta) -> tuple[int, threading.Event]:
    hung = threading.Event()
    run_id = talk_runs.start_run("agent", "audit", lambda _rid: hung.wait(5) or "x")
    if lane:
        talk_runs.annotate_run(run_id, lane=lane, **meta)
    return run_id, hung


# -- argument guards ----------------------------------------------------------


def test_empty_text_is_refused_before_any_lane():
    out = talk_host.host().steer_agent("sa-0-aaaa", "  ")
    assert "the note itself" in out.lower()


def test_tool_layer_requires_agent_id_and_text():
    assert "list_agents" in talk_tools.execute_talk_tool("steer_agent", {"text": "x"})
    assert "note itself" in talk_tools.execute_talk_tool(
        "steer_agent", {"agent_id": "sa-0-aaaa"}
    )


# -- run numbers: lanes that cannot steer -------------------------------------


def test_api_server_run_refuses_and_offers_a_real_stop():
    run_id, hung = _running_run(talk_host.LANE_API_SERVER)
    try:
        out = talk_host.host().steer_agent(str(run_id), "focus on pricing")
    finally:
        hung.set()
    assert "api server" in out.lower() and "stopping it" in out.lower()


def test_detached_run_refuses_and_offers_a_real_stop():
    run_id, hung = _running_run()
    try:
        out = talk_host.host().steer_agent(str(run_id), "focus on pricing")
    finally:
        hung.set()
    assert "detached" in out.lower() and "stopping it" in out.lower()


def test_finished_run_says_finished():
    run_id = talk_runs.start_run("agent", "audit", lambda _rid: "done")
    deadline = 0
    while talk_runs.get_run(run_id)["status"] not in talk_runs.TERMINAL_STATUSES:
        deadline += 1
        assert deadline < 500
        threading.Event().wait(0.01)
    out = talk_host.host().steer_agent(str(run_id), "x")
    assert "already finished" in out.lower()


# -- the ladder ---------------------------------------------------------------


def test_host_steer_subagent_is_preferred_and_queues(monkeypatch):
    calls = []

    def steer_subagent(sid, text):
        calls.append((sid, text))
        return True

    _install_host(monkeypatch, steer_subagent=steer_subagent)
    out = talk_host.host().steer_agent("sa-0-aaaa", "focus on pricing")
    assert calls == [("sa-0-aaaa", "focus on pricing")]
    assert "queued for their next step" in out.lower()
    assert "landed" not in out.split("—")[0].lower()  # call-time claim is queue-only


def test_host_steer_subagent_false_means_unknown_job(monkeypatch):
    _install_host(monkeypatch, steer_subagent=lambda sid, text: False)
    out = talk_host.host().steer_agent("sa-0-gone", "x")
    # False is ambiguous on the host side (finished OR refused) — the reply
    # must say both, not invent one diagnosis.
    assert "already" in out.lower() and "refused" in out.lower()
    assert "list what's running" in out.lower()


def test_host_steer_subagent_raising_is_spoken(monkeypatch):
    def boom(sid, text):
        raise RuntimeError("registry poisoned")

    _install_host(monkeypatch, steer_subagent=boom)
    out = talk_host.host().steer_agent("sa-0-aaaa", "x")
    assert "RuntimeError" in out


def test_no_host_module_refuses_with_capability_wording(monkeypatch):
    _install_host(monkeypatch, absent=True)
    out = talk_host.host().steer_agent("sa-0-aaaa", "x")
    assert "doesn't let me redirect" in out.lower()


# -- the registry bridge (host predates steer_subagent) -----------------------


def test_bridge_steers_and_records_a_receipt(monkeypatch):
    agent = _FakeAgent()
    _install_host(monkeypatch, {"sa-0-aaaa": {"agent": agent}})
    out = talk_host.host().steer_agent("sa-0-aaaa", "focus on pricing")
    assert agent.steered == ["focus on pricing"]
    assert "queued for their next step" in out.lower()
    assert "sa-0-aaaa" in talk_steer.notes_summary()


def test_bridge_unknown_id_lists_the_live_ones(monkeypatch):
    _install_host(
        monkeypatch,
        {"sa-0-aaaa": {"agent": _FakeAgent()}, "sa-1-bbbb": {"agent": _FakeAgent()}},
    )
    out = talk_host.host().steer_agent("sa-9-zzzz", "x")
    assert "sa-0-aaaa" in out and "sa-1-bbbb" in out


def test_bridge_empty_registry_says_nothing_running(monkeypatch):
    _install_host(monkeypatch, {})
    out = talk_host.host().steer_agent("sa-0-aaaa", "x")
    assert "nothing is running" in out.lower()


def test_bridge_dead_record_refuses(monkeypatch):
    _install_host(monkeypatch, {"sa-0-aaaa": {"agent": None}})
    out = talk_host.host().steer_agent("sa-0-aaaa", "x")
    assert "no live agent" in out.lower()


def test_bridge_false_return_is_a_contract_error_not_a_diagnosis(monkeypatch):
    # AIAgent.steer() returns False only for empty text, which the guard
    # already rejected — so the old "past its last tool call" diagnosis was
    # unreachable fiction. The wording must not resurrect it.
    agent = _FakeAgent(accepted=False)
    _install_host(monkeypatch, {"sa-0-aaaa": {"agent": agent}})
    out = talk_host.host().steer_agent("sa-0-aaaa", "x")
    assert "didn't go through" in out.lower()
    assert "last tool call" not in out.lower()


def test_bridge_raising_steer_is_spoken(monkeypatch):
    agent = _FakeAgent(raises=RuntimeError("child died"))
    _install_host(monkeypatch, {"sa-0-aaaa": {"agent": agent}})
    out = talk_host.host().steer_agent("sa-0-aaaa", "x")
    assert "RuntimeError" in out


def test_bridge_wrong_registry_shape_refuses(monkeypatch):
    module = _install_host(monkeypatch, {})
    del module._active_subagents
    out = talk_host.host().steer_agent("sa-0-aaaa", "x")
    assert "isn't in the shape" in out.lower()


# -- stop_work ----------------------------------------------------------------


def test_stop_api_server_run_uses_the_remote_id(monkeypatch):
    stopped = []
    monkeypatch.setattr(talk_apiserver, "stop_run", lambda rid: stopped.append(rid))
    run_id, hung = _running_run(talk_host.LANE_API_SERVER, api_run_id="run-777")
    try:
        out = talk_host.host().stop_work(str(run_id))
    finally:
        hung.set()
    assert stopped == ["run-777"]
    # A 2xx is an accepted REQUEST — the wording must not claim completion.
    assert "sent the stop" in out.lower()
    assert not out.lower().startswith("stopped")


def test_stop_api_server_run_without_remote_id_refuses(monkeypatch):
    monkeypatch.setattr(
        talk_apiserver,
        "stop_run",
        lambda rid: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    run_id, hung = _running_run(talk_host.LANE_API_SERVER)
    try:
        out = talk_host.host().stop_work(str(run_id))
    finally:
        hung.set()
    assert "never told me its run id" in out.lower()


def test_stop_detached_run_terminates_the_retained_handle():
    run_id, hung = _running_run()

    class _Proc:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    proc = _Proc()
    talk_runs.register_process(run_id, proc)
    try:
        out = talk_host.host().stop_work(str(run_id))
    finally:
        hung.set()
    assert proc.terminated is True
    assert "sent the stop" in out.lower()


def test_stop_detached_run_without_handle_refuses():
    run_id, hung = _running_run()
    try:
        out = talk_host.host().stop_work(str(run_id))
    finally:
        hung.set()
    assert "don't hold a handle" in out.lower()


def test_stop_subagent_interrupts_and_supersedes_its_notes(monkeypatch):
    _install_host(monkeypatch, interrupt_subagent=lambda sid: True)
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")
    out = talk_host.host().stop_work("sa-0-aaaa")
    # interrupt_subagent REQUESTS the stop — "asked ... to stop", never done.
    assert "asked sa-0-aaaa to stop" in out.lower()
    # A hard interrupt drops pending steer text (clear_interrupt) — the
    # ledger must not keep claiming the note is queued.
    assert "the note may not have been read" in talk_steer.notes_summary()


def test_stop_unknown_subagent_says_so(monkeypatch):
    _install_host(monkeypatch, interrupt_subagent=lambda sid: False)
    out = talk_host.host().stop_work("sa-9-zzzz")
    assert "don't see a running job" in out.lower()


def test_stop_without_host_module_refuses(monkeypatch):
    _install_host(monkeypatch, absent=True)
    out = talk_host.host().stop_work("sa-0-aaaa")
    assert "doesn't let me stop" in out.lower()


# -- list_agents --------------------------------------------------------------


def test_list_agents_merges_children_and_runs_with_capability_tags(monkeypatch):
    _install_host(
        monkeypatch,
        list_children=lambda: [
            {
                "subagent_id": "sa-0-aaaa",
                "goal": "audit the auth module",
                "started_at": 0,
                "last_tool": "read_file",
            }
        ],
    )
    run_id, hung = _running_run(talk_host.LANE_API_SERVER)
    try:
        out = talk_host.host().list_agents()
    finally:
        hung.set()
    assert "sa-0-aaaa" in out and "can steer" in out
    assert f"run {run_id}" in out and "stop only" in out


def test_list_agents_empty_everywhere(monkeypatch):
    _install_host(monkeypatch, absent=True)
    out = talk_host.host().list_agents()
    assert "nothing is running" in out.lower()


# -- the advertised surface ---------------------------------------------------


def test_the_three_tools_are_advertised():
    names = [tool["name"] for tool in talk_tools.default_talk_tools()]
    for name in ("list_agents", "steer_agent", "stop_work"):
        assert name in names
    assert "steer_run" not in names


def test_steer_agent_description_forbids_delivery_claims():
    schema = next(
        t for t in talk_tools.default_talk_tools() if t["name"] == "steer_agent"
    )
    text = schema["description"].lower()
    assert "queued, not delivered" in text
    assert "never cancels" in text
