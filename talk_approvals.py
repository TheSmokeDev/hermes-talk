"""The spoken approval bridge — voice resolves run approvals out loud.

A delegated run on the api-server lane parks in ``waiting_for_approval`` when
its agent hits a gated action, and until now the only resolver on the voice
surface was silence: the host's own 300s wait eventually denied it. This
module bridges the host's approval substrate
(``GET /v1/runs/{id}/events`` SSE + ``POST /v1/runs/{id}/approval``) into
speech:

1. Every api-server run gets an SSE sidecar (:func:`watch_run`, spawned by
   the run worker when the remote run id lands). An ``approval.request``
   event registers a pending approval and announces a prompt through the live
   Talk session — same contained-announcement channel as run results.
2. The operator's spoken answer comes back as the model calling the
   ``resolve_approval`` talk tool — which on Discord rides the existing
   spoken-permit machinery (``talk_operator_auth``: fresh operator speech,
   bounded window, single use) before :func:`resolve` runs at all.
3. :func:`resolve` POSTs the choice. **``always`` is never grantable by
   voice** — the grantable set is narrowed in code here
   (:data:`GRANTABLE_BY_VOICE`), in the tool schema, and in the prompt
   wording; this module is the choke point.
4. Fail closed on everything ambiguous: the prompt times out into a deny
   (:data:`talk_config.approval_prompt_timeout_s`), and a barge-in that
   interrupts an open prompt denies it (:func:`note_barge_in`) — a question
   not fully heard is not a question answered.

The operator's own answer is the ONLY authorization input. The model relays
it; it cannot mint one, because on the Discord lane the tool call itself
needs the permit the operator's speech just created.

Threading mirrors :mod:`talk_lifecycle`: the SSE reader and deny timers live
on daemon threads, the session callback is marshalled onto the session loop
with ``loop.call_soon_threadsafe``, and every entry point is fail-open.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any

try:
    from . import talk_apiserver, talk_config, talk_runs
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_apiserver
    import talk_config
    import talk_runs

_log = logging.getLogger(__name__)

#: What voice may grant. ``always`` is absent BY CONSTRUCTION: it would
#: outlive the call that granted it, and no spoken sentence may do that.
#: Narrowed again per-request against the host's own offered set.
GRANTABLE_BY_VOICE = ("once", "session", "deny")

#: Event kinds handed to the session callback.
EVENT_APPROVAL_PROMPT = "approval_prompt"
EVENT_APPROVAL_OUTCOME = "approval_outcome"

#: How long the resolve POST politely waits before its receipt detaches (the
#: same shape as stop_work's STOP_CONFIRM_WAIT_S): long enough that the common
#: path speaks the real outcome, short enough that a wedged server cannot
#: dead-air the call.
RESOLVE_CONFIRM_WAIT_S = 1.5

#: Pending approvals are few by nature; the cap exists so a runaway producer
#: (a host that re-fires requests) cannot grow the registry without bound.
#: Eviction denies the evicted — an approval nobody will hear must not park
#: its run until the host's own timeout.
_MAX_PENDING = 8

#: The cap on request text carried into a spoken prompt. The host already
#: redacts secrets from the command; this bound is for the ear, not the wire.
_REQUEST_TEXT_CAP = 300

_LOCK = threading.Lock()
_PENDING: dict[int, _PendingApproval] = {}
_SESSION: tuple[Any, Any] | None = None  # (loop, callback)
#: Attach generation, bumped by every attach AND detach (the sibling owner
#: gate to talk_progress/talk_lifecycle's owner_session_id). A run's SSE
#: sidecar snapshots it at spawn; events from an older generation are
#: quarantined wholesale — never announced, never registered — so a run
#: outliving its session cannot route an approval into the NEXT session's
#: call. The host's own approval timeout governs those runs, exactly the
#: pre-bridge behavior.
_GENERATION = 0
#: run_ids with a live SSE sidecar (spawn → reader exit). While a run's
#: watcher lives, the host's pre-created server-side buffer guarantees its
#: approval events are delivered, so the poll reconcile stays out; when the
#: watcher dies mid-run (the stream is single-shot — a reconnect 404s), the
#: reconcile below is the only remaining ear.
_WATCHERS: set[int] = set()
#: run_ids already given their one reconcile prompt. The host's run status
#: sticks on ``waiting_for_approval`` after its own timeout auto-deny (and
#: emits no SSE event for it), so without this stamp a dead approval would
#: re-prompt on every poll forever.
_RECONCILED: set[int] = set()


@dataclass(slots=True)
class _PendingApproval:
    """One unanswered approval request registered from an SSE event.

    ``opened`` flips only when the prompt was actually handed to the wire —
    the deny timer arms then, not at registration, so a prompt deferred
    behind live speech never times out before the operator has heard it.

    ``resolving`` flips while a spoken answer's POST is in flight: the deny
    timer stands down (an answer on the wire is not silence), barge-in skips
    it (it is not an unanswered question), and a second resolve is refused.
    A transport failure reopens the record via :func:`_reopen`; a late
    ``ok``/``gone`` verdict finalizes it — the record can never be denied on
    top of an answer the host already accepted.
    """

    api_run_id: str
    request_text: str
    choices: tuple[str, ...]
    #: The host's approval request id (always present on real SSE events).
    #: Sent back as ``request_id`` so a host that supports exact routing
    #: resolves THIS request instead of FIFO-popping the oldest; ``None``
    #: for reconciled prompts, where the id was lost with the stream.
    request_id: str | None = None
    opened: bool = False
    resolving: bool = False
    timer: threading.Timer | None = None

    def cancel_timer(self) -> None:
        timer, self.timer = self.timer, None
        if timer is not None:
            timer.cancel()


def _spawn_daemon(fn, *args, name: str = "talk-approval") -> None:
    """Fire-and-forget worker. Daemon by design: a deny POST still in flight
    at hangup must never stall process exit."""

    threading.Thread(target=fn, args=args, daemon=True, name=name).start()


def attach_session(loop: Any, callback: Any) -> None:
    """Bind the live Talk session as the announcement target.

    Same contract as :func:`talk_lifecycle.attach_session`: ``callback`` runs
    ON the loop via ``call_soon_threadsafe`` and owns its own scheduling; one
    session at a time, last attach wins. While nothing is bound, prompts are
    not announced and nothing is denied from here — the host's own approval
    timeout governs, which is exactly the pre-bridge behavior the dashboard
    lane keeps.
    """

    global _SESSION, _GENERATION
    with _LOCK:
        _GENERATION += 1
        _SESSION = (loop, callback)


def detach_session() -> None:
    """Announcements stop; pending records clear without resolving.

    A cleared record is NOT a denial: with no live session there is nobody to
    answer, and the host's own timeout fails the approval closed. The remote
    run outlives this process's session state by design. The generation bump
    orphans every live sidecar: whatever they hear next belongs to a session
    that no longer exists.
    """

    global _SESSION, _GENERATION
    with _LOCK:
        _GENERATION += 1
        _SESSION = None
        for pending in _PENDING.values():
            pending.cancel_timer()
        _PENDING.clear()
        _RECONCILED.clear()


def current_generation() -> int:
    """The attach generation right now — a sidecar's spawn stamp."""

    with _LOCK:
        return _GENERATION


def has_pending(run_id: int) -> bool:
    """Whether the bridge owns an approval for this run right now.

    Read by the lane's progress watcher: while the bridge owns it, the
    generic "waiting on an approval" milestone stays silent — the spoken
    prompt is the actionable sentence.
    """

    with _LOCK:
        return run_id in _PENDING


def pending_choices(run_id: int) -> tuple[str, ...] | None:
    with _LOCK:
        pending = _PENDING.get(run_id)
        return pending.choices if pending is not None else None


def _notify(event: dict) -> None:
    """Marshal one bridge event to the owning Talk session, fail-open."""

    with _LOCK:
        session = _SESSION
    if session is None:
        return
    loop, callback = session
    try:
        loop.call_soon_threadsafe(callback, event)
    except RuntimeError:
        # The loop closed between snapshot and call — the session is tearing
        # down and the event has nowhere to go. Not an error.
        return
    except Exception:  # noqa: BLE001 — a notify must never escape a worker
        _log.debug("approval bridge notify failed", exc_info=True)


def _request_text(event: dict) -> str:
    """The speakable request from an approval.request payload, bounded.

    The host redacts secrets from ``command`` before the event enters the SSE
    stream (``gateway/run.py _redact_approval_command``); the cap here is for
    the ear. Description first — it names the action class; the command is
    the fallback for approvals that carry only one.
    """

    for key in ("description", "command"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_REQUEST_TEXT_CAP]
    return "an action the host gates"


def _narrow_choices(event: dict) -> tuple[str, ...]:
    """Voice-grantable ∩ host-offered. ``always`` cannot survive either side.

    Fail closed on shape: the host always emits ``choices`` as a list of
    strings (api_server ``_approval_event_choices``), so a missing, non-list,
    or unrecognizable value is schema drift or forgery — the answer set
    collapses to deny-only instead of widening to everything voice could
    grant. ``once``/``session`` are offered ONLY when those exact strings
    appear in the host-offered list.
    """

    offered = event.get("choices")
    if not isinstance(offered, list):
        return ("deny",)
    offered_names = {choice for choice in offered if isinstance(choice, str)}
    narrowed = tuple(choice for choice in GRANTABLE_BY_VOICE if choice in offered_names)
    # "deny" is always an answer, even when the host's list is unreadable.
    return narrowed or ("deny",)


def _register(run_id: int, api_run_id: str, event: dict) -> None:
    """Register one approval.request and announce it, or evict-deny for room."""

    raw_request_id = event.get("request_id")
    pending = _PendingApproval(
        api_run_id=api_run_id,
        request_text=_request_text(event),
        choices=_narrow_choices(event),
        request_id=(
            raw_request_id
            if isinstance(raw_request_id, str) and raw_request_id
            else None
        ),
    )
    evicted: _PendingApproval | None = None
    evicted_run_id: int | None = None
    with _LOCK:
        if run_id in _PENDING:
            _PENDING.pop(run_id).cancel_timer()
        if len(_PENDING) >= _MAX_PENDING:
            # Never evict a record whose answer is in flight — denying an
            # approval the host may have just accepted is the contradiction
            # F3 exists to prevent. All-resolving overflow is brief (each
            # verdict finalizes or reopens) and bounded by the cap itself.
            evicted_run_id = next(
                (rid for rid, p in _PENDING.items() if not p.resolving), None
            )
            if evicted_run_id is not None:
                evicted = _PENDING.pop(evicted_run_id)
        _PENDING[run_id] = pending
    if evicted is not None:
        _log.warning(
            "approval bridge full — denying the oldest pending approval (run %s)",
            evicted_run_id,
        )
        evicted.cancel_timer()
        _spawn_daemon(
            _post_choice,
            evicted.api_run_id,
            "deny",
            evicted.request_id,
            name="talk-approval-evict",
        )
    _log.info(
        "run %s is waiting on an approval (%s) — prompting the operator",
        run_id,
        pending.choices,
    )
    _notify(
        {
            "kind": EVENT_APPROVAL_PROMPT,
            "run_id": run_id,
            "request": pending.request_text,
            "choices": pending.choices,
        }
    )


def _clear(run_id: int) -> _PendingApproval | None:
    """Pop a pending record and cancel its deny timer."""

    with _LOCK:
        pending = _PENDING.pop(run_id, None)
    if pending is not None:
        pending.cancel_timer()
    return pending


def note_prompt_sent(run_id: int) -> None:
    """The prompt hit the wire: arm the fail-closed deny timer.

    Called from the announcement pump's post-send hook. A prompt that never
    sends (teardown mid-queue) never arms — its record clears at detach, and
    the host's own timeout denies the approval.
    """

    with _LOCK:
        pending = _PENDING.get(run_id)
        if pending is None or pending.opened:
            return
        pending.opened = True
        if pending.resolving:
            # The answer beat the prompt to the wire (a deferred announcement
            # can land after the operator already spoke). Record the send;
            # arm nothing — _reopen restores the floor if the answer fails.
            return
        timer = threading.Timer(
            talk_config.approval_prompt_timeout_s(), _expire, args=(run_id,)
        )
        timer.daemon = True
        pending.timer = timer
        timer.start()


def _post_choice(
    api_run_id: str, choice: str, request_id: str | None = None
) -> tuple[str, str]:
    """One resolve POST. Returns (kind, detail): "ok", "gone", or "err"."""

    try:
        talk_apiserver.respond_to_approval(api_run_id, choice, approval_id=request_id)
    except talk_apiserver.ApprovalGoneError as exc:
        return ("gone", str(exc))
    except Exception as exc:  # noqa: BLE001 — the outcome IS the record
        return ("err", f"{type(exc).__name__}: {exc}")
    return ("ok", "")


def _annotate(run_id: int, outcome: str) -> None:
    """Record an approval resolution on the run, like a stop receipt."""

    try:
        talk_runs.annotate_run(run_id, tee=True, approval_result=outcome)
    except Exception:  # noqa: BLE001 — a receipt must never cost the run
        _log.debug("approval receipt annotation failed for run %s", run_id, exc_info=True)


def _reopen(run_id: int, pending: _PendingApproval) -> None:
    """A transport failure: the answer never landed, so the record answers
    again. Restore the fail-closed floor — an opened prompt re-arms a FRESH
    deny timer (generous, but bounded; the host's own approval timeout is
    the outer floor either way)."""

    with _LOCK:
        current = _PENDING.get(run_id)
        if current is not pending:
            return
        current.resolving = False
        if current.opened and current.timer is None:
            timer = threading.Timer(
                talk_config.approval_prompt_timeout_s(), _expire, args=(run_id,)
            )
            timer.daemon = True
            current.timer = timer
            timer.start()


def _expire(run_id: int) -> None:
    """Timer fire: the prompt went unanswered — deny. Silence is not consent."""

    with _LOCK:
        pending = _PENDING.get(run_id)
        if pending is None or pending.resolving:
            # Answered, cleared, or an answer is in flight — not silence.
            # A skipped timer is spent: drop the handle so a later _reopen
            # can arm a fresh one.
            if pending is not None:
                pending.timer = None
            return
        del _PENDING[run_id]
    pending.cancel_timer()
    _log.warning("approval prompt for run %s timed out — denying", run_id)
    _annotate(run_id, "denied: no answer before the prompt timed out")
    _spawn_daemon(
        _post_choice,
        pending.api_run_id,
        "deny",
        pending.request_id,
        name="talk-approval-timeout",
    )
    _notify(
        {
            "kind": EVENT_APPROVAL_OUTCOME,
            "run_id": run_id,
            "outcome": "timeout",
        }
    )


def note_barge_in() -> bool:
    """A barge-in over an OPEN approval prompt denies it. True if one fired.

    The lane calls this only when a response was actually live at speech
    start, so an answer spoken AFTER the prompt finished is never misread
    as an interruption of it.
    """

    with _LOCK:
        interrupted = [
            (run_id, p) for run_id, p in _PENDING.items() if p.opened and not p.resolving
        ]
        for run_id, _pending in interrupted:
            del _PENDING[run_id]
    denied = False
    for run_id, pending in interrupted:
        pending.cancel_timer()
        denied = True
        _log.warning("approval prompt for run %s interrupted — denying", run_id)
        _annotate(run_id, "denied: the operator interrupted the approval question")
        _spawn_daemon(
            _post_choice,
            pending.api_run_id,
            "deny",
            pending.request_id,
            name="talk-approval-barge",
        )
        _notify(
            {
                "kind": EVENT_APPROVAL_OUTCOME,
                "run_id": run_id,
                "outcome": "barge_in",
            }
        )
    return denied


def resolve(run_id: int, choice: str) -> str:
    """The resolve_approval tool's brain: validate, POST, receipt.

    NEVER raises — the return text is spoken. The choice set is narrowed
    HERE, in code: ``always`` (and anything else outside
    :data:`GRANTABLE_BY_VOICE`) is rejected before any network call, whatever
    the model emitted.
    """

    choice = str(choice or "").strip().lower()
    if choice not in GRANTABLE_BY_VOICE:
        # Not a denial of this approval — a refusal of the CHOICE itself.
        return (
            f"'{choice or 'that'}' isn't a choice voice can grant — the choices are "
            "once, session, or deny. If the operator asked for always, offer session."
        )
    with _LOCK:
        pending = _PENDING.get(run_id)
        claimed = (
            pending is not None and choice in pending.choices and not pending.resolving
        )
        if claimed:
            # The answer owns the record now: the deny timer stands down (a
            # spoken answer on the wire is not silence), barge-in skips it,
            # and a second resolve is refused until this verdict lands. A
            # transport failure hands the record back via _reopen.
            pending.resolving = True
            pending.cancel_timer()
    if pending is None:
        return (
            f"I don't have a pending approval for run {run_id} — it may already "
            "be answered or timed out."
        )
    if choice not in pending.choices:
        return (
            f"The host didn't offer '{choice}' for this one — it offered: "
            f"{', '.join(pending.choices)}."
        )
    if not claimed:
        return (
            f"I'm already sending an answer for run {run_id} — ask me in a "
            "moment and I'll have the receipt."
        )

    # Off the courtesy-wait path: the POST runs on a daemon with a bounded
    # wait, exactly like stop_work's api-server branch — a wedged server must
    # never dead-air the voice loop.
    outcomes: queue.Queue = queue.Queue(maxsize=1)

    def _post() -> None:
        outcomes.put(_post_choice(pending.api_run_id, choice, pending.request_id))

    _spawn_daemon(_post, name="talk-approval-resolve")
    try:
        verdict, detail = outcomes.get(timeout=RESOLVE_CONFIRM_WAIT_S)
    except queue.Empty:
        # The answer is on its way; the late verdict lands on the run's meta
        # where check_work can read it — a daemon receipt dies with the
        # process, so the durable record carries it (same finding as stop).
        def _late(
            _rid: int = run_id,
            _choice: str = choice,
            _pending: _PendingApproval = pending,
        ) -> None:
            late_kind, late_detail = outcomes.get()
            _annotate(_rid, f"{_choice}: {_late_wording(late_kind, late_detail)}")
            if late_kind in ("ok", "gone"):
                # The host accepted (or had already settled) this approval —
                # finalize the record so no timer or barge-in can deny on top
                # of an answer that landed.
                _clear(_rid)
            else:
                _reopen(_rid, _pending)

        _spawn_daemon(_late, name="talk-approval-late")
        return (
            f"Sending '{choice}' for run {run_id} — the server hasn't answered "
            "yet; ask me in a moment and I'll have the receipt."
        )
    if verdict == "err":
        _reopen(run_id, pending)
        return (
            f"That answer didn't go through ({detail}) — the approval for run "
            f"{run_id} is still open; answer again, or let it time out denied."
        )
    if verdict == "gone":
        _clear(run_id)
        _annotate(run_id, f"{choice}: the host says it was already answered or expired")
        return f"The host says run {run_id}'s approval was already answered or expired."

    _clear(run_id)
    _annotate(run_id, f"{choice}: accepted")
    if choice == "deny":
        return (
            f"Denied — run {run_id} was told no. It will adapt or stop, and "
            "I'll report when it lands."
        )
    if choice == "session":
        return (
            f"Approved for the rest of run {run_id} — that's as far as voice "
            "goes; there is no always. I'll tell you when it lands."
        )
    return (
        f"Approved — just this once. Run {run_id} is continuing; I'll tell "
        "you when it lands."
    )


def _late_wording(verdict: str, detail: str) -> str:
    return {
        "ok": "accepted (late receipt)",
        "gone": "already answered or expired (late receipt)",
    }.get(verdict, f"failed (late receipt): {detail}")


def _note_event(
    run_id: int, api_run_id: str, event: Any, generation: int | None = None
) -> None:
    """Route one SSE event from the run's own stream.

    ``generation`` is the sidecar's spawn stamp; an event from an older
    generation is quarantined wholesale — the run belongs to a session that
    is gone, and the current call must neither hear nor resolve it. ``None``
    (direct callers) means current.
    """

    if generation is not None and generation != current_generation():
        _log.debug(
            "quarantined a stale approval sidecar event for run %s (generation %s)",
            run_id,
            generation,
        )
        return
    if not isinstance(event, dict):
        return
    kind = event.get("event")
    if kind == "approval.request":
        _register(run_id, api_run_id, event)
    elif kind == "approval.responded":
        # Resolved — by this bridge or by any other client. Either way the
        # pending record and its deny timer are done.
        _clear(run_id)
    elif kind in ("run.completed", "run.failed", "run.cancelled"):
        # The run is over; a parked approval is moot. No deny POST — there is
        # nothing left to unblock, and the host has already moved on.
        _clear(run_id)
        with _LOCK:
            _RECONCILED.discard(run_id)


def watch_run(run_id: int, api_run_id: str) -> None:
    """Spawn the SSE sidecar for one api-server run. Never raises.

    One extra connection per run, opened when the remote run id lands and
    closed by the host when the run ends. Only approval events act; anything
    else the stream carries is ignored here — progress narration stays with
    the proven poll loop, unchanged.
    """

    generation = current_generation()
    with _LOCK:
        _WATCHERS.add(run_id)

    def _reader() -> None:
        try:
            talk_apiserver.stream_run_events(
                api_run_id,
                lambda event: _note_event(run_id, api_run_id, event, generation),
            )
        except Exception:  # noqa: BLE001 — the sidecar degrades silently by design
            _log.debug("approval watcher for run %s ended early", run_id, exc_info=True)
        finally:
            # The stream is single-shot (a reconnect 404s): once the reader
            # exits, the poll reconcile is this run's only remaining ear.
            with _LOCK:
                _WATCHERS.discard(run_id)

    threading.Thread(
        target=_reader, daemon=True, name=f"talk-approval-watch-{run_id}"
    ).start()


def reconcile_from_poll(
    run_id: int, api_run_id: str, payload: Any, generation: int
) -> None:
    """Speak for an approval whose SSE sidecar died. Never raises.

    The host buffers a run's events server-side from creation, so a LIVE
    watcher always hears ``approval.request`` — this reconcile registers a
    prompt only for a run whose watcher is gone. The poll payload carries no
    approval details (upstream keeps them out of the status record), so the
    prompt is generic and the choices conservative: "once" appears in every
    host-offered set, "session" does not. One reconcile per run: upstream's
    status sticks on ``waiting_for_approval`` after its own timeout auto-deny
    (with no SSE event), so a repeat prompt could be asking about an approval
    that no longer exists — the resolve's 409 already answers that case with
    "already answered or expired".
    """

    if not isinstance(payload, dict):
        return
    if str(payload.get("status") or "") != "waiting_for_approval":
        return
    with _LOCK:
        if (
            generation != _GENERATION
            or _SESSION is None
            or run_id in _PENDING
            or run_id in _WATCHERS
            or run_id in _RECONCILED
        ):
            return
        _RECONCILED.add(run_id)
    _log.info(
        "run %s is waiting on an approval but its event stream is gone — "
        "reconciling a prompt from the poll",
        run_id,
    )
    _register(
        run_id,
        api_run_id,
        {
            "description": (
                f"run {run_id} is waiting on an approval, but the details were "
                "lost with its event stream"
            ),
            "choices": ["once", "deny"],
        },
    )


def reset_for_tests() -> None:
    """Clear bridge state between tests (never called in production)."""

    detach_session()
    with _LOCK:
        _WATCHERS.clear()
        _RECONCILED.clear()


__all__ = [
    "EVENT_APPROVAL_OUTCOME",
    "EVENT_APPROVAL_PROMPT",
    "GRANTABLE_BY_VOICE",
    "RESOLVE_CONFIRM_WAIT_S",
    "attach_session",
    "current_generation",
    "detach_session",
    "has_pending",
    "note_barge_in",
    "note_prompt_sent",
    "pending_choices",
    "reconcile_from_poll",
    "reset_for_tests",
    "resolve",
    "watch_run",
]
