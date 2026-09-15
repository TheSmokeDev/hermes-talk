"""Subagent lifecycle — push-based, from the host's own hook bus.

v0.5 learned that a child was gone by PULLING: ``degrade_gone_children()``
sweeps the delegation registry at ``check_work`` time, so a finished child
went unnoticed until the operator next asked. The 0.20 host fires
``subagent_start`` / ``subagent_stop`` plugin hooks from
``tools/delegate_tool.py`` — this module subscribes and turns them into two
push effects:

1. **Ledger truth, immediately** — a stop flips that child's still-queued
   steer notes to ``unconfirmed`` the moment the host says it stopped,
   instead of at the next ``check_work``. (The pull sweep stays: hooks are
   fail-open, and a host that never fires them degrades to v0.5 exactly.)
2. **The live call hears about it** — a top-level child finishing is
   announced into the running Talk session as a user turn, the same shape
   :func:`talk_cli.run_finished_messages` uses for detached runs.

The keying fact this module is built around: ``subagent_start`` carries
``child_subagent_id`` AND ``child_session_id``, but ``subagent_stop``
carries ONLY ``child_session_id`` (delegate_tool.py:2700 on the 0.20 host).
So the roster is keyed by session id, written at start, resolved at stop.

Threading: hooks fire on HOST threads (the delegating parent's tool
executor), never on the Talk session's asyncio loop. Everything here is
lock-guarded, and the only hand-off to the session is one
``loop.call_soon_threadsafe`` — the session-side callback runs ON the loop
and does its own scheduling. A hook body that throws is contained: the
host's ``invoke_hook`` logs and moves on, but we never make it try.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

try:
    from . import talk_progress, talk_steer
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_progress
    import talk_steer

_log = logging.getLogger(__name__)

#: Roster entries kept after their child stopped would only grow — the
#: roster is live children only, popped at stop. This cap is the backstop
#: for a host that fires starts without stops (crash-killed children).
_MAX_ROSTER = 100

_LOCK = threading.Lock()
_ROSTER: dict[str, dict] = {}
_SESSION: tuple[Any, Callable[[dict], None], str | None] | None = None


def on_subagent_start(**kwargs: Any) -> None:
    """Hook callback: a delegate child exists. Roster it by session id.

    A start without a ``child_session_id`` cannot be keyed back at stop
    time, so it is skipped rather than rostered under a junk key. Fail-open
    end to end — a hook must never cost the host anything.
    """

    try:
        child_session_id = kwargs.get("child_session_id")
        subagent_id = kwargs.get("child_subagent_id")
        if not child_session_id or not isinstance(subagent_id, str) or not subagent_id:
            return
        parent_subagent_id = kwargs.get("parent_subagent_id")
        entry = {
            "subagent_id": subagent_id,
            # Retained for scoping: the announce path currently exists only
            # in the terminal `hermes talk` process (one operator's session
            # tree — the browser lane never attaches), but the id rides the
            # roster and every event so a future host session oracle can
            # filter foreign-parent completions without a schema change.
            # Tracked: hermes-talk#4.
            "parent_session_id": kwargs.get("parent_session_id"),
            "role": kwargs.get("child_role"),
            "goal": str(kwargs.get("child_goal") or "")[:200],
            # Top-level children have no parent subagent — nested
            # grandchildren do, and their stops stay out of the announce
            # stream (ledger effects still apply).
            "top_level": not parent_subagent_id,
            "started_at": time.time(),
        }
        with _LOCK:
            _ROSTER[str(child_session_id)] = entry
            while len(_ROSTER) > _MAX_ROSTER:
                _ROSTER.pop(next(iter(_ROSTER)))
        # Progress projection (hermes-talk#33) keys on the same correlators
        # and applies its own owner gate, so it gets the raw event verbatim.
        talk_progress.note_subagent_start(**kwargs)
    except Exception:  # noqa: BLE001 — a hook must never raise into the host
        _log.debug("subagent_start hook handling failed", exc_info=True)


def on_subagent_stop(**kwargs: Any) -> None:
    """Hook callback: a delegate child finished. Resolve, degrade, announce.

    By the time the host fires this, every drain that was ever going to
    happen has happened — so a note still ``queued`` here was never
    delivered, and flipping it to ``unconfirmed`` now is strictly more
    truthful than waiting for the next ``check_work`` sweep.
    """

    try:
        child_session_id = kwargs.get("child_session_id")
        # The progress subject (hermes-talk#33) dies with the stop event
        # regardless of roster state — a dead child's session id must stop
        # resolving even for a stop that rostered nothing.
        talk_progress.note_subagent_stop(**kwargs)
        entry = None
        if child_session_id:
            with _LOCK:
                entry = _ROSTER.pop(str(child_session_id), None)
        if entry is None:
            return
        subagent_id = entry["subagent_id"]
        talk_steer.mark_child_gone(subagent_id)
        if not entry.get("top_level"):
            return
        _notify(
            {
                "kind": "subagent_stop",
                "subagent_id": subagent_id,
                "parent_session_id": entry.get("parent_session_id"),
                "role": entry.get("role") or kwargs.get("child_role"),
                "goal": entry.get("goal") or "",
                "status": kwargs.get("child_status"),
                "summary": kwargs.get("child_summary"),
            }
        )
    except Exception:  # noqa: BLE001 — a hook must never raise into the host
        _log.debug("subagent_stop hook handling failed", exc_info=True)


def _notify(event: dict) -> None:
    """Marshal an event only to the Talk session that owns its parent.

    Ownership fails closed in both directions: an older host that cannot name
    the attached parent, and an event that cannot name its parent, are both
    silent. Ledger degradation happens before this function and remains
    global regardless of announcement ownership.
    """

    with _LOCK:
        session = _SESSION
    if session is None:
        return
    loop, callback, owner_session_id = session
    if not owner_session_id or event.get("parent_session_id") != owner_session_id:
        return
    try:
        loop.call_soon_threadsafe(callback, event)
    except RuntimeError:
        # The loop closed between the snapshot and the call — the session is
        # tearing down and the announcement has nowhere to go. Not an error.
        return
    except Exception:  # noqa: BLE001 — a hook must never raise into the host
        _log.debug("lifecycle notify failed", exc_info=True)


def attach_session(
    loop: Any,
    callback: Callable[[dict], None],
    owner_session_id: str | None = None,
) -> None:
    """Register the live Talk session as the announcement target.

    ``callback`` runs ON the loop thread (via ``call_soon_threadsafe``) and
    owns its own scheduling. ``owner_session_id`` is the Hermes parent session
    this Talk call belongs to; absent ownership suppresses announcements
    rather than guessing. One session at a time — a new attach replaces the
    old target, which matches one-terminal-one-call reality.
    """

    global _SESSION
    with _LOCK:
        _SESSION = (loop, callback, owner_session_id)


def detach_session() -> None:
    """Announcements stop; roster and ledger effects continue."""

    global _SESSION
    with _LOCK:
        _SESSION = None


def roster_snapshot() -> list[dict]:
    """Live children as rostered by hooks, oldest first. Copies, not views."""

    with _LOCK:
        return [dict(entry) for entry in _ROSTER.values()]


def reset_for_tests() -> None:
    global _SESSION
    with _LOCK:
        _ROSTER.clear()
        _SESSION = None


__all__ = [
    "attach_session",
    "detach_session",
    "on_subagent_start",
    "on_subagent_stop",
    "reset_for_tests",
    "roster_snapshot",
]
