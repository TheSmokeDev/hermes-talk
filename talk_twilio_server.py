"""EXPERIMENTAL: a standalone Twilio phone server for Hermes Talk.

A separate, opt-in process for self-hosters. It is not the built-in Phone mode
(that is Dograh, ``hermes talk phone``), pressing Talk never starts it, and
nothing in the plugin imports it. Each inbound call to your own Twilio number
gets a fresh Gemini Live voice session: no Hermes task, no tools, no memory.
Setup and limits: docs/TWILIO.md.

Only the Gemini Live lane is wired up, because its server-side endpointing is
what makes a phone line work without local VAD. Other providers raise a clear
error instead of half-working.

    TWILIO_AUTH_TOKEN=... TALK_PUBLIC_BASE_URL=https://voice.example.com \\
    TALK_TWILIO_ALLOWED_CALLERS=+15551234567 GEMINI_API_KEY=... \\
    python -m talk_twilio_server --check
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Callable, Mapping

try:
    from . import talk_config
    from . import talk_realtime as rt
    from . import talk_twilio as twilio
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_config
    import talk_realtime as rt
    import talk_twilio as twilio

EXPERIMENTAL_NOTE = (
    "EXPERIMENTAL: standalone Twilio phone server for Hermes Talk. Not the built-in "
    "Phone mode (Dograh); each call gets a fresh Gemini Live voice session with no tools."
)
DEFAULT_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_VOICE = "Kore"
DEFAULT_INSTRUCTIONS = (
    "You are Hermes, answering a phone call. Say you are an AI assistant near the "
    "start of the call. Keep replies short and spoken-friendly: this line is 8 kHz "
    "and the caller cannot see text. You cannot take actions on this call; if the "
    "caller asks for something that needs one, say plainly that you can't do it here."
)
#: At most this many calls at once; any more are declined unanswered.
MAX_CALLS = 2
#: Hard ceiling per call, so a stuck line cannot run up the provider bill.
MAX_CALL_MINUTES = 15


class ConfigError(RuntimeError):
    """Environment is not usable for a call server."""


def config_from_env(environ: Mapping[str, str] | None = None) -> twilio.TwilioConfig:
    # Secrets resolve through Hermes's profile-scoped get_secret (catalog rule 11);
    # without Hermes this is the process environment.
    env = talk_config.SECRET_ENV if environ is None else environ
    base_url = (env.get("TALK_PUBLIC_BASE_URL") or "").strip()
    if not base_url:
        raise ConfigError("TALK_PUBLIC_BASE_URL is required (the public https origin)")
    auth_token = (env.get("TWILIO_AUTH_TOKEN") or "").strip()
    if not auth_token:
        raise ConfigError("TWILIO_AUTH_TOKEN is required: every webhook is signature-checked")
    try:
        return twilio.TwilioConfig(public_base_url=base_url, auth_token=auth_token)
    except ValueError as exc:  # messages name the URL problem, never the token
        raise ConfigError(f"TALK_PUBLIC_BASE_URL: {exc}") from None


def _digits(value: str | None) -> str:
    """Digits only, so ``+1 (555) 123-4567`` and ``+15551234567`` compare equal."""

    return "".join(char for char in value or "" if char.isdigit())


def allowed_callers(environ: Mapping[str, str] | None = None) -> frozenset[str] | None:
    """Numbers allowed to call, as digits; ``None`` means anyone (``*``).

    Fail-closed: unset is a configuration error, because an open number gets
    robocalls and every answered call spends the provider key.
    """

    env = os.environ if environ is None else environ
    raw = (env.get("TALK_TWILIO_ALLOWED_CALLERS") or "").strip()
    if not raw:
        raise ConfigError(
            "TALK_TWILIO_ALLOWED_CALLERS is required: the numbers allowed to call "
            "(E.164, comma-separated), or * for anyone"
        )
    if raw == "*":
        return None
    numbers = set()
    for part in raw.split(","):
        digits = _digits(part)
        if not digits:
            raise ConfigError(f"TALK_TWILIO_ALLOWED_CALLERS entry {part.strip()!r} is not a number")
        numbers.add(digits)
    return frozenset(numbers)


def caller_policy(allowed: frozenset[str] | None) -> Callable[[str | None], bool]:
    if allowed is None:
        return lambda caller: True
    return lambda caller: bool(_digits(caller)) and _digits(caller) in allowed


def build_session(environ: Mapping[str, str] | None = None) -> rt.RealtimeSession:
    env = talk_config.SECRET_ENV if environ is None else environ
    provider = (env.get("TALK_PROVIDER") or "gemini").strip().lower()
    if provider != "gemini":
        raise ConfigError(
            "talk_twilio_server supports TALK_PROVIDER=gemini only; got "
            f"{provider!r}. Other lanes need their own session factory."
        )
    key = (env.get("GEMINI_API_KEY") or env.get("TALK_GEMINI_API_KEY") or "").strip()
    if not key:
        raise ConfigError("GEMINI_API_KEY (or TALK_GEMINI_API_KEY) is required")
    try:
        from . import talk_gemini_realtime as gemini
    except ImportError:  # pragma: no cover - flat-module fallback
        import talk_gemini_realtime as gemini
    return gemini.GeminiRealtimeSession(auth_token=key, auth_source="env")


def build_setup(environ: Mapping[str, str] | None = None) -> rt.SessionSetup:
    env = os.environ if environ is None else environ
    return rt.SessionSetup(
        model=(env.get("TALK_TWILIO_MODEL") or DEFAULT_MODEL).strip(),
        voice=(env.get("TALK_TWILIO_VOICE") or DEFAULT_VOICE).strip(),
        instructions=(env.get("TALK_TWILIO_INSTRUCTIONS") or DEFAULT_INSTRUCTIONS).strip(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m talk_twilio_server",
        description=EXPERIMENTAL_NOTE,
        epilog="Setup, environment and limits: docs/TWILIO.md",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="loopback only: put a TLS tunnel or reverse proxy on this machine in front",
    )
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the environment and print the URLs to give Twilio, then exit",
    )
    args = parser.parse_args(argv)

    if args.host not in twilio.LOOPBACK_HOSTS:
        print(
            f"talk_twilio_server: refusing to listen on {args.host!r}. It speaks plain "
            "HTTP, so it binds loopback only; put a TLS tunnel or reverse proxy on this "
            "machine in front of it.",
            file=sys.stderr,
        )
        return 2
    try:
        twilio._import_aiohttp()
        config = config_from_env()
        allowed = allowed_callers()
        setup = build_setup()
        build_session()  # fail fast on a missing key rather than on the first call
    except (ConfigError, RuntimeError) as exc:
        print(f"talk_twilio_server: {exc}", file=sys.stderr)
        return 2

    callers = (
        "anyone who dials the number (TALK_TWILIO_ALLOWED_CALLERS=*)"
        if allowed is None
        else f"{len(allowed)} allowed number(s)"
    )
    print(EXPERIMENTAL_NOTE)
    print(f"voice webhook (HTTP POST): {config.voice_url}")
    print(f"stream socket: {config.stream_url}")
    print(f"provider model: {setup.model} / voice {setup.voice}")
    print(f"callers: {callers}; at most {MAX_CALLS} at once, {MAX_CALL_MINUTES} min each")
    if args.check:
        return 0

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
    logging.getLogger(twilio.__name__).setLevel(logging.INFO)
    print(f"listening on http://{args.host}:{args.port}", flush=True)
    try:
        asyncio.run(
            twilio.serve(
                config=config,
                session_factory=build_session,
                setup_factory=build_setup,
                caller_policy=caller_policy(allowed),
                max_calls=MAX_CALLS,
                max_call_s=MAX_CALL_MINUTES * 60,
                host=args.host,
                port=args.port,
            )
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
