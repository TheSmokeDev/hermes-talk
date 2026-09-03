"""Read-only diagnostics for the native ``hermes talk doctor`` command.

The design borrows TaskChad's useful operational loop without importing its
product assumptions: detect local state, decide a bounded status/remediation,
then emit a verification receipt. There is intentionally no write phase here.
Doctor never refreshes OAuth, creates directories, starts audio, probes a
sidecar, edits setup, or exposes the content it inspects.
"""

from __future__ import annotations

import asyncio
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
        talk_core_realtime,
        talk_grok_auth,
        talk_grok_realtime,
        talk_host,
        talk_identity,
        talk_tools,
    )
except ImportError:  # pragma: no cover - flat-module fallback (pip -e install)
    import talk_audio
    import talk_auth
    import talk_config
    import talk_core_realtime
    import talk_grok_auth
    import talk_grok_realtime
    import talk_host
    import talk_identity
    import talk_tools

SCHEMA_VERSION = 1
COMMAND = "hermes talk doctor"
CHECK_ORDER = (
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
)
HOST_CAPABILITIES = (
    "register_cli_command",
    "register_command",
    "register_hook",
    "register_tts_provider",
    "register_transcription_provider",
    "register_realtime_voice_provider",
    "dispatch_tool",
)
SECRET_PATTERNS = (
    re.compile(r"(?i)(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(?<![A-Za-z0-9])(?:github_pat_|gh[pousr]_)[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
)


def redact_text(value: str) -> str:
    """Scrub secret-shaped substrings out of one string.

    Public because the capability catalog reports upstream-supplied skill and
    toolset payloads, which is the same class of "text this process did not
    write, about to be emitted" problem doctor already solved. One pattern
    list beats two that drift apart.
    """

    for pattern in SECRET_PATTERNS:
        value = pattern.sub("<redacted-secret>", value)
    return value


def redact_value(value: Any) -> Any:
    """Recursively scrub secret-shaped strings at the report boundary."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
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
        "summary": redact_text(summary),
        "details": redact_value(details),
        "remediation": [redact_text(action) for action in remediation],
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
    core_readiness = talk_core_realtime.core_provider_diagnostic()
    details = {
        "context_bound": context_bound,
        "failure_count": len(talk_tools.REGISTRATION_FAILURES),
        "required_issue_count": len(required_issues),
        "optional_issue_count": len(optional_issues),
        "requirements": {receipt: requirements.get(receipt, "required") for receipt in receipts},
        "surfaces": receipts,
        "legacy_lane": "legacy-provider-executor",
        "core_realtime_contract": "api-v2-input-only",
        "core_contract_available": core_readiness["contract_available"],
        "core_provider_available": core_readiness["provider_available"],
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


def _key_presence(value: str | None) -> str:
    """A secret-free key receipt: absent, present, or blank — never the value."""

    if value is None:
        return "absent"
    return "present" if value.strip() else "blank"


def _provider_check() -> dict[str, Any]:
    """The selected realtime provider, plus its lane's readiness when chosen.

    Read-only and offline: key PRESENCE only (redacted by construction), and
    model/voice validity against the local fail-closed lists. No live probe.
    """

    raw = os.environ.get("TALK_PROVIDER")
    source = "TALK_PROVIDER" if (raw or "").strip() else "default"
    try:
        provider = talk_config.talk_provider()
    except talk_config.TalkConfigError:
        return _check(
            "provider",
            "fail",
            "configured realtime provider is not supported",
            {"provider": (raw or "").strip().lower() or None, "source": source},
            ("Set TALK_PROVIDER to openai, grok, or gemini, or unset it.",),
        )
    details: dict[str, Any] = {"provider": provider, "source": source}
    if provider == "openai":
        # The auth/model/voice checks below carry the OpenAI lane as before.
        return _check("provider", "pass", "openai is the selected realtime provider", details)

    if provider == "gemini":
        scoped = os.environ.get("TALK_GEMINI_API_KEY")
        shared = os.environ.get("GEMINI_API_KEY")
        keys = {
            "scoped": _key_presence(scoped),
            "shared": _key_presence(shared),
        }
        voice_raw = os.environ.get("TALK_GEMINI_VOICE")
        model_raw = os.environ.get("TALK_GEMINI_MODEL")
        details.update(
            {
                "keys": keys,
                "model": talk_config.talk_gemini_model(),
                "model_source": "TALK_GEMINI_MODEL" if (model_raw or "").strip() else "default",
                "voice_source": "TALK_GEMINI_VOICE" if (voice_raw or "").strip() else "default",
            }
        )
        try:
            details["voice"] = talk_config.talk_gemini_voice()
            voice_valid = True
        except talk_config.TalkConfigError:
            # Live voices are case-sensitive; the raw value is reported as-is,
            # never case-folded into something that looks valid.
            details["voice"] = (voice_raw or "").strip() or None
            voice_valid = False
        details["voice_valid"] = voice_valid

        if keys["scoped"] == "blank" or (scoped is None and keys["shared"] == "blank"):
            return _check(
                "provider",
                "fail",
                "a Gemini key variable is set but blank and refuses closed",
                details,
                (
                    "Set a real key in TALK_GEMINI_API_KEY or GEMINI_API_KEY, "
                    "or unset the blank one.",
                ),
            )
        if "present" not in keys.values():
            return _check(
                "provider",
                "fail",
                "provider gemini is selected but no Gemini key is configured",
                details,
                ("Set TALK_GEMINI_API_KEY or GEMINI_API_KEY.",),
            )
        if not voice_valid:
            return _check(
                "provider",
                "fail",
                "configured Gemini voice is not a built-in Gemini Live voice",
                details,
                (
                    f"Choose a TALK_GEMINI_VOICE from: {', '.join(talk_config.GEMINI_LIVE_VOICES)} "
                    "(case-sensitive).",
                ),
            )
        return _check(
            "provider",
            "pass",
            f"gemini realtime provider is configured (model {details['model']}, "
            f"voice {details['voice']})",
            details,
        )

    scoped = os.environ.get("TALK_XAI_API_KEY")
    shared = os.environ.get("XAI_API_KEY")
    keys = {
        "scoped": _key_presence(scoped),
        "shared": _key_presence(shared),
    }
    voice_raw = os.environ.get("TALK_GROK_VOICE")
    model_raw = os.environ.get("TALK_GROK_MODEL")
    details.update(
        {
            "keys": keys,
            "model": talk_config.talk_grok_model(),
            "model_source": "TALK_GROK_MODEL" if (model_raw or "").strip() else "default",
            "voice_source": "TALK_GROK_VOICE" if (voice_raw or "").strip() else "default",
        }
    )
    try:
        details["voice"] = talk_config.talk_grok_voice()
        voice_valid = True
    except talk_config.TalkConfigError:
        details["voice"] = (voice_raw or "").strip().lower() or None
        voice_valid = False
    details["voice_valid"] = voice_valid

    # Read-only lane receipt: the host resolver (which may refresh and write
    # the store) is never called from doctor. The auth check owns the lane
    # verdict; the provider check only says whether ANY lane exists.
    receipt = talk_grok_auth.grok_auth_diagnostic()
    details["auth_lane"] = receipt["winning_lane"]
    details["xai_oauth"] = receipt["xai_oauth"]
    if receipt["blocked_by"] in ("blank-talk-key", "blank-xai-key"):
        return _check(
            "provider",
            "fail",
            "an xAI key variable is set but blank and refuses closed",
            details,
            ("Set a real key in TALK_XAI_API_KEY or XAI_API_KEY, or unset the blank one.",),
        )
    if receipt["blocked_by"] == "no-usable-auth":
        return _check(
            "provider",
            "fail",
            "provider grok is selected but no xAI key or xAI OAuth login is configured",
            details,
            (
                "Set TALK_XAI_API_KEY or XAI_API_KEY, or run `hermes auth add xai-oauth` "
                "(SuperGrok / X Premium+ subscription).",
            ),
        )
    if not voice_valid:
        return _check(
            "provider",
            "fail",
            "configured Grok voice is not a built-in Grok voice",
            details,
            (f"Choose a TALK_GROK_VOICE from: {', '.join(talk_config.GROK_REALTIME_VOICES)}.",),
        )
    return _check(
        "provider",
        "pass",
        f"grok realtime provider is configured (model {details['model']}, "
        f"voice {details['voice']})",
        details,
    )


def _grok_auth_check() -> dict[str, Any]:
    """The Grok lane's auth verdict, in the same envelope as the OpenAI one.

    Mirrors ``_auth_check`` field for field (``xai_oauth`` where that one says
    ``codex_oauth``) so the human renderer and any machine consumer can key on
    which name is present instead of on the provider.
    """

    receipt = talk_grok_auth.grok_auth_diagnostic()
    details = dict(receipt)
    lane = receipt["winning_lane"]
    remediation: list[str] = []
    relogin = f"Run `{talk_grok_auth.RELOGIN_COMMAND}`"

    if receipt["blocked_by"] == "invalid-preference":
        remediation.append("Set TALK_PREFER_XAI_OAUTH to true or false, or unset it.")
        return _check(
            "auth",
            "fail",
            "auth selection is blocked by an invalid xAI OAuth preference",
            details,
            remediation,
        )
    if lane is None:
        if receipt["blocked_by"] == "blank-talk-key":
            remediation.append("Set a real TALK_XAI_API_KEY or unset it.")
            summary = "TALK_XAI_API_KEY is set but blank and blocks later auth lanes"
        elif receipt["blocked_by"] == "blank-xai-key":
            remediation.append("Set a real XAI_API_KEY or unset it.")
            summary = "XAI_API_KEY is set but blank and blocks xAI OAuth"
        elif receipt["preference"] == "enabled":
            remediation.append(f"{relogin} or unset TALK_PREFER_XAI_OAUTH.")
            summary = "xAI OAuth is required but no usable login was found; API keys are ignored"
        elif receipt["xai_oauth"] in ("invalid", "expired"):
            remediation.append(f"{relogin} to replace the {receipt['xai_oauth']} login.")
            summary = f"the xAI OAuth login is {receipt['xai_oauth']} and no API key is set"
        else:
            remediation.append(f"Set TALK_XAI_API_KEY or XAI_API_KEY, or {relogin.lower()}.")
            summary = "no usable Grok authentication lane was found"
        return _check("auth", "fail", summary, details, remediation)

    if receipt["refresh_required"]:
        remediation.append(
            f"{relogin} if the host's session-start OAuth refresh does not succeed."
        )
        return _check(
            "auth",
            "warn",
            "xAI OAuth wins but needs a refresh; doctor did not refresh or write it",
            details,
            remediation,
        )
    if receipt["metered_key_wins_over_oauth"]:
        remediation.append(
            "Set TALK_PREFER_XAI_OAUTH=true to require the xAI subscription login and "
            "refuse metered key fallback."
        )
        summary = f"{lane} wins by legacy precedence while a usable xAI OAuth login is available"
        return _check("auth", "warn", summary, details, remediation)
    if receipt["metered_keys_ignored"]:
        summary = "xAI OAuth wins by explicit preference; configured API key lanes are ignored"
    else:
        summary = f"{lane} is the winning auth lane"
    return _check("auth", "pass", summary, details)


def _auth_check() -> dict[str, Any]:
    try:
        provider = talk_config.talk_provider()
    except talk_config.TalkConfigError:
        provider = None
    if provider == "grok":
        return _grok_auth_check()
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
            remediation.append("Set TALK_OPENAI_API_KEY or OPENAI_API_KEY, or run `codex login`.")
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


def _cascade_check() -> dict[str, Any]:
    """The custom-voice cascade lane: mode, TTS provider, key and voice presence.

    Read-only and offline, same as the provider check: key PRESENCE only
    (redacted by construction — the value never enters the report), the voice
    id as the semi-public identifier it is, and no live ElevenLabs probe.
    """

    raw_mode = os.environ.get("TALK_VOICE_MODE")
    source = "TALK_VOICE_MODE" if (raw_mode or "").strip() else "default"
    try:
        mode = talk_config.voice_mode()
    except talk_config.TalkConfigError:
        return _check(
            "cascade",
            "fail",
            "configured voice mode is not supported",
            {"voice_mode": (raw_mode or "").strip().lower() or None, "source": source},
            (f"Set TALK_VOICE_MODE to {' or '.join(talk_config.TALK_VOICE_MODES)}, or unset it.",),
        )
    if mode == "native":
        return _check(
            "cascade",
            "pass",
            "voice mode is native; the cascade lane is inactive",
            {"voice_mode": mode, "source": source},
        )

    scoped = os.environ.get("TALK_ELEVENLABS_API_KEY")
    shared = os.environ.get("ELEVENLABS_API_KEY")
    keys = {"scoped": _key_presence(scoped), "shared": _key_presence(shared)}
    voice_id_raw = os.environ.get("TALK_ELEVENLABS_VOICE_ID")
    voice_id = (voice_id_raw or "").strip() or None
    details: dict[str, Any] = {
        "voice_mode": mode,
        "source": source,
        "keys": keys,
        "voice_id": voice_id,
        "model": talk_config.elevenlabs_model(),
    }
    try:
        details["tts"] = talk_config.cascade_tts()
    except talk_config.TalkConfigError:
        details["tts"] = (os.environ.get("TALK_CASCADE_TTS") or "").strip().lower() or None
        return _check(
            "cascade",
            "fail",
            "configured cascade TTS provider is not supported",
            details,
            ("Set TALK_CASCADE_TTS to elevenlabs, or unset it.",),
        )
    try:
        provider = talk_config.talk_provider()
    except talk_config.TalkConfigError:
        provider = None  # the provider check already reports that failure
    details["provider"] = provider
    if provider is not None and provider != "openai":
        return _check(
            "cascade",
            "fail",
            f"cascade voice mode requires the openai provider, not {provider}",
            details,
            ("Set TALK_PROVIDER=openai for cascade mode, or use TALK_VOICE_MODE=native.",),
        )
    if keys["scoped"] == "blank" or (scoped is None and keys["shared"] == "blank"):
        return _check(
            "cascade",
            "fail",
            "an ElevenLabs key variable is set but blank and refuses closed",
            details,
            (
                "Set a real key in TALK_ELEVENLABS_API_KEY or ELEVENLABS_API_KEY, "
                "or unset the blank one.",
            ),
        )
    if "present" not in keys.values():
        return _check(
            "cascade",
            "fail",
            "cascade voice mode is selected but no ElevenLabs key is configured",
            details,
            ("Set TALK_ELEVENLABS_API_KEY or ELEVENLABS_API_KEY.",),
        )
    if voice_id is None:
        return _check(
            "cascade",
            "fail",
            "cascade voice mode needs a voice id and none is configured",
            details,
            (
                "Set TALK_ELEVENLABS_VOICE_ID to a voice id from your ElevenLabs "
                "account (VoiceLab -> your voice -> ID).",
            ),
        )
    return _check(
        "cascade",
        "pass",
        f"cascade voice mode is configured (tts {details['tts']}, model {details['model']})",
        details,
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
        str(name).upper(): {"characters": len(talk_identity.cap_section(str(name), body))}
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
        _provider_check(),
        _auth_check(),
        _model_check(),
        _voice_check(),
        _cascade_check(),
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

    report = redact_value(report)
    lines = ["Hermes Talk doctor (read-only)"]
    for check in report["checks"]:
        lines.append(f"[{check['status'].upper()}] {check['id']}: {check['summary']}")
        details = check["details"]
        if check["id"] == "auth":
            if "xai_oauth" in details:
                oauth = f"xai-oauth={details['xai_oauth']}"
                # Name the store shape that answered, so a "missing" verdict
                # can be told apart from a login the parse never reached.
                if details.get("xai_oauth_source"):
                    oauth += f" (via {details['xai_oauth_source']})"
            else:
                oauth = f"codex={details['codex_oauth']}"
            lines.append(
                "  receipt: "
                f"winner={details['winning_lane'] or 'none'}, "
                f"{oauth}, preference={details['preference']}"
            )
        elif check["id"] == "provider" and details.get("provider") in ("grok", "gemini"):
            keys = details.get("keys", {})
            lines.append(
                "  receipt: "
                f"model={details.get('model')}, voice={details.get('voice')}, "
                f"key-scoped={keys.get('scoped')}, key-shared={keys.get('shared')}"
            )
        elif check["id"] == "cascade" and details.get("voice_mode") == "cascade":
            keys = details.get("keys", {})
            lines.append(
                "  receipt: "
                f"tts={details.get('tts')}, model={details.get('model')}, "
                f"voice-id={details.get('voice_id') or 'unset'}, "
                f"key-scoped={keys.get('scoped')}, key-shared={keys.get('shared')}"
            )
        elif check["id"] == "identity":
            profile = details["active_profile"] or "root"
            counts = (
                ", ".join(
                    f"{name}:{receipt['characters']}"
                    for name, receipt in details["sections"].items()
                )
                or "none"
            )
            lines.append(
                "  receipt: "
                f"home={details['resolved_home']} ({details['home_source']}), "
                f"root={details['root']} ({details['root_source']}), "
                f"profile={profile} ({details['profile_source']}), sections={counts}"
            )
        elif check["id"] == "plugin":
            surfaces = (
                ", ".join(f"{name}={state}" for name, state in details["surfaces"].items())
                or "unobserved"
            )
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
    lines.append(f"Summary: {totals['pass']} pass, {totals['warn']} warn, {totals['fail']} fail.")
    lines.append("No files, credentials, services, or setup were changed.")
    return "\n".join(lines)


PROBE_HTTP_URL = "https://api.x.ai/v1/realtime/client_secrets"
PROBE_TIMEOUT_S = 20.0


async def run_grok_probe(
    *,
    auth_token: str,
    auth_source: str,
    model: str,
    aiohttp_module=None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Two live calls that prove the resolved bearer reaches Grok realtime.

    The subscription question nobody can answer offline: does an ``xai-oauth``
    access token work on ``wss://api.x.ai/v1/realtime``? This asks xAI. The
    receipt carries status codes and the first server event type only — never
    the token, never a store path. Opt-in (``--probe``), grok only.
    """

    if timeout_s is None:
        timeout_s = PROBE_TIMEOUT_S
    aiohttp = aiohttp_module or talk_grok_realtime._import_aiohttp()
    headers = {"Authorization": f"Bearer {auth_token}"}
    result: dict[str, Any] = {
        "auth_source": auth_source,
        "model": model,
        "http_status": None,
        "ws_status": None,
        "first_event": None,
        "error": None,
        "ok": False,
    }
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                PROBE_HTTP_URL,
                json={"model": model},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as response:
                result["http_status"] = int(response.status)
            async with http.ws_connect(
                f"{talk_grok_realtime.XAI_REALTIME_WS_URL}?model={model}",
                headers=headers,
                timeout=timeout_s,
            ) as ws:
                result["ws_status"] = 101
                message = await asyncio.wait_for(ws.receive(), timeout_s)
                data = getattr(message, "data", None)
                if isinstance(data, str):
                    try:
                        event = json.loads(data)
                    except ValueError:
                        event = None
                    if isinstance(event, dict):
                        result["first_event"] = str(event.get("type") or "") or None
    except Exception as exc:  # noqa: BLE001 - a probe reports failure, never raises
        remediation = talk_grok_realtime.handshake_remediation(exc, auth_source=auth_source)
        status = getattr(exc, "status", None)
        if isinstance(status, int) and result["ws_status"] is None:
            result["ws_status"] = status
        result["error"] = remediation or f"{type(exc).__name__}: {redact_text(str(exc))}"
        return result
    result["ok"] = result["http_status"] == 200 and result["first_event"] == "session.created"
    if not result["ok"] and result["error"] is None:
        result["error"] = "unexpected probe receipt (see http_status / first_event)"
    return result


def _probe_grok() -> dict[str, Any]:
    """Resolve the lane (this MAY let the host refresh its own store) and probe."""

    try:
        provider = talk_config.talk_provider()
    except talk_config.TalkConfigError:
        provider = None
    if provider != "grok":
        return {"ok": False, "error": "the live probe exists for TALK_PROVIDER=grok only"}
    try:
        auth = talk_grok_auth.resolve_grok_auth()
    except talk_auth.TalkAuthError as exc:
        return {"ok": False, "error": redact_text(str(exc))}
    return asyncio.run(
        run_grok_probe(
            auth_token=auth.token,
            auth_source=auth.source,
            model=talk_config.talk_grok_model(),
        )
    )


def render_probe(result: dict[str, Any]) -> str:
    result = redact_value(result)
    lines = ["Hermes Talk probe (live: two calls to api.x.ai)"]
    if "auth_source" in result:
        lines.append(f"  auth lane: {result['auth_source']}, model: {result['model']}")
        http = result["http_status"] or "no response"
        lines.append(f"  POST /v1/realtime/client_secrets -> {http}")
        ws = result["ws_status"] or "no handshake"
        lines.append(f"  WS /v1/realtime -> {ws}, first event: {result['first_event'] or 'none'}")
    if result.get("error"):
        lines.append(f"  -> {result['error']}")
    verdict = "PASS: the resolved bearer reaches Grok realtime" if result["ok"] else "FAIL"
    lines.append(f"Probe: {verdict}.")
    return "\n".join(lines)


def cli_entry(*, json_output: bool = False, probe: bool = False) -> int:
    """Print one doctor report and return nonzero only for failing checks.

    ``probe`` is the one path that leaves the machine: it runs only when asked.
    """

    report = redact_value(collect_report())
    probe_result = redact_value(_probe_grok()) if probe else None
    if json_output:
        if probe_result is not None:
            report = dict(report, probe=probe_result)
        print(json.dumps(report, sort_keys=True))
    else:
        print(render_human(report))
        if probe_result is not None:
            print(render_probe(probe_result))
    ok = report["ok"] and (probe_result is None or probe_result["ok"])
    return 0 if ok else 1


__all__ = [
    "CHECK_ORDER",
    "COMMAND",
    "SCHEMA_VERSION",
    "SECRET_PATTERNS",
    "cli_entry",
    "collect_report",
    "redact_text",
    "redact_value",
    "render_human",
    "render_probe",
    "run_grok_probe",
]
