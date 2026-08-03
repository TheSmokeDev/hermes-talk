"""The steer receipt ledger and the drain watcher.

What is being proved: every state transition rides a REAL artifact — the
drain INFO line for ``landed``, registry disappearance for ``unconfirmed``,
a patched host's ``missed_steer`` for ``missed`` — and absence of evidence
never upgrades a claim.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

import talk_steer


@pytest.fixture(autouse=True)
def _clean():
    talk_steer.reset_for_tests()
    yield
    talk_steer.reset_for_tests()


def test_queued_is_the_initial_state():
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")
    assert "queued" in talk_steer.notes_summary()


def test_drain_preview_lands_the_matching_receipt():
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing instead")
    flipped = talk_steer.mark_landed_from_preview("focus on pricing instead")
    assert flipped == 1
    assert "landed" in talk_steer.notes_summary()


def test_drain_preview_is_truncated_at_120_chars_and_still_matches():
    long_note = "focus on " + "pricing " * 30  # far past the 120-char preview
    talk_steer.record_queued("sa-0-aaaa", long_note)
    flipped = talk_steer.mark_landed_from_preview(
        long_note[: talk_steer.DRAIN_PREVIEW_CHARS]
    )
    assert flipped == 1


def test_concatenated_drain_lands_the_whole_batch():
    # Two steers queued before one drain concatenate with newlines; the
    # preview only shows the head. The earlier receipt must land too.
    talk_steer.record_queued("sa-0-aaaa", "first note about auth")
    talk_steer.record_queued("sa-0-aaaa", "second note far past the preview window")
    flipped = talk_steer.mark_landed_from_preview("first note about auth\nsecond")
    assert flipped == 2


def test_unmatched_preview_flips_nothing():
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")
    assert talk_steer.mark_landed_from_preview("completely different text") == 0
    assert "queued" in talk_steer.notes_summary()


def test_child_gone_degrades_to_unconfirmed_never_landed():
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")
    talk_steer.mark_child_gone("sa-0-aaaa")
    summary = talk_steer.notes_summary()
    assert "confirm" in summary  # "finished before I could confirm..."
    assert "landed" not in summary


def test_stop_supersedes_queued_notes():
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")
    talk_steer.mark_superseded("sa-0-aaaa")
    assert "the note may not have been read" in talk_steer.notes_summary()


def test_missed_steer_from_a_patched_host_marks_missed():
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")
    hit = talk_steer.apply_missed_steer(
        "sa-0-aaaa", {"missed_steer": "focus on pricing"}
    )
    assert hit is True
    assert "never saw the note" in talk_steer.notes_summary()


def test_missed_steer_absent_field_changes_nothing():
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")
    assert talk_steer.apply_missed_steer("sa-0-aaaa", {"summary": "done"}) is False
    assert "queued" in talk_steer.notes_summary()


# -- the watcher --------------------------------------------------------------


def _fake_run_agent(monkeypatch, level=logging.INFO) -> logging.Logger:
    logger = logging.getLogger(f"fake-run-agent-{id(monkeypatch)}")
    logger.setLevel(level)
    module = types.ModuleType("run_agent")
    module.logger = logger
    monkeypatch.setitem(sys.modules, "run_agent", module)
    return logger


def test_watcher_attaches_and_flips_on_the_real_drain_line(monkeypatch):
    logger = _fake_run_agent(monkeypatch)
    assert talk_steer.ensure_watcher() is True
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")
    # The EXACT production format string (agent_runtime_helpers.py:3889).
    logger.info(
        "Delivered /steer to agent after tool batch (%d chars): %s",
        16,
        "focus on pricing",
    )
    assert "landed" in talk_steer.notes_summary()


def test_watcher_reports_ineffective_at_warning_level(monkeypatch):
    _fake_run_agent(monkeypatch, level=logging.WARNING)
    # Attached but blind: the INFO record will never exist, so the caller
    # must be told confirmation is unavailable rather than promised.
    assert talk_steer.ensure_watcher() is False


def test_watcher_absent_host_returns_false(monkeypatch):
    monkeypatch.setitem(sys.modules, "run_agent", None)
    assert talk_steer.ensure_watcher() is False


def test_watcher_is_idempotent(monkeypatch):
    logger = _fake_run_agent(monkeypatch)
    assert talk_steer.ensure_watcher() is True
    assert talk_steer.ensure_watcher() is True
    watchers = [h for h in logger.handlers if isinstance(h, logging.Handler)]
    assert len(watchers) == 1


def test_watcher_ignores_unrelated_log_lines(monkeypatch):
    logger = _fake_run_agent(monkeypatch)
    talk_steer.ensure_watcher()
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")
    logger.info("Something else entirely: focus on pricing")
    assert "queued" in talk_steer.notes_summary()
