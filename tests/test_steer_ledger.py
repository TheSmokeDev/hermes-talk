"""The steer receipt ledger and the drain watchers.

What is being proved: every state transition rides a REAL artifact — the
post-tool drain INFO line or the pre-API drain DEBUG line for ``landed``,
post-call active-turn state (or Codex native acceptance) for ``redirected``,
registry disappearance for ``unconfirmed``, a patched host's ``missed_steer``
for ``missed`` — and absence of evidence never upgrades a claim.
"""

from __future__ import annotations

import logging
import re
import sys
import types
import uuid

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
    agent = _Steerable()
    talk_steer.record_queued("sa-0-aaaa", "first note about auth", agent=agent)
    talk_steer.record_queued(
        "sa-0-aaaa", "second note far past the preview window", agent=agent
    )
    flipped = talk_steer.mark_landed_from_preview(
        "first note about auth\nsecond", agent=agent
    )
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


# -- correlation tokens (hermes-talk#1) ---------------------------------------


def test_new_token_shape_and_composition():
    token = talk_steer.new_token()
    assert re.fullmatch(r"tk-[0-9a-f]{8}", token)
    assert (
        talk_steer.compose_wire_text(token, "focus on pricing")
        == f"[{token}] focus on pricing"
    )


def test_identical_notes_on_two_agents_land_only_by_token():
    # THE false positive the token exists to kill (Kimi's v0.5 condition):
    # two live agents holding the SAME >=20-char note must not land each
    # other when one of them drains.
    note = "refocus the whole investigation on pricing"
    token_a = talk_steer.new_token()
    token_b = talk_steer.new_token()
    talk_steer.record_queued(
        "sa-0-aaaa", talk_steer.compose_wire_text(token_a, note), token=token_a
    )
    talk_steer.record_queued(
        "sa-1-bbbb", talk_steer.compose_wire_text(token_b, note), token=token_b
    )
    preview = talk_steer.compose_wire_text(token_a, note)[
        : talk_steer.DRAIN_PREVIEW_CHARS
    ]
    assert talk_steer.mark_landed_from_preview(preview) == 1
    summary = talk_steer.notes_summary()
    assert "note to sa-0-aaaa: landed" in summary
    assert "note to sa-1-bbbb: queued" in summary


def test_token_match_still_batch_lands_the_same_agents_later_notes():
    # One agent, two notes queued before one drain: the joined preview only
    # shows the FIRST token, but the whole batch drained together. The second
    # receipt deliberately lacks a ref: the first exact ref anchors it to this
    # generation for compatibility with callers that cannot always resolve it.
    agent = _Steerable()
    token_a = talk_steer.new_token()
    token_b = talk_steer.new_token()
    talk_steer.record_queued(
        "sa-0-aaaa",
        talk_steer.compose_wire_text(token_a, "first note about auth"),
        token=token_a,
        agent=agent,
    )
    talk_steer.record_queued(
        "sa-0-aaaa",
        talk_steer.compose_wire_text(token_b, "second note far past the preview"),
        token=token_b,
    )
    preview = talk_steer.compose_wire_text(token_a, "first note about auth")[
        : talk_steer.DRAIN_PREVIEW_CHARS
    ]
    assert talk_steer.mark_landed_from_preview(preview, agent=agent) == 2


def test_post_tool_batch_uses_matched_receipt_ref_when_stack_identity_is_absent():
    agent = _Steerable()
    token_a = talk_steer.new_token()
    token_b = talk_steer.new_token()
    wire_a = talk_steer.compose_wire_text(token_a, "first note about auth")
    wire_b = talk_steer.compose_wire_text(token_b, "truncated compatibility sibling")
    talk_steer.record_queued("sa-0-aaaa", wire_a, token=token_a, agent=agent)
    talk_steer.record_queued("sa-0-aaaa", wire_b, token=token_b)

    assert talk_steer.mark_landed_from_preview(wire_a) == 2


# -- the redirected state (return-value artifact) -----------------------------


def test_redirected_is_recorded_and_spoken():
    talk_steer.record_redirected("sa-0-aaaa", "[tk-00000000] wrong repo")
    # "accepted … current or next step" — never a claim that the in-flight
    # work was dropped (the host may have taken the steer-queue path).
    assert (
        "redirect accepted — applied at its current or next step"
        in talk_steer.notes_summary()
    )


def test_redirected_is_not_downgraded_by_gone_or_stop():
    # The claim came from the return value at call time; a later stop or
    # registry disappearance says nothing about it.
    talk_steer.record_redirected("sa-0-aaaa", "[tk-00000000] wrong repo")
    talk_steer.mark_child_gone("sa-0-aaaa")
    talk_steer.mark_superseded("sa-0-aaaa")
    assert "redirect accepted" in talk_steer.notes_summary()
    assert "sa-0-aaaa" not in talk_steer.queued_subagent_ids()


# -- the pre-API drain watcher ------------------------------------------------


class _Steerable:
    """The minimum shape the frame-walker accepts as a draining agent."""

    def _drain_pending_steer(self):
        return None


#: A function whose code object CLAIMS to live in conversation_loop.py, with
#: the draining agent in a local named ``agent`` — exactly the frame the
#: walker keys on. ``exec``/``compile`` is the only way to fake a filename.
_FAKE_DRAIN_SRC = """
def fake_pre_api_drain(logger, agent):
    logger.debug("Pre-API-call steer drain: injected into tool msg at index %d", 2)
"""


def _make_fake_drain():
    namespace: dict = {}
    code = compile(_FAKE_DRAIN_SRC, "/hermes/agent/conversation_loop.py", "exec")
    exec(code, namespace)  # test fixture, fixed source
    return namespace["fake_pre_api_drain"]


def _fake_conversation_loop(monkeypatch, level=logging.INFO) -> logging.Logger:
    logger = logging.getLogger(f"fake-conv-loop-{uuid.uuid4().hex[:8]}")
    logger.setLevel(level)
    module = types.ModuleType("agent.conversation_loop")
    module.logger = logger
    package = types.ModuleType("agent")
    package.conversation_loop = module
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.conversation_loop", module)
    return logger


class _Recorder(logging.Handler):
    """What actually escapes the logger to downstream handlers."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_pre_api_drain_lands_by_agent_identity(monkeypatch):
    logger = _fake_conversation_loop(monkeypatch)
    agent = _Steerable()
    assert talk_steer.ensure_pre_api_watcher() is True
    token = talk_steer.new_token()
    talk_steer.record_queued(
        "sa-0-aaaa",
        talk_steer.compose_wire_text(token, "note that never sees a tool batch"),
        token=token,
        agent=agent,
    )
    _make_fake_drain()(logger, agent)
    assert "landed" in talk_steer.notes_summary()


def test_pre_api_drain_for_another_agent_flips_nothing(monkeypatch):
    logger = _fake_conversation_loop(monkeypatch)
    mine = _Steerable()
    talk_steer.ensure_pre_api_watcher()
    token = talk_steer.new_token()
    talk_steer.record_queued(
        "sa-0-aaaa",
        talk_steer.compose_wire_text(token, "note that never sees a tool batch"),
        token=token,
        agent=mine,
    )
    _make_fake_drain()(logger, _Steerable())  # somebody else drained
    assert "queued" in talk_steer.notes_summary()


def test_pre_api_batch_lands_same_agent_receipts_without_a_ref():
    # The pre-API drain empties the agent's WHOLE queue — receipts recorded
    # without an agent ref still flip when a sibling receipt attributes the
    # drain to their subagent id.
    agent = _Steerable()
    talk_steer.record_queued("sa-0-aaaa", "with a ref", agent=agent)
    talk_steer.record_queued("sa-0-aaaa", "without a ref")
    assert talk_steer.mark_landed_for_agent(agent) == 2


def test_pre_api_drain_does_not_land_ref_less_receipt_from_recycled_id():
    # A host may recycle the public id for a later agent instance.  The old
    # ref-less receipt has no identity artifact tying it to the new child, so
    # an exact match on the new receipt must not sweep the old one into landed.
    talk_steer.record_queued("sa-0-aaaa", "old agent without a ref")
    replacement = _Steerable()
    talk_steer.record_queued("sa-0-aaaa", "replacement with a ref", agent=replacement)

    assert talk_steer.mark_landed_for_agent(replacement) == 1
    summary = talk_steer.notes_summary()
    assert "note to sa-0-aaaa: queued" in summary
    assert "note to sa-0-aaaa: landed" in summary


def test_pre_api_watcher_forces_debug_but_gates_other_lines(monkeypatch):
    logger = _fake_conversation_loop(monkeypatch, level=logging.INFO)
    recorder = _Recorder()
    logger.addHandler(recorder)
    assert talk_steer.ensure_pre_api_watcher() is True
    logger.debug("some hot-path debug chatter")
    assert recorder.messages == []  # gated: the operator's level still rules
    logger.info("a normal INFO line")
    assert recorder.messages == ["a normal INFO line"]
    logger.debug("Pre-API-call steer drain: injected into tool msg at index %d", 1)
    assert recorder.messages[-1].startswith("Pre-API-call steer drain: injected")
    logger.removeHandler(recorder)


def test_pre_api_watcher_leaves_an_operator_debug_level_alone(monkeypatch):
    logger = _fake_conversation_loop(monkeypatch, level=logging.DEBUG)
    recorder = _Recorder()
    logger.addHandler(recorder)
    assert talk_steer.ensure_pre_api_watcher() is True
    logger.debug("operator debugging line")
    assert recorder.messages == ["operator debugging line"]  # no gate installed
    logger.removeHandler(recorder)


def test_pre_api_watcher_absent_host_returns_false(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.setitem(sys.modules, "agent.conversation_loop", None)
    assert talk_steer.ensure_pre_api_watcher() is False


def test_pre_api_watcher_is_idempotent(monkeypatch):
    logger = _fake_conversation_loop(monkeypatch)
    assert talk_steer.ensure_pre_api_watcher() is True
    assert talk_steer.ensure_pre_api_watcher() is True
    assert len(logger.handlers) == 1
    assert len(logger.filters) == 1


class _PendingAgent(_Steerable):
    """A draining agent whose queue already holds a POST-drain note."""

    def __init__(self, pending: str = ""):
        self._pending_steer = pending


def test_post_tool_drain_does_not_land_receipt_from_recycled_id():
    # The public id was reused for a replacement child. A token match from
    # the replacement's drain must not batch-expand into the old generation.
    old_agent = _PendingAgent()
    replacement = _PendingAgent()
    old_token = talk_steer.new_token()
    new_token = talk_steer.new_token()
    old_wire = talk_steer.compose_wire_text(old_token, "old agent note")
    new_wire = talk_steer.compose_wire_text(new_token, "replacement agent note")
    talk_steer.record_queued(
        "sa-0-aaaa", old_wire, token=old_token, agent=old_agent
    )
    talk_steer.record_queued(
        "sa-0-aaaa", new_wire, token=new_token, agent=replacement
    )

    flipped = talk_steer.mark_landed_from_preview(new_wire, agent=replacement)

    assert flipped == 1
    summary = talk_steer.notes_summary()
    assert summary.count("landed") == 1
    assert summary.count("queued") == 1


def test_drain_sweep_spares_a_note_queued_after_the_drain():
    # THE race Codex round 1 reproduced: the host drains, another thread
    # queues note B, THEN the log line fires. B's token is still sitting in
    # the agent's pending queue at emit time — it was not delivered and must
    # not be swept into landed with A's batch.
    token_a = talk_steer.new_token()
    token_b = talk_steer.new_token()
    wire_a = talk_steer.compose_wire_text(token_a, "first note about auth")
    wire_b = talk_steer.compose_wire_text(token_b, "late note that missed the drain")
    agent = _PendingAgent(pending=wire_b)
    talk_steer.record_queued("sa-0-aaaa", wire_a, token=token_a, agent=agent)
    talk_steer.record_queued("sa-0-aaaa", wire_b, token=token_b, agent=agent)
    flipped = talk_steer.mark_landed_from_preview(
        wire_a[: talk_steer.DRAIN_PREVIEW_CHARS], agent=agent
    )
    assert flipped == 1
    summary = talk_steer.notes_summary()
    assert "landed" in summary and "queued" in summary


def test_pre_api_sweep_spares_a_note_queued_after_the_drain():
    token_a = talk_steer.new_token()
    token_b = talk_steer.new_token()
    wire_b = talk_steer.compose_wire_text(token_b, "late note that missed the drain")
    agent = _PendingAgent(pending=wire_b)
    talk_steer.record_queued(
        "sa-0-aaaa",
        talk_steer.compose_wire_text(token_a, "first note about auth"),
        token=token_a,
        agent=agent,
    )
    talk_steer.record_queued("sa-0-aaaa", wire_b, token=token_b, agent=agent)
    assert talk_steer.mark_landed_for_agent(agent) == 1
    assert "queued" in talk_steer.notes_summary()


def test_pending_snapshot_is_taken_under_the_ledger_lock(monkeypatch):
    # Codex r2: a snapshot taken BEFORE _LOCK reopens the drain race — a
    # note queued into the host and ledgered in the gap gets swept into
    # landed. The closure is structural: the snapshot may only ever run
    # while the ledger lock is held (record_queued then can't interleave).
    observed: list[bool] = []
    real = talk_steer._still_pending_text

    def instrumented(agent):
        observed.append(talk_steer._LOCK.locked())
        return real(agent)

    monkeypatch.setattr(talk_steer, "_still_pending_text", instrumented)
    agent = _PendingAgent()
    talk_steer.record_queued("sa-0-aaaa", "a note about auth", agent=agent)
    talk_steer.mark_landed_from_preview("a note about auth", agent=agent)
    talk_steer.mark_landed_for_agent(agent)
    assert observed and all(observed)


def test_reset_restores_the_borrowed_logger_exactly(monkeypatch):
    logger = _fake_conversation_loop(monkeypatch, level=logging.INFO)
    talk_steer.ensure_pre_api_watcher()
    assert logger.level == logging.DEBUG
    talk_steer.reset_for_tests()
    assert logger.level == logging.INFO
    assert logger.handlers == []
    assert logger.filters == []


# -- the borrow lifecycle (hermes-talk#5) -------------------------------------


def test_borrow_is_handed_back_when_the_operator_goes_debug(monkeypatch):
    # The operator's config is the PARENT chain. Borrow while it says INFO;
    # when the operator turns verbose logging on at runtime, the next ensure
    # must remove the gate and restore the found level — our filter must not
    # keep muting a module the operator explicitly asked to hear.
    parent = logging.getLogger(f"fake-op-{uuid.uuid4().hex[:8]}")
    parent.setLevel(logging.INFO)
    child = logging.getLogger(parent.name + ".conversation_loop")
    child.setLevel(logging.NOTSET)  # inherits the operator's level
    module = types.ModuleType("agent.conversation_loop")
    module.logger = child
    package = types.ModuleType("agent")
    package.conversation_loop = module
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.conversation_loop", module)

    recorder = _Recorder()
    child.addHandler(recorder)
    assert talk_steer.ensure_pre_api_watcher() is True
    assert child.level == logging.DEBUG  # borrowed
    child.debug("operator chatter")
    assert recorder.messages == []  # gated while borrowed

    parent.setLevel(logging.DEBUG)  # the operator turns verbose on
    assert talk_steer.ensure_pre_api_watcher() is True  # reconciles
    assert child.level == logging.NOTSET  # found level restored
    assert child.filters == []  # gate gone
    child.debug("operator chatter")
    assert "operator chatter" in recorder.messages  # operator hears their module
    # The watcher itself stays attached and functional.
    assert any(isinstance(h, logging.Handler) and h is not recorder for h in child.handlers)
    child.removeHandler(recorder)


def test_uninstall_watchers_detaches_everything_but_keeps_receipts(monkeypatch):
    run_logger = _fake_run_agent(monkeypatch)
    conv_logger = _fake_conversation_loop(monkeypatch, level=logging.INFO)
    talk_steer.ensure_watcher()
    talk_steer.ensure_pre_api_watcher()
    talk_steer.record_queued("sa-0-aaaa", "a note about auth")

    talk_steer.uninstall_watchers()

    assert run_logger.handlers == []
    assert conv_logger.handlers == []
    assert conv_logger.filters == []
    assert conv_logger.level == logging.INFO  # borrow returned
    assert "queued" in talk_steer.notes_summary()  # history, not wiring
    # Re-attach is clean — uninstall is a pause, not a poison.
    assert talk_steer.ensure_watcher() is True
    assert talk_steer.ensure_pre_api_watcher() is True


# -- the landed push (hermes-talk#2) ------------------------------------------


def test_landed_push_fires_per_agent_outside_the_lock():
    seen: list[tuple[str, bool]] = []
    talk_steer.set_landed_notifier(
        lambda sid: seen.append((sid, talk_steer._LOCK.locked()))
    )
    token = talk_steer.new_token()
    wire = talk_steer.compose_wire_text(token, "a note about auth")
    talk_steer.record_queued("sa-0-aaaa", wire, token=token)
    talk_steer.mark_landed_from_preview(wire)
    # Fired once, with the ledger lock RELEASED — the callback marshals to
    # an event loop and must never run under our lock.
    assert seen == [("sa-0-aaaa", False)]


def test_landed_push_covers_the_pre_api_path_too():
    seen: list[str] = []
    talk_steer.set_landed_notifier(seen.append)
    agent = _Steerable()
    talk_steer.record_queued("sa-0-aaaa", "with a ref", agent=agent)
    talk_steer.mark_landed_for_agent(agent)
    assert seen == ["sa-0-aaaa"]


def test_landed_push_is_silent_when_nothing_flips():
    seen: list[str] = []
    talk_steer.set_landed_notifier(seen.append)
    talk_steer.record_queued("sa-0-aaaa", "a note about auth")
    talk_steer.mark_landed_from_preview("completely different text")
    assert seen == []


def test_landed_push_failure_never_breaks_the_drain_path():
    def boom(_sid: str) -> None:
        raise RuntimeError("notifier died")

    talk_steer.set_landed_notifier(boom)
    token = talk_steer.new_token()
    wire = talk_steer.compose_wire_text(token, "a note about auth")
    talk_steer.record_queued("sa-0-aaaa", wire, token=token)
    talk_steer.mark_landed_from_preview(wire)  # no raise is the assertion
    assert "landed" in talk_steer.notes_summary()
