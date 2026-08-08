"""Read-only diagnostics for the native ``hermes talk doctor`` command.

The design borrows TaskChad's useful operational loop without importing its
product assumptions: detect local state, decide a bounded status/remediation,
then emit a verification receipt. There is intentionally no write phase here.
Doctor never refreshes OAuth, creates directories, starts audio, probes a
sidecar, edits setup, or exposes the content it inspects.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

try:
    from . import (
        talk_audio,
        talk_auth,
        talk_config,
        talk_host,
        talk_identity,
        talk_tools,
    )
except ImportError:  # pragma: no cover - flat-module fallback (pip -e install)
    import talk_audio
    import talk_auth
    import talk_config
    import talk_host
    import talk_identity
    import talk_tools

SCHEMA_VERSION = 1
COMMAND = "hermes talk doctor"
CHECK_ORDER = (
    "plugin",
    "auth",
    "model",
    "voice",
    "audio",
    "identity",
    "discord",
    "host",
)
HOST_CAPABILITIES = (
    "register_cli_command",
    "register_command",
    "register_hook",
    "register_tts_provider",
    "register_transcription_provider",
    "dispatch_tool",
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(?<![A-Za-z0-9])(?:github_pat_|gh[pousr]_)[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
)


def _redact_text(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("<redacted-secret>", value)
    return value


def _redact_value(value: Any) -> Any:
    """Recursively scrub secret-shaped strings at the report boundary."""

    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def _check(
    check_id: str,
    status: str,
    summary: str,
    details: dict[str, Any],
    remediation: Iterable[str] = (),
) -> dict[str, Any]:
    """Build one stable, JSON-safe check envelope."""

    return {
        "id": check_id,
        "status": status,
        "summary": _redact_text(summary),
        "details": _redact_value(details),
        "remediation": [_redact_text(action) for action in remediation],
    }


def _plugin_check() -> dict[str, Any]:
    receipts = dict(sorted(talk_tools.REGISTRATION_RECEIPTS.items()))
    requirements = talk_tools.REGISTRATION_REQUIREMENTS
    issues = {receipt: state for receipt, state in receipts.items() if state != "registered"}
    required_issues = {
        receipt: state
        for receipt, state in issues.items()
        if requirements.get(receipt, "required") == "required"
    }
    optional_issues = {
        receipt: state
        for receipt, state in issues.items()
        if requirements.get(receipt) == "optional"
    }
    context_bound = talk_host.get_ctx() is not None
    details = {
        "context_bound": context_bound,
        "failure_count": len(talk_tools.REGISTRATION_FAILURES),
        "required_issue_count": len(required_issues),
        "optional_issue_count": len(optional_issues),
        "requirements": {receipt: requirements.get(receipt, "required") for receipt in receipts},
        "surfaces": receipts,
    }
    if required_issues:
        return _check(
            "plugin",
            "fail",
            f"plugin registration has {len(required_issues)} required surface issue(s)",
            details,
            ("Restart Hermes after correcting the required plugin registration surface.",),
        )
    if not receipts:
        return _check(
            "plugin",
            "warn",
            "plugin registration receipts are not observable in this process",
            details,
            ("Run this command through Hermes as `hermes talk doctor`.",),
        )
    if optional_issues:
        return _check(
            "plugin",
            "warn",
            f"core registration succeeded; {len(optional_issues)} optional surface issue(s)",
            details,
        )
    return _check("plugin", "pass", "all plugin surfaces registered", details)


def _auth_check() -> dict[str, Any]:
    receipt = talk_auth.auth_diagnostic()
    # Keep the complete diagnostic only because every field is a lane/state
    # receipt. It intentionally contains no paths, tokens, IDs, or env values.
    details = dict(receipt)
    lane = receipt["winning_lane"]
    remediation: list[str] = []

    if receipt["blocked_by"] == "invalid-preference":
        remediation.append("Set TALK_PREFER_CODEX_OAUTH to true or false, or unset it.")
        return _check(
            "auth",
            "fail",
            "auth selection is blocked by an invalid Codex preference",
            details,
            remediation,
        )
    if lane is None:
        if receipt["blocked_by"] == "blank-talk-key":
            remediation.append("Set a real TALK_OPENAI_API_KEY or unset it.")
            summary = "TALK_OPENAI_API_KEY is set but blank and blocks later auth lanes"
        elif receipt["blocked_by"] == "blank-openai-key":
            remediation.append("Set a real OPENAI_API_KEY or unset it.")
            summary = "OPENAI_API_KEY is set but blank and blocks Codex OAuth"
        elif receipt["preference"] == "enabled":
            remediation.append("Run `codex login` or unset TALK_PREFER_CODEX_OAUTH.")
            summary = "Codex OAuth is required but no usable login was found; API keys are ignored"
        else:
            remediation.append(
                "Set TALK_OPENAI_API_KEY or OPENAI_API_KEY, or run `codex login`."
            )
            summary = "no usable Realtime authentication lane was found"
        return _check("auth", "fail", summary, details, remediation)

    if receipt["refresh_required"]:
        remediation.append(
            "Run `codex login` if the normal session-time OAuth refresh does not succeed."
        )
        return _check(
            "auth",
            "warn",
            "Codex OAuth wins but is expired; doctor did not refresh or write it",
            details,
            remediation,
        )
    if receipt["metered_key_wins_over_codex"]:
        remediation.append(
            "Set TALK_PREFER_CODEX_OAUTH=true to require Codex OAuth and "
            "refuse metered key fallback."
        )
        if receipt["codex_oauth"] == "expired":
            summary = (
                f"{lane} wins by legacy precedence while Codex OAuth is expired "
                "and requires a successful refresh before use"
            )
        else:
            summary = f"{lane} wins by legacy precedence while usable Codex OAuth is available"
        return _check(
            "auth",
            "warn",
            summary,
            details,
            remediation,
        )
    if receipt["metered_keys_ignored"]:
        summary = "Codex OAuth wins by explicit preference; configured API key lanes are ignored"
    else:
        summary = f"{lane} is the winning auth lane"
    return _check("auth", "pass", summary, details)


def _model_check() -> dict[str, Any]:
    model = talk_config.talk_model()
    source = "TALK_MODEL" if (os.environ.get("TALK_MODEL") or "").strip() else "default"
    compatibility = talk_config.realtime_model_compatibility(model)
    details = {
        "model": model,
        "source": source,
        "compatibility": compatibility,
        "policy_version": talk_config.MODEL_COMPATIBILITY_POLICY_VERSION,
        "validation_scope": "explicit-capability-policy",
    }
    if compatibility == "compatible":
        return _check(
            "model",
            "pass",
            f"model {model} is explicitly compatible with duplex audio and tool calling",
            details,
        )
    if compatibility == "unknown":
        details["validation_scope"] = "syntax-only"
        return _check(
            "model",
            "warn",
            f"model {model} matches Realtime syntax but duplex/tool compatibility is unknown",
            details,
            (
                "Use a model in the documented compatibility policy, such as "
                f"{talk_config.DEFAULT_TALK_MODEL}.",
            ),
        )
    return _check(
        "model",
        "fail",
        f"model {model} is not compatible with Talk's duplex audio and tool-calling contract",
        details,
        (f"Set TALK_MODEL to a known compatible model, such as {talk_config.DEFAULT_TALK_MODEL}.",),
    )


def _voice_check() -> dict[str, Any]:
    source = "TALK_VOICE" if (os.environ.get("TALK_VOICE") or "").strip() else "default"
    try:
        voice = talk_config.talk_voice()
    except talk_config.TalkConfigError:
        raw = (os.environ.get("TALK_VOICE") or "").strip().lower()
        return _check(
            "voice",
            "fail",
            "configured voice is not a built-in Realtime voice",
            {"voice": raw or None, "source": source, "valid": False},
            ("Choose a TALK_VOICE listed in the hermes-talk operating guide.",),
        )
    return _check(
        "voice",
        "pass",
        f"Realtime voice {voice} is valid",
        {"voice": voice, "source": source, "valid": True},
    )


def _audio_check() -> dict[str, Any]:
    available = talk_audio.audio_available()
    details = {
        "dependency_available": available,
        "input_override": talk_config.audio_input_device() is not None,
        "output_override": talk_config.audio_output_device() is not None,
    }
    if available:
        return _check(
            "audio",
            "pass",
            "terminal audio dependency is importable; devices remain unopened",
            details,
        )
    return _check(
        "audio",
        "warn",
        "terminal audio dependency is unavailable; Discord voice may still be usable",
        details,
        ('Install the audio extra with `pip install "hermes-talk[audio]"`.',),
    )


def _home_receipt() -> dict[str, Any]:
    """Resolve active home/profile/root without constructing any path."""

    home = talk_config.get_hermes_home()
    env_home = (os.environ.get("HERMES_HOME") or "").strip()
    try:
        import hermes_constants
    except Exception:  # noqa: BLE001 - use talk_config's documented fallback semantics
        if env_home and Path(env_home).expanduser() == home:
            home_source = "HERMES_HOME"
        elif home == Path.home() / ".hermes":
            home_source = "default"
        else:
            home_source = "fallback-unknown"
    else:
        override_fn = getattr(hermes_constants, "get_hermes_home_override", None)
        process_home_fn = getattr(hermes_constants, "get_process_hermes_home", None)
        try:
            override = override_fn() if callable(override_fn) else None
            if override and Path(override) == home:
                home_source = "host-context-override"
            elif callable(process_home_fn) and Path(process_home_fn()) == home:
                # Current Hermes passes HERMES_HOME directly through Path(val):
                # do not expand, resolve, or otherwise invent different host
                # semantics merely to label provenance.
                if env_home and Path(env_home) == home:
                    home_source = "HERMES_HOME"
                else:
                    home_source = "host-process-default"
            else:
                # Older hosts expose only get_hermes_home(). Repeating that call
                # proves the path, not which input won, so keep provenance honest.
                home_source = "host-resolver-unknown"
        except Exception:  # noqa: BLE001 - a host API failure makes provenance unknown
            home_source = "host-resolver-unknown"

    if home.parent.name.lower() == "profiles" and home.name:
        root = home.parent.parent
        profile = home.name
        profile_source = "resolved-profile-home"
    else:
        root = home
        explicit = os.environ.get("TALK_AGENT_PROFILE")
        if explicit is not None:
            profile = explicit.strip() or None
            profile_source = "TALK_AGENT_PROFILE" if profile else "TALK_AGENT_PROFILE-root"
        else:
            profile = talk_config.detect_agent_profile()
            profile_source = "auto-single-profile" if profile else "root"
    return {
        "resolved_home": str(home),
        "home_source": home_source,
        "root": str(root),
        "root_source": "collapsed-profile-parent" if root != home else home_source,
        "identity_home": str(home),
        "active_profile": profile,
        "profile_source": profile_source,
    }


def _identity_check() -> dict[str, Any]:
    details = _home_receipt()
    try:
        resolved = talk_host.host().diagnostic_identity_sections()
    except Exception:  # noqa: BLE001 - a diagnostic records absence, never host text
        resolved = None
    if not isinstance(resolved, dict):
        details.update({"section_count": 0, "sections": {}})
        return _check(
            "identity",
            "warn",
            "identity sections could not be resolved",
            details,
            ("Verify the active Hermes profile and its identity files.",),
        )

    sections = {
        str(name).upper(): {
            "characters": len(talk_identity.cap_section(str(name), body))
        }
        for name, body in resolved.items()
        if isinstance(body, str) and body.strip()
    }
    details.update(
        {
            "inspection": "existing-files-only",
            "section_count": len(sections),
            "sections": sections,
        }
    )
    profile = details["active_profile"] or "root"
    if sections:
        return _check(
            "identity",
            "pass",
            f"{len(sections)} existing identity file section(s) found; active profile {profile}",
            details,
        )
    return _check(
        "identity",
        "warn",
        f"no existing identity file sections found; active profile {profile}",
        details,
        ("Verify the active profile/root if Talk should inherit Hermes identity.",),
    )


def _discord_check() -> dict[str, Any]:
    raw = os.environ.get("TALK_DISCORD_OPERATOR_USER_IDS")
    configured = raw is not None and bool(raw.strip())
    operator_ids = talk_config.discord_operator_user_ids()
    valid = not configured or bool(operator_ids)
    details = {
        "configured": configured,
        "valid": valid,
        "operator_count": len(operator_ids),
    }
    if not configured:
        return _check(
            "discord",
            "warn",
            "no Discord operators are configured; mutating voice tools deny everyone",
            details,
            ("Set TALK_DISCORD_OPERATOR_USER_IDS to immutable Discord user IDs if needed.",),
        )
    if not valid:
        return _check(
            "discord",
            "fail",
            "Discord operator configuration is malformed and authorizes nobody",
            details,
            ("Correct every TALK_DISCORD_OPERATOR_USER_IDS entry; partial lists are rejected.",),
        )
    return _check(
        "discord",
        "pass",
        f"{len(operator_ids)} Discord operator(s) configured",
        details,
    )


def _host_check() -> dict[str, Any]:
    ctx = talk_host.get_ctx()
    capabilities = {
        name: bool(ctx is not None and callable(getattr(ctx, name, None)))
        for name in HOST_CAPABILITIES
    }
    details = {"context_bound": ctx is not None, "capabilities": capabilities}
    if ctx is None:
        return _check(
            "host",
            "warn",
            "Hermes host context is not bound in this process",
            details,
            ("Run the native `hermes talk doctor` command to verify host capabilities.",),
        )
    missing = [name for name, available in capabilities.items() if not available]
    if missing:
        return _check(
            "host",
            "warn",
            f"Hermes host lacks {len(missing)} Talk capability surface(s)",
            details,
        )
    return _check("host", "pass", "Hermes host exposes every Talk capability", details)


def collect_report() -> dict[str, Any]:
    """Detect, decide, and return a deterministic read-only verification receipt."""

    checks = [
        _plugin_check(),
        _auth_check(),
        _model_check(),
        _voice_check(),
        _audio_check(),
        _identity_check(),
        _discord_check(),
        _host_check(),
    ]
    assert tuple(check["id"] for check in checks) == CHECK_ORDER
    summary = {
        status: sum(check["status"] == status for check in checks)
        for status in ("pass", "warn", "fail")
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "command": COMMAND,
        "read_only": True,
        "ok": summary["fail"] == 0,
        "summary": summary,
        "checks": checks,
    }


def render_human(report: dict[str, Any]) -> str:
    """Render the same receipt for a person without adding hidden probes."""

    report = _redact_value(report)
    lines = ["Hermes Talk doctor (read-only)"]
    for check in report["checks"]:
        lines.append(f"[{check['status'].upper()}] {check['id']}: {check['summary']}")
        details = check["details"]
        if check["id"] == "auth":
            lines.append(
                "  receipt: "
                f"winner={details['winning_lane'] or 'none'}, "
                f"codex={details['codex_oauth']}, preference={details['preference']}"
            )
        elif check["id"] == "identity":
            profile = details["active_profile"] or "root"
            counts = ", ".join(
                f"{name}:{receipt['characters']}"
                for name, receipt in details["sections"].items()
            ) or "none"
            lines.append(
                "  receipt: "
                f"home={details['resolved_home']} ({details['home_source']}), "
                f"root={details['root']} ({details['root_source']}), "
                f"profile={profile} ({details['profile_source']}), sections={counts}"
            )
        elif check["id"] == "plugin":
            surfaces = ", ".join(
                f"{name}={state}" for name, state in details["surfaces"].items()
            ) or "unobserved"
            lines.append(f"  receipt: {surfaces}")
        elif check["id"] == "host":
            capabilities = ", ".join(
                f"{name}={'yes' if available else 'no'}"
                for name, available in details["capabilities"].items()
            )
            lines.append(f"  receipt: {capabilities}")
        for action in check["remediation"]:
            lines.append(f"  -> {action}")
    totals = report["summary"]
    lines.append(
        f"Summary: {totals['pass']} pass, {totals['warn']} warn, {totals['fail']} fail."
    )
    lines.append("No files, credentials, services, or setup were changed.")
    return "\n".join(lines)


def cli_entry(*, json_output: bool = False) -> int:
    """Print one doctor report and return nonzero only for failing checks."""

    report = _redact_value(collect_report())
    if json_output:
        print(json.dumps(report, sort_keys=True))
    else:
        print(render_human(report))
    return 0 if report["ok"] else 1


__all__ = [
    "CHECK_ORDER",
    "COMMAND",
    "SCHEMA_VERSION",
    "cli_entry",
    "collect_report",
    "render_human",
]
