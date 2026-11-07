"""Force the two observed failures using the actual neighboring test bodies.

The child pytest owns all monkeypatches, fixtures and threads. A delayed worker
must be joined by its fixture, before mocks disappear or run IDs can be reused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import wallclock

pytest_plugins = ["pytester"]


@pytest.mark.parametrize("schedule", ("delegation", "admission", "mid_reset", "admission_failure"))
def test_neighboring_tests_cannot_inherit_a_worker(pytester, monkeypatch, schedule):
    tests = Path(__file__).parent
    for name in (
        "conftest.py", "wallclock.py", "worker_schedule.py", "run_workers.py",
        "test_delegation.py", "test_admission.py",
    ):
        source = tests / name
        (pytester.path / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    pytester.makeini(f'[pytest]\npythonpath = "{tests.parent.as_posix()}"\n')
    monkeypatch.setenv("HERMES_HOME", str(pytester.path / "home"))
    monkeypatch.setenv("TALK_TEST_WORKER_SCHEDULE", schedule)
    if schedule == "delegation":
        nodes = [
            "test_delegation.py::test_no_ctx_at_all_goes_straight_to_the_detached_backend",
            "test_delegation.py::test_a_nonzero_exit_lands_as_a_failed_run_with_the_reason",
        ]
    else:
        nodes = [
            "test_admission.py::test_read_only_pairs_overlap_only_with_the_knob_on",
            "test_admission.py::test_turning_the_knob_off_closes_overlaps_it_had_allowed",
        ]
    result = pytester.runpytest_subprocess(
        "-p", "worker_schedule", "-q", *nodes, timeout=wallclock.stretch(30),
    )
    if schedule == "admission_failure":
        result.assert_outcomes(passed=1, failed=1)
        assert "synthetic assertion failure before fixture teardown" in result.stdout.str()
    else:
        result.assert_outcomes(passed=2)
    trace = json.loads((pytester.path / "schedule.json").read_text(encoding="utf-8"))
    assert trace["joined"]
    assert not trace["reset_while_alive"]
    assert not trace["unmocked_popen"]
    if schedule != "delegation":
        assert trace["gate_released_at_join"]
