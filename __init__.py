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
import os
import shlex

try:
    from . import (
        talk_cli,
        talk_codex_provider,
        talk_config,
        talk_core_provider,
        talk_core_realtime,
        talk_discord,
        talk_host,
        talk_lifecycle,
        talk_progress,
        talk_providers,
        talk_tools,
        talk_transcript,
    )
except ImportError:  # pragma: no cover - flat-module fallback (pip -e install)
    import talk_cli
    import talk_codex_provider
    import talk_config
    import talk_core_provider
    import talk_core_realtime
    import talk_discord
    import talk_host
    import talk_lifecycle
    import talk_progress
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


def _register_core_realtime_providers(ctx) -> None:
    """Publish hermes-talk's three lanes on the Hermes core realtime contract.

    Feature-detected on BOTH sides. Every released Hermes has no such hook,
    and a host may ship a different contract version than the one
    ``talk_core_provider`` targets; either way hermes-talk must load exactly
    as it does today. That path is deliberately silent apart from one debug
    line — an operator running a supported host should never see a warning
    about a surface their Hermes was never expected to have.
    """

    receipt = "core_realtime_providers"
    method = getattr(ctx, "register_realtime_voice_provider", None)
    if not callable(method) or not talk_core_provider.core_contract_available():
        REGISTRATION_RECEIPTS[receipt] = "unsupported-optional"
        logger.debug(
            "hermes-talk: host does not expose the Hermes core realtime voice "
            "contract; core provider registration skipped"
        )
        return

    outcome = "registered"
    for provider in talk_core_provider.build_providers():
        try:
            accepted = method(provider)
        except Exception as exc:  # noqa: BLE001 - isolate this registration surface
            _record("core realtime voice provider", receipt, exc)
            return
        # The host hook returns a registration handle (or None when it
        # refuses); older speculative hosts returned a bare bool. Anything
        # falsy is a refusal, and one refusal makes the whole surface partial.
        if not accepted:
            outcome = "rejected"
    REGISTRATION_RECEIPTS[receipt] = outcome


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
            "gateway also supports pause, resume, leave and status"
        ),
        "args_hint": (
            "[join [TARGET [PEER PROFILE]]|select TARGET|return|reconnect|"
            "say TEXT|pause|resume|leave|status]"
        ),
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

    raw = (raw_args or "").strip()
    sub = raw.lower()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if sub in {"join", "core join", "pause", "resume", "leave", "status"}:
            return (
                "Those are for the gateway's Discord voice channel. Here in a "
                "terminal, plain `/talk` starts the call; the standalone "
                "`hermes talk` command adds Enter to pause and resume the "
                "microphone."
            )
        # This prompt owns the terminal for the whole call (prompt_toolkit,
        # raw mode, its own stdin reader), so the session must not watch
        # stdin for the pause key — and without that key it offers no pause
        # (hermes-talk#100). `hermes talk` on its own is the lane that does.
        return (
            "Voice session ended."
            if talk_cli.cli_entry(keyboard_control=False) == 0
            else ("Voice session ended with errors — see stderr.")
        )

    canonical = talk_discord.native_session_active()
    requested = raw.split(maxsplit=1)[0].lower() if raw else "join"
    explicit_target = requested == "join" and len(raw.split(maxsplit=1)) > 1
    native_join = requested == "join" and (
        explicit_target or os.environ.get("TALK_TASK_TARGET")
        or talk_config.voice_mode() == "live"
    )
    if canonical or native_join or requested in {
        "select", "return", "reconnect", "state", "result", "preference", "interrupt", "say",
    }:
        return _canonical_discord_command(raw, invocation)

    if sub in {"leave", "stop", "hang up"}:
        return talk_discord.stop_session()
    if sub == "status":
        return talk_discord.session_status()
    # The room's microphone control (hermes-talk#100): text, because a paused
    # session hears nobody and the way back cannot be spoken.
    if sub in {"pause", "mute"}:
        return talk_discord.pause_session()
    if sub in {"resume", "unmute"}:
        return talk_discord.resume_session()
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



def _canonical_discord_command(raw, invocation):
    capture = getattr(invocation, "capture_discord_task_context_proof", None)
    if not callable(capture):
        return "Canonical task voice requires an authorized Discord command on a compatible host."
    try:
        context = capture()
        identifiers = {key: int(context[key]) for key in (
            "guild_id", "channel_id", "operator_user_id",
        )}
        if (any(value <= 0 for value in identifiers.values())
                or not isinstance(context["proof"], str)
                or not isinstance(context["anchor_session_id"], str)):
            raise ValueError("Invalid host context")
        parts = shlex.split(raw) if raw else ["join"]
    except Exception:  # noqa: BLE001 - host refusal details and proofs stay private
        return "Canonical task voice was refused: verify the operator and every listener's access."
    operation, arguments = parts[0].lower(), parts[1:]
    if operation == "join":
        if len(arguments) not in {0, 1, 3}:
            return "Use /talk join [TARGET [PEER PROFILE]]."
        if talk_discord.native_session_active():
            return "Canonical task voice is already starting or active; use /talk select or leave."
        peer = arguments[1] if len(arguments) == 3 else "local"
        profile = arguments[2] if len(arguments) == 3 else context.get("profile", "default")
        task = {
            "target_id": (arguments[0] if arguments else
                          os.environ.get("TALK_TASK_TARGET") or context["anchor_session_id"]),
            "peer_id": peer, "profile": profile,
            "tab_id": "discord-" + "-".join(str(value) for value in identifiers.values()),
            "surface_context": {
                "surface": "discord", **identifiers, "surface_token": context["proof"],
                "surface_profile": context.get("profile", "default"),
                "anchor_session_id": context["anchor_session_id"],
            },
        }
        return talk_discord.start_session(guild_id=identifiers["guild_id"], native_task=task)
    return _canonical_discord_control(operation, arguments, identifiers)


async def _canonical_discord_control(operation, arguments, identifiers):
    try:
        if operation in {"leave", "stop"} and not arguments:
            # The existing controller checks its immutable operator and live room before stop.
            await talk_discord.native_command("/state", **identifiers)
            return talk_discord.stop_session()
        aliases = {"mute": "pause", "unmute": "resume", "status": "state"}
        operation = aliases.get(operation, operation)
        if operation == "say":
            if not arguments:
                return "Use /talk say TEXT to send typed input to the selected task."
            command = " ".join(arguments)
        else:
            command = "/" + shlex.join([operation, *arguments])
        result = await talk_discord.native_command(command, **identifiers)
        if operation in {"select", "return", "reconnect"}:
            return ("Task voice connected." if result.get("selected") is True
                    else "Task voice connection was not confirmed; check the selected task.")
        if operation in {"state", "result", "targets"}:
            return (
                "Task access checked. Inspect full task content in the authenticated Talk "
                "dashboard or terminal; this text channel has no private-task audience grant."
            )
        if operation in {"pause", "resume"}:
            return "Microphone paused." if operation == "pause" else "Microphone resumed."
        return "Task command processed. Inspect its receipt in Talk."
    except Exception:  # noqa: BLE001 - task content and credential errors stay out of text channels
        return "Task command was refused or its outcome is unconfirmed. Inspect the original task."


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

    _register_core_realtime_providers(ctx)

    register_worker = getattr(ctx, "register_task_worker_provider", None)
    if callable(register_worker):
        provider = talk_codex_provider.build_provider(ctx)
        if provider is not None:
            try:
                register_worker(provider)
            except Exception as exc:  # noqa: BLE001 - optional registration preserves other surfaces
                logger.warning("Talk task-worker registration failed: %s", type(exc).__name__)

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

    # Progress projection (hermes-talk#33): the per-tool-call and approval
    # hooks are the attached lane's only evidence of executing/blocked work.
    # The host's hook registry is process-scoped, so these register once here
    # and stay inert until a Talk session attaches (talk_progress gates on
    # its own attach state) — registration-time activity would project work
    # belonging to sessions nobody is listening to.
    _attempt_registration(
        ctx,
        "register_hook",
        "post-tool-call hook",
        "post_tool_call_hook",
        "post_tool_call",
        talk_progress.on_post_tool_call,
    )

    _attempt_registration(
        ctx,
        "register_hook",
        "pre-approval hook",
        "pre_approval_request_hook",
        "pre_approval_request",
        talk_progress.on_pre_approval_request,
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
