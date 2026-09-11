"""Readiness observations suppress speech; they never grant task execution authority."""

import pytest

from talk_speech_timing import SpeechTiming
from talk_task_sources import TaskEventError


def snapshot(sequence=1, **overrides):
    return {"sequence": sequence, **dict.fromkeys(SpeechTiming.FIELDS, False), **overrides}


@pytest.mark.parametrize("blocker", sorted(SpeechTiming.FIELDS))
def test_each_distinct_busy_path_blocks_and_then_releases(blocker):
    gate = SpeechTiming(("task", 3))
    assert not gate.ready(("task", 3))
    assert not gate.observe(("task", 3), snapshot(**{blocker: True}))
    assert gate.observe(("task", 3), snapshot(2))


def test_generation_order_and_expired_observations_do_not_authorize_speech():
    now = [100.0]
    gate = SpeechTiming(("task", 3), clock=lambda: now[0])
    assert gate.observe(("task", 3), snapshot(2))
    with pytest.raises(TaskEventError, match="foreign_owner"):
        gate.observe(("other", 3), snapshot(3))
    with pytest.raises(TaskEventError, match="foreign_owner"):
        gate.ready(("task", 4))
    assert not gate.observe(("task", 3), snapshot(1))
    with pytest.raises(TaskEventError, match="event_conflict"):
        gate.observe(("task", 3), snapshot(2, operator_speaking=True))
    now[0] += 5.1
    assert not gate.ready(("task", 3))
    assert not gate.observe(("task", 3), snapshot(2))
    assert gate.observe(("task", 3), snapshot(3))


@pytest.mark.parametrize("value", [None, {}, snapshot(True), snapshot(-1),
                                    snapshot(operator_speaking="false")])
def test_incomplete_or_untyped_readiness_is_refused(value):
    with pytest.raises(TaskEventError, match="invalid_event"):
        SpeechTiming("capture").observe("capture", value)
