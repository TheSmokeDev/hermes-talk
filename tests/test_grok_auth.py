"""The Grok auth lane: preference knob, metered keys, and the host's xAI OAuth login.

Every token here is a short fake. The host (``hermes_cli.auth``) is never
importable in this suite, so each test states which host it wants: a fake
module in ``sys.modules`` (host importable), or ``None`` (host absent, the
plugin falls back to a READ-ONLY parse of ``auth.json``).
"""

from __future__ import annotations

import base64
import importlib.machinery
import json
import sys
import time
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

import talk_auth
import talk_grok_auth

ACCESS = "xai-oauth-access-canary"
REFRESH = "xai-oauth-refresh-canary"


def _jwt(exp: float, marker: str = ACCESS) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp, "sub": marker}).encode())
        .decode()
        .rstrip("=")
    )
    return f"{marker}.{payload}.signature"


def _write_xai_store(home: Path, *, access: str, refresh: str = REFRESH, raw=None) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "auth.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "xai-oauth": {
                        "auth_mode": "oauth_device_code",
                        "tokens": {"access_token": access, "refresh_token": refresh},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


class _Snapshot:
    """Bytes + mtime of the store, so a test can prove nothing wrote it."""

    def __init__(self, path: Path):
        self.path = path
        self.data = path.read_bytes()
        self.mtime = path.stat().st_mtime_ns

    def assert_untouched(self):
        assert self.path.read_bytes() == self.data
        assert self.path.stat().st_mtime_ns == self.mtime


def _install_fake_host(monkeypatch, *, resolver, importable_spec: bool = True):
    """A fake ``hermes_cli.auth`` with the two names the plugin imports."""

    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    auth_module = types.ModuleType("hermes_cli.auth")
    if importable_spec:
        auth_module.__spec__ = importlib.machinery.ModuleSpec("hermes_cli.auth", None)

    class AuthError(RuntimeError):
        def __init__(self, message: str, *, code: str | None = None):
            super().__init__(message)
            self.code = code

    auth_module.AuthError = AuthError
    auth_module.resolve_xai_oauth_runtime_credentials = resolver
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth_module)
    return AuthError


def _remove_host(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", None)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    for name in ("TALK_XAI_API_KEY", "XAI_API_KEY", "TALK_PREFER_XAI_OAUTH"):
        monkeypatch.delenv(name, raising=False)
    _remove_host(monkeypatch)


# -- the preference knob --------------------------------------------------------


def test_preference_defaults_off_when_absent():
    assert talk_grok_auth.prefer_xai_oauth({}) is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "ON"])
def test_preference_accepts_true_spellings(value):
    assert talk_grok_auth.prefer_xai_oauth({"TALK_PREFER_XAI_OAUTH": value}) is True


@pytest.mark.parametrize("value", ["false", "0", "no", "Off"])
def test_preference_accepts_false_spellings(value):
    assert talk_grok_auth.prefer_xai_oauth({"TALK_PREFER_XAI_OAUTH": value}) is False


@pytest.mark.parametrize("value", ["", "   ", "yes please"])
def test_preference_refuses_closed_on_garbage(value):
    with pytest.raises(talk_auth.TalkAuthError, match="must be true or false"):
        talk_grok_auth.prefer_xai_oauth({"TALK_PREFER_XAI_OAUTH": value})


# -- metered keys ---------------------------------------------------------------


def test_blank_scoped_key_refuses_closed():
    with pytest.raises(talk_auth.TalkAuthError, match="TALK_XAI_API_KEY is set but empty"):
        talk_grok_auth.resolve_grok_auth(env={"TALK_XAI_API_KEY": "  ", "XAI_API_KEY": "k"})


def test_blank_shared_key_refuses_closed():
    with pytest.raises(talk_auth.TalkAuthError, match="XAI_API_KEY is set but empty"):
        talk_grok_auth.resolve_grok_auth(env={"XAI_API_KEY": ""})


def test_scoped_key_beats_shared_key():
    auth = talk_grok_auth.resolve_grok_auth(
        env={"TALK_XAI_API_KEY": "scoped-k", "XAI_API_KEY": "shared-k"}
    )
    assert auth.token == "scoped-k"
    assert auth.source == talk_auth.SOURCE_CONFIGURED
    assert auth.expires_at is None


def test_shared_key_is_the_env_lane():
    auth = talk_grok_auth.resolve_grok_auth(env={"XAI_API_KEY": "shared-k"})
    assert auth.token == "shared-k"
    assert auth.source == talk_auth.SOURCE_ENV


def test_key_beats_oauth_without_preference(tmp_path):
    path = _write_xai_store(tmp_path, access=_jwt(time.time() + 7200))
    snap = _Snapshot(path)
    auth = talk_grok_auth.resolve_grok_auth(env={"XAI_API_KEY": "shared-k"}, hermes_home=tmp_path)
    assert auth.source == talk_auth.SOURCE_ENV
    assert auth.token == "shared-k"
    snap.assert_untouched()


def test_preference_puts_oauth_ahead_of_keys(tmp_path):
    token = _jwt(time.time() + 7200)
    _write_xai_store(tmp_path, access=token)
    auth = talk_grok_auth.resolve_grok_auth(
        env={"TALK_PREFER_XAI_OAUTH": "true", "XAI_API_KEY": "shared-k"},
        hermes_home=tmp_path,
    )
    assert auth.source == talk_grok_auth.SOURCE_XAI_OAUTH
    assert auth.token == token


def test_preference_with_no_login_refuses_the_keys(tmp_path):
    with pytest.raises(talk_auth.TalkAuthError) as info:
        talk_grok_auth.resolve_grok_auth(
            env={"TALK_PREFER_XAI_OAUTH": "true", "XAI_API_KEY": "shared-k"},
            hermes_home=tmp_path,
        )
    assert "metered API keys were not used" in str(info.value)
    assert "hermes auth add xai-oauth" in str(info.value)


def test_nothing_anywhere_names_both_remedies(tmp_path):
    with pytest.raises(talk_auth.TalkAuthError) as info:
        talk_grok_auth.resolve_grok_auth(env={}, hermes_home=tmp_path)
    assert str(info.value) == talk_grok_auth.GROK_AUTH_REQUIRED_MESSAGE
    assert "XAI_API_KEY" in str(info.value)
    assert "hermes auth add xai-oauth" in str(info.value)


# -- host resolver (the plugin never refreshes; the host does) ------------------


def test_host_resolver_wins_and_carries_expiry(monkeypatch, tmp_path):
    exp = int(time.time()) + 6 * 3600
    token = _jwt(exp)
    calls = []

    def resolver(**kwargs):
        calls.append(kwargs)
        return {"provider": "xai-oauth", "api_key": token, "source": "hermes-auth-store"}

    _install_fake_host(monkeypatch, resolver=resolver)
    auth = talk_grok_auth.resolve_grok_auth(env={}, hermes_home=tmp_path)

    assert calls == [{}]
    assert auth.token == token
    assert auth.source == talk_grok_auth.SOURCE_XAI_OAUTH
    assert auth.detail == talk_grok_auth.OAUTH_DETAIL
    assert auth.expires_at == datetime.fromtimestamp(exp, tz=UTC)
    assert not (tmp_path / "auth.json").exists()


def test_host_auth_error_becomes_a_remediation_without_paths_or_tokens(monkeypatch, tmp_path):
    secret_path = str(tmp_path / "hermes" / "auth.json")

    def resolver(**_kwargs):
        raise AuthError(f"tier denied for {ACCESS} in {secret_path}", code="xai_oauth_tier_denied")

    AuthError = _install_fake_host(monkeypatch, resolver=resolver)
    with pytest.raises(talk_auth.TalkAuthError) as info:
        talk_grok_auth.resolve_grok_auth(env={}, hermes_home=tmp_path)
    message = str(info.value)
    assert "xai_oauth_tier_denied" in message
    assert "hermes auth add xai-oauth" in message
    assert ACCESS not in message
    assert secret_path not in message
    assert "auth.json" not in message


def test_host_missing_login_falls_through_to_no_usable_auth(monkeypatch, tmp_path):
    # The host is importable and says "no login"; the plugin trusts it and does
    # NOT read the store file itself (a valid file here would be a lie).
    _write_xai_store(tmp_path, access=_jwt(time.time() + 7200))

    def resolver(**_kwargs):
        raise AuthError("no xai-oauth login", code="xai_auth_missing")

    AuthError = _install_fake_host(monkeypatch, resolver=resolver)
    with pytest.raises(talk_auth.TalkAuthError) as info:
        talk_grok_auth.resolve_grok_auth(env={}, hermes_home=tmp_path)
    assert str(info.value) == talk_grok_auth.GROK_AUTH_REQUIRED_MESSAGE


def test_host_generic_failure_is_a_relogin_hint(monkeypatch, tmp_path):
    def resolver(**_kwargs):
        raise OSError(f"network down while reading {ACCESS}")

    _install_fake_host(monkeypatch, resolver=resolver)
    with pytest.raises(talk_auth.TalkAuthError, match="could not be refreshed") as info:
        talk_grok_auth.resolve_grok_auth(env={}, hermes_home=tmp_path)
    assert ACCESS not in str(info.value)


def test_host_returning_no_token_is_refused(monkeypatch, tmp_path):
    _install_fake_host(monkeypatch, resolver=lambda **_kw: {"api_key": ""})
    with pytest.raises(talk_auth.TalkAuthError, match="returned no access token"):
        talk_grok_auth.resolve_grok_auth(env={}, hermes_home=tmp_path)


# -- file fallback (host not importable): read-only, never a write ---------------


def test_file_fallback_reads_a_valid_store_without_touching_it(tmp_path):
    token = _jwt(time.time() + 7200)
    path = _write_xai_store(tmp_path, access=token)
    snap = _Snapshot(path)

    auth = talk_grok_auth.resolve_grok_auth(env={}, hermes_home=tmp_path)

    assert auth.token == token
    assert auth.source == talk_grok_auth.SOURCE_XAI_OAUTH
    assert auth.expires_at is not None
    snap.assert_untouched()


def test_file_fallback_uses_hermes_home_env_by_default(tmp_path):
    token = _jwt(time.time() + 7200)
    _write_xai_store(tmp_path / "hermes", access=token)
    auth = talk_grok_auth.resolve_grok_auth(env={})
    assert auth.token == token


def test_file_fallback_missing_provider_is_no_usable_auth(tmp_path):
    path = _write_xai_store(tmp_path, access="ignored", raw=json.dumps({"providers": {}}))
    snap = _Snapshot(path)
    with pytest.raises(talk_auth.TalkAuthError) as info:
        talk_grok_auth.resolve_grok_auth(env={}, hermes_home=tmp_path)
    assert str(info.value) == talk_grok_auth.GROK_AUTH_REQUIRED_MESSAGE
    snap.assert_untouched()


def test_file_fallback_expired_token_asks_for_relogin(tmp_path):
    token = _jwt(time.time() - 10)
    path = _write_xai_store(tmp_path, access=token)
    snap = _Snapshot(path)
    with pytest.raises(talk_auth.TalkAuthError, match="has expired") as info:
        talk_grok_auth.resolve_grok_auth(env={}, hermes_home=tmp_path)
    assert "hermes auth add xai-oauth" in str(info.value)
    assert token not in str(info.value)
    snap.assert_untouched()


def test_file_fallback_unparseable_store_is_unreadable(tmp_path):
    path = _write_xai_store(tmp_path, access="ignored", raw="{not json")
    snap = _Snapshot(path)
    with pytest.raises(talk_auth.TalkAuthError, match="unreadable"):
        talk_grok_auth.resolve_grok_auth(env={}, hermes_home=tmp_path)
    receipt = talk_grok_auth.grok_auth_diagnostic(env={}, hermes_home=tmp_path)
    assert receipt["xai_oauth"] == "invalid"
    snap.assert_untouched()


def test_file_fallback_blank_refresh_token_is_invalid(tmp_path):
    path = _write_xai_store(tmp_path, access=_jwt(time.time() + 7200), refresh="")
    snap = _Snapshot(path)
    with pytest.raises(talk_auth.TalkAuthError, match="unreadable"):
        talk_grok_auth.resolve_grok_auth(env={}, hermes_home=tmp_path)
    snap.assert_untouched()


def test_file_fallback_reads_a_bom_prefixed_store(tmp_path):
    token = _jwt(time.time() + 7200)
    tokens = {"access_token": token, "refresh_token": REFRESH}
    body = json.dumps({"providers": {"xai-oauth": {"tokens": tokens}}})
    path = _write_xai_store(tmp_path, access="ignored", raw="﻿" + body)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert talk_grok_auth.resolve_grok_auth(env={}, hermes_home=tmp_path).token == token


# -- the read-only diagnostic ---------------------------------------------------


def test_diagnostic_never_calls_the_host_resolver(monkeypatch, tmp_path):
    def resolver(**_kwargs):
        raise AssertionError("doctor must never trigger a host refresh")

    _install_fake_host(monkeypatch, resolver=resolver)
    _write_xai_store(tmp_path, access=_jwt(time.time() - 10))

    receipt = talk_grok_auth.grok_auth_diagnostic(env={}, hermes_home=tmp_path)

    assert receipt["host_refresh_available"] is True
    assert receipt["xai_oauth"] == "expired"
    # Expired-but-refreshable is a usable lane: the host refreshes at session start.
    assert receipt["winning_lane"] == "xai-oauth"
    assert receipt["refresh_required"] is True
    assert receipt["blocked_by"] is None


@pytest.mark.parametrize(
    ("env", "store", "blocked_by", "lane"),
    [
        ({"TALK_PREFER_XAI_OAUTH": "maybe"}, None, "invalid-preference", None),
        ({"TALK_PREFER_XAI_OAUTH": "true"}, None, "xai-oauth-unusable", None),
        ({"TALK_XAI_API_KEY": ""}, None, "blank-talk-key", None),
        ({"XAI_API_KEY": " "}, None, "blank-xai-key", None),
        ({}, None, "no-usable-auth", None),
        ({}, "expired", "xai-oauth-unusable", None),
        ({}, "{bad", "xai-oauth-unusable", None),
        ({"TALK_XAI_API_KEY": "k"}, None, None, "configured"),
        ({"XAI_API_KEY": "k"}, None, None, "env"),
        ({}, "valid", None, "xai-oauth"),
    ],
)
def test_diagnostic_reaches_every_lane_verdict(tmp_path, env, store, blocked_by, lane):
    if store == "valid":
        _write_xai_store(tmp_path, access=_jwt(time.time() + 7200))
    elif store == "expired":
        _write_xai_store(tmp_path, access=_jwt(time.time() - 10))
    elif store is not None:
        _write_xai_store(tmp_path, access="ignored", raw=store)

    receipt = talk_grok_auth.grok_auth_diagnostic(env=env, hermes_home=tmp_path)

    assert receipt["blocked_by"] == blocked_by
    assert receipt["winning_lane"] == lane
    assert receipt["configured"] is (lane is not None)


def test_diagnostic_flags_a_metered_key_beating_a_usable_login(tmp_path):
    _write_xai_store(tmp_path, access=_jwt(time.time() + 7200))
    receipt = talk_grok_auth.grok_auth_diagnostic(env={"XAI_API_KEY": "k"}, hermes_home=tmp_path)
    assert receipt["winning_lane"] == "env"
    assert receipt["metered_key_present"] is True
    assert receipt["metered_key_wins_over_oauth"] is True
    assert receipt["metered_keys_ignored"] is False


def test_diagnostic_flags_ignored_keys_under_preference(tmp_path):
    _write_xai_store(tmp_path, access=_jwt(time.time() + 7200))
    receipt = talk_grok_auth.grok_auth_diagnostic(
        env={"TALK_PREFER_XAI_OAUTH": "true", "TALK_XAI_API_KEY": "k"}, hermes_home=tmp_path
    )
    assert receipt["winning_lane"] == "xai-oauth"
    assert receipt["preference"] == "enabled"
    assert receipt["metered_keys_ignored"] is True
    assert receipt["metered_key_wins_over_oauth"] is False


def test_refresh_required_flips_at_the_hosts_skew_for_a_long_token(tmp_path):
    now = int(time.time())
    _write_xai_store(tmp_path, access=_jwt(now + 3600 + 1))
    fresh = talk_grok_auth.grok_auth_diagnostic(env={}, hermes_home=tmp_path, now_s=now)
    assert fresh["refresh_required"] is False
    inside = talk_grok_auth.grok_auth_diagnostic(env={}, hermes_home=tmp_path, now_s=now + 1)
    assert inside["refresh_required"] is True
    assert inside["winning_lane"] == "xai-oauth"


def test_refresh_required_uses_the_short_skew_for_a_short_token(tmp_path):
    now = int(time.time())
    # 30 minutes left: under the 45-minute short-lifetime line, so the skew is
    # 120s instead of 3600s — otherwise every short token would always "need"
    # a refresh and the doctor warning would never clear.
    _write_xai_store(tmp_path, access=_jwt(now + 1800))
    receipt = talk_grok_auth.grok_auth_diagnostic(env={}, hermes_home=tmp_path, now_s=now)
    assert receipt["refresh_required"] is False
    edge = talk_grok_auth.grok_auth_diagnostic(env={}, hermes_home=tmp_path, now_s=now + 1800 - 120)
    assert edge["refresh_required"] is True


def test_diagnostic_and_status_never_carry_the_token(tmp_path):
    token = _jwt(time.time() + 7200)
    path = _write_xai_store(tmp_path, access=token)
    snap = _Snapshot(path)

    receipt = talk_grok_auth.grok_auth_diagnostic(
        env={"XAI_API_KEY": "shared-k"}, hermes_home=tmp_path
    )
    status = talk_grok_auth.grok_auth_status(env={"XAI_API_KEY": "shared-k"}, hermes_home=tmp_path)

    for blob in (json.dumps(receipt), json.dumps(status), status["detail"]):
        assert token not in blob
        assert ACCESS not in blob
        assert REFRESH not in blob
        assert "shared-k" not in blob
        assert str(path) not in blob
    snap.assert_untouched()


def test_public_names_are_exported():
    for name in (
        "GROK_AUTH_REQUIRED_MESSAGE",
        "RELOGIN_COMMAND",
        "SOURCE_XAI_OAUTH",
        "grok_auth_diagnostic",
        "grok_auth_status",
        "prefer_xai_oauth",
        "resolve_grok_auth",
    ):
        assert name in talk_grok_auth.__all__
