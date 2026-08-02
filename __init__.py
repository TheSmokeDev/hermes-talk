"""hermes-talk — OpenAI Realtime speech-to-speech voice for Hermes Agent.

``register(ctx)`` wires four surfaces: the ``hermes talk`` CLI command, the
``/talk`` slash command, a session-end hook, and (when the host exposes the
provider ABCs) OpenAI TTS/STT backends.

Each registration is guarded on its own. One surface Hermes does not expose —
an older host, a partial install — must not take the other three down with it;
the failure is recorded and ``talk_status`` says so out loud instead of the
plugin looking healthy while half of it is missing.
"""

from __future__ import annotations

import asyncio
import logging

try:
    from . import talk_cli, talk_host, talk_providers, talk_tools
except ImportError:  # pragma: no cover - flat-module fallback (pip -e install)
    import talk_cli
    import talk_host
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

    Duplex audio owns a terminal for as long as the call lasts, so it only
    runs when nothing else owns the loop. Inside an async host (the gateway)
    it says where to run it instead of blocking the event loop for minutes.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return "Voice session ended." if talk_cli.cli_entry() == 0 else (
            "Voice session ended with errors — see stderr."
        )
    return "Voice needs its own terminal: run `hermes talk` in a shell."


def _on_session_end(**kwargs) -> None:
    """Session-end hook. v0.1 has no durable state to tear down.

    TODO(v0.4): flush the call's transcript into Hermes memory here.
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
            description="Start a Realtime voice session",
            args_hint="",
        )
    except Exception as exc:  # noqa: BLE001
        _record("slash command", exc)

    try:
        ctx.register_hook("on_session_end", _on_session_end)
    except Exception as exc:  # noqa: BLE001
        _record("session-end hook", exc)

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
