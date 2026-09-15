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
