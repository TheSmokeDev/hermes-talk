"""Shared suite plumbing."""

from __future__ import annotations

import asyncio
import os
import sys
import threading

import pytest
import wallclock

# Tests that assert ORDERING UNDER A REAL WALL-CLOCK DEADLINE against a scripted local peer:
# a liveness probe racing a subprocess RPC, a cancel or approval deadline, a 25 s decision
# window, a late receipt after a switch. They pass on every Linux job and on every developer
# box. GitHub's Windows runners run the identical job anywhere between 7 and 14 minutes from
# one attempt to the next, and no fixed deadline, scaled or not, survives that variance
# (fourteen distinct tests across ten runs on one day, never the same one twice, two of them
# with the 3x scale and the outbox fix already in place). So on Windows CI exactly these are
# SKIPPED, visibly, in the summary; every other test in their files still runs there.
# Named individually so a Windows-only regression in the subprocess, auth or store paths
# those files also cover cannot hide behind the skip. TALK_TEST_WALL_CLOCK=1 forces them on.
_WALL_CLOCK_TESTS = {
    "test_codex_cancel_timeout.py": {
        "test_silent_responsive_turn_survives_repeated_liveness_reads",
        "test_approval_wait_survives_repeated_metadata_reads_and_resolves_once",
        "test_unanswered_probe_does_not_expire_a_current_approval",
        "test_unanswered_probe_bounds_both_local_deadline_and_rpc_waiter",
        "test_cancellation_during_unanswered_probe_does_not_wait_for_probe_deadline",
        "test_cancellation_keeps_its_deadline_when_probe_and_interrupt_are_unanswered",
        "test_owned_events_keep_a_turn_live_when_a_metadata_rpc_stalls",
        "test_liveness_response_cannot_replace_owned_thread",
        "test_fixture_snapshots_stay_immutable_while_a_reader_holds_an_old_file",
        "test_daemon_cleanup_cannot_wait_forever_for_run_or_close",
        "test_accepted_approval_starts_a_fresh_quiet_interval",
    },
    "test_codex_worker.py": {
        "test_full_result_and_same_job_recovery_do_not_launch_again",
        "test_lost_turn_response_recovers_by_original_client_message_without_relaunch",
        "test_lost_thread_response_never_guesses_or_creates_replacement",
        "test_policy_or_foreign_thread_refusal_closes_only_the_owned_process",
        "test_version_mismatch_starts_no_app_server",
        "test_exact_steering_and_cancellation_keep_original_thread_and_turn",
        "test_current_approval_once_and_replay_cannot_authorize_a_new_request",
        "test_stalled_stdin_cannot_hold_worker_shutdown_forever",
        "test_owner_revoked_after_thread_creation_prevents_turn_execution",
        "test_unconfirmed_cancellation_exits_with_partial_result_and_no_replacement",
    },
    "test_target_switching.py": {
        "test_late_child_receipt_after_switch_stays_with_original_action",
    },
    "test_live_browser_repair.py": {
        "test_async_25_second_decision_preserves_poll_lease_captions_and_parallel_request",
        "test_queued_capture_does_not_delay_exact_job_completion_or_repeat_its_result",
    },
    "test_live_async_ledger.py": {
        "test_async_receipt_precedes_a_real_25_second_decision",
    },
    "test_dashboard_tasks.py": {
        "test_actual_frontend_event_wire_roundtrips_through_coordinator",
        # Future.result(timeout) against a scripted host; twice red on Windows CI in one day.
        "test_first_completed_observation_is_ready_without_transcript_linking",
    },
}


def _is_wall_clock(item) -> bool:
    # originalname drops the parametrization suffix, so every variant of a named test matches
    # and nothing else does.
    return item.originalname in _WALL_CLOCK_TESTS.get(item.path.name, ())


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


@pytest.fixture(autouse=True, scope="session")
def _scale_wall_clock_waits():
    """Stretch every ``Event.wait``, ``asyncio.wait_for`` and ``asyncio.timeout``
    of at least ``wallclock.FLOOR_S`` that the suite reaches by attribute, by one
    factor, in one place. Poll loops that keep their own ``monotonic()`` deadline
    call ``wallclock.stretch`` themselves. A wait that is meant to expire still
    expires, later.
    """

    if wallclock.scale() == 1.0:
        yield
        return

    original_event_wait = threading.Event.wait
    original_wait_for = asyncio.wait_for
    original_timeout = asyncio.timeout
    stretched = wallclock.stretch

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
