"""Standalone entry point: run the Twilio transport as its own process.

Deliberately narrow. The terminal and Discord lanes own their own session
construction, and issue #44 will fold all three into one transport contract;
until then this server builds exactly one provider session per call from the
environment and hands it to :class:`talk_twilio.TwilioCallBridge`.

Only the Gemini Live lane is wired up, because its server-side endpointing is
what makes a phone line work without local VAD. Other providers raise a clear
error instead of half-working.

    TWILIO_AUTH_TOKEN=... TALK_PUBLIC_BASE_URL=https://voice.example.com \
    GEMINI_API_KEY=... python -m talk_twilio_server --port 8767
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping

try:
    from . import talk_realtime as rt
    from . import talk_twilio as twilio
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_realtime as rt
    import talk_twilio as twilio

DEFAULT_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_VOICE = "Kore"
DEFAULT_INSTRUCTIONS = (
    "You are Hermes, answering a phone call. Say you are an AI assistant near the "
    "start of the call. Keep replies short and spoken-friendly: this line is 8 kHz "
    "and the caller cannot see text. You cannot take actions on this call; if the "
    "caller asks for something that needs one, say you will follow up in chat."
)


class ConfigError(RuntimeError):
    """Environment is not usable for a call server."""


def config_from_env(environ: Mapping[str, str] | None = None) -> twilio.TwilioConfig:
    env = os.environ if environ is None else environ
    base_url = (env.get("TALK_PUBLIC_BASE_URL") or "").strip()
    if not base_url:
        raise ConfigError("TALK_PUBLIC_BASE_URL is required (the public https origin)")
    auth_token = (env.get("TWILIO_AUTH_TOKEN") or "").strip()
    allow_unsigned = (env.get("TALK_TWILIO_ALLOW_UNSIGNED") or "").strip() in {"1", "true", "yes"}
    if not auth_token and not allow_unsigned:
        raise ConfigError(
            "TWILIO_AUTH_TOKEN is required unless TALK_TWILIO_ALLOW_UNSIGNED=1 is set "
            "for a local tunnel"
        )
    return twilio.TwilioConfig(
        public_base_url=base_url,
        auth_token=auth_token or None,
        input_rate=int(env.get("TALK_TWILIO_INPUT_RATE") or 16000),
        output_rate=int(env.get("TALK_TWILIO_OUTPUT_RATE") or 24000),
        require_signature=not allow_unsigned,
    )


def build_session(environ: Mapping[str, str] | None = None) -> rt.RealtimeSession:
    env = os.environ if environ is None else environ
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
    parser = argparse.ArgumentParser(description="Twilio Media Streams transport for Talk")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the environment and print the two URLs Twilio will call",
    )
    args = parser.parse_args(argv)

    try:
        config = config_from_env()
        setup = build_setup()
        build_session()  # fail fast on a missing key rather than on the first call
    except ConfigError as exc:
        print(f"talk_twilio_server: {exc}", file=sys.stderr)
        return 2

    if args.check:
        print(f"voice webhook: {config.voice_url}")
        print(f"stream socket: {config.stream_url}")
        print(f"provider model: {setup.model} / voice {setup.voice}")
        return 0

    print(f"listening on http://{args.host}:{args.port}")
    print(f"  voice webhook: {config.voice_url}")
    print(f"  stream socket: {config.stream_url}")
    try:
        asyncio.run(
            twilio.serve(
                config=config,
                session_factory=build_session,
                setup_factory=build_setup,
                host=args.host,
                port=args.port,
            )
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
