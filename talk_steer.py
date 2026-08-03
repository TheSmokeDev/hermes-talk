"""Steer receipts — what we can PROVE about a note sent to running work.

The substrate fact this module exists for: ``AIAgent.steer()`` is a queue
write. It returns ``True`` for any non-empty text and says nothing about
delivery. The only positive delivery artifact in the host is the INFO line
the drain emits on the ``run_agent`` module logger::

    "Delivered /steer to agent after tool batch (%d chars): %s"

(agent_runtime_helpers.py:3889-3893 — the preview is the first 120 chars of
the JOINED pending text). Delegate children run as threads of THIS process,
so a logging.Handler attached to that module's logger sees the line live.

So a note has exactly these knowable states:

- ``queued``      — ``steer()`` accepted it. The only call-time claim.
- ``landed``      — the drain line matched this receipt. Positive-only:
                    the pre-API drain logs at DEBUG on a different logger,
                    so ABSENCE of a match never proves absence of delivery.
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
import threading
import time
import uuid

_log = logging.getLogger(__name__)

#: The stable prefix of the drain line (agent_runtime_helpers.py:3889).
DRAIN_LINE_PREFIX = "Delivered /steer to agent after tool batch"

#: The drain preview carries at most this many chars of the joined text.
DRAIN_PREVIEW_CHARS = 120

STATE_QUEUED = "queued"
STATE_LANDED = "landed"
STATE_UNCONFIRMED = "unconfirmed"
STATE_MISSED = "missed"
STATE_SUPERSEDED = "superseded"

_LOCK = threading.Lock()
_RECEIPTS: list[dict] = []
_MAX_RECEIPTS = 50

_WATCHER: _DrainWatcher | None = None


class _DrainWatcher(logging.Handler):
    """Flips queued receipts to ``landed`` when the drain line fires.

    The line does not name WHICH agent drained, so matching is text-based:
    a receipt lands when its own preview prefix-matches the drained preview.
    Steers queued before one drain concatenate with newlines, so the joined
    preview's FIRST segment is the oldest note — a match on it lands every
    receipt queued at or before that moment (the whole batch drained
    together). A text collision between two live steers flips both; that is
    a false POSITIVE bound, accepted because the state is advisory and the
    alternative (an id in the log line) needs a core edit.
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
            mark_landed_from_preview(preview)
        except Exception:  # noqa: BLE001 — a logging handler must never raise
            return


def ensure_watcher() -> bool:
    """Attach the drain watcher to the host's ``run_agent`` logger. Idempotent.

    Returns ``True`` when the watcher is live. ``False`` means every steer
    will terminate as ``unconfirmed`` at best — callers surface that in
    status, not as an error.
    """

    global _WATCHER
    try:
        import run_agent
    except Exception:  # noqa: BLE001 — no host in this process
        return False
    logger = getattr(run_agent, "logger", None)
    if not isinstance(logger, logging.Logger):
        return False
    with _LOCK:
        if _WATCHER is not None and _WATCHER in logger.handlers:
            return _watcher_effective(logger)
        _WATCHER = _DrainWatcher(level=logging.INFO)
        logger.addHandler(_WATCHER)
    return _watcher_effective(logger)


def _watcher_effective(logger: logging.Logger) -> bool:
    """The handler only ever fires if the logger emits INFO records at all."""

    return logger.getEffectiveLevel() <= logging.INFO


def record_queued(subagent_id: str, text: str) -> str:
    """Ledger a successfully queued steer. Returns the receipt id."""

    receipt = {
        "id": uuid.uuid4().hex[:8],
        "subagent_id": subagent_id,
        "preview": text[:DRAIN_PREVIEW_CHARS],
        "state": STATE_QUEUED,
        "ts": time.time(),
    }
    with _LOCK:
        _RECEIPTS.append(receipt)
        del _RECEIPTS[:-_MAX_RECEIPTS]
    return receipt["id"]


def mark_landed_from_preview(preview: str) -> int:
    """Land receipts matching a drained preview. Returns how many flipped."""

    preview = (preview or "").strip()
    if not preview:
        return 0
    flipped = 0
    with _LOCK:
        matched_agents: set[str] = set()
        for receipt in _RECEIPTS:
            if receipt["state"] != STATE_QUEUED:
                continue
            own = receipt["preview"].strip()
            head = own[: len(preview)] or own
            # Loose containment only for substantial text: a five-char
            # note like "focus" must not match an unrelated "focus
            # elsewhere" drain. Prefix matches stay exact.
            loose_ok = len(own) >= 20 and own in preview
            if preview.startswith(head) or loose_ok:
                receipt["state"] = STATE_LANDED
                flipped += 1
                matched_agents.add(receipt["subagent_id"])
        if matched_agents:
            # Steers concatenate WITHIN one agent's pending queue and drain
            # as a single batch — so a match on any receipt lands every
            # queued receipt for that same agent, even ones whose text the
            # 120-char preview truncated away.
            for receipt in _RECEIPTS:
                if (
                    receipt["state"] == STATE_QUEUED
                    and receipt["subagent_id"] in matched_agents
                ):
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
    global _WATCHER
    with _LOCK:
        _RECEIPTS.clear()
    _WATCHER = None


__all__ = [
    "DRAIN_LINE_PREFIX",
    "DRAIN_PREVIEW_CHARS",
    "SPOKEN",
    "STATE_LANDED",
    "STATE_MISSED",
    "STATE_QUEUED",
    "STATE_SUPERSEDED",
    "STATE_UNCONFIRMED",
    "apply_missed_steer",
    "ensure_watcher",
    "mark_child_gone",
    "mark_landed_from_preview",
    "mark_superseded",
    "notes_summary",
    "queued_subagent_ids",
    "record_queued",
    "reset_for_tests",
]
