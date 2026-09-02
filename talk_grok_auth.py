"""Grok (xAI) credential resolution for Talk sessions.

Mirrors ``talk_auth`` for the xAI realtime surface. Two lanes:

* a metered xAI API key (``TALK_XAI_API_KEY`` or the shared ``XAI_API_KEY``);
* the host's ``xai-oauth`` login (``hermes auth add xai-oauth`` — a SuperGrok /
  X Premium+ subscription), consumed the way the Codex lane consumes the Codex
  CLI store.

hermes-talk never implements OAuth and never writes an auth store. When the
host is importable its resolver owns refresh and quarantine under its own
lock; otherwise the store is parsed read-only.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

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

#: Matches the host: refresh when the access token is within an hour of
#: expiry, but only within two minutes for the ~15-minute tokens device-code
#: logins often mint (a flat hour would flag every one of them).
_REFRESH_SKEW_MAX_S = 3600
_REFRESH_SKEW_SHORT_S = 120
_SHORT_TOKEN_LIFETIME_S = 45 * 60
_EXPIRY_MARGIN_S = 60

GROK_AUTH_REQUIRED_MESSAGE = (
    "provider grok is selected but no xAI key is configured and no xAI OAuth "
    f"login exists; set XAI_API_KEY or run `{RELOGIN_COMMAND}`"
)
_TRUE_VALUES = talk_auth._TRUE_VALUES
_FALSE_VALUES = talk_auth._FALSE_VALUES


def _read_env(env: Mapping[str, str] | None, key: str) -> str | None:
    source = os.environ if env is None else env
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


def _store_path(hermes_home: Path | None) -> Path:
    home = Path(hermes_home) if hermes_home is not None else talk_config.get_hermes_home()
    return home / "auth.json"


def _inspect_store(hermes_home: Path | None) -> tuple[str, str | None, int | None]:
    """Read-only look at the host store's ``xai-oauth`` entry.

    Returns ``(state, access_token, expires_s)`` with ``state`` one of
    ``missing`` (no entry), ``invalid`` (unreadable, or an entry the host
    itself would refuse — both tokens are required), ``expired``, ``valid``.
    """

    path = _store_path(hermes_home)
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return "missing", None, None
    except OSError as exc:
        _log.debug("xai-oauth store unreadable: %s", type(exc).__name__)
        return "invalid", None, None
    try:
        data = json.loads(raw)
    except ValueError:
        return "invalid", None, None
    if not isinstance(data, dict):
        return "invalid", None, None
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return "invalid", None, None
    entry = providers.get(SOURCE_XAI_OAUTH)
    if entry is None:
        return "missing", None, None
    tokens = entry.get("tokens") if isinstance(entry, dict) else None
    if not isinstance(tokens, dict):
        return "invalid", None, None
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not isinstance(access, str) or not access.strip():
        return "invalid", None, None
    if not isinstance(refresh, str) or not refresh.strip():
        return "invalid", None, None
    access = access.strip()
    expires_s = talk_auth._decode_jwt_expiry_s(access)
    if expires_s is not None and expires_s <= int(time.time()) + _EXPIRY_MARGIN_S:
        return "expired", access, expires_s
    return "valid", access, expires_s


def _host_refresh_available() -> bool:
    """Whether the host's xAI resolver (refresh + quarantine) can be imported."""

    try:
        return importlib.util.find_spec("hermes_cli.auth") is not None
    except (ImportError, ValueError):
        return False


def _expires_at(token: str) -> datetime | None:
    expires_s = talk_auth._decode_jwt_expiry_s(token)
    if expires_s is None:
        return None
    return datetime.fromtimestamp(expires_s, tz=UTC)


def _resolve_via_host() -> TalkAuth | None:
    """Ask the host for a fresh access token.

    Returns ``None`` when the host is not importable (caller falls back to
    the read-only store parse) or when the host says no login exists.
    Raises ``TalkAuthError`` for any other host failure — with a fixed
    message, never the host's own text, which can carry store paths.
    """

    try:
        from hermes_cli.auth import AuthError, resolve_xai_oauth_runtime_credentials
    except Exception as exc:  # noqa: BLE001 - optional host, any failure means "not here"
        _log.debug("host xai-oauth resolver unavailable: %s", type(exc).__name__)
        return None
    try:
        creds = resolve_xai_oauth_runtime_credentials()
    except AuthError as exc:
        if getattr(exc, "code", None) == "xai_auth_missing":
            return None
        _log.debug("host refused xai-oauth: code=%s", getattr(exc, "code", None))
        raise TalkAuthError(
            f"xAI OAuth login is unusable (host code: {getattr(exc, 'code', 'unknown')}); "
            f"run `{RELOGIN_COMMAND}`"
        ) from None
    except Exception as exc:  # noqa: BLE001 - host internals must not leak
        _log.debug("host xai-oauth resolver failed: %s", type(exc).__name__)
        raise TalkAuthError(
            f"xAI OAuth login could not be refreshed; run `{RELOGIN_COMMAND}`"
        ) from None
    token = creds.get("api_key") if isinstance(creds, Mapping) else None
    if not isinstance(token, str) or not token.strip():
        raise TalkAuthError(
            f"xAI OAuth login returned no access token; run `{RELOGIN_COMMAND}`"
        )
    token = token.strip()
    return TalkAuth(
        token=token,
        source=SOURCE_XAI_OAUTH,
        detail=OAUTH_DETAIL,
        expires_at=_expires_at(token),
    )


def _resolve_xai_oauth(hermes_home: Path | None) -> TalkAuth | None:
    """Resolve the subscription lane; ``None`` means no login exists at all."""

    via_host = _resolve_via_host()
    if via_host is not None:
        return via_host
    if _host_refresh_available():
        # Host importable and it said "no login"; do not second-guess it.
        return None
    state, token, _expires_s = _inspect_store(hermes_home)
    if state == "missing":
        return None
    if state == "invalid":
        raise TalkAuthError(f"xAI OAuth login is unreadable; run `{RELOGIN_COMMAND}`")
    if state == "expired":
        raise TalkAuthError(
            "xAI OAuth access token has expired and Hermes is not importable to refresh it; "
            f"run `{RELOGIN_COMMAND}`"
        )
    assert token is not None
    return TalkAuth(
        token=token,
        source=SOURCE_XAI_OAUTH,
        detail=OAUTH_DETAIL,
        expires_at=_expires_at(token),
    )


def resolve_grok_auth(
    *,
    env: Mapping[str, str] | None = None,
    hermes_home: Path | None = None,
) -> TalkAuth:
    """Resolve the bearer for the xAI realtime surface.

    Order: ``TALK_PREFER_XAI_OAUTH`` → ``TALK_XAI_API_KEY`` → ``XAI_API_KEY``
    → the host ``xai-oauth`` login. A key that is set but blank refuses
    rather than falling through, matching ``talk_config.resolve_xai_key``.
    """

    if prefer_xai_oauth(env):
        auth = _resolve_xai_oauth(hermes_home)
        if auth is None:
            raise TalkAuthError(
                "xAI preference is enabled, but no usable xAI OAuth login exists. "
                f"Run `{RELOGIN_COMMAND}` or unset {PREFERENCE_ENV}; "
                "metered API keys were not used."
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

    auth = _resolve_xai_oauth(hermes_home)
    if auth is not None:
        return auth
    raise TalkAuthError(GROK_AUTH_REQUIRED_MESSAGE)


def _refresh_required(expires_s: int | None, now_s: int) -> bool:
    if expires_s is None:
        return False
    remaining = expires_s - now_s
    skew = _REFRESH_SKEW_SHORT_S if remaining <= _SHORT_TOKEN_LIFETIME_S else _REFRESH_SKEW_MAX_S
    return remaining <= skew


def grok_auth_diagnostic(
    *,
    env: Mapping[str, str] | None = None,
    hermes_home: Path | None = None,
    now_s: int | None = None,
) -> dict[str, object]:
    """Explain which Grok lane would win, without resolving anything.

    Read-only: parses the host store directly and never calls the host
    resolver (which may refresh and write). Safe to run from ``talk doctor``.
    """

    now = int(time.time()) if now_s is None else int(now_s)
    preference, preferred = _preference_receipt(env)
    scoped_state = _key_state(env, "TALK_XAI_API_KEY")
    shared_state = _key_state(env, "XAI_API_KEY")
    metered_key_present = "present" in (scoped_state, shared_state)
    oauth_state, _token, expires_s = _inspect_store(hermes_home)
    if oauth_state == "valid" and expires_s is not None and expires_s <= now + _EXPIRY_MARGIN_S:
        oauth_state = "expired"
    host_refresh = _host_refresh_available()
    oauth_usable = oauth_state == "valid" or (oauth_state == "expired" and host_refresh)

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
    elif oauth_usable:
        winning_lane = SOURCE_XAI_OAUTH
    elif oauth_state == "missing":
        blocked_by = "no-usable-auth"
    else:
        blocked_by = "xai-oauth-unusable"

    return {
        "configured": winning_lane is not None,
        "winning_lane": winning_lane,
        "preference": preference,
        "xai_oauth": oauth_state,
        "host_refresh_available": host_refresh,
        "metered_key_present": metered_key_present,
        "metered_key_wins_over_oauth": (
            winning_lane in (SOURCE_CONFIGURED, SOURCE_ENV) and oauth_state in ("valid", "expired")
        ),
        "metered_keys_ignored": bool(preferred) and metered_key_present,
        "refresh_required": (
            winning_lane == SOURCE_XAI_OAUTH
            and (oauth_state == "expired" or _refresh_required(expires_s, now))
        ),
        "blocked_by": blocked_by,
    }


def grok_auth_status(
    *,
    env: Mapping[str, str] | None = None,
    hermes_home: Path | None = None,
    now_s: int | None = None,
) -> dict[str, object]:
    """Diagnostic plus a one-line human ``detail`` (secret-free)."""

    diag = grok_auth_diagnostic(env=env, hermes_home=hermes_home, now_s=now_s)
    lane = diag["winning_lane"]
    if lane == SOURCE_XAI_OAUTH:
        detail = OAUTH_DETAIL
        if diag["refresh_required"]:
            detail += " (host will refresh the access token on next start)"
    elif lane == SOURCE_CONFIGURED:
        detail = "TALK_XAI_API_KEY (Talk-scoped key)"
    elif lane == SOURCE_ENV:
        detail = "XAI_API_KEY (shared xAI key)"
    else:
        blocked = diag["blocked_by"]
        if blocked == "invalid-preference":
            detail = f"{PREFERENCE_ENV} must be true or false"
        elif blocked == "xai-oauth-unusable":
            detail = f"xAI OAuth login is {diag['xai_oauth']}; run `{RELOGIN_COMMAND}`"
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
    "TalkAuth",
    "TalkAuthError",
    "grok_auth_diagnostic",
    "grok_auth_status",
    "prefer_xai_oauth",
    "resolve_grok_auth",
]
