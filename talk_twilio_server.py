"""Standalone entry point: run the Twilio transport as its own process.

Deliberately narrow. The terminal and Discord lanes own their own session
construction, and issue #44 will fold all three into one transport contract;
until then this server builds exactly one provider session per call from the
environment and hands it to :class:`talk_twilio.TwilioCallBridge`.

Only the Gemini Live lane is wired up, because its server-side endpointing is
what makes a phone line work without local VAD. Other providers raise a clear
error instead of half-working.

The caller gets a small, real toolset (:data:`TOOLS`) rather than promises: it
can record a message, store a note, read the date and search long-term memory.
Anything wider belongs to the agent behind the gateway, not to a PSTN caller
whose identity is unproven.

    TWILIO_AUTH_TOKEN=... TALK_PUBLIC_BASE_URL=https://voice.example.com \
    GEMINI_API_KEY=... python -m talk_twilio_server --port 8767
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

try:
    from . import talk_realtime as rt
    from . import talk_twilio as twilio
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_realtime as rt
    import talk_twilio as twilio

DEFAULT_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_VOICE = "Kore"
DEFAULT_GREETING = (
    "(The caller has just been connected. Greet them in one short sentence and ask what they need.)"
)
DEFAULT_INSTRUCTIONS = (
    "You are Hermes, Shaan's AI assistant, speaking on a phone call. Say you are an AI "
    "assistant in your first sentence. This line is 8 kHz: keep replies short, concrete "
    "and spoken-friendly, one idea per sentence, and never read out lists, URLs or code. "
    "You have real tools: use check_time when the date or time matters, take_message to "
    "record something Shaan should act on, remember to store a durable note, and "
    "lookup_memory before claiming you don't know something about Shaan. Only state "
    "facts a tool gave you or the briefing below contains; if you don't know, say so "
    "plainly instead of guessing. You cannot send email, submit forms or move money on "
    "this call - say you will follow up in chat instead."
)

TIMEZONE = "Europe/London"
DATA_DIR = pathlib.Path.home() / ".hermes" / "data" / "phone"
BRIEFING_FILE = pathlib.Path.home() / ".hermes" / "data" / "talk-briefing.md"
HINDSIGHT_URL = os.environ.get("TALK_HINDSIGHT_URL", "http://10.8.0.2:8888")
HINDSIGHT_BANK = os.environ.get("TALK_HINDSIGHT_BANK", "shaan-shared")

TOOLS: tuple[rt.ToolDefinition, ...] = (
    rt.ToolDefinition(
        name="check_time",
        description="Current date, time and weekday in Shaan's timezone.",
        parameters={"type": "object", "properties": {}, "required": []},
    ),
    rt.ToolDefinition(
        name="take_message",
        description=(
            "Record a message for Shaan to act on later. Use this whenever the caller "
            "asks for something to be done."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What Shaan needs to know or do."},
                "urgency": {"type": "string", "enum": ["low", "normal", "urgent"]},
            },
            "required": ["message"],
        },
    ),
    rt.ToolDefinition(
        name="remember",
        description="Store a durable note about the caller or the conversation.",
        parameters={
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
    ),
    rt.ToolDefinition(
        name="lookup_memory",
        description="Search Shaan's long-term memory before saying you don't know something.",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    ),
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


def _briefing() -> str:
    """Operator-written context that travels with every call."""

    try:
        text = BRIEFING_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    return text[:4000]


def build_setup(environ: Mapping[str, str] | None = None) -> rt.SessionSetup:
    env = os.environ if environ is None else environ
    now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%A %d %B %Y, %H:%M")
    parts = [
        (env.get("TALK_TWILIO_INSTRUCTIONS") or DEFAULT_INSTRUCTIONS).strip(),
        f"Right now it is {now} ({TIMEZONE}). This is a phone call, so you cannot see "
        "anything the caller does.",
    ]
    briefing = _briefing()
    if briefing:
        parts.append("Context you may rely on:\n" + briefing)
    return rt.SessionSetup(
        model=(env.get("TALK_TWILIO_MODEL") or DEFAULT_MODEL).strip(),
        voice=(env.get("TALK_TWILIO_VOICE") or DEFAULT_VOICE).strip(),
        instructions="\n\n".join(parts),
        tools=TOOLS,
    )


def _append_jsonl(path: pathlib.Path, record: Mapping[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


async def lookup_memory(query: str) -> str:
    """Ask the Hindsight bank what it knows; return a short, quotable digest."""

    try:
        import aiohttp
    except ImportError:  # pragma: no cover - dependency gate
        return "error: memory lookup unavailable (aiohttp missing)"
    payload = {"query": query, "limit": 3}
    url = f"{HINDSIGHT_URL.rstrip('/')}/v1/default/banks/{HINDSIGHT_BANK}/memories/recall"
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as client,
            client.post(url, json=payload) as response,
        ):
            if response.status != 200:
                return f"memory lookup failed (HTTP {response.status})"
            body = await response.json()
    except Exception as exc:  # noqa: BLE001 - a dead memory server must not kill the call
        return f"memory lookup failed: {type(exc).__name__}"
    hits = [str(item.get("text", "")) for item in body.get("results", [])][:3]
    if not hits:
        return "nothing in memory about that"
    return " | ".join(hit[:400] for hit in hits)


async def execute_tool(name: str, arguments: Mapping[str, Any]) -> str:
    """Run one tool call from the live session."""

    print(f"tool: {name}", flush=True)
    if name == "check_time":
        return datetime.now(ZoneInfo(TIMEZONE)).strftime("%A %d %B %Y, %H:%M %Z")
    if name == "take_message":
        message = str(arguments.get("message", "")).strip()
        if not message:
            return "error: message was empty"
        _append_jsonl(
            DATA_DIR / "messages.jsonl",
            {
                "at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds"),
                "message": message,
                "urgency": str(arguments.get("urgency", "normal")),
            },
        )
        print(f"message recorded: {message[:120]}", flush=True)
        return "recorded - Shaan will see it"
    if name == "remember":
        note = str(arguments.get("note", "")).strip()
        if not note:
            return "error: note was empty"
        _append_jsonl(
            DATA_DIR / "notes.jsonl",
            {
                "at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds"),
                "note": note,
            },
        )
        return "noted"
    if name == "lookup_memory":
        query = str(arguments.get("query", "")).strip()
        if not query:
            return "error: query was empty"
        return await lookup_memory(query)
    return f"error: unknown tool {name!r}"


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
        print(f"tools: {', '.join(tool.name for tool in setup.tools)}")
        print(f"briefing: {'yes' if _briefing() else 'none'}")
        return 0

    print(f"listening on http://{args.host}:{args.port}", flush=True)
    print(f"  voice webhook: {config.voice_url}", flush=True)
    print(f"  stream socket: {config.stream_url}", flush=True)
    try:
        asyncio.run(
            twilio.serve(
                config=config,
                session_factory=build_session,
                setup_factory=build_setup,
                greeting=DEFAULT_GREETING,
                tool_executor=execute_tool,
                host=args.host,
                port=args.port,
            )
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
