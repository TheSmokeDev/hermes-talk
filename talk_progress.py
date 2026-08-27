"""Bounded progress phases for background work (hermes-talk#33).

A Talk session used to know two things about a background job: it started
(the ``WORK_STARTED`` receipt) and it landed (the terminal announcement).
This module is the middle: host-authoritative events are projected onto a
bounded phase vocabulary — ``accepted``, ``executing``, ``blocked``,
``complete``, ``failed``, ``stopped`` — so a live session hears concise
milestones instead of silence, and the visual lane reads the same phase off
``meta.phase`` for free (``list_runs`` already surfaces meta).

Three rules are the whole design, and they are the epic #32 invariants:

- **Claims never exceed host evidence.** A phase is set only from a real
  host signal: the api_server run-status ``last_event``
  (``gateway/platforms/api_server.py`` ``_set_run_status``), an in-process
  plugin hook (``post_tool_call`` at ``model_tools.py:1136``,
  ``pre_approval_request`` at ``tools/approval.py:172``,
  ``subagent_start``/``subagent_stop`` at ``tools/delegate_tool.py:1930``
  and ``:3285``), or nothing. ``inspecting``/``verifying`` do not exist here
  because the host emits no evidence for them. An event this module cannot
  map is NOT a phase change — the phase stays at the last evidenced one.
- **Telemetry is never authority.** Writing a terminal phase into meta is a
  receipt OF a terminal artifact, never a substitute for one: registry
  status still flips only through ``talk_runs.finish_run``, and delivery is
  still claimed only by the terminal announcement path. Nothing here calls
  ``claim_delivery`` — phase speech is ephemeral and re-sayable; the result
  is not.
- **Routing keys on correlators, never recency.** A hook event projects
  into a run only when the event's session id exactly matches a correlator
  that run recorded about itself (the api_server-assigned session id, read
  off the poll payload), and into a delegated child only when that child was
  started under THIS Talk session's attached parent session. Two concurrent
  jobs cannot cross-route because neither projection ever consults "the
  most recent" anything.

Redaction is positional, not textual: the only job-specific detail that can
leave this module is a safe tool LABEL from the mapping table below ("Reading
files", "Running tests"). Unknown tools degrade to "Working". Arguments,
paths, URLs, output text, and approval commands never enter a phase, a
detail, or an announcement — there is no code path that carries them.

**What "session start" means for the hooks.** The host's hook registry is
process-scoped — there is no per-Talk-session registration surface — so the
two hooks this module adds (``post_tool_call``,
``pre_approval_request``) are registered once at plugin load next to the
existing lifecycle pair, and the SESSION gate is :func:`attach_session` /
:func:`detach_session`: child tracking and all milestone speech are inert
while no Talk session owns them. (The run-meta projection is correlator-gated
instead — it annotates only a run that recorded the matching session id
itself, so it is safe with no session attached, and the dashboard's run list
reads it.) Threading matches :mod:`talk_lifecycle`: hooks fire on host
threads, every handler is fail-open (a hook must never raise into the host),
and the one hand-off to the live session is ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

try:
    from . import talk_runs
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_runs

_log = logging.getLogger(__name__)

# -- the bounded phase vocabulary ----------------------------------------------

PHASE_ACCEPTED = "accepted"
PHASE_EXECUTING = "executing"
PHASE_BLOCKED = "blocked"
PHASE_COMPLETE = "complete"
PHASE_FAILED = "failed"
PHASE_STOPPED = "stopped"

PHASES = frozenset(
    {
        PHASE_ACCEPTED,
        PHASE_EXECUTING,
        PHASE_BLOCKED,
        PHASE_COMPLETE,
        PHASE_FAILED,
        PHASE_STOPPED,
    }
)

#: Terminal phases name an outcome the host already proved. They are recorded
#: durably (a reconnect's run list reads them off meta), but they are never
#: SPOKEN by the phase path — the terminal announcement owns that sentence.
TERMINAL_PHASES = frozenset({PHASE_COMPLETE, PHASE_FAILED, PHASE_STOPPED})

#: Spoken-lane liveness bound: at most one "still working" per run per
#: minute, and only after a full interval with no milestone speech of any
#: kind. Long quiet work should sound alive; it should never sound busy.
HEARTBEAT_S = 60.0

#: The api_server poll payload's ``last_event`` → phase (tier 2). Only events
#: the host actually stamps on the run status are mapped: ``run.queued`` is
#: the documented creation event and ``run.started`` is the first transition
#: the 0.20 host actually stamps (api_server.py:3918); both mean "the host
#: has the job". ``approval.responded``, ``run.steered``, ``run.stopping``,
#: and anything unrecognized map to NOTHING — no evidence, no phase change.
_API_EVENT_PHASES = {
    "run.queued": PHASE_ACCEPTED,
    "run.started": PHASE_ACCEPTED,
    "tool.started": PHASE_EXECUTING,
    "approval.request": PHASE_BLOCKED,
    "run.completed": PHASE_COMPLETE,
    "run.failed": PHASE_FAILED,
    "run.cancelled": PHASE_STOPPED,
}


def phase_for_api_event(last_event: Any) -> str | None:
    """Map one api_server ``last_event`` string to a phase, or ``None``.

    ``None`` is a first-class answer: unknown, absent, or unmappable events
    leave the run at its last evidenced phase.
    """

    if not isinstance(last_event, str):
        return None
    return _API_EVENT_PHASES.get(last_event)


#: The tier-1 terminal mapping (``subagent_stop`` status → outcome) is owned
#: by the existing lifecycle announcement path — ``talk_cli``'s
#: ``_SUBAGENT_STOP_VERBS`` already speaks ok/error/timeout/interrupted from
#: the host's stop event, which is the authoritative terminal artifact for an
#: attached child. This module deliberately does not re-map it: a second
#: "complete" derived from the same event would be a completion claim built
#: from telemetry, which is exactly what the authority invariant forbids.


# -- redaction: tool name → safe spoken label -----------------------------------

#: The ONLY detail a progress update may carry about what a job is doing.
#: Labels describe the KIND of work in operator-safe words; the table is
#: keyed by the host's canonical tool names, and the value is all that can
#: ever leave here — never the name's arguments, never a path, never output.
_TOOL_LABELS = {
    "read_file": "Reading files",
    "write_file": "Writing files",
    "patch": "Editing files",
    "search_files": "Searching files",
    "session_search": "Searching past sessions",
    "memory": "Updating memory",
    "web_search": "Searching the web",
    "web_extract": "Reading a page",
    "x_search": "Searching X",
    "terminal": "Running commands",
    "execute_code": "Running code",
    "process": "Managing processes",
    "computer_use": "Using the computer",
    "delegate_task": "Delegating work",
    "clarify": "Asking a question",
    "cronjob": "Scheduling work",
    "image_generate": "Generating an image",
    "tool_search": "Looking up tools",
}

#: Family prefixes for hosts that mint per-action tool names (browser_click,
#: browser_navigate, …). Checked only after the exact table misses.
_TOOL_LABEL_PREFIXES = (("browser_", "Browsing"),)

#: The degradation for anything unmapped. Generic ON PURPOSE: inventing a
#: label from an unknown tool's name would put host-controlled text into the
#: operator's ear, and the name itself can carry a path or a URL.
TOOL_LABEL_FALLBACK = "Working"


def tool_label(tool_name: Any) -> str:
    """The safe spoken label for a tool call. Never anything but a label."""

    if not isinstance(tool_name, str):
        return TOOL_LABEL_FALLBACK
    label = _TOOL_LABELS.get(tool_name)
    if label is not None:
        return label
    for prefix, prefixed in _TOOL_LABEL_PREFIXES:
        if tool_name.startswith(prefix):
            return prefixed
    return TOOL_LABEL_FALLBACK


# -- run projection (tier 2 poll loop + same-process hooks) ---------------------


def set_run_phase(run_id: int, phase: str, *, detail: str | None = None) -> bool:
    """Write a phase onto a live run's meta when — and only when — it changed.

    Returns True when this call wrote. Three guards make the write safe:

    - A run that is already terminal (or gone) is never re-phased: the
      registry's first-writer-wins terminal status is the authority, and a
      late-arriving event must not reopen it.
    - A TERMINAL phase already recorded is equally sticky: it is the receipt
      of a terminal artifact, and a straggler tool event landing in the
      annotate→finish gap must not rewrite it back to ``executing``.
    - Same phase, same detail is a no-op, so a poll loop and a hook stream
      can both project without churning ``updated``.
    - Terminal phases ride the durable annotate (a reconnect reads the
      outcome off history meta); non-terminal phases are in-memory speech
      state and stay off the disk.

    ``detail`` is the safe tool label. On a phase CHANGE the detail is
    replaced (a stale "Reading files" must not survive into ``blocked``);
    within the same phase, ``detail=None`` leaves a finer-grained detail
    already set by another producer alone — the tier-2 poll carries no tool
    name and must not erase one the in-process hook already supplied.
    """

    if phase not in PHASES:
        return False
    run = talk_runs.get_run(run_id)
    if run is None or run["status"] in talk_runs.TERMINAL_STATUSES:
        return False
    meta = run.get("meta") if isinstance(run.get("meta"), dict) else {}
    if meta.get("phase") in TERMINAL_PHASES:
        return False
    if meta.get("phase") != phase:
        fields: dict[str, Any] = {
            "phase": phase,
            "phase_at": time.time(),
            "phase_detail": detail or "",
        }
    elif detail is not None and (meta.get("phase_detail") or "") != detail:
        fields = {"phase_detail": detail}
    else:
        return False
    talk_runs.annotate_run(run_id, durable=phase in TERMINAL_PHASES, **fields)
    return True


#: Correlator index for the SAME-PROCESS case: the api_server run executes on
#: this host, so its agent's tool calls fire this process's hooks. The remote
#: session id (read off the poll payload, stamped by the host at queue time)
#: is the exact key; recency is never consulted. Entries self-clear on first
#: touch after the run goes terminal; the cap is the backstop for a host that
#: fires events for sessions whose runs no longer exist.
_RUN_SESSION_LOCK = threading.Lock()
_RUN_BY_SESSION: dict[str, int] = {}
_MAX_RUN_SESSIONS = 200


def note_run_session(run_id: int, session_id: Any) -> None:
    """Record the api_server-side session id a local run is executing under."""

    if not isinstance(session_id, str) or not session_id:
        return
    with _RUN_SESSION_LOCK:
        if session_id not in _RUN_BY_SESSION:
            while len(_RUN_BY_SESSION) >= _MAX_RUN_SESSIONS:
                _RUN_BY_SESSION.pop(next(iter(_RUN_BY_SESSION)))
        _RUN_BY_SESSION[session_id] = int(run_id)


def _run_for_session(session_id: str) -> int | None:
    """The live run id bound to an api-side session id, or ``None``.

    A mapping whose run finished (or died with an evicted registry entry) is
    dropped here rather than honoured: projecting into a terminal run would
    re-open an outcome the registry already closed.
    """

    with _RUN_SESSION_LOCK:
        run_id = _RUN_BY_SESSION.get(session_id)
    if run_id is None:
        return None
    run = talk_runs.get_run(run_id)
    if run is None or run["status"] in talk_runs.TERMINAL_STATUSES:
        with _RUN_SESSION_LOCK:
            _RUN_BY_SESSION.pop(session_id, None)
        return None
    return run_id


def project_api_poll(run_id: int, payload: dict) -> None:
    """Project one api_server status payload onto a local run (tier 2).

    Called from the ``run_to_completion`` poll loop (via the worker's
    ``on_event``), so the payload is always THIS run's own status object —
    the loop's per-run addressing is what makes cross-routing impossible
    here. Also records the api-side session id the first time the payload
    carries one, which is the correlator the same-process hook projection
    keys on.
    """

    if not isinstance(payload, dict):
        return
    note_run_session(run_id, payload.get("session_id"))
    phase = phase_for_api_event(payload.get("last_event"))
    if phase is None:
        return
    set_run_phase(run_id, phase)


# -- attached-lane children (tier 1) --------------------------------------------

#: Progress subjects keyed by the host's child session id — the only
#: correlator every subagent hook carries. A child enters the index only when
#: its ``subagent_start`` names THIS Talk session's attached parent session
#: as its parent, so tool/approval events from foreign sessions (a gateway
#: serves many) can never land here.
_CHILD_LOCK = threading.Lock()
_CHILDREN: dict[str, dict] = {}
_MAX_CHILDREN = 100

#: The live Talk session's announcement target. Same shape and contract as
#: talk_lifecycle's: one session at a time, last attach wins, fail closed
#: while unbound — and hooks become inert the moment it clears, which is what
#: "registered at plugin load, live at Talk session start" means here.
_SESSION_LOCK = threading.Lock()
_SESSION: tuple[Any, Any, str | None] | None = None

#: The event kind handed to the session callback for a phase milestone.
#: Distinct from talk_lifecycle's ``subagent_stop`` kind, which keeps sole
#: ownership of the terminal announcement.
EVENT_SUBAGENT_PHASE = "subagent_phase"


def attach_session(loop: Any, callback: Any, owner_session_id: str | None) -> None:
    """Bind the live Talk session as the announcement target for child phases.

    Same contract as :func:`talk_lifecycle.attach_session`: ``callback`` runs
    ON the loop via ``call_soon_threadsafe``, and an absent
    ``owner_session_id`` suppresses announcements — and child tracking —
    rather than guessing. Children in flight keep their index entries either
    way; speech resumes only behind a bound owner.
    """

    global _SESSION
    with _SESSION_LOCK:
        _SESSION = (loop, callback, owner_session_id)


def detach_session() -> None:
    """Phase speech stops; hook bookkeeping stays fail-open and inert."""

    global _SESSION
    with _SESSION_LOCK:
        _SESSION = None


def _owner_session_id() -> str | None:
    with _SESSION_LOCK:
        session = _SESSION
    if session is None:
        return None
    return session[2]


def _notify_phase(entry: dict) -> None:
    """Marshal one phase milestone to the owning Talk session, fail-open.

    The gate is re-checked here (not just at tracking time) for the same
    reason talk_lifecycle's notify gates at send: the session can detach
    between the hook and the marshal, and a phase event must never reach a
    session that no longer owns it.
    """

    with _SESSION_LOCK:
        session = _SESSION
    if session is None:
        return
    loop, callback, owner_session_id = session
    if not owner_session_id or entry.get("parent_session_id") != owner_session_id:
        return
    event = {
        "kind": EVENT_SUBAGENT_PHASE,
        "phase": entry.get("phase"),
        "subagent_id": entry.get("subagent_id"),
        "role": entry.get("role"),
        "detail": entry.get("detail") or "",
        "parent_session_id": entry.get("parent_session_id"),
    }
    try:
        loop.call_soon_threadsafe(callback, event)
    except RuntimeError:
        # The loop closed between snapshot and call — the session is tearing
        # down and the milestone has nowhere to go. Not an error.
        return
    except Exception:  # noqa: BLE001 — a hook must never raise into the host
        _log.debug("progress notify failed", exc_info=True)


def note_subagent_start(**kwargs: Any) -> None:
    """Track a delegated child of the attached session as a progress subject.

    Called from :func:`talk_lifecycle.on_subagent_start`, which owns the hook
    registration and has already validated the ids. The child enters the
    index at ``accepted`` — the one milestone a spawn event evidences — and
    nested grandchildren are tracked (their tool calls still need a home)
    but, as with the stop announcements, only top-level children speak.
    """

    child_session_id = str(kwargs.get("child_session_id") or "")
    if not child_session_id:
        return
    owner = _owner_session_id()
    parent_session_id = kwargs.get("parent_session_id")
    if not owner or parent_session_id != owner:
        # Not ours: the child may still be real work, but it is not THIS
        # session's job, and projecting it would be the cross-route bug.
        return
    entry = {
        "subagent_id": kwargs.get("child_subagent_id"),
        "parent_session_id": parent_session_id,
        "role": kwargs.get("child_role"),
        "top_level": not kwargs.get("parent_subagent_id"),
        "phase": PHASE_ACCEPTED,
        "detail": "",
    }
    with _CHILD_LOCK:
        if child_session_id not in _CHILDREN:
            while len(_CHILDREN) >= _MAX_CHILDREN:
                _CHILDREN.pop(next(iter(_CHILDREN)))
        _CHILDREN[child_session_id] = entry
    if entry["top_level"]:
        _notify_phase(entry)


def note_subagent_stop(**kwargs: Any) -> None:
    """Drop the child's progress subject when the host says it stopped.

    The pop IS the terminal guard for a child: a later tool or approval event
    naming a dead child's session id finds no subject and goes nowhere. No
    terminal phase is recorded and none is spoken from here — the host's stop
    event already owns that sentence through talk_lifecycle's existing
    ``subagent_stop`` announcement, and a phase-path copy would be a
    completion claim built from telemetry.
    """

    child_session_id = str(kwargs.get("child_session_id") or "")
    if not child_session_id:
        return
    with _CHILD_LOCK:
        _CHILDREN.pop(child_session_id, None)


def _set_child_phase(session_id: str, phase: str, *, detail: str = "") -> None:
    """Advance one tracked child's phase; speech fires on phase CHANGE only.

    Detail churn within a phase (the per-tool-call labels of an ``executing``
    child) updates the entry silently — the spoken lane hears "executing —
    Reading files" once, not once per tool.
    """

    with _CHILD_LOCK:
        entry = _CHILDREN.get(session_id)
        if entry is None:
            return
        changed = entry.get("phase") != phase
        entry["phase"] = phase
        if detail:
            entry["detail"] = detail
        elif changed:
            entry["detail"] = ""
        if not changed or not entry.get("top_level"):
            return
        snapshot = dict(entry)
    _notify_phase(snapshot)


def _project_toolish_event(phase: str, kwargs: dict, *, detail: str = "") -> None:
    """Route one session-keyed hook event to its exact target(s).

    The correlator is the event's ``session_id``. It may name an attached
    child this session is tracking (tier 1) and/or the api-side session a
    local api_server run recorded for itself (the same-process tier-2 case) —
    an event for anything else is not ours and goes nowhere.
    """

    session_id = str(kwargs.get("session_id") or "")
    if not session_id:
        return
    _set_child_phase(session_id, phase, detail=detail)
    run_id = _run_for_session(session_id)
    if run_id is not None:
        set_run_phase(run_id, phase, detail=detail or None)


def on_post_tool_call(**kwargs: Any) -> None:
    """Hook: one tool call finished. Evidence of ``executing``, with a label.

    Registered at plugin load (the host's hook registry is process-scoped);
    inert until a Talk session attaches. Fail-open end to end — the hook
    fires per tool call on the host's own hot path, so the body is two dict
    lookups and nothing else.
    """

    try:
        _project_toolish_event(
            PHASE_EXECUTING, kwargs, detail=tool_label(kwargs.get("tool_name"))
        )
    except Exception:  # noqa: BLE001 — a hook must never raise into the host
        _log.debug("post_tool_call progress handling failed", exc_info=True)


def on_pre_approval_request(**kwargs: Any) -> None:
    """Hook: a job is parked on an approval. Evidence of ``blocked``.

    The approval's command/description never enter the phase model — the
    host already redacts them for its own event stream, and this module's
    redaction is positional: there is simply no field for them.
    """

    try:
        _project_toolish_event(PHASE_BLOCKED, kwargs)
    except Exception:  # noqa: BLE001 — a hook must never raise into the host
        _log.debug("pre_approval_request progress handling failed", exc_info=True)


# -- the watcher's speech state (consumer side) ---------------------------------


class RunProgressWatch:
    """One watcher's progress-speech state for one registry run.

    ``poll`` is called once per watch tick with a fresh run snapshot and
    answers the ONLY question the spoken lane asks: what, if anything, is due
    to be said right now. It returns a milestone kind — a phase name for a
    phase change, ``"heartbeat"`` for a bounded liveness note, or ``None``.

    - Phase speech fires on CHANGE only: a run holding ``executing`` for
      forty polls says it once.
    - Terminal phases are never returned. The run's terminal announcement is
      spoken by the watcher's existing terminal branch off the registry's
      authoritative status; a phase built from the same host event is the
      receipt, not the sentence.
    - Heartbeats are bounded to one per :data:`HEARTBEAT_S` of speech
      silence per run — the clock is speech, not polls, so an active run
      earning milestones is not also interrupted by liveness notes.
    - A run with no phase at all (the detached lane, whose only host signal
      is the exit code) gets exactly the heartbeat — liveness, nothing more.
    """

    def __init__(self, *, heartbeat_s: float = HEARTBEAT_S, now=time.monotonic) -> None:
        self._heartbeat_s = heartbeat_s
        self._now = now
        self._phase: str | None = None
        # Speech silence is measured from the watch's birth: the WORK_STARTED
        # receipt was just spoken, so a fresh watcher owes nothing yet.
        self._last_spoke = now()

    def poll(self, run: dict) -> str | None:
        """The milestone due this tick — a phase name, ``"heartbeat"``, or None."""

        meta = run.get("meta") if isinstance(run.get("meta"), dict) else {}
        phase = meta.get("phase")
        if isinstance(phase, str) and phase in PHASES and phase != self._phase:
            self._phase = phase
            if phase in TERMINAL_PHASES:
                # Recorded so it is not re-evaluated every tick, but never
                # spoken from here: the terminal artifact owns that speech.
                return None
            self._last_spoke = self._now()
            return phase
        if self._now() - self._last_spoke >= self._heartbeat_s:
            self._last_spoke = self._now()
            return "heartbeat"
        return None


def reset_for_tests() -> None:
    """Clear module state between tests (never called in production)."""

    with _RUN_SESSION_LOCK:
        _RUN_BY_SESSION.clear()
    with _CHILD_LOCK:
        _CHILDREN.clear()
    detach_session()


__all__ = [
    "EVENT_SUBAGENT_PHASE",
    "HEARTBEAT_S",
    "PHASES",
    "PHASE_ACCEPTED",
    "PHASE_BLOCKED",
    "PHASE_COMPLETE",
    "PHASE_EXECUTING",
    "PHASE_FAILED",
    "PHASE_STOPPED",
    "TERMINAL_PHASES",
    "TOOL_LABEL_FALLBACK",
    "RunProgressWatch",
    "attach_session",
    "detach_session",
    "note_run_session",
    "note_subagent_start",
    "note_subagent_stop",
    "on_post_tool_call",
    "on_pre_approval_request",
    "phase_for_api_event",
    "project_api_poll",
    "reset_for_tests",
    "set_run_phase",
    "tool_label",
]
