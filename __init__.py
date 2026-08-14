"""hermes-talk — OpenAI Realtime speech-to-speech voice for Hermes Agent.

``register(ctx)`` wires five surfaces: the ``hermes talk`` CLI command, the
``/talk`` slash command, lifecycle hooks (session end plus the v0.6
subagent start/stop pair that powers push-based run control), and (when the
host exposes the provider ABCs) OpenAI TTS/STT backends.

Each registration is guarded on its own. One surface Hermes does not expose —
an older host, a partial install — must not take the other three down with it;
the failure is recorded and ``talk_status`` says so out loud instead of the
plugin looking healthy while half of it is missing.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

try:
    from . import (
        talk_cli,
        talk_config,
        talk_core_realtime,
        talk_discord,
        talk_host,
        talk_lifecycle,
        talk_providers,
        talk_tools,
        talk_transcript,
    )
except ImportError:  # pragma: no cover - flat-module fallback (pip -e install)
    import talk_cli
    import talk_config
    import talk_core_realtime
    import talk_discord
    import talk_host
    import talk_lifecycle
    import talk_providers
    import talk_tools
    import talk_transcript

logger = logging.getLogger(__name__)

#: Registration failures, surfaced by the ``talk_status`` tool. Lives in
#: talk_tools so the tool reads its own module state instead of importing the
#: package back into itself.
REGISTRATION_FAILURES = talk_tools.REGISTRATION_FAILURES
REGISTRATION_RECEIPTS = talk_tools.REGISTRATION_RECEIPTS


def _record(surface: str, receipt: str, exc: Exception) -> None:
    # Host exceptions are untrusted and may contain config values. The receipt
    # needs the surface and exception class, never the raw message.
    detail = f"{surface}: {type(exc).__name__}"
    REGISTRATION_FAILURES.append(detail)
    REGISTRATION_RECEIPTS[receipt] = "failed"
    logger.warning("hermes-talk could not register %s", detail)


def _registered(receipt: str) -> None:
    REGISTRATION_RECEIPTS[receipt] = "registered"


def _unsupported(surface: str, receipt: str) -> None:
    requirement = talk_tools.REGISTRATION_REQUIREMENTS[receipt]
    REGISTRATION_RECEIPTS[receipt] = f"unsupported-{requirement}"
    logger.info("hermes-talk host has no %s registration method", surface)


def _attempt_registration(
    ctx,
    method_name: str,
    surface: str,
    receipt: str,
    *args,
    **kwargs,
) -> None:
    """Call a present host method; absence and implementation errors differ."""

    method = getattr(ctx, method_name, None)
    if not callable(method):
        _unsupported(surface, receipt)
        return
    try:
        method(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - isolate one registration surface
        _record(surface, receipt, exc)
    else:
        _registered(receipt)


def _attempt_boolean_registration(ctx, method_name: str, surface: str, receipt: str, *args) -> None:
    """Record exact acceptance for host registries with boolean receipts."""

    method = getattr(ctx, method_name, None)
    if not callable(method):
        _unsupported(surface, receipt)
        return
    try:
        accepted = method(*args)
    except Exception as exc:  # noqa: BLE001 - isolate optional registration
        _record(surface, receipt, exc)
        return
    REGISTRATION_RECEIPTS[receipt] = "registered" if accepted is True else "rejected"


def _register_talk_command(ctx) -> None:
    """Opt into invocation capture when supported, preserving older hosts."""

    method = getattr(ctx, "register_command", None)
    contextual = False
    if callable(method):
        try:
            parameters = inspect.signature(method).parameters.values()
            contextual = any(
                parameter.name == "invocation_context"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            contextual = False
    kwargs = {
        "handler": _talk_command,
        "description": (
            "Start provider-owned voice with canonical Hermes tools (join), or the "
            "canonical core voice lane (core join); "
            "gateway also supports leave and status"
        ),
        "args_hint": "[join|core join|leave|status]",
    }
    if contextual:
        kwargs["invocation_context"] = True
    _attempt_registration(
        ctx,
        "register_command",
        "slash command",
        "slash_command",
        "talk",
        **kwargs,
    )


def _talk_command(raw_args: str = "", invocation=None) -> str:
    """``/talk`` — start a voice session from inside a Hermes session.

    Two rooms, one command. Outside an event loop (a terminal session) the
    call owns the terminal: microphone in, speaker out. Inside the gateway
    it runs in the Discord voice channel the host is already sitting in —
    ``join`` / ``leave`` / ``status`` — because a duplex call cannot own a
    terminal that nobody is looking at, and the gateway has a better room.
    """

    sub = (raw_args or "").strip().lower()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if sub in {"join", "core join", "leave", "status"}:
            return (
                "Those are for the gateway's Discord voice channel. Here in a "
                "terminal, plain `/talk` starts the call."
            )
        return (
            "Voice session ended."
            if talk_cli.cli_entry() == 0
            else ("Voice session ended with errors — see stderr.")
        )

    if sub in {"leave", "stop", "hang up"}:
        return talk_discord.stop_session()
    if sub == "status":
        return talk_discord.session_status()
    if sub == "core join":
        if not talk_core_realtime.core_provider_available():
            return "Canonical core voice is unsupported by this Hermes host."
        capture = getattr(invocation, "capture_realtime_voice_attachment_factory", None)
        if not callable(capture):
            return "Canonical core voice requires a host-authorized command invocation."
        try:
            factory = capture()
        except Exception as exc:  # noqa: BLE001 - capability refusal is speakable
            return f"Canonical core voice was refused by the host ({type(exc).__name__})."
        return talk_discord.start_core_session(factory)
    if sub in {"", "join"}:
        capture = getattr(invocation, "capture_realtime_execution_attachment", None)
        if callable(capture):
            try:
                attachment = capture()
            except Exception as exc:  # noqa: BLE001 - capability refusal is speakable
                return f"Provider-owned voice was refused by the host ({type(exc).__name__})."
            return talk_discord.start_session(host_execution_attachment=attachment)
        return talk_discord.start_session()
    return talk_discord.JOIN_USAGE


def _on_session_end(**kwargs) -> None:
    """Flush completed Talk transcripts without affecting host teardown."""

    del kwargs
    try:
        talk_transcript.sweep_transcripts(talk_config.get_hermes_home())
    except Exception as exc:  # noqa: BLE001 - a memory debrief never breaks teardown
        logger.warning("Talk transcript session-end sweep failed: %s: %s", type(exc).__name__, exc)


def register(ctx) -> None:
    """Called once by the plugin loader when hermes-talk is enabled."""

    REGISTRATION_FAILURES.clear()
    REGISTRATION_RECEIPTS.clear()
    talk_host.bind_ctx(ctx)

    if talk_core_realtime.core_provider_available():
        _attempt_boolean_registration(
            ctx,
            "register_realtime_voice_provider",
            "realtime voice provider",
            "realtime_voice_provider",
            talk_core_realtime.TalkOpenAIRealtimeProvider(),
        )
    else:
        _unsupported("realtime voice provider", "realtime_voice_provider")

    _attempt_registration(
        ctx,
        "register_cli_command",
        "cli command",
        "cli_command",
        name="talk",
        help="Realtime duplex voice session",
        setup_fn=talk_cli.setup_cli,
        handler_fn=talk_cli.cli_entry,
        description=(
            "Talk to Hermes over the OpenAI Realtime API: speech in, speech "
            "out, interrupt it mid-sentence, and its tool calls run live."
        ),
    )

    _register_talk_command(ctx)

    _attempt_registration(
        ctx,
        "register_hook",
        "session-end hook",
        "session_end_hook",
        "on_session_end",
        _on_session_end,
    )

    # Push-based child lifecycle (v0.6): ledger degrades and in-call
    # announcements ride the host's own subagent hooks instead of waiting
    # for the next check_work sweep. Each registration guarded on its own —
    # a host without these hook names keeps every other surface.
    _attempt_registration(
        ctx,
        "register_hook",
        "subagent-start hook",
        "subagent_start_hook",
        "subagent_start",
        talk_lifecycle.on_subagent_start,
    )

    _attempt_registration(
        ctx,
        "register_hook",
        "subagent-stop hook",
        "subagent_stop_hook",
        "subagent_stop",
        talk_lifecycle.on_subagent_stop,
    )

    if talk_providers.providers_available():
        _attempt_registration(
            ctx,
            "register_tts_provider",
            "tts provider",
            "tts_provider",
            talk_providers.OpenAITTSProvider(),
        )
        _attempt_registration(
            ctx,
            "register_transcription_provider",
            "transcription provider",
            "transcription_provider",
            talk_providers.OpenAITranscriptionProvider(),
        )
    else:
        _unsupported("tts provider", "tts_provider")
        _unsupported("transcription provider", "transcription_provider")


__all__ = ["REGISTRATION_FAILURES", "REGISTRATION_RECEIPTS", "register"]
