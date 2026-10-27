"""The Grok auth lane: preference knob and metered keys.

The ``xai-oauth`` subscription lane is unavailable: catalog admission rule 11
lets a plugin use a sign-in only through Hermes-signed requests, and Hermes
signs ``openai-codex`` alone today. These tests pin that Talk never reads
Hermes's ``auth.json`` or imports its private ``hermes_cli.auth`` resolver, and
that the key lanes behave as before. Every key here is a short fake.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

import talk_auth
import talk_grok_auth

ACCESS = "xai-oauth-access-canary"
REFRESH = "xai-oauth-refresh-canary"


def _write_xai_store(home: Path) -> Path:
    """A store with a usable-looking login, which Talk must ignore."""

    home.mkdir(parents=True, exist_ok=True)
    path = home / "auth.json"
    tokens = {"access_token": ACCESS, "refresh_token": REFRESH}
    path.write_text(
        json.dumps(
            {
                "providers": {"xai-oauth": {"tokens": tokens}},
                "credential_pool": {"xai-oauth": [tokens]},
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


class _ForbiddenModule(types.ModuleType):
    """A ``sys.modules`` entry that fails the test if anything touches it."""

    def __getattr__(self, name):
        raise AssertionError(f"talk_grok_auth touched hermes_cli.auth.{name}")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    for name in ("TALK_XAI_API_KEY", "XAI_API_KEY", "TALK_PREFER_XAI_OAUTH"):
        monkeypatch.delenv(name, raising=False)
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", _ForbiddenModule("hermes_cli.auth"))


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


def test_preference_refuses_and_never_uses_the_keys():
    with pytest.raises(talk_auth.TalkAuthError) as info:
        talk_grok_auth.resolve_grok_auth(
            env={"TALK_PREFER_XAI_OAUTH": "true", "XAI_API_KEY": "shared-k"}
        )
    message = str(info.value)
    assert talk_grok_auth.XAI_OAUTH_UNAVAILABLE in message
    assert "metered API keys were not used" in message
    assert "shared-k" not in message


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


def test_no_key_names_only_the_key_remedy():
    with pytest.raises(talk_auth.TalkAuthError) as info:
        talk_grok_auth.resolve_grok_auth(env={})
    assert str(info.value) == talk_grok_auth.GROK_AUTH_REQUIRED_MESSAGE
    assert "XAI_API_KEY" in str(info.value)
    assert "hermes auth add xai-oauth" not in str(info.value)


def test_keys_come_from_the_profile_scoped_secret_route(monkeypatch):
    """With no explicit env, keys are read through ``talk_config.SECRET_ENV``."""

    seen = []

    def get_secret(name, default=None):
        seen.append(name)
        return {"XAI_API_KEY": "scoped-secret-k"}.get(name, default)

    monkeypatch.setitem(
        sys.modules, "agent.secret_scope", types.SimpleNamespace(get_secret=get_secret)
    )
    monkeypatch.setenv("XAI_API_KEY", "process-env-k")

    auth = talk_grok_auth.resolve_grok_auth()

    assert auth.token == "scoped-secret-k"
    assert "XAI_API_KEY" in seen


# -- no credential store, no private host resolver ------------------------------


def test_a_login_in_the_hermes_store_changes_nothing(tmp_path):
    path = _write_xai_store(tmp_path / "hermes")
    snap = _Snapshot(path)

    with pytest.raises(talk_auth.TalkAuthError) as info:
        talk_grok_auth.resolve_grok_auth()
    receipt = talk_grok_auth.grok_auth_diagnostic()

    assert str(info.value) == talk_grok_auth.GROK_AUTH_REQUIRED_MESSAGE
    assert receipt["xai_oauth"] == "unsupported"
    assert receipt["winning_lane"] is None
    assert receipt["blocked_by"] == "no-usable-auth"
    snap.assert_untouched()


def test_the_store_is_never_opened(monkeypatch, tmp_path):
    path = _write_xai_store(tmp_path / "hermes")
    real_read_text = Path.read_text
    real_open = open

    def guarded_read_text(self, *args, **kwargs):
        assert Path(self).name != "auth.json", "talk_grok_auth read auth.json"
        return real_read_text(self, *args, **kwargs)

    def guarded_open(file, *args, **kwargs):
        assert Path(str(file)).name != "auth.json", "talk_grok_auth opened auth.json"
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr("builtins.open", guarded_open)

    talk_grok_auth.grok_auth_status(env={"TALK_PREFER_XAI_OAUTH": "true"})
    with pytest.raises(talk_auth.TalkAuthError):
        talk_grok_auth.resolve_grok_auth(env={"TALK_PREFER_XAI_OAUTH": "true"})
    with pytest.raises(talk_auth.TalkAuthError):
        talk_grok_auth.resolve_grok_auth()
    assert path.exists()


def test_the_module_no_longer_reaches_for_the_host_resolver():
    """The autouse fixture plants a ``hermes_cli.auth`` that fails on any access."""

    talk_grok_auth.grok_auth_diagnostic(env={"TALK_PREFER_XAI_OAUTH": "true"})
    with pytest.raises(talk_auth.TalkAuthError):
        talk_grok_auth.resolve_grok_auth(env={"TALK_PREFER_XAI_OAUTH": "true"})
    with pytest.raises(talk_auth.TalkAuthError):
        talk_grok_auth.resolve_grok_auth(env={})
    source = Path(talk_grok_auth.__file__).read_text(encoding="utf-8")
    assert "resolve_xai_oauth_runtime_credentials" not in source
    assert "hermes_cli.auth import" not in source


# -- the read-only diagnostic ---------------------------------------------------


@pytest.mark.parametrize(
    ("env", "blocked_by", "lane"),
    [
        ({"TALK_PREFER_XAI_OAUTH": "maybe"}, "invalid-preference", None),
        ({"TALK_PREFER_XAI_OAUTH": "true"}, "xai-oauth-unusable", None),
        ({"TALK_PREFER_XAI_OAUTH": "true", "XAI_API_KEY": "k"}, "xai-oauth-unusable", None),
        ({"TALK_XAI_API_KEY": ""}, "blank-talk-key", None),
        ({"XAI_API_KEY": " "}, "blank-xai-key", None),
        ({}, "no-usable-auth", None),
        ({"TALK_XAI_API_KEY": "k"}, None, "configured"),
        ({"XAI_API_KEY": "k"}, None, "env"),
        ({"TALK_PREFER_XAI_OAUTH": "false", "XAI_API_KEY": "k"}, None, "env"),
    ],
)
def test_diagnostic_reaches_every_lane_verdict(env, blocked_by, lane):
    receipt = talk_grok_auth.grok_auth_diagnostic(env=env)

    assert receipt["blocked_by"] == blocked_by
    assert receipt["winning_lane"] == lane
    assert receipt["configured"] is (lane is not None)
    assert receipt["xai_oauth"] == "unsupported"
    assert receipt["refresh_required"] is False
    assert receipt["metered_key_wins_over_oauth"] is False
    assert "xai_oauth_source" not in receipt
    assert "host_refresh_available" not in receipt


def test_diagnostic_flags_ignored_keys_under_preference():
    receipt = talk_grok_auth.grok_auth_diagnostic(
        env={"TALK_PREFER_XAI_OAUTH": "true", "TALK_XAI_API_KEY": "k"}
    )
    assert receipt["winning_lane"] is None
    assert receipt["preference"] == "enabled"
    assert receipt["metered_key_present"] is True
    assert receipt["metered_keys_ignored"] is True


@pytest.mark.parametrize(
    ("env", "detail"),
    [
        ({"TALK_XAI_API_KEY": "k"}, "TALK_XAI_API_KEY (Talk-scoped key)"),
        ({"XAI_API_KEY": "k"}, "XAI_API_KEY (shared xAI key)"),
        ({"TALK_PREFER_XAI_OAUTH": "true"}, talk_grok_auth.XAI_OAUTH_UNAVAILABLE),
        ({"TALK_PREFER_XAI_OAUTH": "maybe"}, "TALK_PREFER_XAI_OAUTH must be true or false"),
        ({"TALK_XAI_API_KEY": ""}, "TALK_XAI_API_KEY is set but empty"),
        ({"XAI_API_KEY": ""}, "XAI_API_KEY is set but empty"),
        ({}, talk_grok_auth.GROK_AUTH_REQUIRED_MESSAGE),
    ],
)
def test_status_detail_names_the_lane_or_remedy(env, detail):
    assert talk_grok_auth.grok_auth_status(env=env)["detail"] == detail


def test_diagnostic_and_status_never_carry_a_key_or_the_store(tmp_path):
    path = _write_xai_store(tmp_path / "hermes")
    snap = _Snapshot(path)

    receipt = talk_grok_auth.grok_auth_diagnostic(env={"XAI_API_KEY": "shared-k"})
    status = talk_grok_auth.grok_auth_status(env={"XAI_API_KEY": "shared-k"})

    for blob in (json.dumps(receipt), json.dumps(status), status["detail"]):
        assert "shared-k" not in blob
        assert ACCESS not in blob
        assert REFRESH not in blob
        assert str(path) not in blob
    snap.assert_untouched()


def test_public_names_are_exported():
    for name in (
        "GROK_AUTH_REQUIRED_MESSAGE",
        "SOURCE_XAI_OAUTH",
        "XAI_OAUTH_UNAVAILABLE",
        "grok_auth_diagnostic",
        "grok_auth_status",
        "prefer_xai_oauth",
        "resolve_grok_auth",
    ):
        assert name in talk_grok_auth.__all__
    for removed in ("STORE_CREDENTIAL_POOL", "STORE_PROVIDERS"):
        assert removed not in talk_grok_auth.__all__
