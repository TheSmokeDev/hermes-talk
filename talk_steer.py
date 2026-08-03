"""Steer receipts — what we can PROVE about a note sent to running work.

The substrate fact this module exists for: ``AIAgent.steer()`` is a queue
write. It returns ``True`` for any non-empty text and says nothing about
delivery. The host has exactly two positive delivery artifacts, and this
module watches both:

1. The post-tool-batch drain — an INFO line on the ``run_agent`` module
   logger (``agent/agent_runtime_helpers.py:3959-3963`` on the 0.20 host)::

       "Delivered /steer to agent after tool batch (%d chars): %s"

   The preview is the first 120 chars of the JOINED pending text. Notes are
   matched by the correlation token each wire text carries (exact), with the
   v0.5 text-prefix match kept as the tokenless fallback.

2. The pre-API-call drain — a DEBUG line on ``agent.conversation_loop``'s
   logger (``agent/conversation_loop.py:1505`` on the 0.20 host)::

       "Pre-API-call steer drain: injected into tool msg at index %d"

   This line fires ONLY on the injected path (the no-tool-message put-back
   path stays silent), carries no text, and names no agent. Attribution
   comes from the emitting frame instead: the handler runs synchronously on
   the draining child's own thread, so the conversation-loop frame holding
   the ``agent`` local is on the stack, and identity against the agent
   reference captured at steer time is exact. Visibility is forced by
   lowering ONLY that module's logger to DEBUG, with a gate filter that
   drops its other DEBUG records so operator log output is unchanged
   (except the drain line itself, which is rare and informative).

Delegate children run as threads of THIS process, so logging handlers
attached to those module loggers see both lines live.

So a note has exactly these knowable states:

- ``queued``      — ``steer()`` accepted it. The only call-time claim.
- ``landed``      — a drain artifact matched this receipt. Positive-only:
                    absence of a match never proves absence of delivery
                    (the pre-API put-back path is silent by design).
- ``redirected``  — ``AIAgent.redirect()`` returned True on a live turn.
                    The artifact is the return value itself: the model
                    request was aborted (or Codex accepted a native steer),
                    so the correction applies to the CURRENT step. The
                    model-request path emits no Delivered line — this state
                    never waits on one.
- ``unconfirmed`` — the child is gone and no landing was observed.
- ``missed``      — the host's completion entry carried the note back as
                    undelivered (``missed_steer`` — present only on hosts
                    with the hermes-agent#76805 retention patch).
- ``superseded``  — the child was stopped after queueing; a hard interrupt
                    drops pending steer text by design (run_agent
                    ``clear_interrupt``).

Everything here is fail-open: a missing host module, a renamed logger, or an
operator log level above INFO degrades to ``unconfirmed`` — never an
exception on the voice path, and never a claim without its artifact.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
import uuid
import weakref

_log = logging.getLogger(__name__)

#: The stable prefix of the post-tool-batch drain line
#: (agent_runtime_helpers.py:3959).
DRAIN_LINE_PREFIX = "Delivered /steer to agent after tool batch"

#: The stable prefix of the pre-API-call drain line (conversation_loop.py:1505).
#: Only the INJECTED branch logs — the put-back branch is silent, which is
#: exactly why this line (and not a ``_drain_pending_steer`` wrapper) is the
#: truthful delivery artifact.
PRE_API_LINE_PREFIX = "Pre-API-call steer drain: injected"

#: The drain preview carries at most this many chars of the joined text.
DRAIN_PREVIEW_CHARS = 120

#: Correlation tokens ride the wire text as ``[tk-xxxxxxxx] <note>``. Short
#: enough that a token always survives the 120-char preview truncation of the
#: FIRST note in a batch; unique enough that two live notes can never collide
#: the way free text can (the v0.5 false-positive bound, hermes-talk#1).
_TOKEN_RE = re.compile(r"\[(tk-[0-9a-f]{8})\]")

STATE_QUEUED = "queued"
STATE_LANDED = "landed"
STATE_REDIRECTED = "redirected"
STATE_UNCONFIRMED = "unconfirmed"
STATE_MISSED = "missed"
STATE_SUPERSEDED = "superseded"

_LOCK = threading.Lock()
_RECEIPTS: list[dict] = []
_MAX_RECEIPTS = 50

_WATCHER: _DrainWatcher | None = None
_WATCHER_LOGGER: logging.Logger | None = None

_PRE_API_WATCHER: _PreApiDrainWatcher | None = None
_PRE_API_LOGGER: logging.Logger | None = None
_PRE_API_GATE: _DebugGate | None = None
_PRE_API_PREV_LEVEL: int | None = None


def new_token() -> str:
    """A fresh correlation token for one steered note."""

    return f"tk-{uuid.uuid4().hex[:8]}"


def compose_wire_text(token: str, text: str) -> str:
    """The note as it goes over the wire — token first, so the drain
    preview's truncation can never cut it off the oldest note in a batch."""

    return f"[{token}] {text}"


class _DrainWatcher(logging.Handler):
    """Flips queued receipts to ``landed`` when the post-tool drain line fires.

    The line does not name WHICH agent drained, so matching is token-first:
    a receipt lands when its correlation token appears in the drained
    preview (exact). The v0.5 text match stays as the tokenless fallback —
    prefix matches stay exact, loose containment only for substantial text.
    Steers queued before one drain concatenate with newlines, so a match on
    any receipt lands every queued receipt for that same agent (the whole
    batch drained together).
    """

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no branch
        # The ENTIRE body is contained: this runs on the host agent's own
        # thread mid-drain, and an exception here would surface inside the
        # child's logging call — the one place this plugin must never break.
        try:
            message = record.getMessage()
            if DRAIN_LINE_PREFIX not in message:
                return
            _, _, preview = message.partition(": ")
            # The drain frame's agent, when reachable, closes the
            # drained-then-requeued race: a note whose token still sits in
            # the agent's pending queue was queued AFTER this drain.
            mark_landed_from_preview(preview, agent=_draining_agent_from_stack())
        except Exception:  # noqa: BLE001 — a logging handler must never raise
            return


class _PreApiDrainWatcher(logging.Handler):
    """Flips queued receipts to ``landed`` when the pre-API drain line fires.

    The line carries no text and no agent id, so attribution is by frame:
    ``emit`` runs synchronously on the draining child's thread, inside the
    conversation-loop call that holds the ``agent`` local. Identity against
    the reference captured at steer time is exact — no text heuristics. A
    stack that doesn't yield the agent flips nothing: positive-only, the
    receipt stays queued and terminates ``unconfirmed`` at worst, exactly
    the v0.5 behaviour.
    """

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no branch
        try:
            if PRE_API_LINE_PREFIX not in record.getMessage():
                return
            agent = _draining_agent_from_stack()
            if agent is None:
                return
            mark_landed_for_agent(agent)
        except Exception:  # noqa: BLE001 — a logging handler must never raise
            return


class _DebugGate(logging.Filter):
    """Keeps our forced-DEBUG private.

    Present ONLY when this module lowered ``agent.conversation_loop``'s
    level itself (operator level was above DEBUG). DEBUG records other than
    the drain line are dropped at the logger, so they never reach the
    operator's handlers — the observable log output stays what the
    operator's own level implies, plus the rare drain line. When the
    operator already runs DEBUG, this filter is never installed and nothing
    is suppressed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.DEBUG:
            return True
        try:
            return PRE_API_LINE_PREFIX in record.getMessage()
        except Exception:  # noqa: BLE001 — never make logging worse
            return True


#: The files whose frames may hold the draining agent: the pre-API drain
#: logs from conversation_loop.py, the post-tool drain from
#: agent_runtime_helpers.py (``apply_pending_steer_to_tool_results(agent,…)``).
_DRAIN_FRAME_FILES = ("conversation_loop.py", "agent_runtime_helpers.py")


def _draining_agent_from_stack() -> object | None:
    """The ``agent`` local of the drain frame below us, if any.

    CPython-specific by design (the host supports CPython only): the logging
    call stack at ``emit`` time still contains the frame that called the
    logger, and on both drain paths that frame holds the draining agent as a
    local named ``agent``.
    """

    try:
        frame = sys._getframe(1)
    except Exception:  # noqa: BLE001 — no frame introspection, no attribution
        return None
    depth = 0
    while frame is not None and depth < 30:
        if frame.f_code.co_filename.endswith(_DRAIN_FRAME_FILES):
            candidate = frame.f_locals.get("agent")
            if candidate is not None and hasattr(candidate, "_drain_pending_steer"):
                return candidate
        frame = frame.f_back
        depth += 1
    return None


def _still_pending_text(agent: object | None) -> str:
    """The agent's CURRENT pending-steer text — the drained-vs-not oracle.

    The drain empties the queue BEFORE the log line fires, so anything found
    here at emit time was queued AFTER the drain and must not be swept into
    ``landed``. A bare snapshot read is enough: the token search only needs
    the text of notes that provably were not part of the drained batch.
    """

    if agent is None:
        return ""
    try:
        pending = getattr(agent, "_pending_steer", None)
    except Exception:  # noqa: BLE001 — a foreign object must not break emit
        return ""
    return pending if isinstance(pending, str) else ""


def ensure_watcher() -> bool:
    """Attach the drain watcher to the host's ``run_agent`` logger. Idempotent.

    Returns ``True`` when the watcher is live. ``False`` means the
    post-tool-batch artifact is invisible in this process — callers surface
    that in status, not as an error.
    """

    global _WATCHER
    try:
        import run_agent
    except Exception:  # noqa: BLE001 — no host in this process
        return False
    logger = getattr(run_agent, "logger", None)
    if not isinstance(logger, logging.Logger):
        return False
    global _WATCHER_LOGGER
    with _LOCK:
        # Class check, not identity: a recycled logger may still carry a
        # watcher from a previous attach (reset never reaches foreign
        # loggers), and identity alone would double-attach.
        for handler in logger.handlers:
            if isinstance(handler, _DrainWatcher):
                _WATCHER = handler
                _WATCHER_LOGGER = logger
                return _watcher_effective(logger)
        _WATCHER = _DrainWatcher(level=logging.INFO)
        _WATCHER_LOGGER = logger
        logger.addHandler(_WATCHER)
    return _watcher_effective(logger)


def ensure_pre_api_watcher() -> bool:
    """Attach the pre-API drain watcher to ``agent.conversation_loop``.

    Idempotent. Unlike :func:`ensure_watcher`, a ``True`` here is always
    effective: when the module logger's effective level is above DEBUG we
    lower it ourselves and install :class:`_DebugGate` so only the drain
    line escapes at that level. ``False`` means no host module in this
    process — the pre-API artifact is simply unavailable.
    """

    global _PRE_API_WATCHER, _PRE_API_LOGGER, _PRE_API_GATE, _PRE_API_PREV_LEVEL
    try:
        from agent import conversation_loop
    except Exception:  # noqa: BLE001 — no host in this process
        return False
    logger = getattr(conversation_loop, "logger", None)
    if not isinstance(logger, logging.Logger):
        return False
    with _LOCK:
        for handler in logger.handlers:
            if isinstance(handler, _PreApiDrainWatcher):
                _PRE_API_WATCHER = handler
                _PRE_API_LOGGER = logger
                return True
        watcher = _PreApiDrainWatcher(level=logging.DEBUG)
        if logger.getEffectiveLevel() > logging.DEBUG:
            _PRE_API_PREV_LEVEL = logger.level
            gate = _DebugGate()
            logger.addFilter(gate)
            _PRE_API_GATE = gate
            logger.setLevel(logging.DEBUG)
        logger.addHandler(watcher)
        _PRE_API_WATCHER = watcher
        _PRE_API_LOGGER = logger
    return True


def _watcher_effective(logger: logging.Logger) -> bool:
    """The handler only ever fires if the logger emits INFO records at all."""

    return logger.getEffectiveLevel() <= logging.INFO


def _make_receipt(
    subagent_id: str,
    text: str,
    state: str,
    token: str | None,
    agent: object | None,
) -> dict:
    agent_ref = None
    if agent is not None:
        try:
            agent_ref = weakref.ref(agent)
        except TypeError:  # non-weakref-able stub — attribution just degrades
            agent_ref = None
    return {
        "id": uuid.uuid4().hex[:8],
        "subagent_id": subagent_id,
        "preview": text[:DRAIN_PREVIEW_CHARS],
        "state": state,
        "token": token,
        "agent_ref": agent_ref,
        "ts": time.time(),
    }


def record_queued(
    subagent_id: str,
    text: str,
    *,
    token: str | None = None,
    agent: object | None = None,
) -> str:
    """Ledger a successfully queued steer. Returns the receipt id.

    ``text`` is the WIRE text (token included) — every downstream artifact
    (drain preview, ``missed_steer``) quotes what was actually queued.
    ``agent`` is the live AIAgent behind the id when the caller could
    resolve one; it is held weakly and used only for pre-API attribution.
    """

    receipt = _make_receipt(subagent_id, text, STATE_QUEUED, token, agent)
    with _LOCK:
        _RECEIPTS.append(receipt)
        del _RECEIPTS[:-_MAX_RECEIPTS]
    return receipt["id"]


def record_redirected(
    subagent_id: str,
    text: str,
    *,
    token: str | None = None,
    agent: object | None = None,
) -> str:
    """Ledger an accepted redirect. Returns the receipt id.

    The artifact is ``AIAgent.redirect()``'s return value — True on a live
    turn means the in-flight model request was aborted with the correction
    stashed for the retry (or Codex accepted a native turn/steer). No log
    line exists on that path, so this state is terminal at call time and
    never waits on a drain artifact.
    """

    receipt = _make_receipt(subagent_id, text, STATE_REDIRECTED, token, agent)
    with _LOCK:
        _RECEIPTS.append(receipt)
        del _RECEIPTS[:-_MAX_RECEIPTS]
    return receipt["id"]


def mark_landed_from_preview(preview: str, *, agent: object | None = None) -> int:
    """Land receipts matching a drained preview. Returns how many flipped.

    ``agent`` is the draining agent when the watcher could resolve it from
    the drain frame. Its CURRENT pending queue is the drained-vs-requeued
    oracle: the host drains before it logs, so a note whose token is still
    pending at emit time was queued after this drain and stays ``queued``
    instead of being swept into ``landed`` with the batch.
    """

    preview = (preview or "").strip()
    if not preview:
        return 0
    tokens = set(_TOKEN_RE.findall(preview))
    still_pending = _still_pending_text(agent)
    flipped = 0
    with _LOCK:
        matched_agents: set[str] = set()
        for receipt in _RECEIPTS:
            if receipt["state"] != STATE_QUEUED:
                continue
            token = receipt.get("token")
            if token is not None and token in still_pending:
                continue  # queued after this drain — not part of the batch
            if token is not None and token in tokens:
                receipt["state"] = STATE_LANDED
                flipped += 1
                matched_agents.add(receipt["subagent_id"])
                continue
            own = receipt["preview"].strip()
            head = own[: len(preview)] or own
            # Loose containment only for substantial text: a five-char
            # note like "focus" must not match an unrelated "focus
            # elsewhere" drain. Prefix matches stay exact. Tokenized
            # receipts reaching this fallback are still collision-proof —
            # the token is part of the text being matched.
            loose_ok = len(own) >= 20 and own in preview
            if preview.startswith(head) or loose_ok:
                receipt["state"] = STATE_LANDED
                flipped += 1
                matched_agents.add(receipt["subagent_id"])
        if matched_agents:
            # Steers concatenate WITHIN one agent's pending queue and drain
            # as a single batch — so a match on any receipt lands every
            # queued receipt for that same agent, even ones whose text (and
            # token) the 120-char preview truncated away. Except, again,
            # anything provably still sitting in the pending queue.
            for receipt in _RECEIPTS:
                if (
                    receipt["state"] == STATE_QUEUED
                    and receipt["subagent_id"] in matched_agents
                ):
                    token = receipt.get("token")
                    if token is not None and token in still_pending:
                        continue
                    receipt["state"] = STATE_LANDED
                    flipped += 1
    return flipped


def mark_landed_for_agent(agent: object) -> int:
    """Land queued receipts held against this live agent. Returns count.

    The pre-API drain empties the ENTIRE pending queue for one agent, so a
    hit lands every queued receipt attributed to it — by captured reference
    first (exact), then by subagent id for receipts recorded without one
    (same batch, same agent). Receipts whose token is STILL in the agent's
    pending queue at emit time were queued after the drain and are skipped
    on both passes.
    """

    if agent is None:
        return 0
    still_pending = _still_pending_text(agent)
    flipped = 0
    with _LOCK:
        matched_agents: set[str] = set()
        for receipt in _RECEIPTS:
            if receipt["state"] != STATE_QUEUED:
                continue
            token = receipt.get("token")
            if token is not None and token in still_pending:
                continue  # queued after this drain — not part of the batch
            ref = receipt.get("agent_ref")
            target = ref() if ref is not None else None
            if target is not None and target is agent:
                receipt["state"] = STATE_LANDED
                flipped += 1
                matched_agents.add(receipt["subagent_id"])
        if matched_agents:
            for receipt in _RECEIPTS:
                if (
                    receipt["state"] == STATE_QUEUED
                    and receipt["subagent_id"] in matched_agents
                ):
                    token = receipt.get("token")
                    if token is not None and token in still_pending:
                        continue
                    receipt["state"] = STATE_LANDED
                    flipped += 1
    return flipped


def mark_child_gone(subagent_id: str) -> None:
    """The child is no longer in the registry: queued → unconfirmed."""

    with _LOCK:
        for receipt in _RECEIPTS:
            if receipt["subagent_id"] == subagent_id and receipt["state"] == STATE_QUEUED:
                receipt["state"] = STATE_UNCONFIRMED


def mark_superseded(subagent_id: str) -> None:
    """The child was stopped: a hard interrupt drops pending steer text."""

    with _LOCK:
        for receipt in _RECEIPTS:
            if receipt["subagent_id"] == subagent_id and receipt["state"] == STATE_QUEUED:
                receipt["state"] = STATE_SUPERSEDED


def apply_missed_steer(subagent_id: str, entry: dict) -> bool:
    """Apply a patched host's ``missed_steer`` completion field, if present."""

    missed = entry.get("missed_steer") if isinstance(entry, dict) else None
    if not isinstance(missed, str) or not missed.strip():
        return False
    hit = False
    with _LOCK:
        for receipt in _RECEIPTS:
            if (
                receipt["subagent_id"] == subagent_id
                and receipt["state"] in (STATE_QUEUED, STATE_UNCONFIRMED)
                and receipt["preview"].strip()
                and receipt["preview"].strip()[:60] in missed
            ):
                receipt["state"] = STATE_MISSED
                hit = True
    return hit


SPOKEN = {
    STATE_QUEUED: "queued — I'll confirm when it lands",
    STATE_LANDED: "landed",
    STATE_REDIRECTED: "redirect accepted — applied at its current or next step",
    STATE_UNCONFIRMED: "finished before I could confirm the note got in",
    STATE_MISSED: "never saw the note — it finished first",
    STATE_SUPERSEDED: "stopped — the note may not have been read",
}


def queued_subagent_ids() -> set[str]:
    """Subagent ids that still hold a queued note — the gone-sweep's input."""

    with _LOCK:
        return {r["subagent_id"] for r in _RECEIPTS if r["state"] == STATE_QUEUED}


def notes_summary() -> str:
    """One speakable clause per recent note, newest first. Empty string if none."""

    with _LOCK:
        recent = list(_RECEIPTS[-5:])
    if not recent:
        return ""
    parts = [
        f"note to {receipt['subagent_id']}: {SPOKEN[receipt['state']]}"
        for receipt in reversed(recent)
    ]
    return "; ".join(parts)


def reset_for_tests() -> None:
    global _WATCHER, _WATCHER_LOGGER
    global _PRE_API_WATCHER, _PRE_API_LOGGER, _PRE_API_GATE, _PRE_API_PREV_LEVEL
    with _LOCK:
        _RECEIPTS.clear()
    if _WATCHER is not None and _WATCHER_LOGGER is not None:
        # Detach, not just forget — a leaked handler on a long-lived logger
        # is exactly the double-attach CI caught.
        _WATCHER_LOGGER.removeHandler(_WATCHER)
    _WATCHER = None
    _WATCHER_LOGGER = None
    if _PRE_API_LOGGER is not None:
        if _PRE_API_WATCHER is not None:
            _PRE_API_LOGGER.removeHandler(_PRE_API_WATCHER)
        if _PRE_API_GATE is not None:
            _PRE_API_LOGGER.removeFilter(_PRE_API_GATE)
        if _PRE_API_PREV_LEVEL is not None:
            # Restore the level we found — including NOTSET(0).
            _PRE_API_LOGGER.setLevel(_PRE_API_PREV_LEVEL)
    _PRE_API_WATCHER = None
    _PRE_API_LOGGER = None
    _PRE_API_GATE = None
    _PRE_API_PREV_LEVEL = None


__all__ = [
    "DRAIN_LINE_PREFIX",
    "DRAIN_PREVIEW_CHARS",
    "PRE_API_LINE_PREFIX",
    "SPOKEN",
    "STATE_LANDED",
    "STATE_MISSED",
    "STATE_QUEUED",
    "STATE_REDIRECTED",
    "STATE_SUPERSEDED",
    "STATE_UNCONFIRMED",
    "apply_missed_steer",
    "compose_wire_text",
    "ensure_pre_api_watcher",
    "ensure_watcher",
    "mark_child_gone",
    "mark_landed_for_agent",
    "mark_landed_from_preview",
    "mark_superseded",
    "new_token",
    "notes_summary",
    "queued_subagent_ids",
    "record_queued",
    "record_redirected",
    "reset_for_tests",
]
