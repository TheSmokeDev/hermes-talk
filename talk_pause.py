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

A pause is also refused when the attached session registered NO operator
control that can undo it (:func:`attach_session`'s ``resume_control``). A
paused microphone cannot hear the word "resume", so without a key or a
command the only way back would be Ctrl+C — the one exit this feature
exists to avoid. The session decides that control before it advertises the
tool; this gate is the execution-side half of the same decision, so a tool
call that arrives some other way (a relayed name, a stale schema) cannot
arm a pause nobody can end.

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
NO_RESUME_PATH = "no_resume_path"
UNSUPPORTED = "unsupported"

#: The operator's way back from a pause, in the words the receipts use. A
#: session registers exactly one of these at attach time — or none, in which
#: case pausing is refused (see the module docstring).
RESUME_KEYBOARD = "Enter in the terminal"
RESUME_COMMAND = "/talk resume in Discord"

_LOCK = threading.Lock()
_SURFACE: object | None = None
_ON_CHANGE: Callable[[bool, str], None] | None = None
_RESUME_CONTROL: str | None = None


def attach_session(
    audio: object,
    on_change: Callable[[bool, str], None] | None = None,
    *,
    resume_control: str | None = None,
) -> None:
    """Bind the live session's capture surface (and its receipt callback).

    ``on_change(paused, source)`` is called after every ACTUAL flip — never
    for a no-op — from whichever thread made it. ``resume_control`` names
    the operator's own way back (:data:`RESUME_KEYBOARD`,
    :data:`RESUME_COMMAND`); ``None`` means there is none, and every pause
    is then refused with :data:`NO_RESUME_PATH`.
    """

    global _SURFACE, _ON_CHANGE, _RESUME_CONTROL
    with _LOCK:
        _SURFACE = audio
        _ON_CHANGE = on_change
        _RESUME_CONTROL = resume_control


def detach_session(audio: object | None = None) -> None:
    """Drop the attached surface.

    With ``audio`` given, only if it is STILL the attached one: a session's
    teardown must not undo the attach of the session that replaced it.
    """

    global _SURFACE, _ON_CHANGE, _RESUME_CONTROL
    with _LOCK:
        if audio is not None and _SURFACE is not audio:
            return
        _SURFACE = None
        _ON_CHANGE = None
        _RESUME_CONTROL = None


def resume_control() -> str | None:
    """The attached session's operator resume control; ``None`` when there is none."""

    with _LOCK:
        return _RESUME_CONTROL if _SURFACE is not None else None


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
        surface, on_change, way_back = _SURFACE, _ON_CHANGE, _RESUME_CONTROL
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
        if paused and way_back is None:
            # Resuming is always allowed — it can only widen listening back
            # to normal. Pausing needs a way back first.
            return NO_RESUME_PATH
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
    "NO_RESUME_PATH",
    "NO_SESSION",
    "PAUSED",
    "RESUMED",
    "RESUME_COMMAND",
    "RESUME_KEYBOARD",
    "SOURCES",
    "SOURCE_COMMAND",
    "SOURCE_KEYBOARD",
    "SOURCE_TOOL",
    "UNSUPPORTED",
    "attach_session",
    "detach_session",
    "is_paused",
    "reset_for_tests",
    "resume_control",
    "set_paused",
]
