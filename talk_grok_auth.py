"""Grok (xAI) credential resolution for Talk sessions.

Mirrors ``talk_auth`` for the xAI realtime surface:

* a metered xAI API key (``TALK_XAI_API_KEY`` or the shared ``XAI_API_KEY``),
  read through Hermes's profile-scoped secret route;
* the host's ``xai-oauth`` login (a SuperGrok / X Premium+ subscription) is
  NOT used in this build. Catalog admission rule 11 lets a plugin use a
  subscription sign-in only through Hermes-signed requests, and Hermes signs
  for ``openai-codex`` alone today. Talk never reads Hermes's ``auth.json`` or
  calls its private token resolvers; the lane returns once Hermes signs xAI
  requests for plugins.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

try:
    from . import talk_auth, talk_config
except ImportError:
    import talk_auth
    import talk_config

_log = logging.getLogger("hermes_talk.grok_auth")

TalkAuth = talk_auth.TalkAuth
TalkAuthError = talk_auth.TalkAuthError
SOURCE_CONFIGURED = talk_auth.SOURCE_CONFIGURED
SOURCE_ENV = talk_auth.SOURCE_ENV
SOURCE_XAI_OAUTH = "xai-oauth"

PREFERENCE_ENV = "TALK_PREFER_XAI_OAUTH"
RELOGIN_COMMAND = "hermes auth add xai-oauth"
OAUTH_DETAIL = "Hermes xAI OAuth login (SuperGrok / X Premium+ subscription)"

GROK_AUTH_REQUIRED_MESSAGE = (
    "provider grok is selected but no xAI key is configured; set TALK_XAI_API_KEY "
    "or XAI_API_KEY"
)
XAI_OAUTH_UNAVAILABLE = (
    "the xAI subscription lane is unavailable: Hermes does not yet sign xAI requests "
    "for plugins, and Talk does not read Hermes's credential store; set "
    "TALK_XAI_API_KEY or XAI_API_KEY"
)
_TRUE_VALUES = talk_auth._TRUE_VALUES
_FALSE_VALUES = talk_auth._FALSE_VALUES


def _read_env(env: Mapping[str, str] | None, key: str) -> str | None:
    source = talk_config.SECRET_ENV if env is None else env
    return source.get(key)


def prefer_xai_oauth(env: Mapping[str, str] | None = None) -> bool:
    """Whether the operator asked for the xAI subscription lane first.

    Absent means no; anything other than a clear yes/no refuses, because
    guessing could silently pick a metered lane.
    """

    raw = _read_env(env, PREFERENCE_ENV)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise TalkAuthError(
        f"{PREFERENCE_ENV} must be true or false; refusing to choose a possibly metered auth lane"
    )


def _preference_receipt(env: Mapping[str, str] | None) -> tuple[str, bool | None]:
    raw = _read_env(env, PREFERENCE_ENV)
    if raw is None:
        return "absent", False
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return "enabled", True
    if value in _FALSE_VALUES:
        return "disabled", False
    return "invalid", None


def _key_state(env: Mapping[str, str] | None, key: str) -> str:
    raw = _read_env(env, key)
    if raw is None:
        return "absent"
    return "present" if raw.strip() else "blank"


def _resolve_xai_oauth() -> TalkAuth | None:
    """The subscription lane: never available until Hermes signs xAI requests."""

    return None


def resolve_grok_auth(*, env: Mapping[str, str] | None = None) -> TalkAuth:
    """Resolve the bearer for the xAI realtime surface.

    Order: ``TALK_PREFER_XAI_OAUTH`` → ``TALK_XAI_API_KEY`` → ``XAI_API_KEY``
    (the ``xai-oauth`` lane is unavailable; see the module docstring). A key
    that is set but blank refuses
    rather than falling through, matching ``talk_config.resolve_xai_key``.
    """

    if prefer_xai_oauth(env):
        auth = _resolve_xai_oauth()
        if auth is None:
            raise TalkAuthError(
                f"xAI preference is enabled, but {XAI_OAUTH_UNAVAILABLE}, or unset "
                f"{PREFERENCE_ENV}; metered API keys were not used."
            )
        return auth

    scoped = _read_env(env, "TALK_XAI_API_KEY")
    if scoped is not None:
        token = scoped.strip()
        if not token:
            raise TalkAuthError(
                "TALK_XAI_API_KEY is set but empty — fix or remove it to allow other auth sources."
            )
        return TalkAuth(
            token=token,
            source=SOURCE_CONFIGURED,
            detail="TALK_XAI_API_KEY (Talk-scoped key)",
        )

    shared = _read_env(env, "XAI_API_KEY")
    if shared is not None:
        token = shared.strip()
        if not token:
            raise TalkAuthError(
                "XAI_API_KEY is set but empty — fix or remove it to allow other auth sources."
            )
        return TalkAuth(token=token, source=SOURCE_ENV, detail="XAI_API_KEY (shared xAI key)")

    auth = _resolve_xai_oauth()
    if auth is not None:
        return auth
    raise TalkAuthError(GROK_AUTH_REQUIRED_MESSAGE)


def grok_auth_diagnostic(*, env: Mapping[str, str] | None = None) -> dict[str, object]:
    """Explain which Grok lane would win, without resolving anything or reading a store."""

    preference, preferred = _preference_receipt(env)
    scoped_state = _key_state(env, "TALK_XAI_API_KEY")
    shared_state = _key_state(env, "XAI_API_KEY")
    metered_key_present = "present" in (scoped_state, shared_state)
    oauth_state = "unsupported"
    oauth_usable = False

    winning_lane: str | None = None
    blocked_by: str | None = None
    if preferred is None:
        blocked_by = "invalid-preference"
    elif preferred:
        if oauth_usable:
            winning_lane = SOURCE_XAI_OAUTH
        else:
            blocked_by = "xai-oauth-unusable"
    elif scoped_state == "blank":
        blocked_by = "blank-talk-key"
    elif scoped_state == "present":
        winning_lane = SOURCE_CONFIGURED
    elif shared_state == "blank":
        blocked_by = "blank-xai-key"
    elif shared_state == "present":
        winning_lane = SOURCE_ENV
    else:
        blocked_by = "no-usable-auth"

    return {
        "configured": winning_lane is not None,
        "winning_lane": winning_lane,
        "preference": preference,
        "xai_oauth": oauth_state,
        "metered_key_present": metered_key_present,
        "metered_key_wins_over_oauth": False,
        "metered_keys_ignored": bool(preferred) and metered_key_present,
        "refresh_required": False,
        "blocked_by": blocked_by,
    }


def grok_auth_status(*, env: Mapping[str, str] | None = None) -> dict[str, object]:
    """Diagnostic plus a one-line human ``detail`` (secret-free)."""

    diag = grok_auth_diagnostic(env=env)
    lane = diag["winning_lane"]
    if lane == SOURCE_CONFIGURED:
        detail = "TALK_XAI_API_KEY (Talk-scoped key)"
    elif lane == SOURCE_ENV:
        detail = "XAI_API_KEY (shared xAI key)"
    else:
        blocked = diag["blocked_by"]
        if blocked == "invalid-preference":
            detail = f"{PREFERENCE_ENV} must be true or false"
        elif blocked == "xai-oauth-unusable":
            detail = XAI_OAUTH_UNAVAILABLE
        elif blocked == "blank-talk-key":
            detail = "TALK_XAI_API_KEY is set but empty"
        elif blocked == "blank-xai-key":
            detail = "XAI_API_KEY is set but empty"
        else:
            detail = GROK_AUTH_REQUIRED_MESSAGE
    diag["detail"] = detail
    return diag


__all__ = [
    "GROK_AUTH_REQUIRED_MESSAGE",
    "OAUTH_DETAIL",
    "PREFERENCE_ENV",
    "RELOGIN_COMMAND",
    "SOURCE_CONFIGURED",
    "SOURCE_ENV",
    "SOURCE_XAI_OAUTH",
    "XAI_OAUTH_UNAVAILABLE",
    "TalkAuth",
    "TalkAuthError",
    "grok_auth_diagnostic",
    "grok_auth_status",
    "prefer_xai_oauth",
    "resolve_grok_auth",
]
