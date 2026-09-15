"""steer_agent / list_agents / stop_work — the run-control surface.

The contract under test: a steer is QUEUED, never claimed delivered; every
lane that cannot steer refuses with the one thing it CAN do (a stop that is
now real); and the ladder prefers the host's public ``steer_subagent`` when
present, bridging to the registry only when it is not.

All host state is faked through ``sys.modules`` injection — no Hermes
install, no processes, no network.
"""

from __future__ import annotations

import re
import sys
import threading
import time
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
    # Runs are refused without a bound return route (hermes-talk#35), so the
    # suite attaches one. Tests that assert the REFUSAL detach it explicitly.
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
    # The wire text leads with the correlation token (hermes-talk#1); the
    # note itself rides behind it, verbatim.
    assert len(calls) == 1 and calls[0][0] == "sa-0-aaaa"
    assert re.fullmatch(r"\[tk-[0-9a-f]{8}\] focus on pricing", calls[0][1])
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
    assert len(agent.steered) == 1
    assert re.fullmatch(r"\[tk-[0-9a-f]{8}\] focus on pricing", agent.steered[0])
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


def _poll_stop_receipt(run_id: int, expected_fragment: str, timeout: float = 3.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = talk_runs.get_run(run_id)
        meta = run.get("meta") if isinstance(run.get("meta"), dict) else {}
        result = meta.get("stop_result")
        if result and expected_fragment in result:
            return result
        threading.Event().wait(0.05)
    raise AssertionError(f"stop receipt never carried {expected_fragment!r}")


def test_stop_api_run_fast_path_annotates_the_receipt(monkeypatch):
    monkeypatch.setattr(talk_apiserver, "stop_run", lambda rid: None)
    run_id, hung = _running_run(talk_host.LANE_API_SERVER, api_run_id="run-777")
    try:
        out = talk_host.host().stop_work(str(run_id))
    finally:
        hung.set()
    assert "sent the stop" in out.lower()
    assert _poll_stop_receipt(run_id, "accepted") == "accepted"


def test_stop_api_run_slow_server_detaches_then_receipts(monkeypatch):
    # hermes-talk#2: the POST must not dead-air the voice loop. A server
    # slower than the courtesy wait gets honest detached wording, and the
    # receipt lands in the run's meta when the answer finally arrives.
    release = threading.Event()

    def slow_stop(_rid):
        release.wait(5)

    monkeypatch.setattr(talk_apiserver, "stop_run", slow_stop)
    monkeypatch.setattr(talk_host, "STOP_CONFIRM_WAIT_S", 0.05)
    run_id, hung = _running_run(talk_host.LANE_API_SERVER, api_run_id="run-777")
    try:
        out = talk_host.host().stop_work(str(run_id))
        assert "hasn't answered yet" in out.lower()
        release.set()
        assert _poll_stop_receipt(run_id, "accepted") == "accepted"
    finally:
        release.set()
        hung.set()


def test_stop_api_run_error_within_the_window_is_spoken(monkeypatch):
    def failing(_rid):
        raise talk_apiserver.TalkApiServerError("boom")

    monkeypatch.setattr(talk_apiserver, "stop_run", failing)
    run_id, hung = _running_run(talk_host.LANE_API_SERVER, api_run_id="run-777")
    try:
        out = talk_host.host().stop_work(str(run_id))
    finally:
        hung.set()
    assert "didn't go through" in out.lower()
    assert "boom" in out


class _DyingProc:
    """A child that dies (exit 0) as soon as it is terminated."""

    def __init__(self):
        self._code = None

    def poll(self):
        return self._code

    def terminate(self):
        self._code = 0


def test_stop_detached_confirms_the_death_within_budget():
    run_id, hung = _running_run()
    talk_runs.register_process(run_id, _DyingProc())
    try:
        out = talk_host.host().stop_work(str(run_id))
    finally:
        hung.set()
    # terminate() is a signal — but the bounded wait SAW it die, so the
    # spoken claim is allowed to be the outcome, with the exit code on file.
    assert "it's down" in out.lower()
    assert _poll_stop_receipt(run_id, "exited 0") == "exited 0"


def test_stop_detached_undying_child_promises_the_receipt(monkeypatch):
    monkeypatch.setattr(talk_host, "STOP_CONFIRM_WAIT_S", 0.05)
    monkeypatch.setattr(talk_host, "STOP_LATE_CONFIRM_S", 0.05)

    class _Undying:
        def poll(self):
            return None

        def terminate(self):
            pass

    run_id, hung = _running_run()
    talk_runs.register_process(run_id, _Undying())
    try:
        out = talk_host.host().stop_work(str(run_id))
        assert "death receipt" in out.lower()
        # The run is STILL running when the late budget expires, so honest
        # uncertainty is the only truthful receipt (the terminal-record
        # fallback is covered separately).
        assert "never confirmed dead" in _poll_stop_receipt(run_id, "never confirmed dead")
    finally:
        hung.set()


def test_stop_detached_survives_the_worker_reaping_first():
    # Codex v0.6.1 finding 2 repro: the run worker reaps the child and
    # RELEASES the registry handle between terminate and confirm. The
    # captured handle still answers poll(), so the receipt is the truth
    # ("exited 0"), never "never confirmed dead" for a child that died.
    run_id, hung = _running_run()

    class _ReapedProc:
        def __init__(self):
            self._code = None
            self._polls = 0

        def terminate(self):
            self._code = 0

        def poll(self):
            self._polls += 1
            if self._polls == 1:
                talk_runs.release_process(run_id)  # the worker got there first
            return self._code

    talk_runs.register_process(run_id, _ReapedProc())
    try:
        out = talk_host.host().stop_work(str(run_id))
    finally:
        hung.set()
    assert "it's down" in out.lower()
    assert _poll_stop_receipt(run_id, "exited 0") == "exited 0"


def test_stop_confirm_consults_the_run_record_before_claiming_uncertainty(monkeypatch):
    # The handle never shows an exit code, but the RUN finishes anyway —
    # the late confirm must report the terminal record, not uncertainty.
    monkeypatch.setattr(talk_host, "STOP_CONFIRM_WAIT_S", 0.05)
    monkeypatch.setattr(talk_host, "STOP_LATE_CONFIRM_S", 0.3)

    class _Undying:
        def poll(self):
            return None

        def terminate(self):
            pass

    run_id, hung = _running_run()
    talk_runs.register_process(run_id, _Undying())
    try:
        out = talk_host.host().stop_work(str(run_id))
        assert "death receipt" in out.lower()
    finally:
        hung.set()  # the worker finishes; the run flips terminal
    assert "run finished as done" in _poll_stop_receipt(run_id, "run finished as")


def test_check_work_speaks_the_stop_receipt(monkeypatch):
    run_id, hung = _running_run()
    try:
        talk_runs.annotate_run(run_id, stop_result="accepted")
        _install_host(monkeypatch, {})
        out = talk_tools.execute_talk_tool("check_work", {})
    finally:
        hung.set()
    assert "stop receipt: accepted" in out


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


# -- redirect_agent -----------------------------------------------------------


class _FakeRedirectAgent(_FakeAgent):
    """A 0.20-shaped child: public ``redirect()`` beside ``steer()``."""

    def __init__(
        self,
        *,
        redirect_accepted=True,
        executing_tools=False,
        redirect_raises=None,
        steer_accepted=True,
    ):
        super().__init__(accepted=steer_accepted)
        self.redirect_accepted = redirect_accepted
        self.redirect_raises = redirect_raises
        self._executing_tools = executing_tools
        self.redirected: list[str] = []

    def redirect(self, text: str) -> bool:
        if self.redirect_raises is not None:
            raise self.redirect_raises
        self.redirected.append(text)
        if self.redirect_accepted and not self._executing_tools:
            self._pending_redirect = text
        return self.redirect_accepted


def test_redirect_empty_text_is_refused():
    out = talk_host.host().redirect_agent("sa-0-aaaa", "  ")
    assert "the correction itself" in out.lower()


def test_redirect_run_number_refuses_with_lane_wording():
    run_id, hung = _running_run(talk_host.LANE_API_SERVER)
    try:
        out = talk_host.host().redirect_agent(str(run_id), "wrong repo")
    finally:
        hung.set()
    assert "api server" in out.lower()


def test_redirect_without_host_module_refuses(monkeypatch):
    _install_host(monkeypatch, absent=True)
    out = talk_host.host().redirect_agent("sa-0-aaaa", "wrong repo")
    assert "doesn't let me redirect" in out.lower()


def test_redirect_accepted_mid_thought_claims_accepted_never_the_abort(monkeypatch):
    agent = _FakeRedirectAgent()
    _install_host(monkeypatch, {"sa-0-aaaa": {"agent": agent}})
    out = talk_host.host().redirect_agent("sa-0-aaaa", "wrong repo, use taskchad-ship")
    assert "redirect accepted" in out.lower()
    # The fake exposes the same post-call redirect-slot artifact as the host;
    # even then the sentence never claims that already completed work vanished.
    assert "current step, or its very next one" in out.lower()
    assert "drops what it was doing" not in out.lower()
    # The wire text leads with the correlation token — both verbs share it.
    assert len(agent.redirected) == 1
    assert re.fullmatch(
        r"\[tk-[0-9a-f]{8}\] wrong repo, use taskchad-ship", agent.redirected[0]
    )
    assert "redirect accepted" in talk_steer.notes_summary()


def test_redirect_mid_tool_speaks_queued_not_redirected(monkeypatch):
    # The host degrades a mid-tool redirect to steer() — the truthful claim
    # is queued-language, upgraded later by the drain artifacts.
    agent = _FakeRedirectAgent(executing_tools=True)
    _install_host(monkeypatch, {"sa-0-aaaa": {"agent": agent}})
    out = talk_host.host().redirect_agent("sa-0-aaaa", "wrong repo")
    assert "mid-tool" in out.lower()
    assert "queued" in talk_steer.notes_summary()
    assert "redirect accepted" not in talk_steer.notes_summary()


def test_redirect_that_races_into_tools_is_queued_and_can_be_superseded(monkeypatch):
    class _RacingToolAgent(_FakeRedirectAgent):
        def redirect(self, text: str) -> bool:
            # The adapter's pre-call peek saw False; the host crosses into a
            # tool before redirect() chooses its mechanism and queues instead.
            self._executing_tools = True
            self._pending_steer = text
            self.redirected.append(text)
            return True

    agent = _RacingToolAgent()
    _install_host(monkeypatch, {"sa-0-aaaa": {"agent": agent}})

    out = talk_host.host().redirect_agent("sa-0-aaaa", "wrong repo")
    talk_steer.mark_superseded("sa-0-aaaa")

    assert "queued" in out.lower()
    summary = talk_steer.notes_summary()
    assert "stopped — the note may not have been read" in summary
    assert "redirect accepted" not in summary


def test_redirect_false_falls_back_to_the_steer_queue(monkeypatch):
    agent = _FakeRedirectAgent(redirect_accepted=False)
    _install_host(monkeypatch, {"sa-0-aaaa": {"agent": agent}})
    out = talk_host.host().redirect_agent("sa-0-aaaa", "wrong repo")
    assert "queued the correction as a note" in out.lower()
    assert len(agent.steered) == 1  # the fallback reused the SAME wire text
    assert "queued" in talk_steer.notes_summary()


def test_redirect_false_with_a_dead_steer_says_finished(monkeypatch):
    agent = _FakeRedirectAgent(redirect_accepted=False, steer_accepted=False)
    _install_host(monkeypatch, {"sa-0-aaaa": {"agent": agent}})
    out = talk_host.host().redirect_agent("sa-0-aaaa", "wrong repo")
    assert "may have just finished" in out.lower()
    assert talk_steer.notes_summary() == ""  # no claim without an artifact


def test_redirect_raising_is_spoken(monkeypatch):
    agent = _FakeRedirectAgent(redirect_raises=RuntimeError("child died"))
    _install_host(monkeypatch, {"sa-0-aaaa": {"agent": agent}})
    out = talk_host.host().redirect_agent("sa-0-aaaa", "wrong repo")
    assert "RuntimeError" in out


def test_redirect_unknown_id_lists_the_live_ones(monkeypatch):
    _install_host(
        monkeypatch,
        {"sa-0-aaaa": {"agent": _FakeRedirectAgent()}},
    )
    out = talk_host.host().redirect_agent("sa-9-zzzz", "x")
    assert "sa-0-aaaa" in out


def test_redirect_empty_registry_says_nothing_running(monkeypatch):
    _install_host(monkeypatch, {})
    out = talk_host.host().redirect_agent("sa-0-aaaa", "x")
    assert "nothing is running" in out.lower()


def test_redirect_dead_record_refuses(monkeypatch):
    _install_host(monkeypatch, {"sa-0-aaaa": {"agent": None}})
    out = talk_host.host().redirect_agent("sa-0-aaaa", "x")
    assert "no live agent" in out.lower()


def test_redirect_wrong_registry_shape_refuses(monkeypatch):
    module = _install_host(monkeypatch, {})
    del module._active_subagents
    out = talk_host.host().redirect_agent("sa-0-aaaa", "x")
    assert "isn't in the shape" in out.lower()


def test_redirect_on_a_pre_020_host_degrades_to_steer(monkeypatch):
    # A registry child WITHOUT redirect() — the correction still travels,
    # through the steer queue, and the reply says queued (never redirected).
    agent = _FakeAgent()
    _install_host(monkeypatch, {"sa-0-aaaa": {"agent": agent}})
    out = talk_host.host().redirect_agent("sa-0-aaaa", "wrong repo")
    assert "queued for their next step" in out.lower()
    assert len(agent.steered) == 1


def test_redirect_tool_layer_requires_agent_id_and_text():
    assert "list_agents" in talk_tools.execute_talk_tool("redirect_agent", {"text": "x"})
    assert "correction itself" in talk_tools.execute_talk_tool(
        "redirect_agent", {"agent_id": "sa-0-aaaa"}
    )


def test_redirect_description_scopes_the_verb():
    schema = next(
        t for t in talk_tools.default_talk_tools() if t["name"] == "redirect_agent"
    )
    text = schema["description"].lower()
    assert "stronger than steer_agent" in text
    assert "never cancels" in text


# -- the advertised surface ---------------------------------------------------


def test_the_three_tools_are_advertised():
    names = [tool["name"] for tool in talk_tools.default_talk_tools()]
    for name in ("list_agents", "steer_agent", "redirect_agent", "stop_work"):
        assert name in names
    assert "steer_run" not in names


def test_steer_agent_description_forbids_delivery_claims():
    schema = next(
        t for t in talk_tools.default_talk_tools() if t["name"] == "steer_agent"
    )
    text = schema["description"].lower()
    assert "queued, not delivered" in text
    assert "never cancels" in text
