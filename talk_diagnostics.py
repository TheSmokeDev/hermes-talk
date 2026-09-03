"""``hermes talk diagnostics`` — a redacted support bundle for issue reports.

Most "it doesn't work" reports cannot be reproduced from what the reporter
pasted. This command turns the whole picture into one pasteable artifact
that is safe to attach to a public issue: versions (Python, hermes-talk,
the Hermes host, the OS), the NAMES of the configuration and environment
variables that are set (never a value), device and host facts, and the
outcomes of every ``hermes talk doctor`` check.

**Default-deny, opt in per key.** Nothing reaches the bundle by passthrough:
:data:`BUNDLE_ALLOWLIST` names every key the bundle may carry and the
shape its value must have, and :func:`serialize` drops everything else — a
key doctor grows next year cannot leak by inheritance. Leaf shapes are
strict on purpose: ``token`` leaves must look like an identifier (a model
name, a lane, a source label) and are DROPPED, not redacted, if secret
redaction would have changed them; ``text`` leaves (check summaries and
remediations) pass secret redaction, a path scrub, and a length cap.
Values that carry paths by construction (the doctor identity check's
resolved home and root) are simply not on the list.

The file is written owner-only — POSIX ``0600``, a protected owner-only
DACL on Windows through the same helper ``hermes talk setup`` uses for
secret files — and verified after the move; a bundle whose permissions
cannot be proven is deleted, not left behind.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from . import talk_audio, talk_config, talk_doctor, talk_setup, talk_tools
except ImportError:  # pragma: no cover - flat-module fallback (pip -e install)
    import talk_audio
    import talk_config
    import talk_doctor
    import talk_setup
    import talk_tools

SCHEMA_VERSION = 1
COMMAND = "hermes talk diagnostics"
BUNDLE_PREFIX = "hermes-talk-diagnostics-"
PASTE_HINT = (
    "Paste this file into your issue (it contains versions, variable NAMES, "
    "device/host facts, and doctor outcomes; no values, logs, prompts, or audio)."
)

#: Environment variable names outside the ``TALK_`` / ``HERMES_`` prefixes
#: that bear on a voice session. Presence is reported by NAME only.
SHARED_ENV_NAMES = (
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "GEMINI_API_KEY",
    "ELEVENLABS_API_KEY",
    "CODEX_HOME",
    "DISCORD_BOT_TOKEN",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
)
_CONFIG_PREFIXES = ("TALK_",)
_ENV_PREFIXES = ("HERMES_",)

#: A ``token`` leaf: one identifier-shaped value — a model or voice name, a
#: lane, a source label, a version, a platform string. Bounded and free of
#: whitespace and quoting, so it can never be a sentence, a path with
#: spaces, or an encoded blob.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/-]{0,79}$")
#: A ``label`` leaf: printable ASCII with spaces (an audio device name, a
#: kernel version string).
_LABEL_RE = re.compile(r"^[A-Za-z0-9#][A-Za-z0-9 ._+:()/,#;-]{0,119}$")
#: Absolute-path shapes that must never ride a ``text`` leaf.
_PATH_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]+"
    r"|(?<![\w/])/(?:home|Users|root|tmp|var|opt|etc|usr|mnt|private)/[^\s\"'<>|]+"
    r"|(?<![\w/])~/[^\s\"'<>|]+)"
)
_MAX_TEXT_CHARS = 300

# -- the allowlist -----------------------------------------------------------------
#
# Leaf tags: "bool", "int", "token", "label", "text", "token_list",
# "text_list", "token_map" (token -> token), "bool_map" (token -> bool),
# "int_map" (token -> int), "count_map" (token -> {"characters": int}).
# A dict is a nested allowlist. Anything absent is dropped.

_AUTH_DETAILS = {
    "configured": "bool",
    "winning_lane": "token",
    "preference": "token",
    "codex_oauth": "token",
    "xai_oauth": "token",
    "host_refresh_available": "bool",
    "metered_key_present": "bool",
    "metered_key_wins_over_codex": "bool",
    "metered_key_wins_over_oauth": "bool",
    "metered_keys_ignored": "bool",
    "refresh_required": "bool",
    "blocked_by": "token",
}

CHECK_DETAILS_ALLOWLIST: dict[str, dict[str, Any]] = {
    "plugin": {
        "context_bound": "bool",
        "failure_count": "int",
        "required_issue_count": "int",
        "optional_issue_count": "int",
        "requirements": "token_map",
        "surfaces": "token_map",
        "legacy_lane": "token",
        "core_realtime_contract": "token",
        "core_contract_available": "bool",
        "core_provider_available": "bool",
    },
    "provider": {
        "provider": "token",
        "source": "token",
        "keys": "token_map",
        "model": "token",
        "model_source": "token",
        "voice": "token",
        "voice_source": "token",
        "voice_valid": "bool",
        "auth_lane": "token",
        "xai_oauth": "token",
    },
    "auth": _AUTH_DETAILS,
    "model": {
        "model": "token",
        "source": "token",
        "compatibility": "token",
        "policy_version": "token",
        "validation_scope": "token",
    },
    "voice": {"voice": "token", "source": "token", "valid": "bool"},
    "cascade": {
        "voice_mode": "token",
        "source": "token",
        "keys": "token_map",
        "model": "token",
        "tts": "token",
        "provider": "token",
        # voice_id is an account identifier: deliberately not listed.
    },
    "audio": {
        "dependency_available": "bool",
        "input_override": "bool",
        "output_override": "bool",
    },
    "identity": {
        # resolved_home / root / identity_home are paths: deliberately not listed.
        "home_source": "token",
        "root_source": "token",
        "active_profile": "token",
        "profile_source": "token",
        "inspection": "token",
        "section_count": "int",
        "sections": "count_map",
    },
    "discord": {"configured": "bool", "valid": "bool", "operator_count": "int"},
    "host": {"context_bound": "bool", "capabilities": "bool_map"},
}

BUNDLE_ALLOWLIST: dict[str, Any] = {
    "schema_version": "int",
    "command": "label",
    "generated_at": "token",
    "versions": {
        "python": "token",
        "python_implementation": "token",
        "hermes_talk": "token",
        "hermes_agent": "token",
        "os": "token",
        "os_release": "token",
        "os_version": "label",
        "machine": "token",
    },
    "config": {"names": "token_list"},
    "environment": {"names": "token_list"},
    "devices": {
        "audio_dependency_available": "bool",
        "input_device_count": "int",
        "output_device_count": "int",
        "default_input": "label",
        "default_output": "label",
    },
    "host": {"context_bound": "bool", "capabilities": "bool_map"},
    "doctor": {
        "schema_version": "int",
        "ok": "bool",
        "summary": "int_map",
        "checks": "checks",  # handled specially: per-check details allowlist
    },
}


class BundleWriteError(RuntimeError):
    """The bundle could not be written with owner-only permissions."""


# -- leaf validation -------------------------------------------------------------------


def _scrub_text(value: str) -> str:
    text = " ".join(_PATH_RE.sub("<path>", talk_doctor.redact_text(value)).split())
    if len(text) > _MAX_TEXT_CHARS:
        text = text[: _MAX_TEXT_CHARS - 3] + "..."
    return text


def _token(value: Any) -> str | None:
    """An identifier-shaped string, or ``None`` — never a redacted remnant."""

    if not isinstance(value, str):
        return None
    if not _TOKEN_RE.match(value) or talk_doctor.redact_text(value) != value:
        return None
    return value


def _label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if not _LABEL_RE.match(value) or talk_doctor.redact_text(value) != value:
        return None
    return value


def _leaf(tag: str, value: Any) -> Any:
    """Coerce one value to the shape its tag demands; ``None`` when it does not fit."""

    if tag == "bool":
        return value if isinstance(value, bool) else None
    if tag == "int":
        return value if isinstance(value, int) and not isinstance(value, bool) else None
    if tag == "token":
        return _token(value)
    if tag == "label":
        return _label(value)
    if tag == "text":
        return _scrub_text(value) if isinstance(value, str) else None
    if tag == "token_list":
        if not isinstance(value, (list, tuple)):
            return None
        return [token for token in (_token(item) for item in value) if token is not None]
    if tag == "text_list":
        if not isinstance(value, (list, tuple)):
            return None
        return [_scrub_text(item) for item in value if isinstance(item, str)]
    if tag in ("token_map", "bool_map", "int_map", "count_map"):
        if not isinstance(value, dict):
            return None
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = _token(key)
            if name is None:
                continue
            if tag == "token_map":
                coerced: Any = _token(item)
            elif tag == "bool_map":
                coerced = _leaf("bool", item)
            elif tag == "int_map":
                coerced = _leaf("int", item)
            else:
                characters = item.get("characters") if isinstance(item, dict) else None
                coerced = (
                    {"characters": characters}
                    if isinstance(characters, int) and not isinstance(characters, bool)
                    else None
                )
            if coerced is not None:
                out[name] = coerced
        return out
    raise ValueError(f"unknown allowlist tag: {tag!r}")


def serialize(value: Any, allow: dict[str, Any]) -> dict[str, Any]:
    """Keep only the keys ``allow`` names, in the shapes it demands.

    Default-deny: a key missing from ``allow`` is dropped whatever it holds;
    a value that does not fit its tag is dropped too (recorded as absent,
    never coerced into something that looks valid).
    """

    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key, spec in allow.items():
        if key not in value:
            continue
        if isinstance(spec, dict):
            out[key] = serialize(value[key], spec)
        elif spec == "checks":
            out[key] = _serialize_checks(value[key])
        else:
            coerced = _leaf(spec, value[key])
            if coerced is not None:
                out[key] = coerced
    return out


def _serialize_checks(checks: Any) -> list[dict[str, Any]]:
    """Doctor checks, each with its OWN details allowlist; unknown checks are dropped."""

    out: list[dict[str, Any]] = []
    if not isinstance(checks, list):
        return out
    for check in checks:
        if not isinstance(check, dict):
            continue
        check_id = _token(check.get("id"))
        if check_id is None or check_id not in CHECK_DETAILS_ALLOWLIST:
            continue
        status = _token(check.get("status"))
        if status not in ("pass", "warn", "fail"):
            continue
        out.append(
            {
                "id": check_id,
                "status": status,
                "summary": _leaf("text", check.get("summary")) or "",
                "details": serialize(check.get("details"), CHECK_DETAILS_ALLOWLIST[check_id]),
                "remediation": _leaf("text_list", check.get("remediation")) or [],
            }
        )
    return out


# -- collection --------------------------------------------------------------------------


def _host_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("hermes-agent")
    except Exception:  # noqa: BLE001 - a host without metadata is a fact, not an error
        pass
    try:
        import hermes_cli

        found = getattr(hermes_cli, "__version__", None)
        return found if isinstance(found, str) else None
    except Exception:  # noqa: BLE001 - not a Hermes process; the bundle says so with null
        return None


def _versions() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "hermes_talk": talk_tools.plugin_version(),
        "hermes_agent": _host_version(),
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
    }


def _env_names(prefixes: tuple[str, ...], shared: tuple[str, ...] = ()) -> list[str]:
    """NAMES of set variables matching the prefixes, plus any listed shared name.

    Values are never read: presence is the only fact this reports.
    """

    names = {name for name in os.environ if name.upper().startswith(prefixes)}
    names.update(name for name in shared if name in os.environ)
    return sorted(names)


def _audio_facts() -> dict[str, Any]:
    """Device counts and default device names. Reads the device table; opens nothing."""

    facts: dict[str, Any] = {"audio_dependency_available": talk_audio.audio_available()}
    if not facts["audio_dependency_available"]:
        return facts
    try:
        sd = talk_audio.import_sounddevice()
        devices = list(sd.query_devices())
        facts["input_device_count"] = sum(
            1 for device in devices if int(device.get("max_input_channels", 0) or 0) > 0
        )
        facts["output_device_count"] = sum(
            1 for device in devices if int(device.get("max_output_channels", 0) or 0) > 0
        )
        default = sd.default.device
        try:
            indexes: tuple[Any, Any] = (default[0], default[1])
        except (TypeError, IndexError, KeyError):
            indexes = (default, default)
        for index, key in zip(indexes, ("default_input", "default_output"), strict=True):
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(devices):
                facts[key] = str(devices[index].get("name") or "")
    except Exception:  # noqa: BLE001 - a device table that will not read is a fact, not a crash
        pass
    return facts


def collect_bundle(*, doctor_report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect and serialize the bundle. Everything passes the allowlist, nothing else."""

    if doctor_report is None:
        doctor_report = talk_doctor.collect_report()
    checks = {
        check.get("id"): check
        for check in doctor_report.get("checks", [])
        if isinstance(check, dict)
    }
    host_details = checks.get("host", {}).get("details", {})
    raw = {
        "schema_version": SCHEMA_VERSION,
        "command": COMMAND,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "versions": _versions(),
        "config": {"names": _env_names(_CONFIG_PREFIXES)},
        "environment": {"names": _env_names(_ENV_PREFIXES, SHARED_ENV_NAMES)},
        "devices": _audio_facts(),
        "host": host_details if isinstance(host_details, dict) else {},
        "doctor": doctor_report,
    }
    return serialize(raw, BUNDLE_ALLOWLIST)


# -- the owner-only write --------------------------------------------------------------


def default_bundle_path() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return talk_config.state_dir() / f"{BUNDLE_PREFIX}{stamp}.json"


def _verify_owner_only(path: Path) -> None:
    if os.name == "nt":
        sid = talk_setup._windows_current_user_sid()
        if not talk_setup._windows_dacl_grants_only_full_control(
            talk_setup._windows_dacl_sddl(path), sid
        ):
            raise BundleWriteError("bundle DACL verification failed")
        return
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise BundleWriteError("bundle mode verification failed")


def write_bundle(bundle: dict[str, Any], path: Path | None = None) -> Path:
    """Write ``bundle`` owner-only at ``path`` (default: the Talk state dir).

    Same staging shape as the setup wizard's secret files: a unique sibling
    temp is hardened BEFORE any bytes land in it (Windows: closed, DACL
    restricted through ``talk_setup``'s helper, reopened; POSIX: ``0600``
    on the descriptor), fsynced, then moved into place. The destination is
    verified after the move and removed if the verification fails.
    """

    destination = path if path is not None else default_bundle_path()
    destination = Path(destination).expanduser()
    talk_setup._ensure_private_parent(destination.parent)
    payload = (json.dumps(bundle, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.hermes-talk-", suffix=".tmp", dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        if os.name == "nt":
            os.close(fd)
            fd = -1
            talk_setup._windows_restrict_owner_only_dacl(temporary)
            fd = os.open(temporary, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_BINARY", 0))
        elif hasattr(os, "fchmod"):
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        else:  # pragma: no cover - supported targets expose fchmod
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception as exc:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise BundleWriteError(f"could not write the bundle: {type(exc).__name__}") from exc
    try:
        _verify_owner_only(destination)
    except Exception:
        with contextlib.suppress(OSError):
            destination.unlink(missing_ok=True)
        raise
    return destination


# -- rendering and the CLI -----------------------------------------------------------------


def render_human(bundle: dict[str, Any]) -> str:
    versions = bundle.get("versions", {})
    doctor = bundle.get("doctor", {})
    lines = [
        "Hermes Talk diagnostics (redacted: names and outcomes only)",
        "  versions: "
        f"hermes-talk {versions.get('hermes_talk')}, "
        f"hermes-agent {versions.get('hermes_agent') or 'not importable'}, "
        f"python {versions.get('python')}, "
        f"{versions.get('os')} {versions.get('os_release')} {versions.get('machine')}",
        f"  config names: {', '.join(bundle.get('config', {}).get('names', [])) or 'none'}",
        "  environment names: "
        f"{', '.join(bundle.get('environment', {}).get('names', [])) or 'none'}",
    ]
    devices = bundle.get("devices", {})
    lines.append(
        "  devices: "
        f"audio dependency {'yes' if devices.get('audio_dependency_available') else 'no'}, "
        f"inputs {devices.get('input_device_count', '?')}, "
        f"outputs {devices.get('output_device_count', '?')}"
    )
    for check in doctor.get("checks", []):
        lines.append(f"  [{check['status'].upper()}] {check['id']}: {check['summary']}")
    totals = doctor.get("summary", {})
    lines.append(
        f"Doctor: {totals.get('pass', 0)} pass, {totals.get('warn', 0)} warn, "
        f"{totals.get('fail', 0)} fail."
    )
    return "\n".join(lines)


def cli_entry(
    *, json_output: bool = False, write: bool = False, bundle_path: str | None = None
) -> int:
    """Print the redacted bundle (human or JSON), or write it owner-only and say where.

    Exit 1 only when a requested write failed: this is a report, not a
    verdict — doctor's own outcomes are carried inside it, and a bundle that
    documents a broken install is exactly the successful outcome.
    """

    bundle = collect_bundle()
    if not write:
        if json_output:
            print(json.dumps(bundle, indent=2, sort_keys=True))
        else:
            print(render_human(bundle))
            print("Run `hermes talk diagnostics --bundle` to write this as an owner-only file.")
        return 0
    try:
        destination = write_bundle(bundle, Path(bundle_path) if bundle_path else None)
    except Exception as exc:  # noqa: BLE001 - a write failure is the receipt, never a trace
        print(f"diagnostics: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote the redacted support bundle (owner-only): {destination}")
    print(PASTE_HINT)
    return 0


__all__ = [
    "BUNDLE_ALLOWLIST",
    "BUNDLE_PREFIX",
    "CHECK_DETAILS_ALLOWLIST",
    "COMMAND",
    "PASTE_HINT",
    "SCHEMA_VERSION",
    "SHARED_ENV_NAMES",
    "BundleWriteError",
    "cli_entry",
    "collect_bundle",
    "default_bundle_path",
    "render_human",
    "serialize",
    "write_bundle",
]
