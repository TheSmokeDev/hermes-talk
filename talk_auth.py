"""OpenAI Platform auth resolution with Codex OAuth fallback.

Port of the proven Talk Mode auth ordering (itself a port of OpenClaw
PR #100671, "Reuse Codex OAuth for OpenAI Realtime voice"). Resolution
order, fail-closed at every step:

1. ``TALK_OPENAI_API_KEY`` — a Talk-scoped configured key. Present but
   blank fails closed: no silent fall-through past an operator's explicit
   configuration.
2. ``OPENAI_API_KEY`` — the shared environment key.
3. External Codex CLI login (``$CODEX_HOME/auth.json`` or
   ``~/.codex/auth.json`` in ChatGPT token mode) — the operator's ChatGPT
   subscription. Expired access tokens refresh against the official OAuth
   endpoint and write back atomically so the Codex CLI keeps working.

API keys win by default to preserve the original contract. An explicit
``TALK_PREFER_CODEX_OAUTH=true`` reverses that choice and refuses key fallback
when OAuth is unusable. OAuth billing follows the authenticated ChatGPT
account's Realtime entitlement. The returned token is a bearer credential —
never log it.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

REALTIME_AUTH_REQUIRED_MESSAGE = (
    "Realtime voice needs a credential: set TALK_OPENAI_API_KEY or "
    "OPENAI_API_KEY, or sign in with `codex login` to use your ChatGPT "
    "subscription"
)

_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
# Public client id of the official Codex CLI (same id OpenClaw/Codex use).
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_FALLBACK_EXPIRY_S = 60 * 60
_REFRESH_MARGIN_S = 60
_TOKEN_REQUEST_TIMEOUT_S = 30.0

SOURCE_CONFIGURED = "configured"
SOURCE_ENV = "env"
SOURCE_CODEX_OAUTH = "codex-oauth"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class TalkAuthError(RuntimeError):
    """Raised when no OpenAI Platform credential can be resolved."""


@dataclass(frozen=True, slots=True)
class TalkAuth:
    """A resolved OpenAI Platform bearer credential."""

    token: str
    source: str  # one of SOURCE_CONFIGURED, SOURCE_ENV, SOURCE_CODEX_OAUTH
    detail: str
    expires_at: datetime | None = None


def _read_env(env: Mapping[str, str] | None, key: str) -> str | None:
    source = os.environ if env is None else env
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

    source = os.environ if env is None else env
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


def _codex_auth_path(codex_home: Path | None = None) -> Path:
    if codex_home is not None:
        return codex_home / "auth.json"
    configured = os.environ.get("CODEX_HOME", "").strip()
    base = Path(configured) if configured else Path.home() / ".codex"
    return base / "auth.json"


def _decode_jwt_payload(token: str) -> tuple[dict | None, bool]:
    """Return a decoded object payload and whether JWT-shaped input was malformed."""

    parts = token.split(".")
    if len(parts) < 2:
        return None, False
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + padding))
    except Exception:  # noqa: BLE001 - any malformed JWT means "no expiry claim"
        return None, True
    if not isinstance(data, dict):
        return None, True
    return data, False


def _decode_jwt_expiry_s(token: str) -> int | None:
    """Read the ``exp`` claim from a JWT without verifying the signature."""

    data, _malformed = _decode_jwt_payload(token)
    if data is None:
        return None
    exp = data.get("exp")
    return exp if isinstance(exp, (int, float)) else None


def _auth_json_uses_chatgpt_tokens(data: dict) -> bool:
    """Mirror OpenClaw ``codexAuthJsonUsesChatGptTokens``."""

    auth_mode = data.get("auth_mode")
    if isinstance(auth_mode, str) and auth_mode.strip():
        return auth_mode.strip().lower() in {"chatgpt", "chatgptauthtokens"}
    return not isinstance(data.get("OPENAI_API_KEY"), str)


@dataclass(slots=True)
class _CodexOauthCredential:
    access: str
    refresh: str
    expires_s: int
    account_id: str | None
    id_token: str | None


def _parse_codex_oauth_credential(
    data: dict, fallback_expiry_s: int
) -> _CodexOauthCredential | None:
    if not _auth_json_uses_chatgpt_tokens(data):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not isinstance(access, str) or not access:
        return None
    if not isinstance(refresh, str) or not refresh:
        return None
    _payload, malformed_jwt = _decode_jwt_payload(access)
    if malformed_jwt:
        return None
    expires_s = _decode_jwt_expiry_s(access)
    if expires_s is None:
        expires_s = fallback_expiry_s
    account_id = tokens.get("account_id")
    id_token = tokens.get("id_token")
    return _CodexOauthCredential(
        access=access,
        refresh=refresh,
        expires_s=expires_s,
        account_id=account_id if isinstance(account_id, str) else None,
        id_token=id_token if isinstance(id_token, str) else None,
    )


def _read_codex_auth_json(auth_path: Path) -> dict | None:
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable auth.json means "no OAuth lane"
        return None
    return data if isinstance(data, dict) else None


def _fallback_expiry_s(auth_path: Path) -> int:
    """Expiry estimate when the access token carries no JWT ``exp``.

    Codex CLI access tokens live about an hour; anchor on the auth.json
    mtime, which every token refresh (CLI or ours) bumps.
    """

    try:
        base = auth_path.stat().st_mtime
    except OSError:
        base = time.time()
    return int(base) + _CODEX_FALLBACK_EXPIRY_S


def _post_token_form(fields: dict[str, str]) -> dict:
    """POST a form to the OpenAI OAuth token endpoint. Isolated for tests."""

    response = httpx.post(
        _CODEX_TOKEN_URL,
        data=fields,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=_TOKEN_REQUEST_TIMEOUT_S,
    )
    if response.status_code != 200:
        raise TalkAuthError(
            f"Codex token refresh failed ({response.status_code}): "
            f"{response.text[:300] or response.reason_phrase}"
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise TalkAuthError("Codex token refresh returned non-JSON response") from exc
    if not isinstance(payload, dict):
        raise TalkAuthError("Codex token refresh returned invalid response")
    return payload


def _write_auth_json(auth_path: Path, data: dict) -> None:
    """Atomically persist refreshed tokens, preserving the file's shape."""

    fd, tmp_name = tempfile.mkstemp(prefix="auth-", suffix=".json", dir=str(auth_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_name, auth_path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _refresh_codex_credential(
    auth_path: Path, data: dict, credential: _CodexOauthCredential
) -> _CodexOauthCredential:
    try:
        payload = _post_token_form(
            {
                "grant_type": "refresh_token",
                "refresh_token": credential.refresh,
                "client_id": _CODEX_CLIENT_ID,
            }
        )
    except TalkAuthError as exc:
        # Refresh-token race: the Codex CLI may have refreshed concurrently,
        # consuming the single-use refresh token we just tried. Re-read the
        # file once — if it now holds a different, valid access token, use it.
        reread = _read_codex_auth_json(auth_path)
        if reread is not None:
            reparsed = _parse_codex_oauth_credential(reread, _fallback_expiry_s(auth_path))
            if (
                reparsed is not None
                and reparsed.access != credential.access
                and reparsed.expires_s > time.time() + _REFRESH_MARGIN_S
            ):
                return reparsed
        raise TalkAuthError(
            f"{REALTIME_AUTH_REQUIRED_MESSAGE}. Codex token refresh failed — "
            f"run `codex login` to refresh your ChatGPT sign-in. Detail: {exc}"
        ) from exc

    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if (
        not isinstance(access, str)
        or not access
        or not isinstance(refresh, str)
        or not refresh
        or not isinstance(expires_in, (int, float))
    ):
        raise TalkAuthError(
            "Codex token refresh response missing fields "
            "(access_token, refresh_token, expires_in)"
        )

    expires_s = int(time.time()) + int(expires_in)
    refreshed = _CodexOauthCredential(
        access=access,
        refresh=refresh,
        expires_s=expires_s,
        account_id=credential.account_id,
        id_token=(
            payload.get("id_token")
            if isinstance(payload.get("id_token"), str)
            else credential.id_token
        ),
    )

    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    data["tokens"] = {
        **tokens,
        "access_token": refreshed.access,
        "refresh_token": refreshed.refresh,
        **({"id_token": refreshed.id_token} if refreshed.id_token else {}),
        **({"account_id": refreshed.account_id} if refreshed.account_id else {}),
    }
    data["last_refresh"] = datetime.now(UTC).isoformat()
    # Persisting is best-effort: the in-memory token is still valid for this
    # session even if the write-back fails (read-only profile dir).
    with contextlib.suppress(OSError):
        _write_auth_json(auth_path, data)
    return refreshed


def _resolve_codex_oauth(codex_home: Path | None = None) -> TalkAuth | None:
    auth_path = _codex_auth_path(codex_home)
    data = _read_codex_auth_json(auth_path)
    if data is None:
        return None
    credential = _parse_codex_oauth_credential(data, _fallback_expiry_s(auth_path))
    if credential is None:
        return None
    if credential.expires_s <= time.time() + _REFRESH_MARGIN_S:
        credential = _refresh_codex_credential(auth_path, data, credential)
    return TalkAuth(
        token=credential.access,
        source=SOURCE_CODEX_OAUTH,
        detail="Codex CLI login (ChatGPT subscription)",
        expires_at=datetime.fromtimestamp(credential.expires_s, tz=UTC),
    )


def resolve_auth(
    *,
    env: Mapping[str, str] | None = None,
    codex_home: Path | None = None,
) -> TalkAuth:
    """Resolve an OpenAI Platform bearer token in fail-closed order.

    Order: ``TALK_OPENAI_API_KEY`` -> ``OPENAI_API_KEY`` -> Codex OAuth.
    A Talk-scoped key that is present but blank fails closed instead of
    silently falling through to another source.
    """

    source = os.environ if env is None else env
    if prefer_codex_oauth(source):
        oauth = _resolve_codex_oauth(codex_home)
        if oauth is not None:
            return oauth
        raise TalkAuthError(
            "Codex preference is enabled, but no usable Codex OAuth login exists. "
            "Run `codex login` or unset TALK_PREFER_CODEX_OAUTH; metered API keys "
            "were not used."
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

    oauth = _resolve_codex_oauth(codex_home)
    if oauth is not None:
        return oauth

    raise TalkAuthError(REALTIME_AUTH_REQUIRED_MESSAGE)


def auth_diagnostic(
    *,
    env: Mapping[str, str] | None = None,
    codex_home: Path | None = None,
    now_s: float | None = None,
) -> dict:
    """Inspect auth selection without refreshing, writing, or exposing secrets.

    This is deliberately separate from :func:`resolve_auth`. An expired Codex
    token is useful diagnostic evidence, but resolving it would perform a
    network refresh and atomically rewrite ``auth.json`` — forbidden behavior
    for ``hermes talk doctor``.
    """

    source = os.environ if env is None else env
    now = time.time() if now_s is None else now_s
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

    auth_path = _codex_auth_path(codex_home)
    oauth_state = "missing"
    data = _read_codex_auth_json(auth_path)
    if auth_path.exists() and data is None:
        oauth_state = "invalid"
    elif data is not None:
        credential = _parse_codex_oauth_credential(data, _fallback_expiry_s(auth_path))
        if credential is None:
            oauth_state = "invalid"
        elif credential.expires_s <= now + _REFRESH_MARGIN_S:
            oauth_state = "expired"
        else:
            oauth_state = "valid"

    winning_lane: str | None = None
    blocked_by: str | None = None
    if preferred is None:
        blocked_by = "invalid-preference"
    elif preferred:
        if oauth_state in {"valid", "expired"}:
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
    elif oauth_state in {"valid", "expired"}:
        winning_lane = SOURCE_CODEX_OAUTH
    else:
        blocked_by = "no-usable-auth"

    metered_wins = (
        winning_lane in {SOURCE_CONFIGURED, SOURCE_ENV}
        and oauth_state in {"valid", "expired"}
    )
    return {
        "configured": winning_lane is not None,
        "winning_lane": winning_lane,
        "preference": preference,
        "codex_oauth": oauth_state,
        "metered_key_present": metered_key_present,
        "metered_key_wins_over_codex": metered_wins,
        "metered_keys_ignored": bool(preferred and metered_key_present),
        "refresh_required": winning_lane == SOURCE_CODEX_OAUTH and oauth_state == "expired",
        "blocked_by": blocked_by,
    }


def auth_status(
    *,
    env: Mapping[str, str] | None = None,
    codex_home: Path | None = None,
) -> dict:
    """Report which auth source would be used, without exposing any token."""

    receipt = auth_diagnostic(env=env, codex_home=codex_home)
    lane = receipt["winning_lane"]
    if lane == SOURCE_CONFIGURED:
        detail = "TALK_OPENAI_API_KEY (Talk-scoped key)"
    elif lane == SOURCE_ENV:
        detail = "OPENAI_API_KEY environment variable"
    elif lane == SOURCE_CODEX_OAUTH:
        detail = (
            "Codex CLI login (ChatGPT subscription), token "
            + ("expired-will-refresh" if receipt["refresh_required"] else "valid")
        )
    elif receipt["blocked_by"] == "invalid-preference":
        detail = "TALK_PREFER_CODEX_OAUTH is invalid; set it to true or false"
    elif receipt["blocked_by"] == "blank-talk-key":
        detail = "TALK_OPENAI_API_KEY is set but empty; fix or unset it"
    elif receipt["blocked_by"] == "blank-openai-key":
        detail = "OPENAI_API_KEY is set but empty; fix or unset it"
    elif receipt["blocked_by"] == "codex-oauth-unusable":
        detail = "Codex OAuth is required but unusable; run `codex login`"
    else:
        detail = REALTIME_AUTH_REQUIRED_MESSAGE
    return {"configured": lane is not None, "source": lane, "detail": detail}


__all__ = [
    "REALTIME_AUTH_REQUIRED_MESSAGE",
    "SOURCE_CODEX_OAUTH",
    "SOURCE_CONFIGURED",
    "SOURCE_ENV",
    "TalkAuth",
    "TalkAuthError",
    "auth_diagnostic",
    "auth_status",
    "prefer_codex_oauth",
    "resolve_auth",
]
