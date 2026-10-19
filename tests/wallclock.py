"""One knob for every wall-clock allowance the suite makes.

The coordinator tests wait on threads, coroutines and poll loops with literal
seconds tuned on Linux. GitHub's Windows runners are two to four times slower
to spawn a process or wake a thread, so those allowances expire there once
every few runs, in a different test each time. ``TALK_TEST_TIMEOUT_SCALE``
overrides; otherwise Windows-under-CI gets 3x and everything else runs at the
literal values. Allowances under ``FLOOR_S`` are left alone: every sub-second
wait in the suite is a "must NOT happen yet" assertion whose product-side
deadline is a config value, and stretching only the test's half of that pair
turns the assertion false.
"""

from __future__ import annotations

import os
import sys

FLOOR_S = 1.0


def scale() -> float:
    raw = os.environ.get("TALK_TEST_TIMEOUT_SCALE", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            return 1.0
    if sys.platform == "win32" and os.environ.get("CI"):
        return 3.0
    return 1.0


def stretch(seconds):
    """The allowance a test should actually grant for a nominal ``seconds``."""

    if seconds is None or seconds < FLOOR_S:
        return seconds
    return seconds * scale()
