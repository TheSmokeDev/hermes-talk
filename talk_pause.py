"""Voice-input pause — mute the microphone without ending the call (hermes-talk#100).

One live capture surface per process. :func:`talk_cli.run_talk_session`
attaches the audio object it is pumping; the model's ``pause_voice_input``
tool and the operator's own controls (Enter in the terminal, ``/talk pause``
and ``/talk resume`` in Discord) flip it through here. Only capture stops:
the speaker keeps playing, every run watcher keeps polling, and
announcements keep landing.

Same one-at-a-time contract as :func:`talk_runs.attach_owner` — last attach
wins, and while nothing is attached a pause is REFUSED rather than
remembered. A flag armed against a session that has not started yet would
silently mute the next one, and the dashboard tab (whose microphone lives in
the browser) must hear "there is nothing here to pause", not "paused".

Thread model: the tool runs on the relay's daemon pool, the terminal key on
its own reader thread, the Discord command on the gateway loop. Every entry
point takes the lock; the attaching session's ``on_change`` callback fires
OUTSIDE it and is the session's own business to marshal onto its loop.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

_log = logging.getLogger(__name__)

#: Who flipped the flag. The session's receipt wording depends on it: a
#: pause the MODEL made is already spoken by the model as its tool result,
#: while an operator control has no voice of its own and is announced.
SOURCE_TOOL = "tool"
SOURCE_KEYBOARD = "keyboard"
SOURCE_COMMAND = "command"
SOURCES = frozenset({SOURCE_TOOL, SOURCE_KEYBOARD, SOURCE_COMMAND})

#: Outcomes of :func:`set_paused`. Callers compose the sentence; the state
#: change itself is decided here, once, under the lock.
PAUSED = "paused"
RESUMED = "resumed"
ALREADY_PAUSED = "already_paused"
ALREADY_LISTENING = "already_listening"
NO_SESSION = "no_session"
UNSUPPORTED = "unsupported"

_LOCK = threading.Lock()
_SURFACE: object | None = None
_ON_CHANGE: Callable[[bool, str], None] | None = None


def attach_session(audio: object, on_change: Callable[[bool, str], None] | None = None) -> None:
    """Bind the live session's capture surface (and its receipt callback).

    ``on_change(paused, source)`` is called after every ACTUAL flip — never
    for a no-op — from whichever thread made it.
    """

    global _SURFACE, _ON_CHANGE
    with _LOCK:
        _SURFACE = audio
        _ON_CHANGE = on_change


def detach_session(audio: object | None = None) -> None:
    """Drop the attached surface.

    With ``audio`` given, only if it is STILL the attached one: a session's
    teardown must not undo the attach of the session that replaced it.
    """

    global _SURFACE, _ON_CHANGE
    with _LOCK:
        if audio is not None and _SURFACE is not audio:
            return
        _SURFACE = None
        _ON_CHANGE = None


def is_paused() -> bool | None:
    """Whether the attached surface is paused; ``None`` when nothing is attached."""

    with _LOCK:
        surface = _SURFACE
    if surface is None:
        return None
    paused = getattr(surface, "input_paused", None)
    return paused if isinstance(paused, bool) else None


def set_paused(paused: bool, *, source: str) -> str:
    """Pause or resume capture on the attached surface. Returns an outcome.

    The read-modify-write is one critical section, so two controls racing
    (the model and a keypress, say) resolve to one flip and one no-op instead
    of two receipts for the same state. The surface's own ``pause_input`` /
    ``resume_input`` are called inside it; they are queue flips, not device
    calls, and hold nothing that calls back into this module.
    """

    if source not in SOURCES:
        raise ValueError(f"unknown pause source: {source!r}")
    with _LOCK:
        surface, on_change = _SURFACE, _ON_CHANGE
        if surface is None:
            return NO_SESSION
        pause = getattr(surface, "pause_input", None)
        resume = getattr(surface, "resume_input", None)
        current = getattr(surface, "input_paused", None)
        if not callable(pause) or not callable(resume) or not isinstance(current, bool):
            return UNSUPPORTED
        if paused and current:
            return ALREADY_PAUSED
        if not paused and not current:
            return ALREADY_LISTENING
        (pause if paused else resume)()
    if on_change is not None:
        try:
            on_change(paused, source)
        except Exception as exc:  # noqa: BLE001 — a receipt must never undo the flip
            _log.debug("pause change callback failed: %s: %s", type(exc).__name__, exc)
    return PAUSED if paused else RESUMED


def reset_for_tests() -> None:
    detach_session()


__all__ = [
    "ALREADY_LISTENING",
    "ALREADY_PAUSED",
    "NO_SESSION",
    "PAUSED",
    "RESUMED",
    "SOURCES",
    "SOURCE_COMMAND",
    "SOURCE_KEYBOARD",
    "SOURCE_TOOL",
    "UNSUPPORTED",
    "attach_session",
    "detach_session",
    "is_paused",
    "reset_for_tests",
    "set_paused",
]
