"""subagent_start/stop hooks — the roster, the ledger, and the loop marshal.

What is being proved: the roster is keyed by ``child_session_id`` (the only
key ``subagent_stop`` carries on the 0.20 host), stops degrade that child's
queued notes IMMEDIATELY, announcements reach the session only through
``call_soon_threadsafe``, only top-level children are announced, and every
path is fail-open — a hook must never cost the host anything.
"""

from __future__ import annotations

import pytest

import talk_lifecycle
import talk_steer


@pytest.fixture(autouse=True)
def _clean():
    talk_lifecycle.reset_for_tests()
    talk_steer.reset_for_tests()
    yield
    talk_lifecycle.reset_for_tests()
    talk_steer.reset_for_tests()


def _start(csid: str = "sess-1", sid: str = "sa-0-aaaa", **overrides):
    kwargs = {
        "parent_session_id": "parent-sess",
        "parent_turn_id": "turn-1",
        "parent_subagent_id": None,
        "child_session_id": csid,
        "child_subagent_id": sid,
        "child_role": "researcher",
        "child_goal": "audit the auth module",
    }
    kwargs.update(overrides)
    talk_lifecycle.on_subagent_start(**kwargs)


def _stop(csid: str = "sess-1", **overrides):
    kwargs = {
        "parent_session_id": "parent-sess",
        "parent_turn_id": "turn-1",
        "child_session_id": csid,
        "child_role": "researcher",
        "child_summary": "found three issues",
        "child_status": "ok",
        "tool_call_history": [],
        "duration_ms": 1200,
    }
    kwargs.update(overrides)
    talk_lifecycle.on_subagent_stop(**kwargs)


class _StubLoop:
    """Records the marshal and runs the callback inline, as the loop would."""

    def __init__(self, raises: Exception | None = None):
        self.raises = raises
        self.marshalled = 0

    def call_soon_threadsafe(self, callback, *args):
        if self.raises is not None:
            raise self.raises
        self.marshalled += 1
        callback(*args)


# -- the roster ---------------------------------------------------------------


def test_start_rosters_the_child_by_session_id():
    _start()
    snapshot = talk_lifecycle.roster_snapshot()
    assert len(snapshot) == 1
    assert snapshot[0]["subagent_id"] == "sa-0-aaaa"
    assert snapshot[0]["top_level"] is True


def test_start_without_a_session_id_is_skipped():
    # A start that cannot be keyed back at stop time is useless — rostering
    # it under a junk key would only leak.
    _start(csid="")
    assert talk_lifecycle.roster_snapshot() == []


def test_start_without_a_subagent_id_is_skipped():
    _start(sid="")
    assert talk_lifecycle.roster_snapshot() == []


def test_stop_pops_the_roster_entry():
    _start()
    _stop()
    assert talk_lifecycle.roster_snapshot() == []


def test_roster_is_capped():
    for i in range(110):
        _start(csid=f"sess-{i}", sid=f"sa-0-{i:04d}")
    assert len(talk_lifecycle.roster_snapshot()) == 100


# -- ledger truth on stop -----------------------------------------------------


def test_stop_degrades_that_childs_queued_notes_immediately():
    _start()
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")
    _stop()
    # By stop time every drain that was ever going to happen has happened —
    # still-queued means never delivered.
    assert "finished before I could confirm" in talk_steer.notes_summary()


def test_stop_for_an_unknown_session_touches_nothing():
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")
    _stop(csid="sess-nobody-started")
    assert "queued" in talk_steer.notes_summary()


def test_nested_children_still_degrade_the_ledger():
    _start(csid="sess-nested", sid="sa-1-cccc", parent_subagent_id="sa-0-aaaa")
    talk_steer.record_queued("sa-1-cccc", "skip the tests")
    _stop(csid="sess-nested")
    assert "finished before I could confirm" in talk_steer.notes_summary()


# -- the loop marshal ---------------------------------------------------------


def test_stop_announces_a_top_level_child_through_the_loop():
    events: list[dict] = []
    loop = _StubLoop()
    talk_lifecycle.attach_session(loop, events.append, "parent-sess")
    _start()
    _stop()
    assert loop.marshalled == 1
    assert len(events) == 1
    event = events[0]
    assert event["kind"] == "subagent_stop"
    assert event["subagent_id"] == "sa-0-aaaa"
    assert event["status"] == "ok"
    assert event["summary"] == "found three issues"
    # Scoping handle: every event names the parent session that owns the
    # child, so a consumer with a session oracle can drop foreign-parent
    # completions (hermes-talk#4).
    assert event["parent_session_id"] == "parent-sess"


def test_roster_retains_the_parent_session():
    _start()
    assert talk_lifecycle.roster_snapshot()[0]["parent_session_id"] == "parent-sess"


def test_nested_children_stop_silently():
    events: list[dict] = []
    talk_lifecycle.attach_session(_StubLoop(), events.append, "parent-sess")
    _start(csid="sess-nested", sid="sa-1-cccc", parent_subagent_id="sa-0-aaaa")
    _stop(csid="sess-nested")
    assert events == []


def test_no_attached_session_is_fine():
    _start()
    _stop()  # nothing to announce to, nothing raised


def test_detach_stops_announcements():
    events: list[dict] = []
    talk_lifecycle.attach_session(_StubLoop(), events.append, "parent-sess")
    talk_lifecycle.detach_session()
    _start()
    _stop()
    assert events == []


def test_a_closed_loop_is_contained():
    # call_soon_threadsafe raises RuntimeError once a loop closes — the hook
    # must swallow it, because it is running on a HOST thread.
    talk_lifecycle.attach_session(
        _StubLoop(raises=RuntimeError("loop closed")), lambda e: None, "parent-sess"
    )
    _start()
    _stop()  # no exception is the assertion


def test_foreign_parent_stop_is_not_announced_but_still_degrades_the_ledger():
    events: list[dict] = []
    talk_lifecycle.attach_session(_StubLoop(), events.append, "other-parent")
    _start()
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")

    _stop()

    assert events == []
    assert "finished before I could confirm" in talk_steer.notes_summary()


def test_missing_event_parent_is_not_announced_but_still_degrades_the_ledger():
    events: list[dict] = []
    talk_lifecycle.attach_session(_StubLoop(), events.append, "parent-sess")
    _start(parent_session_id=None)
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")

    _stop()

    assert events == []
    assert "finished before I could confirm" in talk_steer.notes_summary()


@pytest.mark.parametrize("owner_session_id", [None, ""])
def test_missing_owner_rejects_all_announcements(owner_session_id):
    events: list[dict] = []
    talk_lifecycle.attach_session(_StubLoop(), events.append, owner_session_id)
    _start()
    talk_steer.record_queued("sa-0-aaaa", "focus on pricing")

    _stop()

    assert events == []
    assert "finished before I could confirm" in talk_steer.notes_summary()


# -- fail-open ----------------------------------------------------------------


class _Explosive:
    def __str__(self):
        raise RuntimeError("boom")


def test_start_swallows_poisoned_kwargs():
    _start(child_goal=_Explosive())  # str() raises inside the hook body
    # Contained, and the poisoned start was simply dropped.
    assert talk_lifecycle.roster_snapshot() == []


def test_stop_swallows_a_poisoned_ledger(monkeypatch):
    _start()
    monkeypatch.setattr(
        talk_steer,
        "mark_child_gone",
        lambda sid: (_ for _ in ()).throw(RuntimeError("ledger poisoned")),
    )
    _stop()  # no exception is the assertion
