"""Deterministic schedules for the real delegation/admission test pairs.

Loaded only inside isolated pytest subprocesses by test_run_worker_cleanup.
No Hermes process is launched, even when a worker outlives its Popen mock.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

import talk_host
import talk_runs

_Thread = threading.Thread
_reset = talk_runs.reset_for_tests
_finish = talk_runs.finish_run
_start = talk_runs.start_run
_popen = talk_host.subprocess.Popen
_mode = os.environ["TALK_TEST_WORKER_SCHEDULE"]
_phase = ""
_threads = []
_old = None
_old_gate = None
_starts = 0
_release = threading.Event()
_entered = threading.Event()
_done = threading.Event()
_finishing = threading.Event()
_next = threading.Event()
_joined = False
_trace = {
    "reset_while_alive": False, "unmocked_popen": False, "joined": False,
    "gate_released_at_join": False, "finishes": [],
}


class ScheduledThread(_Thread):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.name.startswith("talk-run-"):
            return
        global _old, _old_gate
        _threads.append(self)
        ordinal = 2 if _mode == "admission" else 1
        if _phase == "first" and len(_threads) == ordinal:
            _old = self
            _old_gate = getattr(getattr(self._args[-1], "__self__", None), "release", None)
        target, arguments, keywords = self._target, self._args, self._kwargs

        def scheduled():
            try:
                if self is _old:
                    _entered.set()
                    assert _release.wait(5), "old worker was never released"
                elif _old is not None and _old.is_alive() and (
                    _phase == "second" or _mode == "mid_reset"
                ):
                    _next.set()
                    _release.set()
                    assert _done.wait(5), "old worker never finished"
                target(*arguments, **keywords)
            finally:
                if self is _old:
                    _done.set()

        self._target, self._args, self._kwargs = scheduled, (), {}

    def start(self):
        super().start()
        if self is _old:
            assert _entered.wait(5)
        elif _old is not None and _old.is_alive() and (
            _phase == "second" or _mode == "mid_reset"
        ):
            # The next test must observe the late terminal write BEFORE its
            # admission assertion; scheduling luck cannot hide the failure.
            assert _done.wait(5)

    def join(self, timeout=None):
        global _joined
        if self is _old:
            _joined = _trace["joined"] = True
            _trace["gate_released_at_join"] = _old_gate is not None and _old_gate.is_set()
            _release.set()
        return super().join(timeout)


def reset():
    if _old is not None and _old.is_alive():
        _trace["reset_while_alive"] = True
    return _reset()


def finish(*args, **kwargs):
    if threading.current_thread() is _old and _mode == "delegation" and not _joined:
        _finishing.set()
        assert _next.wait(5), "next run was never accepted"
    changed = _finish(*args, **kwargs)
    _trace["finishes"].append({
        "old_worker": threading.current_thread() is _old,
        "phase": _phase, "run_id": args[0], "status": args[1], "changed": changed,
    })
    return changed


def start(*args, **kwargs):
    global _starts
    if _mode == "admission_failure" and _phase == "first":
        _starts += 1
        if _starts == 2:
            raise AssertionError("synthetic assertion failure before fixture teardown")
    return _start(*args, **kwargs)


def pytest_configure():
    threading.Thread = ScheduledThread
    talk_runs.reset_for_tests = reset
    talk_runs.finish_run = finish
    talk_runs.start_run = start


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    global _phase
    _phase = "first" if item.name in (
        "test_no_ctx_at_all_goes_straight_to_the_detached_backend",
        "test_read_only_pairs_overlap_only_with_the_knob_on",
    ) else "second"
    result = yield
    return result


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item):
    result = yield
    if _phase == "first" and _mode == "admission_failure" and _old.is_alive():
        # Keep the deliberately broken pre-fix child bounded after observing
        # that its fixture reset with a live, unreleased gate.
        _old_gate.set()
    if _phase == "first" and _mode == "delegation" and _old.is_alive():
        # This is the real fixture's post-undo window. Trap the missing fake
        # executable rather than risk launching an installed Hermes binary.
        def unmocked_popen(*args, **kwargs):
            _trace["unmocked_popen"] = True
            raise FileNotFoundError("synthetic missing Hermes after Popen mock removal")

        talk_host.subprocess.Popen = unmocked_popen
        _release.set()
        assert _finishing.wait(5)
    return result


def pytest_sessionfinish():
    _release.set()
    _next.set()
    for thread in _threads:
        _Thread.join(thread, 6)
        assert not thread.is_alive()
    _trace["workers"] = len(_threads)
    Path("schedule.json").write_text(json.dumps(_trace), encoding="utf-8")
    print("worker schedule trace:", json.dumps(_trace))


def pytest_unconfigure():
    threading.Thread = _Thread
    talk_runs.reset_for_tests = _reset
    talk_runs.finish_run = _finish
    talk_runs.start_run = _start
    talk_host.subprocess.Popen = _popen
