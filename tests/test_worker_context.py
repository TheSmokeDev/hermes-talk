"""Portable context-boundary regressions: no Hermes, network, or credentials.

Dispatch is deferred until the caller changes scope. Each captured target then
runs on a real, initially unscoped thread, including nested approval workers.
Cache characterization deliberately records the limits of context propagation.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

import pytest

import talk_apiserver
import talk_approvals
import talk_capabilities
import talk_host
import talk_runs
import talk_transcript

SCOPE = ContextVar("synthetic_profile", default=None)
MODULES = (
    talk_host, talk_runs, talk_transcript, talk_approvals,
    talk_capabilities, talk_apiserver,
)


@pytest.fixture
def dispatch(monkeypatch):
    """Delay starts without replacing the thread implementation process-wide."""
    queued = []

    class DeferredThread:
        def __init__(self, *, target, args=(), name=None, daemon=None):
            self.target, self.args = target, args
            self.name, self.daemon = name, daemon
            self.cancelled = False

        def start(self):
            queued.append(self)

        def cancel(self):
            self.cancelled = True

    class DeferredTimer(DeferredThread):
        def __init__(self, interval, function, args=()):
            super().__init__(target=function, args=args)

    def drain(*, even_cancelled=False):
        errors = []
        while queued:
            job = queued.pop(0)
            assert job.daemon is True
            if job.cancelled and not even_cancelled:
                continue

            def invoke(job=job):
                try:
                    job.target(*job.args)
                except BaseException as exc:  # noqa: BLE001 - surface thread failures on caller
                    errors.append(exc)

            thread = threading.Thread(target=invoke)
            thread.start()
            thread.join(5)
            assert not thread.is_alive()
        assert errors == []

    for module in MODULES:
        monkeypatch.setattr(module, "threading", SimpleNamespace(
            **{**vars(threading), "Thread": DeferredThread, "Timer": DeferredTimer},
        ))
    for module in (talk_runs, talk_approvals, talk_capabilities, talk_apiserver):
        module.reset_for_tests()
    talk_runs.attach_owner(
        talk_session_id="context-test", generation_id="generation-test",
        hermes_session_id="session-test", operator="test", profile=None,
    )
    token = SCOPE.set(None)
    try:
        yield queued, drain
    finally:
        drain()
        SCOPE.reset(token)
        for module in (talk_runs, talk_approvals, talk_capabilities, talk_apiserver):
            module.reset_for_tests()


PATHS = (
    "host", "runs", "transcript", "approval-worker", "approval-sse",
    "approval-timer", "approval-reopen-timer", "catalog", "api",
)


@pytest.mark.parametrize("path", PATHS)
def test_each_dispatch_keeps_its_own_scope_after_caller_changes(path, dispatch, monkeypatch):
    queued, drain = dispatch
    seen = []

    def observe(label):
        seen.append((label, SCOPE.get()))
        return "done"

    def stream(remote_id, callback):
        observe(remote_id)
        callback({"event": "run.completed"})

    def post(remote_id, choice, approval_id=None):
        observe(remote_id)
        assert choice == "deny"

    monkeypatch.setattr(talk_apiserver, "stream_run_events", stream)
    monkeypatch.setattr(talk_apiserver, "respond_to_approval", post)
    monkeypatch.setattr(talk_transcript, "_finish_claim", lambda label, *_: observe(label))
    monkeypatch.setattr(talk_capabilities, "_resolve_or_explain", lambda: (
        observe(SCOPE.get()), talk_capabilities._empty("synthetic catalog")
    )[1])
    monkeypatch.setattr(talk_apiserver, "probe", lambda: (
        observe(SCOPE.get()), talk_apiserver.ApiServerStatus(True, "ok", "synthetic probe")
    )[1])

    for run_id, profile in enumerate(("alpha", "beta"), 1):
        SCOPE.set(profile)
        if path == "host":
            talk_host._spawn_daemon(observe, profile)
        elif path == "runs":
            talk_runs.start_run("agent", profile, lambda _rid, p=profile: observe(p))
        elif path == "transcript":
            talk_transcript._start_default_handoff(profile, None, None)
        elif path == "approval-worker":
            talk_approvals._spawn_daemon(observe, profile)
        elif path == "approval-sse":
            talk_approvals.watch_run(run_id, profile)
        elif path in ("approval-timer", "approval-reopen-timer"):
            pending = talk_approvals._PendingApproval(profile, "synthetic request", ("deny",))
            talk_approvals._PENDING[run_id] = pending
            if path == "approval-timer":
                talk_approvals.note_prompt_sent(run_id)
            else:
                pending.opened = pending.resolving = True
                talk_approvals._reopen(run_id, pending)
        else:
            module = talk_capabilities if path == "catalog" else talk_apiserver
            module._refresh_in_background()
            # Finish this refresh before starting another: the module's single
            # in-flight guard intentionally coalesces calls across scopes.
            SCOPE.set("caller-moved")
            drain()
    assert not queued or len(queued) == 2
    SCOPE.set("caller-moved")
    drain()
    assert seen == [("alpha", "alpha"), ("beta", "beta")]
    assert SCOPE.get() == "caller-moved"
    if path == "runs":
        assert [run["status"] for run in talk_runs.list_runs()] == ["done", "done"]
    if path == "approval-sse":
        assert not talk_approvals._WATCHERS


@pytest.mark.parametrize("module", (talk_host, talk_approvals))
def test_simultaneous_workers_enter_distinct_contexts(module, monkeypatch):
    barrier = threading.Barrier(3)
    threads, seen, errors = [], [], []
    real_thread = threading.Thread

    def tracked_thread(**kwargs):
        thread = real_thread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(module, "threading", SimpleNamespace(Thread=tracked_thread))

    def worker():
        try:
            barrier.wait(5)
            seen.append(SCOPE.get())
        except BaseException as exc:  # noqa: BLE001 - surface barrier failures on caller
            errors.append(exc)

    token = SCOPE.set("alpha")
    try:
        module._spawn_daemon(worker)
        SCOPE.set("beta")
        module._spawn_daemon(worker)
        SCOPE.set("caller-moved")
        barrier.wait(5)
    finally:
        for thread in threads:
            thread.join(6)
            assert not thread.is_alive()
        SCOPE.reset(token)
    assert not errors
    assert sorted(seen) == ["alpha", "beta"]


@pytest.mark.parametrize("reopen", (False, True))
def test_cancelled_approval_timer_firing_after_detach_does_not_post(
    reopen, dispatch, monkeypatch,
):
    _, drain = dispatch
    posts = []
    monkeypatch.setattr(talk_apiserver, "respond_to_approval", lambda *a, **kw: posts.append(a))
    SCOPE.set("alpha")
    pending = talk_approvals._PendingApproval("remote", "request", ("deny",))
    talk_approvals._PENDING[7] = pending
    if reopen:
        pending.opened = pending.resolving = True
        talk_approvals._reopen(7, pending)
    else:
        talk_approvals.note_prompt_sent(7)
    talk_approvals.detach_session()
    SCOPE.set("beta")
    # Model a callback already entering when cancel() races with teardown.
    drain(even_cancelled=True)
    assert posts == []
    assert not talk_approvals.has_pending(7)


def test_late_sse_after_detach_is_quarantined(dispatch, monkeypatch):
    _, drain = dispatch
    seen = []

    def stream(_remote_id, callback):
        seen.append(SCOPE.get())
        callback({"event": "approval.request", "choices": ["deny"], "command": "test"})

    monkeypatch.setattr(talk_apiserver, "stream_run_events", stream)
    SCOPE.set("alpha")
    talk_approvals.watch_run(7, "remote")
    talk_approvals.detach_session()
    SCOPE.set("beta")
    drain()
    assert seen == ["alpha"]
    assert not talk_approvals.has_pending(7)
    assert not talk_approvals._WATCHERS


def test_stopped_run_keeps_terminal_record_when_scoped_worker_returns_late(dispatch):
    _, drain = dispatch
    seen = []

    def worker(_rid):
        seen.append(SCOPE.get())
        return "late result"

    SCOPE.set("alpha")
    run_id = talk_runs.start_run("agent", "test", worker)
    assert talk_runs.finish_run(run_id, "failed", "stopped locally")
    SCOPE.set("beta")
    drain()
    assert seen == ["alpha"]
    run = talk_runs.get_run(run_id)
    assert (run["status"], run["output"]) == ("failed", "stopped locally")


@pytest.mark.parametrize("deferred", (False, True))
def test_transcript_handoff_keeps_scope_and_releases_claim(
    deferred, dispatch, monkeypatch, tmp_path,
):
    _, drain = dispatch
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", "synthetic user turn " * 20)
    capture.append_turn("assistant", "synthetic assistant turn " * 20)
    capture.finish()
    seen = []

    def flush(_prompt):
        seen.append(SCOPE.get())
        if deferred:
            raise talk_transcript.FlushDeferred("synthetic offline host")
        return "WORK_STARTED accepted"

    monkeypatch.setattr(talk_transcript, "_default_run_agent", flush)
    SCOPE.set("alpha")
    talk_transcript.sweep_transcripts(tmp_path)
    SCOPE.set("beta")
    drain()
    assert seen == ["alpha"]
    assert not talk_transcript._ACTIVE_TRANSCRIPTS
    root = tmp_path / "state" / "talk-transcripts"
    assert not list(root.glob("*.claimed-*"))
    if deferred:
        assert len(list(root.glob("*.jsonl"))) == 1
        # A second sweep proves the OS lease and descriptor were released.
        talk_transcript.sweep_transcripts(tmp_path, run_agent=lambda _prompt: "FLUSH_DONE")
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("name", ("talk_capabilities", "talk_apiserver"))
@pytest.mark.parametrize("separate_modules", (False, True))
def test_cache_scope_is_per_module_not_per_context(name, separate_modules, monkeypatch):
    """Loader-like separate module identities isolate these two caches only.

    This is a synthetic loader model, not evidence about an installed host's
    import graph or the isolation of any imported dependency.
    """
    source = Path(__file__).resolve().parents[1] / f"{name}.py"

    def load(profile):
        qualified_name = f"_context_test_{profile}_{name}"
        spec = importlib.util.spec_from_file_location(qualified_name, source)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, qualified_name, module)
        spec.loader.exec_module(module)
        if name == "talk_apiserver":
            monkeypatch.setattr(module, "_lane_enabled", lambda: True)
        return module

    first = load("alpha")
    second = load("beta") if separate_modules else first
    token = SCOPE.set("alpha")
    try:
        if name == "talk_capabilities":
            snapshot = first._empty("alpha catalog")
            first._store(snapshot, first._read_seq())
        else:
            snapshot = first.ApiServerStatus(True, "ok", "alpha probe")
            first._store(snapshot)
        # Do not allow a cold cache to start incidental work in this probe.
        refreshes = []
        monkeypatch.setattr(second, "_refresh_in_background", lambda: refreshes.append(SCOPE.get()))
        SCOPE.set("beta")
        result = second.status()
    finally:
        SCOPE.reset(token)
    if separate_modules:
        assert result is not snapshot
        assert refreshes == ["beta"]
    else:
        assert result is snapshot
        assert refreshes == []
