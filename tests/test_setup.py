"""Native ``hermes talk setup`` detect/confirm/apply/verify workflow."""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

import pytest

import talk_cli
import talk_setup


def _windows_owner_sid(path: Path) -> str:
    if os.name != "nt":  # pragma: no cover - guarded by Windows-only tests
        raise AssertionError("Windows security APIs are unavailable")

    from ctypes import wintypes

    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = wintypes.DWORD
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    convert_sid.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    error = get_security(
        str(path), 1, 0x00000001, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor)
    )
    if error:
        raise ctypes.WinError(error)
    sid_text = wintypes.LPWSTR()
    try:
        if not convert_sid(owner, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        return sid_text.value
    finally:
        if sid_text:
            local_free(sid_text)
        if descriptor:
            local_free(descriptor)


def _windows_dacl_sddl(path: Path) -> str:
    if os.name != "nt":  # pragma: no cover - guarded by Windows-only tests
        raise AssertionError("Windows security APIs are unavailable")

    from ctypes import wintypes

    descriptor = ctypes.c_void_p()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = wintypes.DWORD
    to_sddl = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    to_sddl.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG),
    ]
    to_sddl.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    error = get_security(
        str(path), 1, 0x00000004, None, None, None, None, ctypes.byref(descriptor)
    )
    if error:
        raise ctypes.WinError(error)
    sddl = wintypes.LPWSTR()
    try:
        if not to_sddl(descriptor, 1, 0x00000004, ctypes.byref(sddl), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return sddl.value
    finally:
        if sddl:
            local_free(sddl)
        if descriptor:
            local_free(descriptor)


def _windows_set_dacl(path: Path, sddl: str) -> None:
    if os.name != "nt":  # pragma: no cover - guarded by Windows-only tests
        raise AssertionError("Windows security APIs are unavailable")

    from ctypes import wintypes

    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    dacl_present = wintypes.BOOL()
    dacl_defaulted = wintypes.BOOL()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    from_sddl = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    from_sddl.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    from_sddl.restype = wintypes.BOOL
    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    get_dacl.restype = wintypes.BOOL
    set_security = advapi32.SetNamedSecurityInfoW
    set_security.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    set_security.restype = wintypes.DWORD
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    if not from_sddl(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not get_dacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        assert dacl_present.value
        error = set_security(
            str(path), 1, 0x00000004 | 0x80000000, None, None, dacl, None
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        if descriptor:
            local_free(descriptor)


def _assert_windows_owner_only_dacl(path: Path) -> None:
    owner_sid = talk_setup._windows_current_user_sid()
    dacl = _windows_dacl_sddl(path)
    assert dacl.startswith("D:P")
    assert dacl.count("(A;") == 1
    assert ";;;WD)" not in dacl
    assert f";;;{owner_sid})" in dacl


@pytest.fixture(autouse=True)
def _clean_setup_environment(monkeypatch):
    for name in (
        "TALK_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "TALK_PREFER_CODEX_OAUTH",
        "TALK_MODEL",
        "TALK_VOICE",
    ):
        monkeypatch.delenv(name, raising=False)


def _report(*, auth: dict, model_status: str = "pass", voice_status: str = "pass") -> dict:
    checks = [
        {
            "id": "auth",
            "status": auth.pop("status", "pass"),
            "summary": "auth",
            "details": auth,
            "remediation": [],
        },
        {
            "id": "model",
            "status": model_status,
            "summary": "model",
            "details": {
                "model": "broken-model" if model_status == "fail" else "gpt-realtime-2.1",
                "compatibility": "incompatible" if model_status == "fail" else "compatible",
            },
            "remediation": [],
        },
        {
            "id": "voice",
            "status": voice_status,
            "summary": "voice",
            "details": {"voice": "cedar", "valid": voice_status == "pass"},
            "remediation": [],
        },
    ]
    failures = sum(check["status"] == "fail" for check in checks)
    return {
        "schema_version": 1,
        "command": "hermes talk doctor",
        "read_only": True,
        "ok": failures == 0,
        "summary": {"pass": len(checks) - failures, "warn": 0, "fail": failures},
        "checks": checks,
    }


def _healthy_report() -> dict:
    return _report(
        auth={
            "winning_lane": "openai-api-key",
            "blocked_by": None,
            "preference": "absent",
            "codex_oauth": "missing",
            "metered_key_present": True,
            "metered_key_wins_over_codex": False,
        }
    )


def _preferred_codex_failure(*, metered_key_present: bool) -> dict:
    return _report(
        auth={
            "status": "fail",
            "winning_lane": None,
            "blocked_by": "codex-oauth-unusable",
            "preference": "enabled",
            "codex_oauth": "missing",
            "metered_key_present": metered_key_present,
            "metered_key_wins_over_codex": False,
        }
    )


def test_native_cli_parser_and_dispatch_expose_setup(monkeypatch):
    parser = argparse.ArgumentParser()
    talk_cli.setup_cli(parser)
    args = parser.parse_args(["setup"])

    called = []
    monkeypatch.setattr(talk_cli.talk_setup, "cli_entry", lambda: called.append(True) or 0)

    assert talk_cli.cli_entry(args) == 0
    assert called == [True]


def test_setup_asks_only_for_missing_model_confirms_write_and_reruns_doctor(
    monkeypatch, tmp_path, capsys
):
    reports = iter(
        [
            _report(
                auth={
                    "winning_lane": "openai-api-key",
                    "blocked_by": None,
                    "preference": "absent",
                    "codex_oauth": "missing",
                    "metered_key_present": True,
                    "metered_key_wins_over_codex": False,
                },
                model_status="fail",
            ),
            _healthy_report(),
        ]
    )
    doctor_calls = []
    monkeypatch.setattr(
        talk_setup.talk_doctor,
        "collect_report",
        lambda: doctor_calls.append(1) or next(reports),
    )
    monkeypatch.setattr(talk_setup.talk_doctor, "render_human", lambda _report: "doctor receipt")
    monkeypatch.setattr(talk_setup.talk_config, "get_hermes_home", lambda: tmp_path)

    answers = iter(["gpt-realtime-2.1", "yes"])
    prompts = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    assert talk_setup.cli_entry(input_fn=answer) == 0
    assert len(prompts) == 2
    assert "model" in prompts[0].lower()
    assert "write" in prompts[1].lower()
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        "TALK_MODEL=gpt-realtime-2.1\n"
    )
    assert len(doctor_calls) == 2
    assert "Verification: PASS" in capsys.readouterr().out


def test_setup_with_no_missing_decisions_asks_nothing_and_writes_nothing(
    monkeypatch, tmp_path
):
    doctor_calls = []
    monkeypatch.setattr(
        talk_setup.talk_doctor,
        "collect_report",
        lambda: doctor_calls.append(1) or _healthy_report(),
    )
    monkeypatch.setattr(talk_setup.talk_doctor, "render_human", lambda _report: "doctor receipt")
    monkeypatch.setattr(talk_setup.talk_config, "get_hermes_home", lambda: tmp_path)

    def unexpected(_prompt: str) -> str:
        raise AssertionError("healthy setup asked a decision")

    assert talk_setup.cli_entry(input_fn=unexpected) == 0
    assert doctor_calls == [1, 1]
    assert not (tmp_path / ".env").exists()


def test_setup_key_entry_is_secret_safe_and_requires_confirmation(monkeypatch, tmp_path, capsys):
    missing = _report(
        auth={
            "status": "fail",
            "winning_lane": None,
            "blocked_by": "no-usable-auth",
            "preference": "absent",
            "codex_oauth": "missing",
            "metered_key_present": False,
            "metered_key_wins_over_codex": False,
        }
    )
    reports = iter([missing, _healthy_report()])
    monkeypatch.setattr(talk_setup.talk_doctor, "collect_report", lambda: next(reports))
    monkeypatch.setattr(talk_setup.talk_doctor, "render_human", lambda _report: "doctor receipt")
    monkeypatch.setattr(talk_setup.talk_config, "get_hermes_home", lambda: tmp_path)

    secret = "sk-proj-setup-secret"
    prompts = []
    answers = iter(["key", "yes"])

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    assert talk_setup.cli_entry(input_fn=answer, secret_input_fn=lambda _prompt: secret) == 0
    output = capsys.readouterr().out
    assert secret not in output
    assert secret not in " ".join(prompts)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        f"TALK_OPENAI_API_KEY={secret}\n"
    )


def test_setup_key_choice_reuses_existing_metered_key_and_confirms_policy_transition(
    monkeypatch, tmp_path, capsys
):
    existing_secret = "sk-existing-metered-secret"
    monkeypatch.setenv("OPENAI_API_KEY", existing_secret)
    reports = [_preferred_codex_failure(metered_key_present=True)]
    doctor_calls = []

    def collect_report():
        doctor_calls.append(1)
        if reports:
            return reports.pop()
        assert os.environ["OPENAI_API_KEY"] == existing_secret
        assert os.environ["TALK_PREFER_CODEX_OAUTH"] == "false"
        return _healthy_report()

    monkeypatch.setattr(talk_setup.talk_doctor, "collect_report", collect_report)
    monkeypatch.setattr(talk_setup.talk_doctor, "render_human", lambda _report: "doctor receipt")
    monkeypatch.setattr(talk_setup.talk_config, "get_hermes_home", lambda: tmp_path)

    prompts = []
    answers = iter(["key", "yes"])

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    def unexpected_secret(_prompt: str) -> str:
        raise AssertionError("setup asked for a replacement despite an existing metered key")

    assert talk_setup.cli_entry(input_fn=answer, secret_input_fn=unexpected_secret) == 0
    output = capsys.readouterr().out

    assert doctor_calls == [1, 1]
    assert len(prompts) == 2
    assert "TALK_PREFER_CODEX_OAUTH" in prompts[1]
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        "TALK_PREFER_CODEX_OAUTH=false\n"
    )
    assert existing_secret not in output
    assert "state=applied" in output
    assert "Verification: PASS" in output


def test_setup_key_choice_confirms_new_secret_and_policy_as_separate_settings(
    monkeypatch, tmp_path, capsys
):
    secret = "sk-new-metered-secret"
    reports = [_preferred_codex_failure(metered_key_present=False)]

    def collect_report():
        if reports:
            return reports.pop()
        assert os.environ["TALK_OPENAI_API_KEY"] == secret
        assert os.environ["TALK_PREFER_CODEX_OAUTH"] == "false"
        return _healthy_report()

    monkeypatch.setattr(talk_setup.talk_doctor, "collect_report", collect_report)
    monkeypatch.setattr(talk_setup.talk_doctor, "render_human", lambda _report: "doctor receipt")
    monkeypatch.setattr(talk_setup.talk_config, "get_hermes_home", lambda: tmp_path)

    prompts = []
    answers = iter(["key", "yes", "yes"])

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    assert talk_setup.cli_entry(input_fn=answer, secret_input_fn=lambda _prompt: secret) == 0
    output = capsys.readouterr().out

    confirmations = [prompt for prompt in prompts if prompt.startswith("Write ")]
    assert len(confirmations) == 2
    assert any("TALK_OPENAI_API_KEY=<redacted>" in prompt for prompt in confirmations)
    assert any("TALK_PREFER_CODEX_OAUTH=false" in prompt for prompt in confirmations)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        f"TALK_OPENAI_API_KEY={secret}\nTALK_PREFER_CODEX_OAUTH=false\n"
    )
    assert secret not in output
    assert "Verification: PASS" in output


def test_invalid_preference_key_choice_completes_real_doctor_setup_doctor_in_one_run(
    monkeypatch, tmp_path, capsys, request
):
    managed_environment = (
        "HERMES_HOME",
        "CODEX_HOME",
        "TALK_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "TALK_PREFER_CODEX_OAUTH",
        "TALK_DISCORD_OPERATOR_USER_IDS",
    )
    environment_before = {
        name: (name in os.environ, os.environ.get(name)) for name in managed_environment
    }

    def restore_environment():
        for name, (present, value) in environment_before.items():
            if present:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)

    request.addfinalizer(restore_environment)
    secret = "sk-proj-synthetic-setup-regression"
    prompts = []
    secret_prompts = []
    transactions = []

    with monkeypatch.context() as scoped:
        scoped.setitem(sys.modules, "hermes_constants", None)
        scoped.setenv("HERMES_HOME", str(tmp_path / "hermes"))
        scoped.setenv("CODEX_HOME", str(tmp_path / "codex-missing"))
        scoped.setenv("TALK_PREFER_CODEX_OAUTH", "definitely-not-a-bool")
        scoped.delenv("TALK_OPENAI_API_KEY", raising=False)
        scoped.delenv("OPENAI_API_KEY", raising=False)
        scoped.delenv("TALK_DISCORD_OPERATOR_USER_IDS", raising=False)

        real_apply = talk_setup.apply_env_transaction

        def record_transaction(env_path, changes, **kwargs):
            transactions.append(
                tuple(
                    (key, "<redacted>" if is_secret else value, is_secret)
                    for key, value, is_secret in changes
                )
            )
            return real_apply(env_path, changes, **kwargs)

        scoped.setattr(talk_setup, "apply_env_transaction", record_transaction)

        before = talk_setup.talk_doctor.collect_report()
        before_auth = {check["id"]: check for check in before["checks"]}["auth"]
        assert before_auth["status"] == "fail"
        assert before_auth["details"]["blocked_by"] == "invalid-preference"
        assert before_auth["details"]["codex_oauth"] == "missing"
        assert before_auth["details"]["metered_key_present"] is False

        answers = iter(["key", "yes", "yes"])

        def answer(prompt):
            prompts.append(prompt)
            return next(answers)

        def read_secret(prompt):
            secret_prompts.append(prompt)
            return secret

        assert talk_setup.cli_entry(input_fn=answer, secret_input_fn=read_secret) == 0

        after = talk_setup.talk_doctor.collect_report()
        after_auth = {check["id"]: check for check in after["checks"]}["auth"]
        assert after_auth["status"] == "pass"
        assert after_auth["details"]["winning_lane"] == "configured"
        assert after_auth["details"]["preference"] == "disabled"
        assert secret_prompts == ["Enter TALK_OPENAI_API_KEY (input hidden): "]
        assert transactions == [
            (
                ("TALK_OPENAI_API_KEY", "<redacted>", True),
                ("TALK_PREFER_CODEX_OAUTH", "false", False),
            )
        ]
        assert (tmp_path / "hermes" / ".env").read_text(encoding="utf-8") == (
            f"TALK_OPENAI_API_KEY={secret}\nTALK_PREFER_CODEX_OAUTH=false\n"
        )

    restore_environment()
    assert {
        name: (name in os.environ, os.environ.get(name)) for name in managed_environment
    } == environment_before
    output = capsys.readouterr().out
    assert secret not in output
    assert "state=applied" in output
    assert "Verification: PASS" in output
    assert len([prompt for prompt in prompts if prompt.startswith("Write ")]) == 2


def test_declined_write_is_not_applied_and_failed_verification_is_returned(
    monkeypatch, tmp_path
):
    broken = _report(
        auth={
            "winning_lane": "openai-api-key",
            "blocked_by": None,
            "preference": "absent",
            "codex_oauth": "missing",
            "metered_key_present": True,
            "metered_key_wins_over_codex": False,
        },
        voice_status="fail",
    )
    reports = iter([broken, broken])
    monkeypatch.setattr(talk_setup.talk_doctor, "collect_report", lambda: next(reports))
    monkeypatch.setattr(talk_setup.talk_doctor, "render_human", lambda _report: "doctor receipt")
    monkeypatch.setattr(talk_setup.talk_config, "get_hermes_home", lambda: tmp_path)

    answers = iter(["cedar", "no"])
    assert talk_setup.cli_entry(input_fn=lambda _prompt: next(answers)) == 1
    assert not (tmp_path / ".env").exists()


def test_env_update_preserves_unrelated_lines(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("# owned by Hermes\nOTHER=value\nTALK_VOICE=ash\n", encoding="utf-8")

    talk_setup.apply_env_write(env_path, "TALK_VOICE", "cedar")

    assert env_path.read_text(encoding="utf-8") == (
        "# owned by Hermes\nOTHER=value\nTALK_VOICE=cedar\n"
    )


def test_transaction_rolls_back_partial_environment_apply_and_never_changes_file(tmp_path):
    class FailOnceEnvironment(dict):
        failed = False

        def __setitem__(self, key, value):
            if key == "TALK_VOICE" and not self.failed:
                self.failed = True
                raise PermissionError("simulated environment failure")
            super().__setitem__(key, value)

    env_path = tmp_path / ".env"
    original = "TALK_MODEL=old-model\nTALK_VOICE=ash\n"
    env_path.write_text(original, encoding="utf-8")
    environment = FailOnceEnvironment(TALK_MODEL="old-model", TALK_VOICE="ash")

    receipt = talk_setup.apply_env_transaction(
        env_path,
        [
            ("TALK_MODEL", "gpt-realtime-2.1", False),
            ("TALK_VOICE", "cedar", False),
        ],
        environ=environment,
    )

    assert receipt.state == "rolled-back"
    assert receipt.failed_at == "environment:TALK_VOICE"
    assert receipt.mutation_survived is False
    assert environment == {"TALK_MODEL": "old-model", "TALK_VOICE": "ash"}
    assert env_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".*.hermes-talk-*.tmp")) == []


def test_setup_catches_replace_failure_cleans_secret_temp_and_reruns_doctor(
    monkeypatch, tmp_path, capsys
):
    secret = "sk-secret-that-must-not-remain"
    env_path = tmp_path / ".env"
    original = "OTHER=preserved\n"
    env_path.write_text(original, encoding="utf-8")
    reports = [_report(
        auth={
            "status": "fail",
            "winning_lane": None,
            "blocked_by": "no-usable-auth",
            "preference": "absent",
            "codex_oauth": "missing",
            "metered_key_present": False,
            "metered_key_wins_over_codex": False,
        }
    )]
    doctor_calls = []

    def collect_report():
        doctor_calls.append(1)
        return reports.pop() if reports else _healthy_report()

    monkeypatch.setattr(talk_setup.talk_doctor, "collect_report", collect_report)
    monkeypatch.setattr(talk_setup.talk_doctor, "render_human", lambda _report: "doctor receipt")
    monkeypatch.setattr(talk_setup.talk_config, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        talk_setup,
        "_replace_env_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("secret?")),
    )

    answers = iter(["key", "yes"])
    assert talk_setup.cli_entry(
        input_fn=lambda _prompt: next(answers),
        secret_input_fn=lambda _prompt: secret,
    ) == 1
    output = capsys.readouterr().out

    assert doctor_calls == [1, 1]
    assert env_path.read_text(encoding="utf-8") == original
    assert "TALK_OPENAI_API_KEY" not in os.environ
    assert list(tmp_path.glob(".*.hermes-talk-*.tmp")) == []
    assert secret not in output
    assert "state=rolled-back" in output
    assert "failed_at=commit" in output
    assert "error=PermissionError" in output
    assert "Verification: FAIL" in output


def test_commit_failure_and_denied_temp_cleanup_reports_surviving_secret_mutation(
    monkeypatch, tmp_path
):
    secret = "sk-cleanup-denial-secret-must-be-redacted"
    cleanup_error_secret = "cleanup-error-must-also-be-redacted"
    env_path = tmp_path / ".env"
    env_path.write_text("OTHER=preserved\n", encoding="utf-8")
    staged_paths = []
    real_stage = talk_setup._stage_env_file
    real_unlink = Path.unlink

    def capture_stage(*args, **kwargs):
        temporary = real_stage(*args, **kwargs)
        staged_paths.append(temporary)
        return temporary

    def deny_staged_cleanup(path, *args, **kwargs):
        if path in staged_paths:
            raise PermissionError(cleanup_error_secret)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(talk_setup, "_stage_env_file", capture_stage)
    monkeypatch.setattr(
        talk_setup,
        "_replace_env_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError(secret)),
    )
    monkeypatch.setattr(Path, "unlink", deny_staged_cleanup)

    try:
        receipt = talk_setup.apply_env_transaction(
            env_path,
            [("TALK_OPENAI_API_KEY", secret, True)],
            environ={},
        )
        rendered = talk_setup._render_apply_receipt(receipt)

        assert receipt.state == "failed"
        assert receipt.mutation_survived is True
        assert receipt.failed_at == "commit"
        assert receipt.error_type == "PermissionError"
        assert receipt.cleanup_errors == (
            "staged-temp[0]-beside-destination:PermissionError",
        )
        assert len(staged_paths) == 1
        assert staged_paths[0].exists()
        assert secret in staged_paths[0].read_text(encoding="utf-8")
        assert env_path.read_text(encoding="utf-8") == "OTHER=preserved\n"
        assert "cleanup=staged-temp[0]-beside-destination:PermissionError" in rendered
        assert secret not in rendered
        assert cleanup_error_secret not in rendered
        assert str(staged_paths[0]) not in rendered
    finally:
        for staged_path in staged_paths:
            real_unlink(staged_path, missing_ok=True)


def test_transaction_tracks_initial_and_rollback_stages_through_cleanup(
    monkeypatch, tmp_path
):
    env_path = tmp_path / ".env"
    original = b"TALK_VOICE=ash\n"
    env_path.write_bytes(original)
    if os.name == "nt":
        owner_sid = _windows_owner_sid(env_path)
        _windows_set_dacl(env_path, f"D:P(A;;FA;;;{owner_sid})")
    environment = {"TALK_VOICE": "ash"}
    staged_paths = []
    cleanup_attempts = []
    real_stage = talk_setup._stage_env_file
    real_replace = talk_setup._replace_env_file
    real_unlink = Path.unlink
    replace_calls = 0

    def capture_stage(*args, **kwargs):
        temporary = real_stage(*args, **kwargs)
        staged_paths.append(temporary)
        return temporary

    def fail_after_mutating_destination(temporary, destination, *, destination_existed):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            destination.write_bytes(temporary.read_bytes())
            raise PermissionError("simulated post-write commit failure")
        real_replace(temporary, destination, destination_existed=destination_existed)

    def observe_cleanup(path, *args, **kwargs):
        if path in staged_paths:
            cleanup_attempts.append(path)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(talk_setup, "_stage_env_file", capture_stage)
    monkeypatch.setattr(talk_setup, "_replace_env_file", fail_after_mutating_destination)
    monkeypatch.setattr(Path, "unlink", observe_cleanup)

    receipt = talk_setup.apply_env_transaction(
        env_path,
        [("TALK_VOICE", "cedar", False)],
        environ=environment,
    )

    assert receipt.state == "rolled-back"
    assert receipt.mutation_survived is False
    assert receipt.cleanup_errors == ()
    assert replace_calls == 2
    assert len(staged_paths) == 2
    assert set(staged_paths).issubset(cleanup_attempts)
    assert all(not path.exists() for path in staged_paths)
    assert env_path.read_bytes() == original
    assert environment == {"TALK_VOICE": "ash"}


def test_failed_receipt_with_surviving_mutation_still_reruns_doctor(
    monkeypatch, tmp_path, capsys
):
    broken = _report(
        auth={
            "winning_lane": "openai-api-key",
            "blocked_by": None,
            "preference": "absent",
            "codex_oauth": "missing",
            "metered_key_present": True,
            "metered_key_wins_over_codex": False,
        },
        voice_status="fail",
    )
    reports = iter([broken, broken])
    doctor_calls = []
    monkeypatch.setattr(
        talk_setup.talk_doctor,
        "collect_report",
        lambda: doctor_calls.append(1) or next(reports),
    )
    monkeypatch.setattr(talk_setup.talk_doctor, "render_human", lambda _report: "doctor receipt")
    monkeypatch.setattr(talk_setup.talk_config, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        talk_setup,
        "apply_env_transaction",
        lambda *_args, **_kwargs: talk_setup.ApplyReceipt(
            state="failed",
            settings=("TALK_VOICE",),
            failed_at="rollback:environment:TALK_VOICE",
            error_type="PermissionError",
            mutation_survived=True,
        ),
    )

    answers = iter(["cedar", "yes"])
    assert talk_setup.cli_entry(input_fn=lambda _prompt: next(answers)) == 1
    output = capsys.readouterr().out

    assert doctor_calls == [1, 1]
    assert "state=failed" in output
    assert "mutation_survived=true" in output
    assert "error=PermissionError" in output


def test_transaction_reports_failed_when_environment_rollback_cannot_complete(tmp_path):
    class RollbackFailingEnvironment(dict):
        apply_failed = False

        def __setitem__(self, key, value):
            if key == "TALK_VOICE" and value == "cedar":
                self.apply_failed = True
                raise PermissionError("simulated apply failure")
            if key == "TALK_MODEL" and value == "old-model" and self.apply_failed:
                raise PermissionError("simulated rollback failure")
            super().__setitem__(key, value)

    env_path = tmp_path / ".env"
    original = "TALK_MODEL=old-model\nTALK_VOICE=ash\n"
    env_path.write_text(original, encoding="utf-8")
    environment = RollbackFailingEnvironment(TALK_MODEL="old-model", TALK_VOICE="ash")

    receipt = talk_setup.apply_env_transaction(
        env_path,
        [
            ("TALK_MODEL", "gpt-realtime-2.1", False),
            ("TALK_VOICE", "cedar", False),
        ],
        environ=environment,
    )

    assert receipt.state == "failed"
    assert receipt.failed_at == "rollback:environment:TALK_MODEL"
    assert receipt.error_type == "PermissionError"
    assert receipt.mutation_survived is True
    assert "TALK_MODEL" not in environment
    assert environment["TALK_VOICE"] == "ash"
    assert env_path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".*.hermes-talk-*.tmp")) == []


def test_atomic_env_update_preserves_existing_file_mode(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("TALK_VOICE=ash\n", encoding="utf-8")
    env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    before_mode = stat.S_IMODE(env_path.stat().st_mode)

    receipt = talk_setup.apply_env_transaction(
        env_path,
        [("TALK_VOICE", "cedar", False)],
        environ={},
    )

    assert receipt.state == "applied"
    assert stat.S_IMODE(env_path.stat().st_mode) == before_mode
    assert list(tmp_path.glob(".*.hermes-talk-*.tmp")) == []


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL discriminator")
def test_new_secret_temp_and_env_override_permissive_inherited_parent_dacl(
    monkeypatch, tmp_path
):
    parent = tmp_path / "permissive-parent"
    parent.mkdir()
    _windows_set_dacl(parent, "D:P(A;OICI;FA;;;WD)")
    inherited_control = parent / "inherits-everyone.txt"
    inherited_control.write_text("control", encoding="utf-8")
    assert ";;;WD)" in _windows_dacl_sddl(inherited_control)
    inherited_control.unlink()

    env_path = parent / ".env"
    observed_temps = []
    real_replace = talk_setup._replace_env_file

    def inspect_replace(temporary, destination, *, destination_existed):
        observed_temps.append(temporary)
        _assert_windows_owner_only_dacl(temporary)
        real_replace(temporary, destination, destination_existed=destination_existed)

    monkeypatch.setattr(talk_setup, "_replace_env_file", inspect_replace)

    receipt = talk_setup.apply_env_transaction(
        env_path,
        [("TALK_OPENAI_API_KEY", "sk-native-acl-secret", True)],
        environ={},
    )

    assert receipt.state == "applied"
    assert len(observed_temps) == 1
    assert not observed_temps[0].exists()
    _assert_windows_owner_only_dacl(env_path)


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL discriminator")
def test_existing_destination_dacl_is_preserved_exactly_while_temp_is_restrictive(
    monkeypatch, tmp_path
):
    env_path = tmp_path / ".env"
    env_path.write_text("TALK_VOICE=ash\n", encoding="utf-8")
    owner_sid = talk_setup._windows_current_user_sid()
    _windows_set_dacl(env_path, f"D:P(A;;FA;;;{owner_sid})(A;;FR;;;WD)")
    original_dacl = _windows_dacl_sddl(env_path)
    observed_temps = []
    real_replace = talk_setup._replace_env_file

    def inspect_replace(temporary, destination, *, destination_existed):
        observed_temps.append(temporary)
        _assert_windows_owner_only_dacl(temporary)
        real_replace(temporary, destination, destination_existed=destination_existed)

    monkeypatch.setattr(talk_setup, "_replace_env_file", inspect_replace)

    receipt = talk_setup.apply_env_transaction(
        env_path,
        [("TALK_VOICE", "cedar", False)],
        environ={},
    )

    assert receipt.state == "applied"
    assert len(observed_temps) == 1
    assert _windows_dacl_sddl(env_path) == original_dacl


def test_windows_restrictive_dacl_targets_active_user_not_owner_group(monkeypatch, tmp_path):
    path = tmp_path / "secret.env"
    path.write_text("secret", encoding="utf-8")
    active_user = "S-1-5-21-111-222-333-1001"
    owner_group = "S-1-5-32-544"
    applied = []

    monkeypatch.setattr(talk_setup, "_windows_current_user_sid", lambda: active_user)
    monkeypatch.setattr(talk_setup, "_windows_owner_sid", lambda _path: owner_group)
    monkeypatch.setattr(
        talk_setup,
        "_windows_apply_dacl_sddl",
        lambda target, sddl: applied.append((target, sddl)),
    )
    monkeypatch.setattr(
        talk_setup,
        "_windows_dacl_sddl",
        lambda _path: f"D:P(A;;FA;;;{active_user})",
    )

    talk_setup._windows_restrict_owner_only_dacl(path)

    assert applied == [(path, f"D:P(A;;FA;;;{active_user})")]


@pytest.mark.skipif(os.name != "nt", reason="native Windows handle discriminator")
def test_windows_stage_closes_empty_temp_before_applying_dacl(monkeypatch, tmp_path):
    captured_fd = None
    real_mkstemp = tempfile.mkstemp
    real_restrict = talk_setup._windows_restrict_owner_only_dacl

    def capture_mkstemp(*args, **kwargs):
        nonlocal captured_fd
        captured_fd, name = real_mkstemp(*args, **kwargs)
        return captured_fd, name

    def inspect_restrict(path):
        assert captured_fd is not None
        with pytest.raises(OSError):
            os.fstat(captured_fd)
        assert path.read_bytes() == b""
        try:
            real_restrict(path)
        except PermissionError:
            actual = talk_setup._windows_dacl_sddl(path)
            active_user = talk_setup._windows_current_user_sid()
            flags = actual.partition("(")[0]
            entries = []
            for raw_ace in re.findall(r"\(([^()]*)\)", actual):
                fields = raw_ace.split(";")
                entries.append(
                    {
                        "type": fields[0] if len(fields) > 0 else None,
                        "flags": fields[1] if len(fields) > 1 else None,
                        "rights": fields[2] if len(fields) > 2 else None,
                        "object_guid": fields[3] if len(fields) > 3 else None,
                        "inherit_guid": fields[4] if len(fields) > 4 else None,
                        "active_user": len(fields) > 5 and fields[5] == active_user,
                    }
                )
            pytest.fail(
                f"restrictive DACL verification shape: flags={flags!r}; "
                f"ace_count={len(entries)}; entries={entries!r}"
            )

    monkeypatch.setattr(talk_setup.tempfile, "mkstemp", capture_mkstemp)
    monkeypatch.setattr(talk_setup, "_windows_restrict_owner_only_dacl", inspect_restrict)
    staged_paths = []
    destination = tmp_path / ".env"

    temporary = talk_setup._stage_env_file(
        destination,
        b"TALK_OPENAI_API_KEY=secret\n",
        stat.S_IRUSR | stat.S_IWUSR,
        staged_paths=staged_paths,
    )

    assert temporary.read_bytes() == b"TALK_OPENAI_API_KEY=secret\n"
    temporary.unlink()


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL discriminator")
@pytest.mark.parametrize(
    ("failure_call", "failed_at"),
    [(1, "stage"), (2, "commit-security")],
)
def test_windows_acl_hardening_failure_fails_closed_and_cleans_every_path(
    monkeypatch, tmp_path, failure_call, failed_at
):
    env_path = tmp_path / ".env"
    environment = {}
    real_restrict = talk_setup._windows_restrict_owner_only_dacl
    calls = 0

    def fail_selected_hardening(path):
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise PermissionError("native ACL hardening denied")
        real_restrict(path)

    monkeypatch.setattr(
        talk_setup,
        "_windows_restrict_owner_only_dacl",
        fail_selected_hardening,
    )

    receipt = talk_setup.apply_env_transaction(
        env_path,
        [("TALK_OPENAI_API_KEY", "sk-acl-failure-secret", True)],
        environ=environment,
    )

    assert calls == failure_call
    assert receipt.state == "rolled-back"
    assert receipt.failed_at == failed_at
    assert receipt.error_type == "PermissionError"
    assert receipt.mutation_survived is False
    assert receipt.cleanup_errors == ()
    assert environment == {}
    assert not env_path.exists()
    assert list(tmp_path.glob(".*.hermes-talk-*.tmp")) == []


def test_transactions_use_unique_securely_created_temp_files(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    observed = []
    real_replace = talk_setup._replace_env_file

    def observe_replace(temporary, destination, *, destination_existed):
        observed.append(temporary)
        assert temporary.exists()
        assert temporary.name != f".{env_path.name}.hermes-talk.tmp"
        if os.name != "nt":
            assert stat.S_IMODE(temporary.stat().st_mode) & 0o077 == 0
        real_replace(temporary, destination, destination_existed=destination_existed)

    monkeypatch.setattr(talk_setup, "_replace_env_file", observe_replace)

    first = talk_setup.apply_env_transaction(
        env_path,
        [("TALK_VOICE", "ash", False)],
        environ={},
    )
    second = talk_setup.apply_env_transaction(
        env_path,
        [("TALK_VOICE", "cedar", False)],
        environ={},
    )

    assert first.state == second.state == "applied"
    assert len(observed) == 2
    assert observed[0].name != observed[1].name
    assert all(not temporary.exists() for temporary in observed)


def test_parent_security_failure_removes_created_directory_and_changes_nothing(
    monkeypatch, tmp_path
):
    env_path = tmp_path / "new-hermes-home" / ".env"
    real_chmod = Path.chmod

    def fail_target_chmod(path, mode):
        if path == env_path.parent:
            raise PermissionError("simulated permission hardening failure")
        return real_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", fail_target_chmod)
    environment = {}

    receipt = talk_setup.apply_env_transaction(
        env_path,
        [("TALK_VOICE", "cedar", False)],
        environ=environment,
    )

    assert receipt.state == "rolled-back"
    assert receipt.failed_at == "parent"
    assert receipt.error_type == "PermissionError"
    assert receipt.mutation_survived is False
    assert environment == {}
    assert not env_path.parent.exists()


@pytest.mark.parametrize("remove", [False, True])
def test_dotenv_windows_env_names_replace_or_remove_all_case_variants(tmp_path, remove):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "talk_openai_api_key=lower\n"
        "OTHER=preserved\n"
        "TALK_OPENAI_API_KEY=upper\n"
        "Talk_OpenAI_Api_Key=mixed\n",
        encoding="utf-8",
    )

    talk_setup.apply_env_write(
        env_path,
        "TALK_OPENAI_API_KEY",
        None if remove else "replacement",
        windows_env_names=True,
    )
    persisted = env_path.read_text(encoding="utf-8")
    assignments = [
        line
        for line in persisted.splitlines()
        if line.partition("=")[0].upper() == "TALK_OPENAI_API_KEY"
    ]

    assert "OTHER=preserved" in persisted
    assert assignments == ([] if remove else ["TALK_OPENAI_API_KEY=replacement"])
    reloaded = {
        line.partition("=")[0].upper(): line.partition("=")[2]
        for line in persisted.splitlines()
        if "=" in line
    }
    if remove:
        assert "TALK_OPENAI_API_KEY" not in reloaded
    else:
        assert reloaded["TALK_OPENAI_API_KEY"] == "replacement"


def test_dotenv_posix_env_names_remain_case_sensitive(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "talk_openai_api_key=lower\nTALK_OPENAI_API_KEY=upper\n",
        encoding="utf-8",
    )

    talk_setup.apply_env_write(
        env_path,
        "TALK_OPENAI_API_KEY",
        None,
        windows_env_names=False,
    )

    assert env_path.read_text(encoding="utf-8") == "talk_openai_api_key=lower\n"
