"""OpenAI Platform auth resolution with the Hermes Codex sign-in.

Resolution order, fail-closed at every step:

1. ``TALK_OPENAI_API_KEY`` — a Talk-scoped configured key. Present but
   blank fails closed: no silent fall-through past an operator's explicit
   configuration.
2. ``OPENAI_API_KEY`` — the shared environment key.
3. The profile's Hermes sign-in for ``openai-codex`` (ChatGPT subscription).
   This lane carries NO token: Hermes makes the signed request through
   ``hermes_cli.plugin_provider_requests.credentialed_provider_request``
   (``requires_auth: [openai-codex]`` in ``plugin.yaml``) with its own locked
   refresh. Talk never reads, refreshes or rewrites a Codex token or a vendor
   credential file (catalog admission rule 11).

Keys are read through Hermes's profile-scoped secret route
(``talk_config.SECRET_ENV``), never a bare process-environment lookup.

API keys win by default to preserve the original contract. An explicit
``TALK_PREFER_CODEX_OAUTH=true`` reverses that choice and refuses key fallback.
OAuth billing follows the signed-in ChatGPT account's Realtime entitlement. An
API-key token is a bearer credential — never log it.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

try:
    from . import talk_config
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_config

CODEX_PROVIDER = "openai-codex"
CODEX_SIGN_IN_COMMAND = "hermes auth add openai-codex"
REALTIME_AUTH_REQUIRED_MESSAGE = (
    "Realtime voice needs a credential: set TALK_OPENAI_API_KEY or "
    f"OPENAI_API_KEY, or sign in to Codex in Hermes (`{CODEX_SIGN_IN_COMMAND}`) "
    "to use your ChatGPT subscription"
)
CODEX_HOST_REQUIRED_MESSAGE = (
    "The ChatGPT-subscription lane needs Hermes Agent 0.21.7 or newer, which "
    "signs Codex requests for plugins; set TALK_OPENAI_API_KEY or OPENAI_API_KEY "
    "to use an API key instead"
)

SOURCE_CONFIGURED = "configured"
SOURCE_ENV = "env"
SOURCE_CODEX_OAUTH = "codex-oauth"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class TalkAuthError(RuntimeError):
    """Raised when no OpenAI Platform credential can be resolved."""


@dataclass(frozen=True, slots=True)
class TalkAuth:
    """A resolved auth lane. ``token`` is empty on the Codex lane: Hermes signs."""

    token: str = field(repr=False)
    source: str  # one of SOURCE_CONFIGURED, SOURCE_ENV, SOURCE_CODEX_OAUTH
    detail: str
    expires_at: datetime | None = None
    account_id: str | None = field(default=None, repr=False)  # API lanes never set it


def _read_env(env: Mapping[str, str] | None, key: str) -> str | None:
    source = talk_config.SECRET_ENV if env is None else env
    raw = source.get(key)
    if raw is None:
        return None
    trimmed = raw.strip()
    return trimmed or None


def prefer_codex_oauth(env: Mapping[str, str] | None = None) -> bool:
    """Whether voice is explicitly pinned to the Codex OAuth lane.

    Absence preserves the historical key-first order. Once the operator sets
    the knob, however, a blank or misspelled value is configuration ambiguity
    at a billing boundary and therefore fails closed.
    """

    source = talk_config.SECRET_ENV if env is None else env
    if "TALK_PREFER_CODEX_OAUTH" not in source:
        return False
    raw = str(source.get("TALK_PREFER_CODEX_OAUTH") or "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise TalkAuthError(
        "TALK_PREFER_CODEX_OAUTH must be true or false; refusing to choose a "
        "possibly metered auth lane"
    )


def _preference_receipt(env: Mapping[str, str]) -> tuple[str, bool | None]:
    """A secret-free preference state for read-only diagnostics."""

    if "TALK_PREFER_CODEX_OAUTH" not in env:
        return "absent", False
    raw = str(env.get("TALK_PREFER_CODEX_OAUTH") or "").strip().lower()
    if raw in _TRUE_VALUES:
        return "enabled", True
    if raw in _FALSE_VALUES:
        return "disabled", False
    return "invalid", None


def hermes_signs_codex() -> bool:
    """Whether this host can sign Codex requests for plugins (no token leaves Hermes)."""

    try:
        return importlib.util.find_spec("hermes_cli.plugin_provider_requests") is not None
    except (ImportError, ValueError):
        return False


def _resolve_codex_oauth() -> TalkAuth | None:
    """The ChatGPT-subscription lane, or None when the host cannot sign for it.

    Whether the profile is signed in is answered by Hermes at request time
    (``ProviderNotSignedIn``, surfaced as :class:`TalkAuthError`), never by
    reading a token here.
    """

    if not hermes_signs_codex():
        return None
    return TalkAuth(
        token="",
        source=SOURCE_CODEX_OAUTH,
        detail="Hermes Codex sign-in (ChatGPT subscription; Hermes signs the requests)",
    )


def codex_request(
    method: str,
    url: str,
    *,
    json: object = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
):
    """Send one request with the profile's Codex sign-in attached by Hermes.

    Returns Hermes's ``ProviderResponse`` (``status``, ``headers``, ``body``,
    ``text``, ``json()``). Hermes allows only ``https://chatgpt.com`` and
    ``https://api.openai.com`` and never follows redirects. Call this from plugin
    code, not as a bare executor target: Hermes identifies the caller by the
    plugin file on the stack.
    """

    try:
        from hermes_cli.plugin_provider_requests import (
            ProviderNotSignedIn,
            credentialed_provider_request,
        )
    except ImportError:
        raise TalkAuthError(CODEX_HOST_REQUIRED_MESSAGE) from None
    try:
        return credentialed_provider_request(
            CODEX_PROVIDER, method, url, json=json, headers=dict(headers or {}), timeout=timeout
        )
    except ProviderNotSignedIn as exc:
        raise TalkAuthError(str(exc)) from None
    except PermissionError as exc:
        raise TalkAuthError(f"Hermes refused the Codex request: {exc}") from None


def resolve_auth(*, env: Mapping[str, str] | None = None) -> TalkAuth:
    """Resolve the Realtime auth lane in fail-closed order.

    Order: ``TALK_OPENAI_API_KEY`` -> ``OPENAI_API_KEY`` -> the Hermes Codex
    sign-in (no token: Hermes signs the request). A Talk-scoped key that is
    present but blank fails closed instead of silently falling through to
    another source.
    """

    source = talk_config.SECRET_ENV if env is None else env
    if prefer_codex_oauth(source):
        oauth = _resolve_codex_oauth()
        if oauth is not None:
            return oauth
        raise TalkAuthError(
            f"Codex preference is enabled, but this host cannot sign Codex requests. "
            f"{CODEX_HOST_REQUIRED_MESSAGE}; metered API keys were not used."
        )

    configured = source.get("TALK_OPENAI_API_KEY")
    if configured is not None:
        trimmed = configured.strip()
        if not trimmed:
            raise TalkAuthError(
                f"{REALTIME_AUTH_REQUIRED_MESSAGE}. TALK_OPENAI_API_KEY is set "
                "but empty — fix or remove it to allow other auth sources."
            )
        return TalkAuth(
            token=trimmed,
            source=SOURCE_CONFIGURED,
            detail="TALK_OPENAI_API_KEY (Talk-scoped key)",
        )

    shared = source.get("OPENAI_API_KEY")
    if shared is not None:
        env_key = shared.strip()
        if not env_key:
            raise TalkAuthError(
                f"{REALTIME_AUTH_REQUIRED_MESSAGE}. OPENAI_API_KEY is set but "
                "empty — fix or remove it to allow Codex OAuth."
            )
        return TalkAuth(
            token=env_key,
            source=SOURCE_ENV,
            detail="OPENAI_API_KEY environment variable",
        )

    oauth = _resolve_codex_oauth()
    if oauth is not None:
        return oauth

    raise TalkAuthError(REALTIME_AUTH_REQUIRED_MESSAGE)


def auth_diagnostic(*, env: Mapping[str, str] | None = None) -> dict:
    """Inspect auth selection without a request, a token, or any secret value.

    ``codex_oauth`` is ``hermes`` when the host signs Codex requests for
    plugins and ``missing`` otherwise. Whether the profile is actually signed
    in is Hermes's answer at session start, so nothing here reads a token.
    """

    source = talk_config.SECRET_ENV if env is None else env
    preference, preferred = _preference_receipt(source)

    scoped_raw = source.get("TALK_OPENAI_API_KEY")
    shared_raw = source.get("OPENAI_API_KEY")
    scoped_state = (
        "absent" if scoped_raw is None else ("present" if scoped_raw.strip() else "blank")
    )
    shared_state = (
        "absent" if shared_raw is None else ("present" if shared_raw.strip() else "blank")
    )
    metered_key_present = "present" in {scoped_state, shared_state}
    oauth_state = "hermes" if hermes_signs_codex() else "missing"
    oauth_usable = oauth_state == "hermes"

    winning_lane: str | None = None
    blocked_by: str | None = None
    if preferred is None:
        blocked_by = "invalid-preference"
    elif preferred:
        if oauth_usable:
            winning_lane = SOURCE_CODEX_OAUTH
        else:
            blocked_by = "codex-oauth-unusable"
    elif scoped_state == "blank":
        blocked_by = "blank-talk-key"
    elif scoped_state == "present":
        winning_lane = SOURCE_CONFIGURED
    elif shared_state == "blank":
        blocked_by = "blank-openai-key"
    elif shared_state == "present":
        winning_lane = SOURCE_ENV
    elif oauth_usable:
        winning_lane = SOURCE_CODEX_OAUTH
    else:
        blocked_by = "no-usable-auth"

    return {
        "configured": winning_lane is not None,
        "winning_lane": winning_lane,
        "preference": preference,
        "codex_oauth": oauth_state,
        "metered_key_present": metered_key_present,
        "metered_key_wins_over_codex": (
            winning_lane in {SOURCE_CONFIGURED, SOURCE_ENV} and oauth_usable
        ),
        "metered_keys_ignored": bool(preferred and metered_key_present),
        # Hermes owns refresh; there is never a plugin-side refresh to require.
        "refresh_required": False,
        "blocked_by": blocked_by,
    }


def auth_status(*, env: Mapping[str, str] | None = None) -> dict:
    """Report which auth source would be used, without exposing any token."""

    receipt = auth_diagnostic(env=env)
    lane = receipt["winning_lane"]
    if lane == SOURCE_CONFIGURED:
        detail = "TALK_OPENAI_API_KEY (Talk-scoped key)"
    elif lane == SOURCE_ENV:
        detail = "OPENAI_API_KEY environment variable"
    elif lane == SOURCE_CODEX_OAUTH:
        detail = "Hermes Codex sign-in (ChatGPT subscription; Hermes signs the requests)"
    elif receipt["blocked_by"] == "invalid-preference":
        detail = "TALK_PREFER_CODEX_OAUTH is invalid; set it to true or false"
    elif receipt["blocked_by"] == "blank-talk-key":
        detail = "TALK_OPENAI_API_KEY is set but empty; fix or unset it"
    elif receipt["blocked_by"] == "blank-openai-key":
        detail = "OPENAI_API_KEY is set but empty; fix or unset it"
    elif receipt["blocked_by"] == "codex-oauth-unusable":
        detail = CODEX_HOST_REQUIRED_MESSAGE
    else:
        detail = REALTIME_AUTH_REQUIRED_MESSAGE
    return {"configured": lane is not None, "source": lane, "detail": detail}


__all__ = [
    "CODEX_HOST_REQUIRED_MESSAGE",
    "CODEX_PROVIDER",
    "CODEX_SIGN_IN_COMMAND",
    "REALTIME_AUTH_REQUIRED_MESSAGE",
    "SOURCE_CODEX_OAUTH",
    "SOURCE_CONFIGURED",
    "SOURCE_ENV",
    "TalkAuth",
    "TalkAuthError",
    "auth_diagnostic",
    "auth_status",
    "codex_request",
    "hermes_signs_codex",
    "prefer_codex_oauth",
    "resolve_auth",
]
