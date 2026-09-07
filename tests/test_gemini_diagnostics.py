"""Gemini diagnostics must agree with the session lane, without inspecting other auth."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import talk_auth
import talk_check
import talk_cli
import talk_config
import talk_diagnostics
import talk_doctor
import talk_host
import talk_setup
import talk_tools

KEY = "AQ.offline-gemini-key-canary"


@pytest.fixture(autouse=True)
def isolated_gemini(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith("TALK_") or name in {
            "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY",
        }:
            monkeypatch.delenv(name)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("TALK_PROVIDER", "gemini")
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.setitem(sys.modules, "plugins", None)
    monkeypatch.setattr(talk_doctor.talk_audio, "audio_available", lambda: True)

    def forbidden(*args, **kwargs):
        raise AssertionError("Gemini diagnostics consulted foreign auth or the network")

    monkeypatch.setattr(talk_auth, "auth_diagnostic", forbidden)
    monkeypatch.setattr(talk_doctor.talk_grok_auth, "grok_auth_diagnostic", forbidden)
    monkeypatch.setattr(talk_auth.httpx, "post", forbidden)
    talk_host.bind_ctx(None)
    talk_tools.REGISTRATION_RECEIPTS.clear()
    talk_tools.REGISTRATION_FAILURES.clear()
    yield
    talk_host.bind_ctx(None)


def checks(report):
    return {row["id"]: row for row in report["checks"]}


def test_gemini_diagnostics_do_not_write_or_open_a_provider_session(monkeypatch, tmp_path):
    import aiohttp

    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    before = dict(os.environ)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline diagnostics tried to write or connect")

    with monkeypatch.context() as guard:
        guard.setattr(aiohttp, "ClientSession", forbidden)
        guard.setattr(Path, "mkdir", forbidden)
        guard.setattr(Path, "write_text", forbidden)
        guard.setattr(Path, "write_bytes", forbidden)
        report = talk_doctor.collect_report()
        talk_doctor.render_human(report)
        talk_diagnostics.collect_bundle(doctor_report=report)
    assert os.environ == before
    assert not (tmp_path / "hermes").exists()


@pytest.mark.parametrize(
    ("env", "status", "source"),
    [
        ({"GEMINI_API_KEY": KEY}, "pass", "GEMINI_API_KEY"),
        ({"TALK_GEMINI_API_KEY": KEY}, "pass", "TALK_GEMINI_API_KEY"),
        ({"TALK_GEMINI_API_KEY": KEY, "GEMINI_API_KEY": " "}, "pass", "TALK_GEMINI_API_KEY"),
        ({}, "fail", None),
        ({"GEMINI_API_KEY": " "}, "fail", "GEMINI_API_KEY"),
        ({"TALK_GEMINI_API_KEY": " ", "GEMINI_API_KEY": KEY}, "fail", "TALK_GEMINI_API_KEY"),
    ],
)
def test_full_report_uses_the_gemini_key_precedence(monkeypatch, env, status, source):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    report = talk_doctor.collect_report()
    auth = checks(report)["auth"]
    assert auth["status"] == status
    assert auth["details"]["source"] == source
    assert auth["details"]["provider"] == "gemini"
    assert auth["details"]["validation_scope"] == "presence-only"
    assert report["ok"] is (status == "pass")
    rendered = talk_doctor.render_human(report)
    assert KEY not in rendered + json.dumps(report)
    assert "codex login" not in rendered
    assert "OpenAI" not in rendered


def test_gemini_report_ignores_unrelated_openai_configuration(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("TALK_OPENAI_API_KEY", " ")
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "invalid")
    monkeypatch.setenv("TALK_MODEL", "unrelated-invalid-model")
    monkeypatch.setenv("TALK_VOICE", "unrelated-invalid-voice")
    monkeypatch.setenv("TALK_GEMINI_VOICE", "Kore")
    report = talk_doctor.collect_report()
    rows = checks(report)
    assert report["ok"]
    assert rows["model"]["details"]["model"] == talk_config.DEFAULT_GEMINI_MODEL
    assert rows["voice"]["details"]["voice"] == "Kore"
    assert rows["voice"]["details"]["source"] == "TALK_GEMINI_VOICE"
    assert rows["auth"]["details"]["winning_lane"] == talk_cli.resolve_provider_lane().auth.source


def test_custom_gemini_model_is_reported_without_claiming_live_compatibility(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("TALK_GEMINI_MODEL", "gemini-custom-live-preview")
    report = talk_doctor.collect_report()
    model = checks(report)["model"]
    assert report["ok"]
    assert model["status"] == "warn"
    assert model["details"]["model"] == "gemini-custom-live-preview"
    assert model["details"]["source"] == "TALK_GEMINI_MODEL"
    assert model["details"]["compatibility"] == "unknown"
    assert model["details"]["validation_scope"] == "configuration-only"


def test_gemini_voice_validation_preserves_case(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("TALK_GEMINI_VOICE", "puck")
    report = talk_doctor.collect_report()
    voice = checks(report)["voice"]
    assert not report["ok"]
    assert voice["status"] == "fail"
    assert voice["details"]["voice"] == "puck"
    assert "TALK_GEMINI_VOICE" in " ".join(voice["remediation"])


def test_renderer_prefers_gemini_identity_even_with_legacy_receipt_fields(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    report = talk_doctor.collect_report()
    checks(report)["auth"]["details"].update(
        codex_oauth="missing", xai_oauth="missing", preference="enabled"
    )
    rendered = talk_doctor.render_human(report)
    assert "provider=gemini" in rendered
    assert "codex=" not in rendered and "xai-oauth=" not in rendered


def test_bundle_keeps_gemini_receipt_facts_without_key_values(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    report = talk_doctor.collect_report()
    checks(report)["auth"]["details"]["api_key"] = KEY
    bundle = talk_diagnostics.collect_bundle(doctor_report=report)
    rows = checks(bundle["doctor"])
    assert rows["auth"]["details"]["provider"] == "gemini"
    assert rows["auth"]["details"]["source"] == "GEMINI_API_KEY"
    assert rows["auth"]["details"]["validation_scope"] == "presence-only"
    assert rows["model"]["details"]["provider"] == "gemini"
    assert rows["voice"]["details"]["provider"] == "gemini"
    assert "GEMINI_API_KEY" in bundle["environment"]["names"]
    assert KEY not in json.dumps(bundle)


@pytest.mark.parametrize("has_key", [False, True])
def test_real_doctor_controls_the_check_gate_without_a_live_call(monkeypatch, has_key):
    if has_key:
        monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setattr(talk_check, "_live_enabled", lambda: True)
    calls = []

    def stop_at_transport(auth):
        calls.append(auth.source)
        raise RuntimeError("offline sentinel: provider factory reached")

    report = talk_check.run_check(
        no_run=True,
        lane_resolver=talk_cli.resolve_provider_lane,
        session_factory=stop_at_transport,
    )
    rows = {row["id"]: row for row in report["steps"]}
    assert rows["static"]["status"] == ("pass" if has_key else "fail")
    assert calls == (["env"] if has_key else [])
    assert rows["provider_session"]["status"] == ("fail" if has_key else "skip")
    assert not report["ok"]  # A fake transport must never become a live receipt.


@pytest.mark.parametrize("confirm", ["yes", "no"])
def test_setup_repairs_only_gemini_with_per_write_confirmation(monkeypatch, tmp_path, confirm):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-placeholder")
    monkeypatch.setenv("TALK_MODEL", "unrelated-model")
    prompts, output = [], []

    def answer(prompt):
        prompts.append(prompt)
        assert prompt.startswith("Write TALK_GEMINI_API_KEY=")
        return confirm

    def secret(prompt):
        prompts.append(prompt)
        assert "TALK_GEMINI_API_KEY" in prompt
        return KEY

    result = talk_setup.cli_entry(input_fn=answer, secret_input_fn=secret, output_fn=output.append)
    env_path = tmp_path / "hermes" / ".env"
    assert result == (0 if confirm == "yes" else 1)
    assert env_path.exists() is (confirm == "yes")
    if env_path.exists():
        assert env_path.read_text(encoding="utf-8").strip() == f"TALK_GEMINI_API_KEY={KEY}"
    assert os.environ["OPENAI_API_KEY"] == "unrelated-openai-placeholder"
    assert os.environ["TALK_MODEL"] == "unrelated-model"
    assert KEY not in "\n".join(prompts + output)
    assert "codex login" not in "\n".join(prompts + output)


@pytest.mark.parametrize(
    ("key_name", "action"),
    [("TALK_GEMINI_API_KEY", "remove"), ("GEMINI_API_KEY", "replace")],
)
def test_setup_targets_the_blank_gemini_key(monkeypatch, key_name, action):
    monkeypatch.setenv(key_name, " ")
    if key_name == "TALK_GEMINI_API_KEY":
        monkeypatch.setenv("GEMINI_API_KEY", KEY)
    auth = checks(talk_doctor.collect_report())["auth"]
    proposed = talk_setup._auth_changes(
        auth, input_fn=lambda _: action, secret_input_fn=lambda _: KEY, output_fn=lambda _: None
    )
    assert proposed == [(key_name, None if action == "remove" else KEY, True)]


def test_setup_model_and_voice_repairs_use_gemini_settings(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setenv("TALK_GEMINI_MODEL", "gemini-custom-live-preview")
    monkeypatch.setenv("TALK_GEMINI_VOICE", "puck")
    rows = checks(talk_doctor.collect_report())
    assert talk_setup._model_changes(rows["model"], lambda _: "default") == [
        ("TALK_GEMINI_MODEL", talk_config.DEFAULT_GEMINI_MODEL, False)
    ]
    assert talk_setup._model_changes(rows["model"], lambda _: "keep") == []
    voices = iter(["puck", "Puck"])
    assert talk_setup._voice_changes(rows["voice"], lambda _: next(voices)) == [
        ("TALK_GEMINI_VOICE", "Puck", False)
    ]
