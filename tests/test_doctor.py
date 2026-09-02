"""Native ``hermes talk doctor`` diagnostics and their read-only contract."""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import types
from pathlib import Path

import fixture_data
import pytest

import talk_auth
import talk_cli
import talk_doctor
import talk_host
import talk_tools


def _jwt_with_exp(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _jwt_with_payload(value) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _write_oauth(home: Path, *, access: str, refresh: str = "refresh-secret") -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "auth.json"
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": access,
                    "refresh_token": refresh,
                    "account_id": "account-secret",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-missing"))
    for name in (
        "TALK_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "TALK_PREFER_CODEX_OAUTH",
        "TALK_AGENT_PROFILE",
        "TALK_IDENTITY_INCLUDE",
        "TALK_MODEL",
        "TALK_VOICE",
        "TALK_DISCORD_OPERATOR_USER_IDS",
        "TALK_PROVIDER",
        "TALK_GROK_MODEL",
        "TALK_GROK_VOICE",
        "TALK_XAI_API_KEY",
        "XAI_API_KEY",
        "TALK_PREFER_XAI_OAUTH",
        "TALK_GEMINI_MODEL",
        "TALK_GEMINI_VOICE",
        "TALK_GEMINI_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    talk_host.bind_ctx(None)
    talk_tools.REGISTRATION_FAILURES.clear()
    talk_tools.REGISTRATION_RECEIPTS.clear()
    yield
    talk_host.bind_ctx(None)
    talk_tools.REGISTRATION_FAILURES.clear()
    talk_tools.REGISTRATION_RECEIPTS.clear()


def _checks(report: dict) -> dict[str, dict]:
    return {check["id"]: check for check in report["checks"]}


def test_json_shape_is_stable_and_machine_readable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-redacted")
    monkeypatch.setattr(talk_doctor.talk_audio, "audio_available", lambda: True)

    report = talk_doctor.collect_report()

    assert set(report) == {
        "schema_version",
        "command",
        "read_only",
        "ok",
        "summary",
        "checks",
    }
    assert report["schema_version"] == 1
    assert report["command"] == "hermes talk doctor"
    assert report["read_only"] is True
    assert set(report["summary"]) == {"pass", "warn", "fail"}
    assert [check["id"] for check in report["checks"]] == [
        "plugin",
        "provider",
        "auth",
        "model",
        "voice",
        "cascade",
        "audio",
        "identity",
        "discord",
        "host",
    ]
    assert all(
        set(check) == {"id", "status", "summary", "details", "remediation"}
        for check in report["checks"]
    )
    json.dumps(report)


def test_doctor_is_read_only_even_when_oauth_is_expired(monkeypatch, tmp_path):
    auth_path = _write_oauth(tmp_path / "codex", access=_jwt_with_exp(time.time() - 60))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    before = auth_path.read_bytes()
    before_mtime = auth_path.stat().st_mtime_ns

    def forbidden(*args, **kwargs):
        raise AssertionError("doctor tried to refresh or write OAuth")

    monkeypatch.setattr(talk_auth, "_post_token_form", forbidden)
    monkeypatch.setattr(talk_auth, "_write_auth_json", forbidden)

    auth = _checks(talk_doctor.collect_report())["auth"]

    assert auth["status"] == "warn"
    assert auth["details"]["codex_oauth"] == "expired"
    assert auth_path.read_bytes() == before
    assert auth_path.stat().st_mtime_ns == before_mtime


def test_doctor_does_not_create_missing_home_or_codex_directories(monkeypatch, tmp_path):
    hermes_home = tmp_path / "missing-hermes"
    codex_home = tmp_path / "missing-codex"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(talk_doctor.talk_audio, "audio_available", lambda: False)

    talk_doctor.collect_report()

    assert not hermes_home.exists()
    assert not codex_home.exists()


def test_blank_oauth_is_reported_without_tokens(monkeypatch, tmp_path):
    access = ""
    _write_oauth(tmp_path / "codex", access=access)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    payload = json.dumps(talk_doctor.collect_report())
    auth = _checks(json.loads(payload))["auth"]

    assert auth["status"] == "fail"
    assert auth["details"]["codex_oauth"] == "invalid"
    assert access not in payload or not access
    assert "refresh-secret" not in payload
    assert "account-secret" not in payload


@pytest.mark.parametrize("jwt_payload", [[], None, "not-an-object", 7])
def test_malformed_non_object_jwt_preserves_doctor_schema(monkeypatch, tmp_path, jwt_payload):
    _write_oauth(tmp_path / "codex", access=_jwt_with_payload(jwt_payload))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    report = talk_doctor.collect_report()
    payload = json.loads(json.dumps(report))

    assert payload["schema_version"] == 1
    assert set(payload) == {"schema_version", "command", "read_only", "ok", "summary", "checks"}
    assert _checks(payload)["auth"]["details"]["codex_oauth"] == "invalid"


def test_metered_key_wins_by_old_precedence_and_warns_when_codex_is_ready(monkeypatch, tmp_path):
    _write_oauth(tmp_path / "codex", access=_jwt_with_exp(time.time() + 3600))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-metered-secret")

    auth = _checks(talk_doctor.collect_report())["auth"]

    assert auth["status"] == "warn"
    assert auth["details"]["winning_lane"] == talk_auth.SOURCE_ENV
    assert auth["details"]["metered_key_wins_over_codex"] is True
    assert "TALK_PREFER_CODEX_OAUTH=true" in " ".join(auth["remediation"])


def test_metered_key_winner_describes_expired_oauth_truthfully(monkeypatch, tmp_path):
    _write_oauth(tmp_path / "codex", access=_jwt_with_exp(time.time() - 60))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-metered-secret")

    auth = _checks(talk_doctor.collect_report())["auth"]

    assert auth["status"] == "warn"
    assert auth["details"]["winning_lane"] == talk_auth.SOURCE_ENV
    assert auth["details"]["codex_oauth"] == "expired"
    assert auth["details"]["metered_key_wins_over_codex"] is True
    assert "expired" in auth["summary"].lower()
    assert "refresh" in auth["summary"].lower()
    assert "usable" not in auth["summary"].lower()


def test_blank_shared_key_blocks_oauth_with_specific_remediation(monkeypatch, tmp_path):
    _write_oauth(tmp_path / "codex", access=_jwt_with_exp(time.time() + 3600))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("OPENAI_API_KEY", "   ")

    auth = _checks(talk_doctor.collect_report())["auth"]

    assert auth["status"] == "fail"
    assert auth["details"]["blocked_by"] == "blank-openai-key"
    assert "unset it" in " ".join(auth["remediation"])


def test_preferred_missing_oauth_fails_closed_and_reports_ignored_key(monkeypatch):
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-metered-secret")

    auth = _checks(talk_doctor.collect_report())["auth"]

    assert auth["status"] == "fail"
    assert auth["details"]["winning_lane"] is None
    assert auth["details"]["codex_oauth"] == "missing"
    assert auth["details"]["metered_keys_ignored"] is True


def test_identity_profile_home_collapses_to_root_without_duplicate_profile(monkeypatch, tmp_path):
    root = tmp_path / "hermes-root"
    profile_home = root / "profiles" / "voice"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setattr(
        talk_host.host(),
        "diagnostic_identity_sections",
        lambda: {"PERSONA": "private persona", "MEMORY": "private memory"},
    )

    identity = _checks(talk_doctor.collect_report())["identity"]

    assert identity["details"]["root"] == str(root)
    assert identity["details"]["resolved_home"] == str(profile_home)
    assert identity["details"]["identity_home"] == str(profile_home)
    assert identity["details"]["active_profile"] == "voice"
    assert identity["details"]["profile_source"] == "resolved-profile-home"
    assert identity["details"]["sections"] == {
        "PERSONA": {"characters": len("private persona")},
        "MEMORY": {"characters": len("private memory")},
    }
    assert "profiles\\voice\\profiles" not in json.dumps(identity)


def test_human_identity_receipt_includes_root_profile_and_counts(monkeypatch, tmp_path):
    root = tmp_path / "hermes-root"
    profile_home = root / "profiles" / "voice"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setattr(
        talk_host.host(),
        "diagnostic_identity_sections",
        lambda: {"USER": "private"},
    )

    rendered = talk_doctor.render_human(talk_doctor.collect_report())

    assert f"root={root}" in rendered
    assert "profile=voice (resolved-profile-home)" in rendered
    assert "sections=USER:7" in rendered
    assert "private" not in rendered


def test_home_provenance_reports_host_context_override(monkeypatch, tmp_path):
    override_home = tmp_path / "override"
    process_home = tmp_path / "process"
    monkeypatch.setenv("HERMES_HOME", str(process_home))
    monkeypatch.setitem(
        sys.modules,
        "hermes_constants",
        types.SimpleNamespace(
            get_hermes_home=lambda: override_home,
            get_hermes_home_override=lambda: str(override_home),
            get_process_hermes_home=lambda: process_home,
        ),
    )

    details = _checks(talk_doctor.collect_report())["identity"]["details"]

    assert details["resolved_home"] == str(override_home)
    assert details["home_source"] == "host-context-override"


def test_home_provenance_reports_process_env_winner(monkeypatch, tmp_path):
    process_home = tmp_path / "explicit"
    monkeypatch.setenv("HERMES_HOME", str(process_home))
    monkeypatch.setitem(
        sys.modules,
        "hermes_constants",
        types.SimpleNamespace(
            get_hermes_home=lambda: process_home,
            get_hermes_home_override=lambda: None,
            get_process_hermes_home=lambda: process_home,
        ),
    )

    details = _checks(talk_doctor.collect_report())["identity"]["details"]

    assert details["home_source"] == "HERMES_HOME"


@pytest.mark.parametrize("env_value", [r"~\issue21-home", r"relative\issue21-home"])
def test_home_provenance_uses_host_exact_env_path_semantics(monkeypatch, env_value):
    process_home = Path(env_value)
    monkeypatch.setenv("HERMES_HOME", env_value)
    monkeypatch.setitem(
        sys.modules,
        "hermes_constants",
        types.SimpleNamespace(
            get_hermes_home=lambda: process_home,
            get_hermes_home_override=lambda: None,
            get_process_hermes_home=lambda: process_home,
        ),
    )

    details = _checks(talk_doctor.collect_report())["identity"]["details"]

    assert details["resolved_home"] == str(process_home)
    assert details["home_source"] == "HERMES_HOME"


def test_home_provenance_reports_windows_platform_default_from_host(monkeypatch):
    windows_default = Path(r"C:\Users\operator\AppData\Local\hermes")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "hermes_constants",
        types.SimpleNamespace(
            get_hermes_home=lambda: windows_default,
            get_hermes_home_override=lambda: None,
            get_process_hermes_home=lambda: windows_default,
        ),
    )

    details = _checks(talk_doctor.collect_report())["identity"]["details"]

    assert details["resolved_home"] == str(windows_default)
    assert details["home_source"] == "host-process-default"


def test_home_provenance_is_unknown_when_host_process_semantics_fail(monkeypatch):
    host_home = Path(r"~\issue21-home")
    monkeypatch.setenv("HERMES_HOME", str(host_home))
    monkeypatch.setitem(
        sys.modules,
        "hermes_constants",
        types.SimpleNamespace(
            get_hermes_home=lambda: host_home,
            get_hermes_home_override=lambda: None,
            get_process_hermes_home=lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
        ),
    )

    details = _checks(talk_doctor.collect_report())["identity"]["details"]

    assert details["resolved_home"] == str(host_home)
    assert details["home_source"] == "host-resolver-unknown"


def test_home_provenance_uses_honest_fallback_for_older_host(monkeypatch, tmp_path):
    legacy_home = tmp_path / "legacy-host"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "different-env"))
    monkeypatch.setitem(
        sys.modules,
        "hermes_constants",
        types.SimpleNamespace(get_hermes_home=lambda: legacy_home),
    )

    details = _checks(talk_doctor.collect_report())["identity"]["details"]

    assert details["resolved_home"] == str(legacy_home)
    assert details["home_source"] == "host-resolver-unknown"


def test_unsupported_host_surfaces_are_diagnostic_not_an_exception(monkeypatch):
    talk_host.bind_ctx(object())

    host = _checks(talk_doctor.collect_report())["host"]

    assert host["status"] == "warn"
    assert host["details"]["context_bound"] is True
    assert host["details"]["capabilities"]["register_cli_command"] is False
    assert host["details"]["capabilities"]["register_realtime_voice_provider"] is False
    assert host["details"]["capabilities"]["dispatch_tool"] is False


def test_plugin_diagnostic_labels_core_and_legacy_lanes():
    talk_tools.REGISTRATION_RECEIPTS["realtime_voice_provider"] = "rejected"

    plugin = talk_doctor._plugin_check()

    assert plugin["details"]["legacy_lane"] == "legacy-provider-executor"
    assert plugin["details"]["core_realtime_contract"] == "api-v2-input-only"
    assert isinstance(plugin["details"]["core_contract_available"], bool)
    assert isinstance(plugin["details"]["core_provider_available"], bool)
    assert plugin["details"]["surfaces"]["realtime_voice_provider"] == "rejected"


def test_discord_receipt_counts_operators_without_ids(monkeypatch):
    first = "586638048133906576"
    second = "123456789012345678"
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", f"{first},{second}")

    report = talk_doctor.collect_report()
    rendered = json.dumps(report)
    discord = _checks(report)["discord"]

    assert discord["details"] == {"configured": True, "valid": True, "operator_count": 2}
    assert first not in rendered
    assert second not in rendered


def test_human_and_json_output_redact_all_sensitive_values(monkeypatch, tmp_path, capsys):
    api_secret = fixture_data.fake_credential("doctor-api")
    identity_secret = "the operator keeps this private"
    discord_secret = "586638048133906576"
    monkeypatch.setenv("TALK_OPENAI_API_KEY", api_secret)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", discord_secret)
    monkeypatch.setattr(
        talk_host.host(),
        "diagnostic_identity_sections",
        lambda: {"PERSONA": identity_secret},
    )
    monkeypatch.setattr(talk_doctor.talk_audio, "audio_available", lambda: True)

    assert talk_doctor.cli_entry(json_output=False) in {0, 1}
    human = capsys.readouterr().out
    assert talk_doctor.cli_entry(json_output=True) in {0, 1}
    machine = capsys.readouterr().out

    json.loads(machine)
    for secret in (api_secret, identity_secret, discord_secret):
        assert secret not in human
        assert secret not in machine


def test_secret_shaped_malformed_config_is_redacted_from_report_and_human(monkeypatch):
    model_secret = fixture_data.fake_credential("doctor-model")
    voice_secret = fixture_data.fake_credential("doctor-voice")
    monkeypatch.setenv("OPENAI_API_KEY", "configured-for-test")
    monkeypatch.setenv("TALK_MODEL", model_secret)
    monkeypatch.setenv("TALK_VOICE", voice_secret)

    report = talk_doctor.collect_report()
    machine = json.dumps(report)
    human = talk_doctor.render_human(report)

    for secret in (model_secret, voice_secret.lower()):
        assert secret not in machine
        assert secret not in human
    assert "<redacted-secret>" in machine
    assert "<redacted-secret>" in human


def test_cli_parser_and_dispatch_make_doctor_a_native_subcommand(monkeypatch):
    parser = argparse.ArgumentParser()
    talk_cli.setup_cli(parser)
    args = parser.parse_args(["doctor", "--json"])
    seen = []
    monkeypatch.setattr(
        talk_cli.talk_doctor,
        "cli_entry",
        lambda **kwargs: seen.append(kwargs) or 0,
    )

    assert talk_cli.cli_entry(args) == 0
    assert seen == [{"json_output": True, "probe": False}]


def test_native_cli_json_path_emits_the_doctor_envelope(monkeypatch, capsys):
    parser = argparse.ArgumentParser()
    talk_cli.setup_cli(parser)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-never-render-this")
    monkeypatch.setattr(talk_doctor.talk_audio, "audio_available", lambda: True)

    assert talk_cli.cli_entry(parser.parse_args(["doctor", "--json"])) == 0
    payload = capsys.readouterr().out

    assert json.loads(payload)["command"] == "hermes talk doctor"
    assert "sk-never-render-this" not in payload


def test_invalid_model_and_voice_are_separate_failures(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-redacted")
    monkeypatch.setenv("TALK_MODEL", "gpt-not-realtime")
    monkeypatch.setenv("TALK_VOICE", "morgan-freeman")

    checks = _checks(talk_doctor.collect_report())

    assert checks["model"]["status"] == "fail"
    assert checks["voice"]["status"] == "fail"


@pytest.mark.parametrize("model", ["gpt-realtime-whisper", "gpt-realtime-translate"])
def test_doctor_rejects_realtime_models_without_duplex_tool_contract(monkeypatch, model):
    monkeypatch.setenv("OPENAI_API_KEY", "configured-for-test")
    monkeypatch.setenv("TALK_MODEL", model)

    check = _checks(talk_doctor.collect_report())["model"]

    assert check["status"] == "fail"
    assert check["details"]["compatibility"] == "incompatible"
    assert "duplex" in check["summary"].lower()
    assert "tool" in check["summary"].lower()


def test_doctor_labels_unlisted_realtime_id_as_syntax_only_unknown(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "configured-for-test")
    monkeypatch.setenv("TALK_MODEL", "gpt-realtime-totally-fake")

    check = _checks(talk_doctor.collect_report())["model"]

    assert check["status"] == "warn"
    assert check["details"]["compatibility"] == "unknown"
    assert check["details"]["validation_scope"] == "syntax-only"
    assert "unknown" in check["summary"].lower()
    assert "valid" not in check["summary"].lower()


def test_provider_check_passes_openai_by_default():
    check = _checks(talk_doctor.collect_report())["provider"]

    assert check["status"] == "pass"
    assert check["details"]["provider"] == "openai"
    assert check["details"]["source"] == "default"


def test_provider_check_refuses_an_unknown_provider(monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "alexa")

    check = _checks(talk_doctor.collect_report())["provider"]

    assert check["status"] == "fail"
    assert check["details"]["provider"] == "alexa"
    assert check["remediation"]


def test_grok_provider_check_reports_readiness_without_the_key_value(monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "grok")
    monkeypatch.setenv("TALK_XAI_API_KEY", "xai-scoped-test")

    check = _checks(talk_doctor.collect_report())["provider"]

    assert check["status"] == "pass"
    assert check["details"]["keys"] == {"scoped": "present", "shared": "absent"}
    assert check["details"]["model"] == "grok-voice-latest"
    assert check["details"]["voice"] == "ara"
    assert check["details"]["voice_valid"] is True
    assert "xai-scoped-test" not in json.dumps(check)


def test_grok_provider_check_fails_without_a_key(monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "grok")

    check = _checks(talk_doctor.collect_report())["provider"]

    assert check["status"] == "fail"
    assert check["details"]["keys"] == {"scoped": "absent", "shared": "absent"}
    assert "no xAI key" in check["summary"]


def test_grok_provider_check_fails_on_a_blank_scoped_key(monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "grok")
    monkeypatch.setenv("TALK_XAI_API_KEY", "   ")
    monkeypatch.setenv("XAI_API_KEY", "xai-shared-test")

    check = _checks(talk_doctor.collect_report())["provider"]

    assert check["status"] == "fail"
    assert check["details"]["keys"]["scoped"] == "blank"


def test_grok_provider_check_fails_on_an_unknown_voice(monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "grok")
    monkeypatch.setenv("XAI_API_KEY", "xai-shared-test")
    monkeypatch.setenv("TALK_GROK_VOICE", "morgan-freeman")

    check = _checks(talk_doctor.collect_report())["provider"]

    assert check["status"] == "fail"
    assert check["details"]["voice"] == "morgan-freeman"
    assert check["details"]["voice_valid"] is False


def test_human_report_renders_the_grok_provider_receipt(monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "grok")
    monkeypatch.setenv("XAI_API_KEY", "xai-shared-test")
    monkeypatch.setattr(talk_doctor.talk_audio, "audio_available", lambda: True)

    rendered = talk_doctor.render_human(talk_doctor.collect_report())

    assert "[PASS] provider: grok realtime provider is configured" in rendered
    assert "key-shared=present" in rendered
    assert "xai-shared-test" not in rendered


def test_gemini_provider_check_reports_readiness_without_the_key_value(monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "gemini")
    monkeypatch.setenv("TALK_GEMINI_API_KEY", "gemini-scoped-test")

    check = _checks(talk_doctor.collect_report())["provider"]

    assert check["status"] == "pass"
    assert check["details"]["keys"] == {"scoped": "present", "shared": "absent"}
    assert check["details"]["model"] == "gemini-3.1-flash-live-preview"
    assert check["details"]["voice"] == "Puck"
    assert check["details"]["voice_valid"] is True
    assert "gemini-scoped-test" not in json.dumps(check)


def test_gemini_provider_check_fails_without_a_key(monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "gemini")

    check = _checks(talk_doctor.collect_report())["provider"]

    assert check["status"] == "fail"
    assert check["details"]["keys"] == {"scoped": "absent", "shared": "absent"}
    assert "no Gemini key" in check["summary"]


def test_gemini_provider_check_fails_on_a_blank_scoped_key(monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "gemini")
    monkeypatch.setenv("TALK_GEMINI_API_KEY", "   ")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-shared-test")

    check = _checks(talk_doctor.collect_report())["provider"]

    assert check["status"] == "fail"
    assert check["details"]["keys"]["scoped"] == "blank"


def test_gemini_provider_check_fails_on_an_unknown_voice(monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-shared-test")
    monkeypatch.setenv("TALK_GEMINI_VOICE", "morgan-freeman")

    check = _checks(talk_doctor.collect_report())["provider"]

    assert check["status"] == "fail"
    assert check["details"]["voice"] == "morgan-freeman"
    assert check["details"]["voice_valid"] is False


def test_gemini_provider_check_keeps_voice_casing_in_failures(monkeypatch):
    # Live voice names are case-sensitive; a lowercase typo must surface
    # as-is, never case-folded into something that looks valid.
    monkeypatch.setenv("TALK_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-shared-test")
    monkeypatch.setenv("TALK_GEMINI_VOICE", "puck")

    check = _checks(talk_doctor.collect_report())["provider"]

    assert check["status"] == "fail"
    assert check["details"]["voice"] == "puck"
    assert "case-sensitive" in " ".join(check["remediation"])


def test_human_report_renders_the_gemini_provider_receipt(monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-shared-test")
    monkeypatch.setattr(talk_doctor.talk_audio, "audio_available", lambda: True)

    rendered = talk_doctor.render_human(talk_doctor.collect_report())

    assert "[PASS] provider: gemini realtime provider is configured" in rendered
    assert "key-shared=present" in rendered
    assert "gemini-shared-test" not in rendered


# -- grok: the xAI OAuth lane ---------------------------------------------------

XAI_ACCESS = "xai-oauth-access-canary"
XAI_REFRESH = "xai-oauth-refresh-canary"


def _write_xai_oauth(home: Path, *, access: str, refresh: str = XAI_REFRESH) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "auth.json"
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


@pytest.fixture
def _no_host(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", None)


def test_grok_provider_and_auth_pass_on_a_valid_xai_oauth_login(monkeypatch, tmp_path, _no_host):
    monkeypatch.setenv("TALK_PROVIDER", "grok")
    store = _write_xai_oauth(tmp_path / "hermes", access=_jwt_with_exp(time.time() + 6 * 3600))
    before = store.read_bytes()

    checks = _checks(talk_doctor.collect_report())

    assert checks["provider"]["status"] == "pass"
    assert checks["provider"]["details"]["auth_lane"] == "xai-oauth"
    assert checks["provider"]["details"]["xai_oauth"] == "valid"
    assert checks["auth"]["status"] == "pass"
    assert checks["auth"]["details"]["winning_lane"] == "xai-oauth"
    assert checks["auth"]["summary"] == "xai-oauth is the winning auth lane"
    assert store.read_bytes() == before
    dumped = json.dumps(checks)
    assert XAI_ACCESS not in dumped
    assert XAI_REFRESH not in dumped
    assert str(store) not in dumped


def test_grok_provider_check_fails_without_a_key_or_a_login(monkeypatch, _no_host):
    monkeypatch.setenv("TALK_PROVIDER", "grok")

    checks = _checks(talk_doctor.collect_report())

    assert checks["provider"]["status"] == "fail"
    assert "no xAI key or xAI OAuth login" in checks["provider"]["summary"]
    assert "hermes auth add xai-oauth" in " ".join(checks["provider"]["remediation"])
    assert checks["auth"]["status"] == "fail"
    assert checks["auth"]["details"]["blocked_by"] == "no-usable-auth"


def test_grok_auth_warns_when_a_metered_key_outranks_a_valid_login(monkeypatch, tmp_path, _no_host):
    monkeypatch.setenv("TALK_PROVIDER", "grok")
    monkeypatch.setenv("XAI_API_KEY", "xai-shared-test")
    _write_xai_oauth(tmp_path / "hermes", access=_jwt_with_exp(time.time() + 6 * 3600))

    check = _checks(talk_doctor.collect_report())["auth"]

    assert check["status"] == "warn"
    assert check["details"]["winning_lane"] == "env"
    assert check["details"]["metered_key_wins_over_oauth"] is True
    assert "TALK_PREFER_XAI_OAUTH=true" in " ".join(check["remediation"])


def test_grok_auth_fails_closed_on_a_preferred_but_missing_login(monkeypatch, _no_host):
    monkeypatch.setenv("TALK_PROVIDER", "grok")
    monkeypatch.setenv("XAI_API_KEY", "xai-shared-test")
    monkeypatch.setenv("TALK_PREFER_XAI_OAUTH", "true")

    check = _checks(talk_doctor.collect_report())["auth"]

    assert check["status"] == "fail"
    assert check["details"]["winning_lane"] is None
    assert "API keys are ignored" in check["summary"]
    assert "hermes auth add xai-oauth" in " ".join(check["remediation"])


def test_grok_auth_warns_when_the_login_needs_a_refresh(monkeypatch, tmp_path, _no_host):
    monkeypatch.setenv("TALK_PROVIDER", "grok")
    # 50 minutes left: inside the host's one-hour refresh skew for a normal-lifetime token.
    _write_xai_oauth(tmp_path / "hermes", access=_jwt_with_exp(time.time() + 50 * 60))

    check = _checks(talk_doctor.collect_report())["auth"]

    assert check["status"] == "warn"
    assert check["details"]["refresh_required"] is True
    assert "did not refresh or write it" in check["summary"]


def test_human_report_renders_the_xai_oauth_receipt(monkeypatch, tmp_path, _no_host):
    monkeypatch.setenv("TALK_PROVIDER", "grok")
    _write_xai_oauth(tmp_path / "hermes", access=_jwt_with_exp(time.time() + 6 * 3600))
    monkeypatch.setattr(talk_doctor.talk_audio, "audio_available", lambda: True)

    rendered = talk_doctor.render_human(talk_doctor.collect_report())

    assert "[PASS] auth: xai-oauth is the winning auth lane" in rendered
    assert "receipt: winner=xai-oauth, xai-oauth=valid, preference=absent" in rendered
    assert XAI_ACCESS not in rendered


def test_doctor_never_probes_unless_asked(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-never-render-this")
    monkeypatch.setattr(talk_doctor.talk_audio, "audio_available", lambda: True)

    def forbidden():
        raise AssertionError("doctor probed the network without --probe")

    monkeypatch.setattr(talk_doctor, "_probe_grok", forbidden)

    assert talk_doctor.cli_entry(json_output=True) == 0
    assert "probe" not in json.loads(capsys.readouterr().out)


# -- grok: the live probe, with a fake aiohttp --------------------------------


class _ProbeContext:
    def __init__(self, value=None, *, raises=None):
        self.value = value
        self.raises = raises

    async def __aenter__(self):
        if self.raises is not None:
            raise self.raises
        return self.value

    async def __aexit__(self, *_exc):
        return False


class _ProbeSocket:
    def __init__(self, first_event: str):
        self.first_event = first_event

    async def receive(self):
        return types.SimpleNamespace(data=json.dumps({"type": self.first_event}))


class _ProbeHttp:
    def __init__(self, *, http_status: int, ws):
        self.http_status = http_status
        self.ws = ws
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _ProbeContext(types.SimpleNamespace(status=self.http_status))

    def ws_connect(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.ws, BaseException):
            return _ProbeContext(raises=self.ws)
        return _ProbeContext(self.ws)


def _fake_aiohttp(http: _ProbeHttp):
    return types.SimpleNamespace(
        ClientSession=lambda: http,
        ClientTimeout=lambda total: ("timeout", total),
    )


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_grok_probe_passes_on_200_and_session_created():
    http = _ProbeHttp(http_status=200, ws=_ProbeSocket("session.created"))

    result = _run(
        talk_doctor.run_grok_probe(
            auth_token="probe-token-canary",
            auth_source="xai-oauth",
            model="grok-voice-latest",
            aiohttp_module=_fake_aiohttp(http),
        )
    )

    assert result == {
        "auth_source": "xai-oauth",
        "model": "grok-voice-latest",
        "http_status": 200,
        "ws_status": 101,
        "first_event": "session.created",
        "error": None,
        "ok": True,
    }
    assert [url for url, _ in http.calls] == [
        talk_doctor.PROBE_HTTP_URL,
        f"{talk_doctor.talk_grok_realtime.XAI_REALTIME_WS_URL}?model=grok-voice-latest",
    ]
    expected = {"Authorization": "Bearer probe-token-canary"}
    assert all(kw["headers"] == expected for _, kw in http.calls)
    assert "probe-token-canary" not in talk_doctor.render_probe(result)
    assert "Probe: PASS" in talk_doctor.render_probe(result)


def test_grok_probe_reports_a_401_as_the_relogin_remediation():
    class WSServerHandshakeError(Exception):
        def __init__(self, status):
            super().__init__("401, message=Invalid response status probe-token-canary")
            self.status = status

    http = _ProbeHttp(http_status=200, ws=WSServerHandshakeError(401))

    result = _run(
        talk_doctor.run_grok_probe(
            auth_token="probe-token-canary",
            auth_source="xai-oauth",
            model="grok-voice-latest",
            aiohttp_module=_fake_aiohttp(http),
        )
    )

    assert result["ok"] is False
    assert result["http_status"] == 200
    assert result["ws_status"] == 401
    assert result["error"] == "xAI OAuth token rejected — run `hermes auth add xai-oauth`"
    rendered = talk_doctor.render_probe(result)
    assert "WS /v1/realtime -> 401" in rendered
    assert "Probe: FAIL" in rendered
    assert "probe-token-canary" not in json.dumps(result)


def test_grok_probe_reports_a_tier_denial_without_the_token():
    class WSServerHandshakeError(Exception):
        def __init__(self, status):
            super().__init__("forbidden")
            self.status = status

    http = _ProbeHttp(http_status=403, ws=WSServerHandshakeError(403))

    result = _run(
        talk_doctor.run_grok_probe(
            auth_token="probe-token-canary",
            auth_source="xai-oauth",
            model="grok-voice-latest",
            aiohttp_module=_fake_aiohttp(http),
        )
    )

    assert result["ok"] is False
    assert result["http_status"] == 403
    assert "does not include realtime API access" in result["error"]
    assert "probe-token-canary" not in json.dumps(result)


def test_grok_probe_flags_an_unexpected_first_event():
    http = _ProbeHttp(http_status=200, ws=_ProbeSocket("error"))

    result = _run(
        talk_doctor.run_grok_probe(
            auth_token="probe-token-canary",
            auth_source="env",
            model="grok-voice-latest",
            aiohttp_module=_fake_aiohttp(http),
        )
    )

    assert result["ok"] is False
    assert result["first_event"] == "error"
    assert result["error"] == "unexpected probe receipt (see http_status / first_event)"


def test_probe_refuses_non_grok_providers(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-never-render-this")

    result = talk_doctor._probe_grok()

    assert result == {"ok": False, "error": "the live probe exists for TALK_PROVIDER=grok only"}


def test_probe_reports_a_missing_lane_without_going_live(monkeypatch, _no_host):
    monkeypatch.setenv("TALK_PROVIDER", "grok")
    monkeypatch.setattr(talk_doctor, "run_grok_probe", None)

    result = talk_doctor._probe_grok()

    assert result["ok"] is False
    assert "hermes auth add xai-oauth" in result["error"]


def test_cli_entry_merges_the_probe_into_json_and_the_exit_code(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-never-render-this")
    monkeypatch.setattr(talk_doctor.talk_audio, "audio_available", lambda: True)
    monkeypatch.setattr(
        talk_doctor,
        "_probe_grok",
        lambda: {"ok": False, "error": "the live probe exists for TALK_PROVIDER=grok only"},
    )

    assert talk_doctor.cli_entry(json_output=True, probe=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert payload["probe"]["ok"] is False


def test_cli_entry_prints_the_probe_receipt_in_human_mode(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-never-render-this")
    monkeypatch.setattr(talk_doctor.talk_audio, "audio_available", lambda: True)
    monkeypatch.setattr(
        talk_doctor,
        "_probe_grok",
        lambda: {
            "auth_source": "xai-oauth",
            "model": "grok-voice-latest",
            "http_status": 200,
            "ws_status": 101,
            "first_event": "session.created",
            "error": None,
            "ok": True,
        },
    )

    assert talk_doctor.cli_entry(probe=True) == 0
    out = capsys.readouterr().out

    assert "Hermes Talk probe (live: two calls to api.x.ai)" in out
    assert "auth lane: xai-oauth, model: grok-voice-latest" in out
    assert "Probe: PASS: the resolved bearer reaches Grok realtime." in out


def test_cli_parser_passes_the_probe_flag(monkeypatch):
    parser = argparse.ArgumentParser()
    talk_cli.setup_cli(parser)
    seen = []
    monkeypatch.setattr(
        talk_cli.talk_doctor, "cli_entry", lambda **kwargs: seen.append(kwargs) or 0
    )

    assert talk_cli.cli_entry(parser.parse_args(["doctor", "--probe"])) == 0
    assert seen == [{"json_output": False, "probe": True}]
