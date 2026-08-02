"""hermes-talk configuration — TALK_* env namespace and host paths.

Every knob is resolved at CALL time, never bound at import time, so a test
(or a live operator) can flip an env var and the very next call sees it.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_TALK_MODEL = "gpt-realtime-2.1"
DEFAULT_TALK_VOICE = "cedar"
OPENAI_REALTIME_VOICES = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
)


class TalkConfigError(Exception):
    """A Talk configuration value is unusable."""


def get_hermes_home() -> Path:
    """Hermes home, preferring the host's own resolver when importable."""

    try:
        from hermes_constants import get_hermes_home as _host_home

        return Path(_host_home())
    except Exception:  # noqa: BLE001 - any host failure falls back to the env
        env = os.environ.get("HERMES_HOME")
        if env:
            return Path(env).expanduser()
        return Path.home() / ".hermes"


def state_dir() -> Path:
    """Where hermes-talk keeps durable state (run history, flush dedup)."""

    path = get_hermes_home() / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def talk_model() -> str:
    """Realtime model, resolved at call time."""

    return (os.environ.get("TALK_MODEL") or DEFAULT_TALK_MODEL).strip() or DEFAULT_TALK_MODEL


def talk_voice() -> str:
    """Realtime voice, fail-closed on unknown ids."""

    raw = (os.environ.get("TALK_VOICE") or DEFAULT_TALK_VOICE).strip().lower() or DEFAULT_TALK_VOICE
    if raw not in OPENAI_REALTIME_VOICES:
        raise TalkConfigError(
            f"TALK_VOICE '{raw}' is not a built-in Realtime voice "
            f"({', '.join(OPENAI_REALTIME_VOICES)})"
        )
    return raw


def resolve_openai_key() -> str:
    """The OpenAI Platform key for Talk. Fail-closed, never silent.

    Order: TALK_OPENAI_API_KEY (Talk-scoped) -> OPENAI_API_KEY. A key that is
    SET but blank is a hard refusal, not a fall-through — an operator who
    scoped a key expects that key to be used or the surface to say why not.
    """

    scoped = os.environ.get("TALK_OPENAI_API_KEY")
    if scoped is not None:
        if not scoped.strip():
            raise TalkConfigError(
                "TALK_OPENAI_API_KEY is set but empty — set a real key or unset it"
            )
        return scoped.strip()
    shared = os.environ.get("OPENAI_API_KEY")
    if shared is not None:
        if not shared.strip():
            raise TalkConfigError("OPENAI_API_KEY is set but empty — set a real key or unset it")
        return shared.strip()
    raise TalkConfigError(
        "no OpenAI key for Talk: set TALK_OPENAI_API_KEY or OPENAI_API_KEY"
    )


def audio_input_device() -> str | None:
    """Optional sounddevice input override (Windows/WASAPI proofing)."""

    raw = (os.environ.get("TALK_INPUT_DEVICE") or "").strip()
    return raw or None


def audio_output_device() -> str | None:
    """Optional sounddevice output override."""

    raw = (os.environ.get("TALK_OUTPUT_DEVICE") or "").strip()
    return raw or None


__all__ = [
    "DEFAULT_TALK_MODEL",
    "DEFAULT_TALK_VOICE",
    "OPENAI_REALTIME_VOICES",
    "TalkConfigError",
    "audio_input_device",
    "audio_output_device",
    "get_hermes_home",
    "resolve_openai_key",
    "state_dir",
    "talk_model",
    "talk_voice",
]
