"""The spoken approval bridge — scripted SSE in, correct resolve POSTs out.

Zero network: ``talk_apiserver.respond_to_approval`` and
``talk_apiserver.stream_run_events`` are faked at the module seam, and the
session is a fake loop that runs callbacks inline. The real SSE frame parsing
and the real approval POST have their own tests against fake httpx objects.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

import talk_apiserver
import talk_approvals
import talk_cli
import talk_host
import talk_runs
import talk_tools


class FakeLoop:
    """call_soon_threadsafe that runs inline and records."""

    def __init__(self):
        self.events: list[dict] = []

    def call_soon_threadsafe(self, callback, event):
        self.events.append(event)
        callback(event)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    talk_approvals.reset_for_tests()
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    monkeypatch.delenv("TALK_APPROVAL_PROMPT_TIMEOUT_S", raising=False)
    yield
    talk_approvals.reset_for_tests()
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()


def _approval_event(**overrides) -> dict:
    event = {
        "event": "approval.request",
        "run_id": "run_remote_1",
        "timestamp": time.time(),
        "command": "rm -rf ./build",
        "description": "Run a shell command",
        "choices": ["once", "session", "always", "deny"],
    }
    event.update(overrides)
    return event


def _register(loop: FakeLoop, run_id: int = 7, **overrides):
    talk_approvals.attach_session(loop, lambda event: None)
    talk_approvals._note_event(run_id, "run_remote_1", _approval_event(**overrides))
    return loop.events[-1]


# -- prompt announcement ---------------------------------------------------------


def test_approval_request_announces_a_prompt_through_the_session():
    loop = FakeLoop()
    seen = []
    talk_approvals.attach_session(loop, seen.append)

    talk_approvals._note_event(7, "run_remote_1", _approval_event())

    assert len(seen) == 1
    event = seen[0]
    assert event["kind"] == talk_approvals.EVENT_APPROVAL_PROMPT
    assert event["run_id"] == 7
    assert event["request"] == "Run a shell command"
    assert talk_approvals.has_pending(7)


def test_choices_are_narrowed_in_code_never_including_always():
    """The host offers always; voice does not. The narrowing is in the
    registered record, not in the prompt wording."""

    loop = FakeLoop()
    seen = []
    talk_approvals.attach_session(loop, seen.append)

    talk_approvals._note_event(7, "run_remote_1", _approval_event())

    assert seen[0]["choices"] == ("once", "session", "deny")
    assert "always" not in seen[0]["choices"]
    assert talk_approvals.pending_choices(7) == ("once", "session", "deny")


def test_a_host_narrowed_offer_is_narrowed_further_not_widened():
    loop = FakeLoop()
    seen = []
    talk_approvals.attach_session(loop, seen.append)

    talk_approvals._note_event(7, "run_remote_1", _approval_event(choices=["once", "deny"]))

    assert seen[0]["choices"] == ("once", "deny")


def test_malformed_or_missing_choices_collapse_to_deny_only():
    """Fail closed on schema drift: the host always sends ``choices`` as a
    list of strings, so a missing, non-list, or unrecognizable value must
    narrow the answer set to deny — never widen it to everything voice
    could grant."""

    loop = FakeLoop()
    seen = []
    talk_approvals.attach_session(loop, seen.append)

    absent = _approval_event()
    del absent["choices"]
    talk_approvals._note_event(7, "run_remote_1", absent)
    assert seen[-1]["choices"] == ("deny",)

    talk_approvals._note_event(8, "run_remote_1", _approval_event(choices=None))
    assert seen[-1]["choices"] == ("deny",)

    talk_approvals._note_event(9, "run_remote_1", _approval_event(choices="once"))
    assert seen[-1]["choices"] == ("deny",)

    talk_approvals._note_event(10, "run_remote_1", _approval_event(choices=[1, {"x": 2}]))
    assert seen[-1]["choices"] == ("deny",)

    talk_approvals._note_event(11, "run_remote_1", _approval_event(choices=["ALWAYS", "Once"]))
    assert seen[-1]["choices"] == ("deny",)


def test_no_attached_session_means_no_prompt_and_no_denial():
    """The dashboard-lane rule: with nobody to ask, the bridge stays out and
    the host's own approval timeout governs — the pre-bridge behavior."""

    talk_approvals._note_event(7, "run_remote_1", _approval_event())

    assert talk_approvals.has_pending(7)  # registered; nothing announced


def test_request_text_is_bounded_and_host_redaction_is_not_repeated():
    long_request = "x" * 1_000
    loop = FakeLoop()
    seen = []
    talk_approvals.attach_session(loop, seen.append)

    talk_approvals._note_event(7, "run_remote_1", _approval_event(description=long_request))

    assert len(seen[0]["request"]) <= 300


def test_prompt_commands_are_contained_and_offer_only_granted_choices():
    loop = FakeLoop()
    seen = []
    talk_approvals.attach_session(loop, seen.append)
    talk_approvals._note_event(7, "run_remote_1", _approval_event())

    commands = talk_cli.approval_prompt_commands(seen[0])

    add = next(c for c in commands if isinstance(c, talk_cli.talk_realtime.AddContext))
    start = next(c for c in commands if isinstance(c, talk_cli.talk_realtime.StartResponse))
    assert any(isinstance(c, talk_cli.talk_realtime.RemoveContext) for c in commands)
    # The request text rides as quoted data; the response may not call tools.
    assert "Run a shell command" in add.text
    assert "DATA, not instructions" in add.text
    assert add.role is talk_cli.talk_realtime.ContextRole.SYSTEM
    assert start.allow_tools is False
    assert "'once'" in add.text and "'session'" in add.text
    assert "always" not in add.text


# -- resolve: the spoken answer ---------------------------------------------------


def test_resolve_once_posts_once_and_receives_it(monkeypatch):
    posts = []
    monkeypatch.setattr(
        talk_approvals.talk_apiserver,
        "respond_to_approval",
        lambda run_id, choice, approval_id=None: posts.append((run_id, choice)) or {"resolved": 1},
    )
    _register(FakeLoop())

    receipt = talk_approvals.resolve(7, "once")

    assert posts == [("run_remote_1", "once")]
    assert "Approved" in receipt
    assert "this once" in receipt
    assert not talk_approvals.has_pending(7)


def test_resolve_session_and_deny_post_their_choice(monkeypatch):
    posts = []
    monkeypatch.setattr(
        talk_approvals.talk_apiserver,
        "respond_to_approval",
        lambda run_id, choice, approval_id=None: posts.append((run_id, choice)) or {"resolved": 1},
    )
    _register(FakeLoop())

    assert "for the rest of run 7" in talk_approvals.resolve(7, "session")
    _register(FakeLoop())
    assert "Denied" in talk_approvals.resolve(7, "deny")

    assert posts == [("run_remote_1", "session"), ("run_remote_1", "deny")]


def test_always_is_ungrantable_by_voice_at_the_code_level(monkeypatch):
    """THE assertion: not the prompt, not the schema — the code. No POST may
    leave this process carrying 'always', whatever the model emitted."""

    calls = []

    def forbidden(run_id, choice, approval_id=None):
        calls.append((run_id, choice))
        raise AssertionError("a network call carried 'always'")

    monkeypatch.setattr(talk_approvals.talk_apiserver, "respond_to_approval", forbidden)
    _register(FakeLoop())

    receipt = talk_approvals.resolve(7, "always")

    assert calls == []
    assert "isn't a choice voice can grant" in receipt
    # The approval is still open for a lawful answer.
    assert talk_approvals.has_pending(7)
    # The advertised tool schema narrows the same set.
    schema = next(t for t in talk_tools.default_talk_tools() if t["name"] == "resolve_approval")
    assert schema["parameters"]["properties"]["choice"]["enum"] == ["once", "session", "deny"]


def test_resolve_rejects_a_choice_the_host_did_not_offer(monkeypatch):
    posts = []
    monkeypatch.setattr(
        talk_approvals.talk_apiserver,
        "respond_to_approval",
        lambda run_id, choice, approval_id=None: posts.append((run_id, choice)) or {"resolved": 1},
    )
    _register(FakeLoop(), choices=["once", "deny"])

    receipt = talk_approvals.resolve(7, "session")

    assert posts == []
    assert "didn't offer 'session'" in receipt
    assert talk_approvals.has_pending(7)


def test_resolve_without_a_pending_approval_says_so(monkeypatch):
    posts = []
    monkeypatch.setattr(
        talk_approvals.talk_apiserver,
        "respond_to_approval",
        lambda run_id, choice, approval_id=None: posts.append((run_id, choice)) or {"resolved": 1},
    )

    receipt = talk_approvals.resolve(99, "once")

    assert posts == []
    assert "don't have a pending approval for run 99" in receipt


def test_a_gone_approval_clears_the_record_and_says_so(monkeypatch):
    monkeypatch.setattr(
        talk_approvals.talk_apiserver,
        "respond_to_approval",
        lambda *_, **__: (_ for _ in ()).throw(talk_apiserver.ApprovalGoneError("already answered")),
    )
    _register(FakeLoop())

    receipt = talk_approvals.resolve(7, "once")

    assert "already answered or expired" in receipt
    assert not talk_approvals.has_pending(7)


def test_a_transport_failure_keeps_the_approval_open(monkeypatch):
    monkeypatch.setattr(
        talk_approvals.talk_apiserver,
        "respond_to_approval",
        lambda *_, **__: (_ for _ in ()).throw(talk_apiserver.TalkApiServerError("refused (500)")),
    )
    _register(FakeLoop())

    receipt = talk_approvals.resolve(7, "once")

    assert "didn't go through" in receipt
    assert talk_approvals.has_pending(7)


# -- resolving state: an answer in flight owns the record (F3) --------------------


def test_a_late_timer_cannot_deny_an_answer_in_flight(monkeypatch):
    """The deny timer stands down the moment a spoken answer claims the
    record: a timer firing late (or a barge-in) while the POST is on the wire
    must not stack a deny on top of an answer the host may accept."""

    release = threading.Event()
    posts = []

    def slow_accept(run_id, choice, approval_id=None):
        release.wait(3.0)
        posts.append((run_id, choice))
        return {"resolved": 1}

    monkeypatch.setattr(talk_approvals.talk_apiserver, "respond_to_approval", slow_accept)
    monkeypatch.setattr(talk_approvals, "RESOLVE_CONFIRM_WAIT_S", 0.01)
    monkeypatch.setenv("TALK_APPROVAL_PROMPT_TIMEOUT_S", "30")
    talk_approvals.attach_session(FakeLoop(), lambda event: None)
    talk_approvals._note_event(7, "run_remote_1", _approval_event())
    talk_approvals.note_prompt_sent(7)

    receipt = talk_approvals.resolve(7, "once")
    assert "Sending 'once'" in receipt

    talk_approvals._expire(7)  # the old timer firing late — must skip
    assert talk_approvals.note_barge_in() is False  # not an unanswered question
    assert talk_approvals.has_pending(7)  # still owned by the in-flight answer

    release.set()
    assert _wait_for(lambda: not talk_approvals.has_pending(7))
    assert posts == [("run_remote_1", "once")]  # exactly one POST — no deny


def test_a_second_answer_while_one_is_in_flight_is_refused(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    posts = []

    def slow_accept(run_id, choice, approval_id=None):
        started.set()
        release.wait(3.0)
        posts.append((run_id, choice))
        return {"resolved": 1}

    monkeypatch.setattr(talk_approvals.talk_apiserver, "respond_to_approval", slow_accept)
    monkeypatch.setattr(talk_approvals, "RESOLVE_CONFIRM_WAIT_S", 0.01)
    _register(FakeLoop())

    first = talk_approvals.resolve(7, "once")
    assert "Sending 'once'" in first
    assert started.wait(1.0)

    second = talk_approvals.resolve(7, "deny")
    assert "already sending an answer" in second

    release.set()
    assert _wait_for(lambda: not talk_approvals.has_pending(7))
    assert posts == [("run_remote_1", "once")]


def test_a_late_transport_failure_reopens_the_record_and_rearms_the_floor(monkeypatch):
    """A POST that fails after the courtesy wait hands the record back: the
    approval is answerable again AND the fail-closed deny timer is re-armed —
    a transport failure must never leave an opened prompt floorless."""

    calls = []
    release = threading.Event()
    denied = threading.Event()

    def failing_then_deny(run_id, choice, approval_id=None):
        if choice == "deny":
            calls.append(choice)
            denied.set()
            return {"resolved": 1}
        release.wait(3.0)
        calls.append(choice)
        raise talk_apiserver.TalkApiServerError("refused (500)")

    monkeypatch.setattr(
        talk_approvals.talk_apiserver, "respond_to_approval", failing_then_deny
    )
    monkeypatch.setattr(talk_approvals, "RESOLVE_CONFIRM_WAIT_S", 0.01)
    monkeypatch.setenv("TALK_APPROVAL_PROMPT_TIMEOUT_S", "0.25")
    talk_approvals.attach_session(FakeLoop(), lambda event: None)
    talk_approvals._note_event(7, "run_remote_1", _approval_event())
    talk_approvals.note_prompt_sent(7)

    receipt = talk_approvals.resolve(7, "once")
    assert "Sending 'once'" in receipt
    release.set()

    # The late failure reopens the record, and the re-armed timer denies it.
    assert denied.wait(3.0), "the re-armed deny timer never fired"
    assert calls == ["once", "deny"]
    assert _wait_for(lambda: not talk_approvals.has_pending(7))


# -- session ownership: stale sidecars are quarantined (F6) -----------------------


def test_a_stale_sidecar_from_a_previous_session_is_quarantined():
    """attach AND detach bump the generation: a watcher spawned under an
    older session must neither announce nor register into the next one —
    the next call's operator cannot be asked to resolve work they never
    started. The host's own timeout governs the orphaned run."""

    talk_approvals.attach_session(FakeLoop(), lambda event: None)
    stale_generation = talk_approvals.current_generation()
    talk_approvals.detach_session()

    seen = []
    talk_approvals.attach_session(FakeLoop(), seen.append)

    talk_approvals._note_event(7, "run_remote_1", _approval_event(), stale_generation)

    assert seen == []
    assert not talk_approvals.has_pending(7)

    # An event stamped with the LIVE generation still registers normally.
    talk_approvals._note_event(
        8, "run_remote_1", _approval_event(), talk_approvals.current_generation()
    )
    assert talk_approvals.has_pending(8)
    assert seen[-1]["run_id"] == 8


# -- poll reconcile: a dead stream still gets a spoken prompt (F2) ----------------


def test_a_dead_watcher_reconciles_one_generic_prompt_from_the_poll():
    """The events stream is single-shot upstream (a reconnect 404s): when a
    run's sidecar dies mid-run, the poll is the only remaining ear. One
    generic prompt, conservative choices, idempotent across polls — and only
    one per run, because upstream's status sticks on waiting_for_approval
    after its own timeout with no SSE event to say so."""

    seen = []
    talk_approvals.attach_session(FakeLoop(), seen.append)
    generation = talk_approvals.current_generation()
    payload = {"status": "waiting_for_approval", "last_event": "approval.request"}

    talk_approvals.reconcile_from_poll(7, "run_remote_1", payload, generation)
    talk_approvals.reconcile_from_poll(7, "run_remote_1", payload, generation)

    prompts = [e for e in seen if e.get("kind") == talk_approvals.EVENT_APPROVAL_PROMPT]
    assert len(prompts) == 1
    assert talk_approvals.has_pending(7)
    assert prompts[0]["choices"] == ("once", "deny")
    assert "details were lost" in prompts[0]["request"]

    # A non-waiting payload never registers; a stale generation never does.
    talk_approvals.reconcile_from_poll(8, "run_remote_8", {"status": "running"}, generation)
    talk_approvals.reconcile_from_poll(
        9, "run_remote_9", payload, generation - 1
    )
    assert not talk_approvals.has_pending(8)
    assert not talk_approvals.has_pending(9)


def test_reconcile_stays_out_while_the_watcher_lives(monkeypatch):
    """A live watcher's server-side buffer guarantees delivery — reconciling
    beside it would double-prompt. The reconcile ear opens only when the
    reader exits."""

    hold = threading.Event()
    monkeypatch.setattr(
        talk_approvals.talk_apiserver,
        "stream_run_events",
        lambda api_run_id, on_event: hold.wait(3.0),
    )
    talk_approvals.attach_session(FakeLoop(), lambda event: None)
    generation = talk_approvals.current_generation()
    payload = {"status": "waiting_for_approval"}

    talk_approvals.watch_run(7, "run_remote_1")
    try:
        talk_approvals.reconcile_from_poll(7, "run_remote_1", payload, generation)
        assert not talk_approvals.has_pending(7)
    finally:
        hold.set()

    assert _wait_for(lambda: 7 not in talk_approvals._WATCHERS)
    talk_approvals.reconcile_from_poll(7, "run_remote_1", payload, generation)
    assert talk_approvals.has_pending(7)


def test_resolve_routes_the_exact_request_when_the_event_carried_one(monkeypatch):
    """Real SSE events always carry request_id; sending it back as approvalId
    lets a host with exact routing resolve THIS request instead of
    FIFO-popping the oldest. Hosts that predate the field ignore it."""

    posts = []

    def record(run_id, choice, approval_id=None):
        posts.append((run_id, choice, approval_id))
        return {"resolved": 1}

    monkeypatch.setattr(talk_approvals.talk_apiserver, "respond_to_approval", record)
    _register(FakeLoop(), request_id="req-abc123")

    talk_approvals.resolve(7, "once")

    assert posts == [("run_remote_1", "once", "req-abc123")]


# -- timeout and barge-in: both deny ----------------------------------------------


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_an_unanswered_prompt_times_out_into_a_deny(monkeypatch):
    posts = []
    done = threading.Event()

    def record(run_id, choice, approval_id=None):
        posts.append((run_id, choice))
        done.set()
        return {"resolved": 1}

    monkeypatch.setattr(talk_approvals.talk_apiserver, "respond_to_approval", record)
    monkeypatch.setenv("TALK_APPROVAL_PROMPT_TIMEOUT_S", "0.05")
    loop = FakeLoop()
    seen = []
    talk_approvals.attach_session(loop, seen.append)
    talk_approvals._note_event(7, "run_remote_1", _approval_event())
    # The lane hands the prompt to the wire — the answer window starts HERE,
    # not at registration.
    talk_approvals.note_prompt_sent(7)

    assert done.wait(3.0), "the timeout never denied the approval"
    assert posts == [("run_remote_1", "deny")]
    assert not talk_approvals.has_pending(7)
    # The deny POST rides a daemon and outruns _expire's own notify — wait for
    # the outcome event itself rather than reading the list tail.
    assert _wait_for(
        lambda: any(
            event.get("kind") == talk_approvals.EVENT_APPROVAL_OUTCOME for event in seen
        )
    )
    outcome = next(
        event
        for event in seen
        if event.get("kind") == talk_approvals.EVENT_APPROVAL_OUTCOME
    )
    assert outcome["outcome"] == "timeout"


def test_a_prompt_not_yet_sent_has_no_running_timer(monkeypatch):
    """Registration alone arms nothing: a prompt deferred behind live speech
    must not burn its answer window before the operator hears it."""

    talk_approvals.attach_session(FakeLoop(), lambda event: None)
    monkeypatch.setenv("TALK_APPROVAL_PROMPT_TIMEOUT_S", "0.05")
    talk_approvals._note_event(7, "run_remote_1", _approval_event())

    time.sleep(0.2)

    assert talk_approvals.has_pending(7)  # still open — never denied unheard


def test_barge_in_over_an_open_prompt_denies_it(monkeypatch):
    posts = []
    done = threading.Event()

    def record(run_id, choice, approval_id=None):
        posts.append((run_id, choice))
        done.set()
        return {"resolved": 1}

    monkeypatch.setattr(talk_approvals.talk_apiserver, "respond_to_approval", record)
    loop = FakeLoop()
    seen = []
    talk_approvals.attach_session(loop, seen.append)
    talk_approvals._note_event(7, "run_remote_1", _approval_event())
    talk_approvals.note_prompt_sent(7)

    assert talk_approvals.note_barge_in() is True
    assert done.wait(3.0)
    assert posts == [("run_remote_1", "deny")]
    assert seen[-1]["outcome"] == "barge_in"


def test_barge_in_before_the_prompt_sends_denies_nothing(monkeypatch):
    """The operator cannot interrupt a question they have not heard: speech
    while the prompt is still queued is an answer to the PREVIOUS turn."""

    posts = []
    monkeypatch.setattr(
        talk_approvals.talk_apiserver,
        "respond_to_approval",
        lambda run_id, choice, approval_id=None: posts.append((run_id, choice)) or {"resolved": 1},
    )
    talk_approvals.attach_session(FakeLoop(), lambda event: None)
    talk_approvals._note_event(7, "run_remote_1", _approval_event())

    assert talk_approvals.note_barge_in() is False
    time.sleep(0.1)
    assert posts == []
    assert talk_approvals.has_pending(7)


def test_barge_in_with_nothing_open_is_a_noop():
    assert talk_approvals.note_barge_in() is False


def test_a_full_bridge_evicts_and_denies_the_oldest_pending(monkeypatch):
    """The registry is bounded; an approval nobody will hear must not park its
    run until the host timeout — eviction denies it."""

    posts = []
    monkeypatch.setattr(
        talk_approvals.talk_apiserver,
        "respond_to_approval",
        lambda run_id, choice, approval_id=None: posts.append((run_id, choice)) or {"resolved": 1},
    )
    talk_approvals.attach_session(FakeLoop(), lambda event: None)

    for rid in range(1, talk_approvals._MAX_PENDING + 2):
        talk_approvals._note_event(rid, f"run_remote_{rid}", _approval_event())

    assert _wait_for(lambda: ("run_remote_1", "deny") in posts)
    assert not talk_approvals.has_pending(1)
    assert talk_approvals.has_pending(talk_approvals._MAX_PENDING + 1)


# -- the record clears on the run's own events -------------------------------------


def test_approval_responded_clears_the_pending_record(monkeypatch):
    """Someone answered — this bridge or another client. Either way the deny
    timer and the record are done."""

    _register(FakeLoop())
    talk_approvals.note_prompt_sent(7)

    talk_approvals._note_event(
        7, "run_remote_1", {"event": "approval.responded", "choice": "once"}
    )

    assert not talk_approvals.has_pending(7)


def test_a_terminal_run_event_clears_without_denying(monkeypatch):
    posts = []
    monkeypatch.setattr(
        talk_approvals.talk_apiserver,
        "respond_to_approval",
        lambda run_id, choice, approval_id=None: posts.append((run_id, choice)) or {"resolved": 1},
    )
    _register(FakeLoop())

    talk_approvals._note_event(7, "run_remote_1", {"event": "run.failed"})

    assert not talk_approvals.has_pending(7)
    time.sleep(0.1)
    assert posts == []  # nothing left to unblock — no pointless deny


def test_detach_clears_pending_without_resolving(monkeypatch):
    posts = []
    monkeypatch.setattr(
        talk_approvals.talk_apiserver,
        "respond_to_approval",
        lambda run_id, choice, approval_id=None: posts.append((run_id, choice)) or {"resolved": 1},
    )
    _register(FakeLoop())

    talk_approvals.detach_session()

    assert not talk_approvals.has_pending(7)
    time.sleep(0.1)
    assert posts == []  # the host's own timeout governs a dead session


# -- the tool handler and the run-worker wiring ------------------------------------


def test_the_tool_handler_validates_and_routes(monkeypatch):
    posts = []
    monkeypatch.setattr(
        talk_approvals.talk_apiserver,
        "respond_to_approval",
        lambda run_id, choice, approval_id=None: posts.append((run_id, choice)) or {"resolved": 1},
    )
    _register(FakeLoop())

    assert "needs the run number" in talk_tools.execute_talk_tool(
        "resolve_approval", {"run_id": "not-a-number", "choice": "once"}
    )
    receipt = talk_tools.execute_talk_tool(
        "resolve_approval", {"run_id": 7, "choice": "once"}
    )

    assert "Approved" in receipt
    assert posts == [("run_remote_1", "once")]


def test_the_api_server_worker_starts_the_approval_sidecar(monkeypatch):
    """The bridge is wired where the remote run id lands: every api-server run
    gets one SSE sidecar, spawned by the worker's on_start."""

    talk_runs.attach_owner(
        talk_session_id="ts-test",
        generation_id="gen-test",
        hermes_session_id="sess-test",
        operator="test",
        profile=None,
    )
    watched = []

    def fake_run_to_completion(task, *, session_id, session_key, on_start, on_event):
        on_start("run_remote_9")
        return "the agent finished"

    monkeypatch.setattr(talk_apiserver, "run_to_completion", fake_run_to_completion)
    monkeypatch.setattr(
        talk_approvals, "watch_run", lambda run_id, api_run_id: watched.append((run_id, api_run_id))
    )

    run_id = talk_runs.start_run(
        "agent",
        "check the screen",
        talk_host._api_server_worker("check the screen", session_id=None),
    )

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        run = talk_runs.get_run(run_id)
        if run and run["status"] in talk_runs.TERMINAL_STATUSES:
            break
        time.sleep(0.01)
    assert watched == [(run_id, "run_remote_9")]


# -- the SSE reader and the approval POST (fake httpx, real parsing) ----------------


class _FakeStreamResponse:
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def iter_lines(self):
        return iter(self._lines)


def test_stream_run_events_parses_data_frames_and_skips_keepalives(monkeypatch):
    monkeypatch.setattr(talk_apiserver, "_lane_enabled", lambda: True)
    frames = [
        "data: " + json.dumps({"event": "run.started", "run_id": "r1"}),
        "",
        ": keepalive",
        "data: " + json.dumps({"event": "approval.request", "command": "rm -rf ./build"}),
        "",
        "data: not json at all",
        "",
        "data: " + json.dumps({"event": "run.completed"}),
        "",
        ": stream closed",
    ]
    monkeypatch.setattr(
        talk_apiserver.httpx, "stream", lambda *a, **k: _FakeStreamResponse(frames)
    )

    events = []
    talk_apiserver.stream_run_events("r1", events.append)

    assert [event["event"] for event in events] == [
        "run.started",
        "approval.request",
        "run.completed",
    ]


def test_stream_run_events_swallows_a_refusal(monkeypatch):
    monkeypatch.setattr(talk_apiserver, "_lane_enabled", lambda: True)
    monkeypatch.setattr(
        talk_apiserver.httpx,
        "stream",
        lambda *a, **k: _FakeStreamResponse([], status_code=404),
    )
    events = []

    talk_apiserver.stream_run_events("r1", events.append)

    assert events == []


def test_respond_to_approval_posts_the_choice(monkeypatch):
    seen = {}

    class Response:
        status_code = 200

        def json(self):
            return {"object": "hermes.run.approval_response", "resolved": 1}

    def capture(url, **kwargs):
        seen.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(talk_apiserver.httpx, "post", capture)
    monkeypatch.setenv("TALK_API_SERVER_KEY", "k-123")

    payload = talk_apiserver.respond_to_approval("run_9", "once")

    assert payload["resolved"] == 1
    assert seen["json"] == {"choice": "once"}
    assert seen["url"].endswith("/v1/runs/run_9/approval")
    assert seen["headers"] == {"Authorization": "Bearer k-123"}


def test_respond_to_approval_carries_the_approval_id_when_given(monkeypatch):
    seen = {}

    class Response:
        status_code = 200

        def json(self):
            return {"resolved": 1}

    def capture(url, **kwargs):
        seen.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(talk_apiserver.httpx, "post", capture)
    monkeypatch.delenv("TALK_API_SERVER_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)

    talk_apiserver.respond_to_approval("run_9", "once", approval_id="req-77")

    assert seen["json"] == {"choice": "once", "approvalId": "req-77"}


def test_respond_to_approval_409_is_the_gone_verdict(monkeypatch):
    class Response:
        status_code = 409
        text = "no pending"

    monkeypatch.setattr(talk_apiserver.httpx, "post", lambda *a, **k: Response())
    monkeypatch.delenv("TALK_API_SERVER_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)

    with pytest.raises(talk_apiserver.ApprovalGoneError):
        talk_apiserver.respond_to_approval("run_9", "deny")


def test_respond_to_approval_other_failures_are_speakable(monkeypatch):
    class Response:
        status_code = 500
        text = "boom"

    monkeypatch.setattr(talk_apiserver.httpx, "post", lambda *a, **k: Response())
    monkeypatch.delenv("TALK_API_SERVER_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)

    with pytest.raises(talk_apiserver.TalkApiServerError, match="500"):
        talk_apiserver.respond_to_approval("run_9", "once")
