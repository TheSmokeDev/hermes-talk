"""Auth resolution — the fail-closed dual lane (API key / Hermes-signed Codex sign-in)."""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

import pytest

import talk_auth
import talk_config
import talk_wire

_REAL_HERMES_SIGNS_CODEX = talk_auth.hermes_signs_codex


class _NotSignedIn(RuntimeError):
    pass


def _install_fake_signer(monkeypatch, handler) -> list:
    """Stand in for ``hermes_cli.plugin_provider_requests`` so the real broker path runs."""

    calls = []
    package = types.ModuleType("hermes_cli")
    module = types.ModuleType("hermes_cli.plugin_provider_requests")
    module.ProviderNotSignedIn = _NotSignedIn

    def credentialed_provider_request(
        provider, method, url, *, json=None, headers=None, timeout=30.0
    ):
        calls.append(
            {
                "provider": provider,
                "method": method,
                "url": url,
                "json": json,
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )
        return handler()

    module.credentialed_provider_request = credentialed_provider_request
    package.plugin_provider_requests = module
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_provider_requests", module)
    return calls


def _signed_response(status=200, payload=None):
    body = json.dumps(payload if payload is not None else {}).encode()
    return types.SimpleNamespace(
        status=status, headers={}, body=body, text=body.decode(), json=lambda: json.loads(body)
    )


@pytest.fixture(autouse=True)
def _hermetic_lanes(monkeypatch):
    """No test may see the dev box's real keys, Hermes host, or Codex sign-in."""

    monkeypatch.delenv("TALK_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TALK_PREFER_CODEX_OAUTH", raising=False)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    monkeypatch.setattr(talk_auth, "hermes_signs_codex", lambda: False)


def _hermes_host(monkeypatch):
    monkeypatch.setattr(talk_auth, "hermes_signs_codex", lambda: True)


def test_scoped_key_wins_over_everything(monkeypatch):
    _hermes_host(monkeypatch)
    monkeypatch.setenv("TALK_OPENAI_API_KEY", "sk-scoped")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")

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


def test_shared_key_set_but_empty_fails_closed_before_oauth(monkeypatch):
    _hermes_host(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "   ")

    with pytest.raises(talk_auth.TalkAuthError, match="OPENAI_API_KEY is set but empty"):
        talk_auth.resolve_auth()


def test_codex_lane_third_carries_no_token(monkeypatch):
    _hermes_host(monkeypatch)

    auth = talk_auth.resolve_auth()
    assert auth.source == talk_auth.SOURCE_CODEX_OAUTH
    assert auth.token == ""
    assert auth.account_id is None


def test_no_lane_raises_actionable():
    with pytest.raises(talk_auth.TalkAuthError, match="hermes auth add openai-codex"):
        talk_auth.resolve_auth()


def test_codex_lane_never_reads_vendor_files(monkeypatch, tmp_path):
    """Rule 11: no ~/.codex/auth.json read, even when one exists and Hermes is absent."""

    home = tmp_path / "codex"
    home.mkdir()
    (home / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "a.b.c"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    with pytest.raises(talk_auth.TalkAuthError):
        talk_auth.resolve_auth()
    assert talk_auth.auth_diagnostic()["codex_oauth"] == "missing"


def test_hermes_signs_codex_follows_the_host_helper(monkeypatch):
    monkeypatch.setattr(talk_auth.importlib.util, "find_spec", lambda name: object())
    assert _REAL_HERMES_SIGNS_CODEX() is True
    monkeypatch.setattr(talk_auth.importlib.util, "find_spec", lambda name: None)
    assert _REAL_HERMES_SIGNS_CODEX() is False

    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr(talk_auth.importlib.util, "find_spec", missing)
    assert _REAL_HERMES_SIGNS_CODEX() is False


def test_codex_request_lets_hermes_attach_the_credential(monkeypatch):
    calls = _install_fake_signer(monkeypatch, lambda: _signed_response(200, {"ok": True}))

    response = talk_auth.codex_request(
        "POST", "https://api.openai.com/v1/realtime/client_secrets", json={"a": 1}, timeout=5
    )

    assert response.status == 200
    assert calls == [
        {
            "provider": talk_auth.CODEX_PROVIDER,
            "method": "POST",
            "url": "https://api.openai.com/v1/realtime/client_secrets",
            "json": {"a": 1},
            "headers": {},
            "timeout": 5,
        }
    ]
    assert "Authorization" not in calls[0]["headers"]


def test_codex_request_maps_not_signed_in_to_an_auth_error(monkeypatch):
    def refuse():
        raise _NotSignedIn("Not signed in to Codex in Hermes. Run `hermes auth add openai-codex`")

    _install_fake_signer(monkeypatch, refuse)

    with pytest.raises(talk_auth.TalkAuthError, match="hermes auth add openai-codex") as caught:
        talk_auth.codex_request("POST", "https://api.openai.com/x")
    assert caught.value.__cause__ is None


def test_codex_request_maps_a_refused_origin_to_an_auth_error(monkeypatch):
    def refuse():
        raise PermissionError("Refusing to send the openai-codex sign-in to evil.example")

    _install_fake_signer(monkeypatch, refuse)

    with pytest.raises(talk_auth.TalkAuthError, match="Hermes refused the Codex request"):
        talk_auth.codex_request("POST", "https://evil.example/x")


def test_codex_request_without_the_host_names_the_version(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_provider_requests", None)

    with pytest.raises(talk_auth.TalkAuthError, match=r"0\.21\.7"):
        talk_auth.codex_request("POST", "https://api.openai.com/x")


def test_signed_mint_goes_through_hermes_and_returns_only_the_ephemeral_secret(monkeypatch):
    calls = _install_fake_signer(
        monkeypatch,
        lambda: _signed_response(200, {"value": "ek_ephemeral", "expires_at": 1700000000}),
    )

    descriptor = talk_wire.mint_ephemeral_session(
        auth_token="",
        hermes_signed=True,
        model="gpt-realtime-2.1",
        voice="cedar",
        instructions="hi",
    )

    assert descriptor.client_secret == "ek_ephemeral"
    assert calls[0]["url"] == talk_wire.CLIENT_SECRETS_URL
    assert calls[0]["json"]["session"]["model"] == "gpt-realtime-2.1"


def test_signed_mint_surfaces_an_upstream_refusal(monkeypatch):
    _install_fake_signer(monkeypatch, lambda: _signed_response(401, {"error": "no"}))

    with pytest.raises(talk_wire.TalkUpstreamError, match="401"):
        talk_wire.post_signed_client_secret({"type": "realtime"})


def test_key_mint_never_asks_hermes(monkeypatch):
    calls = _install_fake_signer(monkeypatch, lambda: _signed_response(200, {"value": "x"}))
    seen = []
    monkeypatch.setattr(
        talk_wire, "post_client_secret", lambda token, session: seen.append(token) or {"value": "x"}
    )

    talk_wire.mint_ephemeral_session(
        auth_token="sk-key", model="gpt-realtime-2.1", voice="cedar", instructions="hi"
    )

    assert seen == ["sk-key"]
    assert calls == []


def test_secret_reads_through_the_profile_scope_when_hermes_is_present(monkeypatch):
    package = types.ModuleType("agent")
    module = types.ModuleType("agent.secret_scope")
    asked = []
    scoped = {"OPENAI_API_KEY": "scoped-value"}
    module.get_secret = lambda name: asked.append(name) or scoped.get(name)
    package.secret_scope = module
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", module)
    monkeypatch.setenv("OPENAI_API_KEY", "process-env-value")

    assert talk_config.secret("OPENAI_API_KEY") == "scoped-value"
    assert talk_auth.resolve_auth().token == "scoped-value"
    assert "OPENAI_API_KEY" in asked


def test_secret_falls_back_to_the_process_env_standalone(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-standalone")
    assert talk_config.secret("OPENAI_API_KEY") == "sk-standalone"
    assert talk_config.SECRET_ENV.get("OPENAI_API_KEY") == "sk-standalone"
    assert "TALK_DEFINITELY_UNSET" not in talk_config.SECRET_ENV


def test_status_reports_lane_without_tokens(monkeypatch):
    _hermes_host(monkeypatch)

    status = talk_auth.auth_status()
    assert status["configured"] is True
    assert status["source"] == talk_auth.SOURCE_CODEX_OAUTH
    assert "Hermes signs" in status["detail"]


def test_status_unconfigured_is_actionable():
    status = talk_auth.auth_status()
    assert status["configured"] is False
    assert "hermes auth add openai-codex" in status["detail"]


@pytest.mark.parametrize("host", [False, True])
def test_diagnostic_names_the_host_route_without_secrets(monkeypatch, host):
    monkeypatch.setattr(talk_auth, "hermes_signs_codex", lambda: host)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared-secret")

    receipt = talk_auth.auth_diagnostic()

    assert receipt["codex_oauth"] == ("hermes" if host else "missing")
    assert receipt["winning_lane"] == talk_auth.SOURCE_ENV
    assert receipt["metered_key_wins_over_codex"] is host
    assert receipt["refresh_required"] is False
    assert "sk-shared-secret" not in json.dumps(receipt)


def test_prefer_codex_outranks_metered_keys(monkeypatch):
    _hermes_host(monkeypatch)
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "true")
    monkeypatch.setenv("TALK_OPENAI_API_KEY", "sk-scoped-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared-secret")

    auth = talk_auth.resolve_auth()

    assert auth.source == talk_auth.SOURCE_CODEX_OAUTH
    assert auth.token == ""
    receipt = talk_auth.auth_diagnostic()
    assert receipt["metered_keys_ignored"] is True


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_prefer_codex_fails_closed_when_the_host_cannot_sign(monkeypatch, value):
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", value)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-metered-secret")

    with pytest.raises(talk_auth.TalkAuthError, match="metered API keys were not used"):
        talk_auth.resolve_auth()
    assert talk_auth.auth_diagnostic()["blocked_by"] == "codex-oauth-unusable"


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


def test_explicit_false_keeps_existing_key_precedence(monkeypatch):
    _hermes_host(monkeypatch)
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "false")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")

    assert talk_auth.resolve_auth().source == talk_auth.SOURCE_ENV


def test_explicit_env_mapping_is_used_verbatim(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "process-value")

    auth = talk_auth.resolve_auth(env={"TALK_OPENAI_API_KEY": "mapped-value"})

    assert auth.token == "mapped-value"
    assert "mapped-value" not in repr(auth)


def test_manifest_declares_the_codex_sign_in_hermes_signs():
    """Hermes refuses signed requests for a plugin that does not declare the provider."""

    manifest = (Path(__file__).parents[1] / "plugin.yaml").read_text("utf-8")
    assert re.search(r"(?m)^requires_auth:\r?\n  - openai-codex\r?$", manifest)
