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

API keys always win over OAuth; OAuth billing follows the authenticated
ChatGPT account's Realtime entitlement. The returned token is a bearer
credential — never log it.
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


class TalkAuthError(RuntimeError):
    """Raised when no OpenAI Platform credential can be resolved."""


@dataclass(frozen=True, slots=True)
class TalkAuth:
    """A resolved OpenAI Platform bearer credential."""

    token: str
    source: str  # SOURCE_CONFIGURED | SOURCE_ENV | SOURCE_CODEX_OAUTH
    detail: str
    expires_at: datetime | None = None


def _read_env(env: Mapping[str, str] | None, key: str) -> str | None:
    source = os.environ if env is None else env
    raw = source.get(key)
    if raw is None:
        return None
    trimmed = raw.strip()
    return trimmed or None


def _codex_auth_path(codex_home: Path | None = None) -> Path:
    if codex_home is not None:
        return codex_home / "auth.json"
    configured = os.environ.get("CODEX_HOME", "").strip()
    base = Path(configured) if configured else Path.home() / ".codex"
    return base / "auth.json"


def _decode_jwt_expiry_s(token: str) -> int | None:
    """Read the ``exp`` claim from a JWT without verifying the signature."""

    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + padding))
    except Exception:  # noqa: BLE001 - any malformed JWT means "no expiry claim"
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

    env_key = _read_env(env, "OPENAI_API_KEY")
    if env_key:
        return TalkAuth(
            token=env_key,
            source=SOURCE_ENV,
            detail="OPENAI_API_KEY environment variable",
        )

    oauth = _resolve_codex_oauth(codex_home)
    if oauth is not None:
        return oauth

    raise TalkAuthError(REALTIME_AUTH_REQUIRED_MESSAGE)


def auth_status(
    *,
    env: Mapping[str, str] | None = None,
    codex_home: Path | None = None,
) -> dict:
    """Report which auth source would be used, without exposing any token."""

    source = os.environ if env is None else env
    configured = source.get("TALK_OPENAI_API_KEY")
    if configured is not None and configured.strip():
        return {
            "configured": True,
            "source": SOURCE_CONFIGURED,
            "detail": "TALK_OPENAI_API_KEY (Talk-scoped key)",
        }
    if _read_env(env, "OPENAI_API_KEY"):
        return {
            "configured": True,
            "source": SOURCE_ENV,
            "detail": "OPENAI_API_KEY environment variable",
        }

    auth_path = _codex_auth_path(codex_home)
    data = _read_codex_auth_json(auth_path)
    if data is not None:
        credential = _parse_codex_oauth_credential(data, _fallback_expiry_s(auth_path))
        if credential is not None:
            state = (
                "valid"
                if credential.expires_s > time.time() + _REFRESH_MARGIN_S
                else "expired-will-refresh"
            )
            return {
                "configured": True,
                "source": SOURCE_CODEX_OAUTH,
                "detail": f"Codex CLI login (ChatGPT subscription), token {state}",
            }

    return {"configured": False, "source": None, "detail": REALTIME_AUTH_REQUIRED_MESSAGE}


__all__ = [
    "REALTIME_AUTH_REQUIRED_MESSAGE",
    "SOURCE_CODEX_OAUTH",
    "SOURCE_CONFIGURED",
    "SOURCE_ENV",
    "TalkAuth",
    "TalkAuthError",
    "auth_status",
    "resolve_auth",
]
