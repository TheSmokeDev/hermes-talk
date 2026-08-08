"""Auth resolution — the fail-closed dual lane (API key / Codex OAuth)."""

from __future__ import annotations

import base64
import json
import time

import pytest

import talk_auth


def _jwt_with_exp(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def _jwt_with_payload(value) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def _write_codex_auth(
    home,
    *,
    access: str,
    refresh: str = "refresh-1",
    auth_mode: str = "chatgpt",
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": auth_mode,
                "tokens": {
                    "access_token": access,
                    "refresh_token": refresh,
                    "account_id": "acct-1",
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _hermetic_lanes(monkeypatch, tmp_path):
    """No test may see the dev box's real keys or Codex login."""

    monkeypatch.delenv("TALK_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TALK_PREFER_CODEX_OAUTH", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))


def test_scoped_key_wins_over_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("TALK_OPENAI_API_KEY", "sk-scoped")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")
    _write_codex_auth(tmp_path / "codex", access=_jwt_with_exp(time.time() + 3600))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    auth = talk_auth.resolve_auth()
    assert auth.token == "sk-scoped"
    assert auth.source == talk_auth.SOURCE_CONFIGURED


def test_scoped_key_set_but_empty_fails_closed(monkeypatch):
    monkeypatch.setenv("TALK_OPENAI_API_KEY", "   ")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")

    with pytest.raises(talk_auth.TalkAuthError, match="set but empty"):
        talk_auth.resolve_auth()


def test_env_key_second(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")

    auth = talk_auth.resolve_auth()
    assert auth.token == "sk-shared"
    assert auth.source == talk_auth.SOURCE_ENV


def test_shared_key_set_but_empty_fails_closed_before_oauth(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    _write_codex_auth(tmp_path / "codex", access=_jwt_with_exp(time.time() + 3600))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    with pytest.raises(talk_auth.TalkAuthError, match="OPENAI_API_KEY is set but empty"):
        talk_auth.resolve_auth()


def test_codex_oauth_third(monkeypatch, tmp_path):
    _write_codex_auth(tmp_path / "codex", access=_jwt_with_exp(time.time() + 3600))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    auth = talk_auth.resolve_auth()
    assert auth.source == talk_auth.SOURCE_CODEX_OAUTH
    assert auth.expires_at is not None


def test_no_lane_raises_actionable(monkeypatch):
    with pytest.raises(talk_auth.TalkAuthError, match="codex login"):
        talk_auth.resolve_auth()


def test_api_key_mode_auth_json_is_not_oauth(monkeypatch, tmp_path):
    home = tmp_path / "codex"
    home.mkdir(parents=True)
    (home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-from-codex", "tokens": {}}), encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(home))

    with pytest.raises(talk_auth.TalkAuthError):
        talk_auth.resolve_auth()


def test_expired_token_refreshes_and_writes_back(monkeypatch, tmp_path):
    home = tmp_path / "codex"
    _write_codex_auth(home, access=_jwt_with_exp(time.time() - 10), refresh="refresh-old")
    monkeypatch.setenv("CODEX_HOME", str(home))

    seen: dict = {}

    def fake_post(fields: dict[str, str]) -> dict:
        seen.update(fields)
        return {
            "access_token": _jwt_with_exp(time.time() + 3600),
            "refresh_token": "refresh-new",
            "expires_in": 3600,
            "id_token": "id-new",
        }

    monkeypatch.setattr(talk_auth, "_post_token_form", fake_post)

    auth = talk_auth.resolve_auth()
    assert auth.source == talk_auth.SOURCE_CODEX_OAUTH
    assert seen["grant_type"] == "refresh_token"
    assert seen["refresh_token"] == "refresh-old"

    # Write-back is atomic and preserves shape: the CLI keeps working.
    persisted = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    assert persisted["tokens"]["refresh_token"] == "refresh-new"
    assert persisted["tokens"]["account_id"] == "acct-1"
    assert "last_refresh" in persisted


def test_refresh_race_rereads_the_cli_winner(monkeypatch, tmp_path):
    home = tmp_path / "codex"
    _write_codex_auth(home, access=_jwt_with_exp(time.time() - 10))
    monkeypatch.setenv("CODEX_HOME", str(home))

    def losing_post(fields: dict[str, str]) -> dict:
        # Simulate the Codex CLI refreshing concurrently: our single-use
        # refresh token is dead, but the file now holds the CLI's fresh token.
        _write_codex_auth(home, access=_jwt_with_exp(time.time() + 3600), refresh="refresh-cli")
        raise talk_auth.TalkAuthError("refresh failed (400)")

    monkeypatch.setattr(talk_auth, "_post_token_form", losing_post)

    auth = talk_auth.resolve_auth()
    assert auth.source == talk_auth.SOURCE_CODEX_OAUTH


def test_refresh_failure_with_no_winner_is_actionable(monkeypatch, tmp_path):
    home = tmp_path / "codex"
    _write_codex_auth(home, access=_jwt_with_exp(time.time() - 10))
    monkeypatch.setenv("CODEX_HOME", str(home))

    def failing_post(fields: dict[str, str]) -> dict:
        raise talk_auth.TalkAuthError("refresh failed (400)")

    monkeypatch.setattr(talk_auth, "_post_token_form", failing_post)

    with pytest.raises(talk_auth.TalkAuthError, match="codex login"):
        talk_auth.resolve_auth()


def test_status_reports_lane_without_tokens(monkeypatch, tmp_path):
    _write_codex_auth(tmp_path / "codex", access=_jwt_with_exp(time.time() + 3600))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    status = talk_auth.auth_status()
    assert status["configured"] is True
    assert status["source"] == talk_auth.SOURCE_CODEX_OAUTH
    # The whole point of a status surface: it can be logged and spoken.
    rendered = json.dumps(status)
    assert "access_token" not in rendered
    assert "header." not in rendered  # the fake JWT's literal prefix


def test_status_unconfigured_is_actionable():
    status = talk_auth.auth_status()
    assert status["configured"] is False
    assert "codex login" in status["detail"]


def test_jwt_decode_tolerates_garbage():
    assert talk_auth._decode_jwt_expiry_s("not-a-jwt") is None
    assert talk_auth._decode_jwt_expiry_s("a.!!!.c") is None


@pytest.mark.parametrize("payload", [[], None, "not-an-object", 7])
def test_non_object_jwt_payload_is_invalid_without_raising(monkeypatch, tmp_path, payload):
    home = tmp_path / "codex"
    access = _jwt_with_payload(payload)
    _write_codex_auth(home, access=access)
    monkeypatch.setenv("CODEX_HOME", str(home))

    assert talk_auth._decode_jwt_expiry_s(access) is None
    receipt = talk_auth.auth_diagnostic()
    assert receipt["codex_oauth"] == "invalid"
    assert receipt["winning_lane"] is None


def test_prefer_codex_outranks_metered_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "true")
    monkeypatch.setenv("TALK_OPENAI_API_KEY", "sk-scoped-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared-secret")
    _write_codex_auth(tmp_path / "codex", access=_jwt_with_exp(time.time() + 3600))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    auth = talk_auth.resolve_auth()

    assert auth.source == talk_auth.SOURCE_CODEX_OAUTH


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_prefer_codex_fails_closed_when_oauth_is_missing(monkeypatch, value):
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", value)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-metered-secret")

    with pytest.raises(talk_auth.TalkAuthError, match="codex login"):
        talk_auth.resolve_auth()


def test_blank_preference_fails_closed_instead_of_spending_a_key(monkeypatch):
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "   ")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")

    with pytest.raises(talk_auth.TalkAuthError, match="TALK_PREFER_CODEX_OAUTH"):
        talk_auth.resolve_auth()


def test_invalid_preference_fails_closed_instead_of_spending_a_key(monkeypatch):
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "sometimes")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")

    with pytest.raises(talk_auth.TalkAuthError, match="TALK_PREFER_CODEX_OAUTH"):
        talk_auth.resolve_auth()


def test_explicit_false_keeps_existing_key_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "false")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")
    _write_codex_auth(tmp_path / "codex", access=_jwt_with_exp(time.time() + 3600))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    assert talk_auth.resolve_auth().source == talk_auth.SOURCE_ENV


def test_preferred_expired_oauth_refreshes_instead_of_spending_a_key(
    monkeypatch, tmp_path
):
    home = tmp_path / "codex"
    _write_codex_auth(home, access=_jwt_with_exp(time.time() - 10), refresh="refresh-old")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-metered-secret")
    monkeypatch.setattr(
        talk_auth,
        "_post_token_form",
        lambda fields: {
            "access_token": _jwt_with_exp(time.time() + 3600),
            "refresh_token": "refresh-new",
            "expires_in": 3600,
        },
    )

    assert talk_auth.resolve_auth().source == talk_auth.SOURCE_CODEX_OAUTH


def test_preferred_blank_oauth_fails_closed_without_spending_a_key(
    monkeypatch, tmp_path
):
    home = tmp_path / "codex"
    _write_codex_auth(home, access="", refresh="")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-metered-secret")

    with pytest.raises(talk_auth.TalkAuthError, match="codex login"):
        talk_auth.resolve_auth()
