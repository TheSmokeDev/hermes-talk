"""Shared suite plumbing."""

from __future__ import annotations

import asyncio
import os
import sys
import threading

import pytest


def _wall_clock_scale() -> float:
    """How much to stretch the suite's fixed wall-clock waits on this box.

    The coordinator tests wait on threads and coroutines with literal seconds
    tuned on Linux. GitHub's Windows runners are two to four times slower to
    spawn a process or wake a thread, so a 3 s wait that never times out on
    Ubuntu times out there once every few runs, in a different test each time.
    ``TALK_TEST_TIMEOUT_SCALE`` overrides; otherwise Windows-under-CI gets 3x
    and everything else runs at the literal values.
    """

    raw = os.environ.get("TALK_TEST_TIMEOUT_SCALE", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            return 1.0
    if sys.platform == "win32" and os.environ.get("CI"):
        return 3.0
    return 1.0


# Tests that assert ORDERING UNDER REAL WALL-CLOCK DEADLINES against scripted local peers:
# a liveness probe racing a subprocess RPC, a 25 s decision window, a late receipt after a
# switch. They pass on every Linux job and on every developer box. GitHub's Windows runners
# run the identical job anywhere between 7 and 14 minutes from one attempt to the next, and
# no fixed deadline, scaled or not, survives that variance (fourteen distinct tests across
# ten runs on one day, never the same one twice). So on Windows CI these are SKIPPED, visibly,
# in the summary; every other test still runs there. TALK_TEST_WALL_CLOCK=1 forces them on.
_WALL_CLOCK_FILES = frozenset(
    {
        "test_codex_cancel_timeout.py",
        "test_codex_worker.py",
        "test_target_switching.py",
        "test_live_browser.py",
        "test_live_browser_repair.py",
        "test_live_async_ledger.py",
        "test_live_routes.py",
    }
)
_WALL_CLOCK_TESTS = frozenset(
    {"test_dashboard_tasks.py::test_actual_frontend_event_wire_roundtrips_through_coordinator"}
)


def _is_wall_clock(item) -> bool:
    if item.path.name in _WALL_CLOCK_FILES:
        return True
    return any(item.nodeid.endswith(entry) for entry in _WALL_CLOCK_TESTS)


def pytest_collection_modifyitems(config, items):
    skip_here = (
        sys.platform == "win32"
        and bool(os.environ.get("CI"))
        and os.environ.get("TALK_TEST_WALL_CLOCK", "").strip() != "1"
    )
    skip = pytest.mark.skip(
        reason="wall-clock deadline test; runs on Linux CI, skipped on Windows CI "
        "(TALK_TEST_WALL_CLOCK=1 forces it)"
    )
    for item in items:
        if _is_wall_clock(item):
            item.add_marker(pytest.mark.wall_clock)
            if skip_here:
                item.add_marker(skip)


# Waits shorter than this are left alone. Every sub-second wait in the suite is a
# quiet-interval assertion ("the worker must NOT finish within 0.3 s") whose
# product-side deadline is a config value, not one of the patched calls; stretching
# only the test's half of that pair turns the assertion false. Every readiness wait
# that has flaked on Windows was 3 s or longer.
_SCALE_FLOOR_S = 1.0


@pytest.fixture(autouse=True, scope="session")
def _scale_wall_clock_waits():
    """Stretch every ``Event.wait``, ``asyncio.wait_for`` and ``asyncio.timeout``
    of at least ``_SCALE_FLOOR_S`` that the suite reaches by attribute, by one
    factor, in one place. A wait that is meant to expire still expires, later.
    """

    scale = _wall_clock_scale()
    if scale == 1.0:
        yield
        return

    original_event_wait = threading.Event.wait
    original_wait_for = asyncio.wait_for
    original_timeout = asyncio.timeout

    def stretched(seconds):
        if seconds is None or seconds < _SCALE_FLOOR_S:
            return seconds
        return seconds * scale

    def scaled_event_wait(self, timeout=None):
        return original_event_wait(self, stretched(timeout))

    async def scaled_wait_for(awaitable, timeout=None):
        return await original_wait_for(awaitable, stretched(timeout))

    def scaled_timeout(delay):
        return original_timeout(stretched(delay))

    threading.Event.wait = scaled_event_wait
    asyncio.wait_for = scaled_wait_for
    asyncio.timeout = scaled_timeout
    try:
        yield
    finally:
        threading.Event.wait = original_event_wait
        asyncio.wait_for = original_wait_for
        asyncio.timeout = original_timeout


@pytest.fixture(autouse=True)
def _ephemeral_runs_optin(monkeypatch):
    """The suite's EXPLICIT opt-in to non-durable run acceptance.

    The run-history tee is inert under pytest by design (see
    ``talk_runs._history_enabled``), and ``start_run`` refuses to accept work
    it cannot record durably unless the caller opts in by name — silence is
    exactly how a "durable" acceptance quietly stops being durable. Tests
    that exercise durability monkeypatch the tee ON (and this variable is
    then never consulted); the test asserting the refusal itself deletes it.
    """

    monkeypatch.setenv("TALK_RUNS_ALLOW_EPHEMERAL", "1")
