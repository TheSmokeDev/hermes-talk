"""Native ``hermes talk diagnostics`` — the redacted, default-deny support bundle."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest

import talk_audio
import talk_cli
import talk_diagnostics
import talk_doctor
import talk_host
import talk_setup
import talk_tools

SECRET = "sk-abcdefghijklmnop"
_REAL_DOCTOR = talk_doctor.collect_report
WIN_PATH = "C:\\Users\\someone\\.hermes\\config.yaml"
POSIX_PATH = "/home/someone/.hermes/config.yaml"


def _doctor_report(**overrides):
    """A doctor report with every real check id, plus planted poison."""

    checks = []
    for check_id in talk_doctor.CHECK_ORDER:
        checks.append(
            {
                "id": check_id,
                "status": "pass",
                "summary": f"{check_id} looks fine",
                "details": {},
                "remediation": [],
            }
        )
    by_id = {check["id"]: check for check in checks}
    by_id["identity"]["details"] = {
        "resolved_home": WIN_PATH,
        "root": POSIX_PATH,
        "identity_home": WIN_PATH,
        "home_source": "HERMES_HOME",
        "root_source": "HERMES_HOME",
        "active_profile": "root",
        "profile_source": "root",
        "inspection": "existing-files-only",
        "section_count": 2,
        "sections": {"PERSONA": {"characters": 663}, "USER": {"characters": 1337}},
    }
    by_id["auth"]["details"] = {
        "configured": True,
        "winning_lane": "codex-oauth",
        "preference": "absent",
        "codex_oauth": "valid",
        "metered_key_present": False,
        "metered_key_wins_over_codex": False,
        "metered_keys_ignored": False,
        "refresh_required": False,
        "blocked_by": None,
        "access_token": SECRET,  # never a real doctor key; planted
    }
    by_id["model"]["details"] = {
        "model": SECRET,  # a token leaf that redaction would change: dropped
        "source": "TALK_MODEL",
        "compatibility": "compatible",
        "policy_version": "2026-08",
        "validation_scope": "explicit-capability-policy",
    }
    by_id["host"]["details"] = {
        "context_bound": True,
        "capabilities": {"dispatch_tool": True, "register_hook": False, "weird": "yes"},
    }
    by_id["cascade"]["details"] = {
        "voice_mode": "cascade",
        "source": "TALK_VOICE_MODE",
        "keys": {"scoped": "present", "shared": "absent"},
        "voice_id": "voiceid1234567890abcdef",
        "model": "eleven_flash_v2_5",
        "tts": "elevenlabs",
        "provider": "openai",
    }
    by_id["provider"]["summary"] = f"provider is configured with {SECRET} at {WIN_PATH}"
    by_id["provider"]["remediation"] = [f"see {POSIX_PATH} and {SECRET}"]
    report = {
        "schema_version": 1,
        "command": "hermes talk doctor",
        "read_only": True,
        "ok": True,
        "summary": {"pass": 10, "warn": 0, "fail": 0},
        "checks": checks,
        "planted_top_level": SECRET,
    }
    report.update(overrides)
    return report


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    for name in list(os.environ):
        if name.upper().startswith(("TALK_", "HERMES_")) and name != "HERMES_HOME":
            monkeypatch.delenv(name, raising=False)
    for name in talk_diagnostics.SHARED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(talk_doctor, "collect_report", _doctor_report)
    monkeypatch.setattr(talk_audio, "audio_available", lambda: False)
    talk_host.bind_ctx(None)
    talk_tools.REGISTRATION_FAILURES.clear()
    talk_tools.REGISTRATION_RECEIPTS.clear()
    yield
    talk_host.bind_ctx(None)


def _check(bundle, check_id):
    return next(check for check in bundle["doctor"]["checks"] if check["id"] == check_id)


# -- shape ----------------------------------------------------------------------------


def test_bundle_shape_is_stable_and_machine_readable():
    bundle = talk_diagnostics.collect_bundle()

    assert set(bundle) == {
        "schema_version",
        "command",
        "generated_at",
        "versions",
        "config",
        "environment",
        "devices",
        "host",
        "doctor",
    }
    assert bundle["schema_version"] == 1
    assert bundle["command"] == "hermes talk diagnostics"
    assert bundle["generated_at"].endswith("Z")
    assert {"python", "python_implementation", "hermes_talk", "os", "os_release", "machine"} <= set(
        bundle["versions"]
    )
    assert bundle["config"] == {"names": []}
    assert bundle["environment"] == {"names": ["HERMES_HOME"]}
    assert bundle["devices"] == {"audio_dependency_available": False}
    assert bundle["host"] == {
        "context_bound": True,
        "capabilities": {"dispatch_tool": True, "register_hook": False},
    }
    assert [check["id"] for check in bundle["doctor"]["checks"]] == list(talk_doctor.CHECK_ORDER)
    assert all(
        set(check) == {"id", "status", "summary", "details", "remediation"}
        for check in bundle["doctor"]["checks"]
    )
    assert bundle["doctor"]["summary"] == {"pass": 10, "warn": 0, "fail": 0}
    json.dumps(bundle)


# -- default-deny --------------------------------------------------------------------


def test_planted_secret_shaped_values_never_reach_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("TALK_OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("TALK_PLANTED", f"value {SECRET} {WIN_PATH}")
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)

    bundle = talk_diagnostics.collect_bundle()
    path = talk_diagnostics.write_bundle(bundle, tmp_path / "bundle.json")
    text = path.read_text(encoding="utf-8")

    assert SECRET not in text
    assert "someone" not in text
    assert WIN_PATH not in text and POSIX_PATH not in text
    assert "planted_top_level" not in text
    assert "access_token" not in text
    assert "voice_id" not in text
    assert "resolved_home" not in text and "identity_home" not in text
    # Names are the whole point; values are never read.
    assert bundle["config"]["names"] == ["TALK_OPENAI_API_KEY", "TALK_PLANTED"]
    assert bundle["environment"]["names"] == ["HERMES_HOME", "OPENAI_API_KEY"]
    # A text leaf keeps its sentence with the secret and the paths scrubbed.
    provider = _check(bundle, "provider")
    assert provider["summary"] == "provider is configured with <redacted-secret> at <path>"
    assert provider["remediation"] == ["see <path> and <redacted-secret>"]
    # A token leaf that redaction would change is DROPPED, never carried.
    assert "model" not in _check(bundle, "model")["details"]
    assert _check(bundle, "model")["details"]["source"] == "TALK_MODEL"


def test_allowlist_drops_unknown_keys_at_every_level():
    raw = {
        "schema_version": 1,
        "command": "hermes talk diagnostics",
        "extra": "dropped",
        "versions": {"python": "3.12.1", "shell": "dropped"},
        "doctor": {
            "ok": True,
            "read_only": True,  # a real doctor key that the bundle does not carry
            "summary": {"pass": 1, "warn": 0, "fail": 0, "note": "dropped"},
            "checks": [
                {
                    "id": "audio",
                    "status": "warn",
                    "summary": "s",
                    "details": {"dependency_available": False, "device_path": WIN_PATH},
                    "remediation": ["r"],
                },
                {"id": "not-a-check", "status": "pass", "summary": "s", "details": {}},
                {"id": "voice", "status": "unknown", "summary": "s", "details": {}},
                "garbage",
            ],
        },
    }

    out = talk_diagnostics.serialize(raw, talk_diagnostics.BUNDLE_ALLOWLIST)

    assert out == {
        "schema_version": 1,
        "command": "hermes talk diagnostics",
        "versions": {"python": "3.12.1"},
        "doctor": {
            "ok": True,
            "summary": {"pass": 1, "warn": 0, "fail": 0},
            "checks": [
                {
                    "id": "audio",
                    "status": "warn",
                    "summary": "s",
                    "details": {"dependency_available": False},
                    "remediation": ["r"],
                }
            ],
        },
    }


@pytest.mark.parametrize(
    ("tag", "value"),
    [
        ("bool", 1),
        ("bool", "true"),
        ("int", True),
        ("int", "3"),
        ("token", "a sentence with spaces"),
        ("token", WIN_PATH),
        ("token", POSIX_PATH),
        ("token", "x" * 81),
        ("token", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop"),
        ("token", 12),
        ("label", "-leading dash"),
        ("label", "x" * 121),
        ("text", 12),
        ("token_list", "not-a-list"),
        ("token_map", ["not", "a", "map"]),
        ("bool_map", "no"),
        ("int_map", 3),
        ("count_map", None),
    ],
)
def test_leaves_that_do_not_fit_their_tag_are_dropped(tag, value):
    assert talk_diagnostics.serialize({"k": value}, {"k": tag}) == {}


def test_map_leaves_keep_only_well_shaped_entries():
    raw = {
        "m": {"good": "present", "bad value": SECRET, "path": WIN_PATH, "not a key!": "x"},
        "b": {"dispatch_tool": True, "register_hook": "yes"},
        "c": {"PERSONA": {"characters": 12}, "USER": {"characters": "12"}, "X": 5},
        "i": {"pass": 1, "warn": True},
    }

    out = talk_diagnostics.serialize(
        raw, {"m": "token_map", "b": "bool_map", "c": "count_map", "i": "int_map"}
    )

    assert out == {
        "m": {"good": "present"},
        "b": {"dispatch_tool": True},
        "c": {"PERSONA": {"characters": 12}},
        "i": {"pass": 1},
    }


def test_text_leaves_are_bounded():
    long = "word " * 200
    out = talk_diagnostics.serialize({"t": long}, {"t": "text"})
    assert len(out["t"]) == 300 and out["t"].endswith("...")


def test_unknown_allowlist_tag_is_a_programming_error():
    with pytest.raises(ValueError):
        talk_diagnostics.serialize({"k": 1}, {"k": "mystery"})


# -- collectors ---------------------------------------------------------------------------


def test_audio_facts_read_the_device_table_without_opening_a_device(monkeypatch):
    calls = []

    class _Default:
        device = (1, 0)

    fake = types.SimpleNamespace(
        query_devices=lambda: calls.append("query")
        or [
            {"name": "Speakers (Realtek)", "max_input_channels": 0, "max_output_channels": 2},
            {"name": "Microphone Array", "max_input_channels": 2, "max_output_channels": 0},
        ],
        default=_Default(),
        InputStream=lambda *a, **k: calls.append("stream"),
    )
    monkeypatch.setattr(talk_audio, "audio_available", lambda: True)
    monkeypatch.setattr(talk_audio, "import_sounddevice", lambda: fake)

    bundle = talk_diagnostics.collect_bundle()

    assert calls == ["query"]
    assert bundle["devices"] == {
        "audio_dependency_available": True,
        "input_device_count": 1,
        "output_device_count": 1,
        "default_input": "Microphone Array",
        "default_output": "Speakers (Realtek)",
    }


def test_a_device_table_that_will_not_read_is_a_fact_not_a_crash(monkeypatch):
    def explode():
        raise RuntimeError("PortAudio not initialized")

    monkeypatch.setattr(talk_audio, "audio_available", lambda: True)
    monkeypatch.setattr(talk_audio, "import_sounddevice", explode)

    assert talk_diagnostics.collect_bundle()["devices"] == {"audio_dependency_available": True}


def test_host_version_is_absent_not_invented_off_host(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    monkeypatch.setattr(
        talk_diagnostics, "_host_version", lambda: None
    )

    assert "hermes_agent" not in talk_diagnostics.collect_bundle()["versions"]


# -- owner-only write ---------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode discriminator")
def test_bundle_is_written_0600_on_posix(tmp_path):
    path = talk_diagnostics.write_bundle({"schema_version": 1}, tmp_path / "deep" / "b.json")

    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IRUSR | stat.S_IWUSR
    assert stat.S_IMODE(path.parent.stat().st_mode) == stat.S_IRWXU
    assert json.loads(path.read_text(encoding="utf-8")) == {"schema_version": 1}
    assert [p.name for p in path.parent.iterdir()] == ["b.json"]


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL discriminator")
def test_bundle_carries_an_owner_only_dacl_on_windows(tmp_path):
    path = talk_diagnostics.write_bundle({"schema_version": 1}, tmp_path / "deep" / "b.json")

    sid = talk_setup._windows_current_user_sid()
    assert talk_setup._windows_dacl_grants_only_full_control(
        talk_setup._windows_dacl_sddl(path), sid
    )
    assert json.loads(path.read_text(encoding="utf-8")) == {"schema_version": 1}
    assert [p.name for p in path.parent.iterdir()] == ["b.json"]


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL discriminator")
def test_windows_write_hardens_the_empty_temp_before_any_bytes_land(monkeypatch, tmp_path):
    real = talk_setup._windows_restrict_owner_only_dacl
    seen = []

    def inspect(path):
        seen.append(path.stat().st_size)
        real(path)

    monkeypatch.setattr(talk_setup, "_windows_restrict_owner_only_dacl", inspect)

    talk_diagnostics.write_bundle({"schema_version": 1}, tmp_path / "b.json")

    assert seen == [0]


def test_a_failed_verification_removes_the_bundle(monkeypatch, tmp_path):
    def refuse(_path):
        raise talk_diagnostics.BundleWriteError("bundle mode verification failed")

    monkeypatch.setattr(talk_diagnostics, "_verify_owner_only", refuse)

    with pytest.raises(talk_diagnostics.BundleWriteError):
        talk_diagnostics.write_bundle({"schema_version": 1}, tmp_path / "b.json")

    assert list(tmp_path.iterdir()) == []


def test_a_failed_move_cleans_the_temp_and_names_only_the_error_class(monkeypatch, tmp_path):
    def refuse(_src, _dst):
        raise PermissionError(f"denied: {WIN_PATH}")

    monkeypatch.setattr(talk_diagnostics.os, "replace", refuse)

    with pytest.raises(talk_diagnostics.BundleWriteError) as excinfo:
        talk_diagnostics.write_bundle({"schema_version": 1}, tmp_path / "b.json")

    assert str(excinfo.value) == "could not write the bundle: PermissionError"
    assert list(tmp_path.iterdir()) == []


def test_default_bundle_path_lives_in_the_talk_state_dir(tmp_path):
    path = talk_diagnostics.default_bundle_path()

    assert path.parent == tmp_path / "hermes" / "state"
    assert path.name.startswith("hermes-talk-diagnostics-") and path.suffix == ".json"


# -- the CLI ----------------------------------------------------------------------------------


def test_cli_parser_and_dispatch_make_diagnostics_a_native_subcommand(monkeypatch):
    parser = argparse.ArgumentParser()
    talk_cli.setup_cli(parser)
    seen = []
    monkeypatch.setattr(
        talk_cli.talk_diagnostics, "cli_entry", lambda **kwargs: seen.append(kwargs) or 0
    )

    assert talk_cli.cli_entry(parser.parse_args(["diagnostics"])) == 0
    assert talk_cli.cli_entry(parser.parse_args(["diagnostics", "--json"])) == 0
    assert talk_cli.cli_entry(parser.parse_args(["diagnostics", "--bundle"])) == 0
    assert talk_cli.cli_entry(parser.parse_args(["diagnostics", "--bundle", "out.json"])) == 0
    assert seen == [
        {"json_output": False, "write": False, "bundle_path": None},
        {"json_output": True, "write": False, "bundle_path": None},
        {"json_output": False, "write": True, "bundle_path": None},
        {"json_output": False, "write": True, "bundle_path": "out.json"},
    ]


def test_cli_dispatch_raises_system_exit_on_a_failed_write(monkeypatch):
    parser = argparse.ArgumentParser()
    talk_cli.setup_cli(parser)
    monkeypatch.setattr(talk_cli.talk_diagnostics, "cli_entry", lambda **kwargs: 1)

    with pytest.raises(SystemExit) as excinfo:
        talk_cli.cli_entry(parser.parse_args(["diagnostics", "--bundle"]))

    assert excinfo.value.code == 1


def test_cli_bundle_writes_owner_only_and_prints_the_path_and_the_paste_hint(capsys, tmp_path):
    code = talk_diagnostics.cli_entry(write=True)
    out = capsys.readouterr().out

    assert code == 0
    written = next((tmp_path / "hermes" / "state").glob("hermes-talk-diagnostics-*.json"))
    assert str(written) in out
    assert talk_diagnostics.PASTE_HINT in out
    assert json.loads(written.read_text(encoding="utf-8"))["command"] == "hermes talk diagnostics"


def test_cli_bundle_write_failure_is_a_one_line_receipt(monkeypatch, capsys, tmp_path):
    def refuse(_bundle, _path=None):
        raise talk_diagnostics.BundleWriteError("could not write the bundle: PermissionError")

    monkeypatch.setattr(talk_diagnostics, "write_bundle", refuse)

    code = talk_diagnostics.cli_entry(write=True, bundle_path=str(tmp_path / "b.json"))
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert captured.err.strip() == (
        "diagnostics: BundleWriteError: could not write the bundle: PermissionError"
    )


def test_cli_json_prints_the_bundle_and_human_prints_the_summary(monkeypatch, capsys):
    monkeypatch.setenv("TALK_OPENAI_API_KEY", SECRET)

    assert talk_diagnostics.cli_entry(json_output=True) == 0
    payload = capsys.readouterr().out
    assert json.loads(payload)["config"]["names"] == ["TALK_OPENAI_API_KEY"]
    assert SECRET not in payload

    assert talk_diagnostics.cli_entry() == 0
    human = capsys.readouterr().out
    assert human.startswith("Hermes Talk diagnostics (redacted: names and outcomes only)")
    assert "config names: TALK_OPENAI_API_KEY" in human
    assert "[PASS] auth: auth looks fine" in human
    assert "Doctor: 10 pass, 0 warn, 0 fail." in human
    assert "--bundle" in human
    assert SECRET not in human


def test_native_cli_json_path_emits_the_bundle_without_the_secret(monkeypatch, capsys):
    monkeypatch.setenv("TALK_OPENAI_API_KEY", SECRET)
    parser = argparse.ArgumentParser()
    talk_cli.setup_cli(parser)

    assert talk_cli.cli_entry(parser.parse_args(["diagnostics", "--json"])) == 0
    payload = capsys.readouterr().out

    assert json.loads(payload)["command"] == "hermes talk diagnostics"
    assert SECRET not in payload


def test_real_doctor_output_carries_provenance_labels_but_never_the_home_path(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(talk_doctor, "collect_report", _REAL_DOCTOR)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)

    bundle = talk_diagnostics.collect_bundle()
    identity = _check(bundle, "identity")["details"]
    text = json.dumps(bundle)

    assert identity["home_source"] == "HERMES_HOME"
    assert "resolved_home" not in identity and "root" not in identity
    assert str(tmp_path) not in text
    assert SECRET not in text
    assert _check(bundle, "auth")["details"]["winning_lane"] == "env"


def test_issue_template_asks_for_the_bundle():
    root = Path(__file__).resolve().parent.parent
    template = (root / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(encoding="utf-8")

    assert "hermes talk diagnostics --bundle" in template
    assert "id: diagnostics_bundle" in template
