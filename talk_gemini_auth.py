"""Resolve Gemini Live credentials into Talk's provider-neutral auth shape."""

from __future__ import annotations

import os

try:
    from . import talk_auth, talk_config
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_auth
    import talk_config


def resolve_gemini_auth() -> talk_auth.TalkAuth:
    """Return the configured Gemini key without exposing it in receipts."""

    scoped = (os.environ.get("TALK_GEMINI_API_KEY") or "").strip()
    token = talk_config.resolve_gemini_key()
    if scoped:
        return talk_auth.TalkAuth(
            token=token,
            source=talk_auth.SOURCE_CONFIGURED,
            detail="TALK_GEMINI_API_KEY (Talk-scoped key)",
        )
    return talk_auth.TalkAuth(
        token=token,
        source=talk_auth.SOURCE_ENV,
        detail="GEMINI_API_KEY environment variable",
    )


__all__ = ["resolve_gemini_auth"]
