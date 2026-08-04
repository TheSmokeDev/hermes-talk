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
import logging

try:
    from . import (
        talk_cli,
        talk_discord,
        talk_host,
        talk_lifecycle,
        talk_providers,
        talk_tools,
    )
except ImportError:  # pragma: no cover - flat-module fallback (pip -e install)
    import talk_cli
    import talk_discord
    import talk_host
    import talk_lifecycle
    import talk_providers
    import talk_tools

logger = logging.getLogger(__name__)

#: Registration failures, surfaced by the ``talk_status`` tool. Lives in
#: talk_tools so the tool reads its own module state instead of importing the
#: package back into itself.
REGISTRATION_FAILURES = talk_tools.REGISTRATION_FAILURES


def _record(surface: str, exc: Exception) -> None:
    detail = f"{surface}: {type(exc).__name__}: {exc}"
    REGISTRATION_FAILURES.append(detail)
    logger.warning("hermes-talk could not register %s", detail)


def _talk_command(raw_args: str = "") -> str:
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
        if sub in {"join", "leave", "status"}:
            return (
                "Those are for the gateway's Discord voice channel. Here in a "
                "terminal, plain `/talk` starts the call."
            )
        return "Voice session ended." if talk_cli.cli_entry() == 0 else (
            "Voice session ended with errors — see stderr."
        )

    if sub in {"leave", "stop", "hang up"}:
        return talk_discord.stop_session()
    if sub == "status":
        return talk_discord.session_status()
    if sub in {"", "join"}:
        return talk_discord.start_session()
    return talk_discord.JOIN_USAGE


def _on_session_end(**kwargs) -> None:
    """Session-end hook. No durable state to tear down yet.

    TODO: flush the call's transcript into Hermes memory here
    (roadmap: session-end memory debrief).
    """


def register(ctx) -> None:
    """Called once by the plugin loader when hermes-talk is enabled."""

    talk_host.bind_ctx(ctx)

    try:
        ctx.register_cli_command(
            name="talk",
            help="Realtime duplex voice session",
            setup_fn=talk_cli.setup_cli,
            handler_fn=talk_cli.cli_entry,
            description=(
                "Talk to Hermes over the OpenAI Realtime API: speech in, speech "
                "out, interrupt it mid-sentence, and its tool calls run live."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — one dead surface, not four
        _record("cli command", exc)

    try:
        ctx.register_command(
            "talk",
            handler=_talk_command,
            description="Start a Realtime voice session (gateway: join|leave|status)",
            args_hint="[join|leave|status]",
        )
    except Exception as exc:  # noqa: BLE001
        _record("slash command", exc)

    try:
        ctx.register_hook("on_session_end", _on_session_end)
    except Exception as exc:  # noqa: BLE001
        _record("session-end hook", exc)

    # Push-based child lifecycle (v0.6): ledger degrades and in-call
    # announcements ride the host's own subagent hooks instead of waiting
    # for the next check_work sweep. Each registration guarded on its own —
    # a host without these hook names keeps every other surface.
    try:
        ctx.register_hook("subagent_start", talk_lifecycle.on_subagent_start)
    except Exception as exc:  # noqa: BLE001
        _record("subagent-start hook", exc)

    try:
        ctx.register_hook("subagent_stop", talk_lifecycle.on_subagent_stop)
    except Exception as exc:  # noqa: BLE001
        _record("subagent-stop hook", exc)

    if talk_providers.providers_available():
        try:
            ctx.register_tts_provider(talk_providers.OpenAITTSProvider())
        except Exception as exc:  # noqa: BLE001
            _record("tts provider", exc)
        try:
            ctx.register_transcription_provider(talk_providers.OpenAITranscriptionProvider())
        except Exception as exc:  # noqa: BLE001
            _record("transcription provider", exc)


__all__ = ["REGISTRATION_FAILURES", "register"]
