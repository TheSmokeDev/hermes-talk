"""Standalone entry point: run the Twilio transport as its own process.

Deliberately narrow. The terminal and Discord lanes own their own session
construction, and issue #44 will fold all three into one transport contract;
until then this server builds exactly one provider session per call from the
environment and hands it to :class:`talk_twilio.TwilioCallBridge`.

Only the Gemini Live lane is wired up, because its server-side endpointing is
what makes a phone line work without local VAD. Other providers raise a clear
error instead of half-working.

One assistant, two scopes. The owner (numbers in ``TALK_OWNER_NUMBERS``) always
gets through with the private briefing and the full toolset. Anyone else is
refused unless demo mode is on, and then gets the same assistant with public
facts only and no memory or message tools.

Demo mode is a switch, not a config edit: it is a JSON file flipped from the
``/demo`` page - password-only, cookie-backed - and it expires on its own.

    TWILIO_AUTH_TOKEN=... TALK_PUBLIC_BASE_URL=https://voice.example.com \\
    GEMINI_API_KEY=... python -m talk_twilio_server --port 8767
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import html
import json
import os
import pathlib
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta
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

ASSISTANT_INSTRUCTIONS = (
    "You are Shaan's Personal Assistant, speaking on a phone call. Introduce yourself "
    "as his AI assistant in your first sentence. This line is 8 kHz: keep replies "
    "short, concrete and spoken-friendly, one idea per sentence, and never read out "
    "lists, URLs or code. Only state facts a tool gave you or the briefing below "
    "contains; if you don't know, say so plainly instead of guessing. You cannot send "
    "email, submit forms or move money on this call - say you will follow up instead. "
    "If Shaan hands the phone to someone else, call handover_to_stranger immediately, "
    "and from then on treat everything you know about him as private."
)

STRANGER_SCOPE = (
    "You are speaking to someone Shaan has just met, so only the public facts below may "
    "be used. You have no memory tools and no way to look things up about him: if you "
    "are asked something outside the profile, use ask_hermes to pass the question on, "
    "or say you don't know. Never invent anything and never ask for personal details."
)

TIMEZONE = "Europe/London"
DATA_DIR = pathlib.Path.home() / ".hermes" / "data" / "phone"
BRIEFING_FILE = pathlib.Path.home() / ".hermes" / "data" / "talk-briefing.md"
PUBLIC_PROFILE_FILE = pathlib.Path.home() / ".hermes" / "data" / "talk-public-profile.md"
DEMO_STATE_FILE = DATA_DIR / "demo.json"
DEMO_MINUTES = 45
DEMO_COOKIE = "talk_demo"
DEMO_SESSION_HOURS = 12
HINDSIGHT_URL = os.environ.get("TALK_HINDSIGHT_URL", "http://10.8.0.2:8888")
HINDSIGHT_BANK = os.environ.get("TALK_HINDSIGHT_BANK", "shaan-shared")

CHECK_TIME = rt.ToolDefinition(
    name="check_time",
    description="Current date, time and weekday in Shaan's timezone.",
    parameters={"type": "object", "properties": {}, "required": []},
)
TAKE_MESSAGE = rt.ToolDefinition(
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
)
REMEMBER = rt.ToolDefinition(
    name="remember",
    description="Store a durable note about the caller or the conversation.",
    parameters={
        "type": "object",
        "properties": {"note": {"type": "string"}},
        "required": ["note"],
    },
)
LOOKUP_MEMORY = rt.ToolDefinition(
    name="lookup_memory",
    description="Search Shaan's long-term memory before saying you don't know something.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)
ASK_HERMES = rt.ToolDefinition(
    name="ask_hermes",
    description=(
        "Ask Shaan directly when the answer is not in memory or the briefing. The reply "
        "is not instant: if nothing comes back immediately, tell the caller Shaan will "
        "get back to them. The question is always queued for him either way."
    ),
    parameters={
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
)
HANDOVER = rt.ToolDefinition(
    name="handover_to_stranger",
    description=(
        "Call this the moment Shaan says he is handing the phone to someone else, or a "
        "new voice joins. It switches you to public-facts-only rules."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)

OWNER_TOOLS = (CHECK_TIME, TAKE_MESSAGE, REMEMBER, LOOKUP_MEMORY, ASK_HERMES, HANDOVER)
STRANGER_TOOLS = (CHECK_TIME, ASK_HERMES)


class ConfigError(RuntimeError):
    """Environment is not usable for a call server."""


def _normalise_number(value: str | None) -> str:
    """Digits only, so ``+447700900001`` and ``447700900001`` compare equal."""

    if not value:
        return ""
    return "".join(char for char in value if char.isdigit())


def owner_numbers(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    env = os.environ if environ is None else environ
    raw = env.get("TALK_OWNER_NUMBERS") or "+447466605683"
    return frozenset(
        number for number in (_normalise_number(part) for part in raw.split(",")) if number
    )


def demo_state() -> dict[str, Any]:
    try:
        state = json.loads(DEMO_STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"enabled": False, "until": None}
    if not isinstance(state, dict):
        return {"enabled": False, "until": None}
    return state


def demo_active() -> bool:
    state = demo_state()
    if not state.get("enabled"):
        return False
    until = state.get("until")
    try:
        deadline = datetime.fromisoformat(until) if until else None
    except (TypeError, ValueError):
        return False
    if deadline is None:
        return False
    if deadline.tzinfo is None:  # a hand-edited file may omit the offset
        deadline = deadline.replace(tzinfo=ZoneInfo(TIMEZONE))
    return deadline > datetime.now(ZoneInfo(TIMEZONE))


def set_demo(enabled: bool, minutes: int = DEMO_MINUTES) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(ZoneInfo(TIMEZONE))
    state = {
        "enabled": bool(enabled),
        "until": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        if enabled
        else None,
        "updated": now.isoformat(timespec="seconds"),
    }
    DEMO_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    return state


def is_owner(caller: str | None) -> bool:
    number = _normalise_number(caller)
    return bool(number) and number in owner_numbers()


def caller_policy(caller: str | None) -> bool:
    """Admit the owner always; admit anyone else only while demo mode is on."""

    return is_owner(caller) or demo_active()


# --- demo page authentication --------------------------------------------


def password_matches(password: str, stored: str) -> bool:
    """Verify a password against a ``scrypt$salt$hash`` string."""

    try:
        scheme, salt_hex, digest_hex = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    candidate = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return hmac.compare_digest(candidate, expected)


def _cookie_secret(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get("TALK_DEMO_COOKIE_SECRET") or "").strip()


def issue_session(secret: str, hours: int = DEMO_SESSION_HOURS) -> str:
    expiry = str(int(datetime.now().timestamp()) + hours * 3600)
    signature = hmac.new(secret.encode(), expiry.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{signature}"


def session_valid(secret: str, value: str | None) -> bool:
    if not secret or not value or "." not in value:
        return False
    expiry, signature = value.split(".", 1)
    expected = hmac.new(secret.encode(), expiry.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        return int(expiry) > int(datetime.now().timestamp())
    except ValueError:
        return False


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


def build_session(
    caller: str | None = None, environ: Mapping[str, str] | None = None
) -> rt.RealtimeSession:
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


def _read(path: pathlib.Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()[:limit]
    except FileNotFoundError:
        return ""


def _briefing() -> str:
    return _read(BRIEFING_FILE)


def _public_profile() -> str:
    return _read(PUBLIC_PROFILE_FILE)


def build_setup(
    caller: str | None = None, environ: Mapping[str, str] | None = None
) -> rt.SessionSetup:
    env = os.environ if environ is None else environ
    now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%A %d %B %Y, %H:%M")
    clock = (
        f"Right now it is {now} ({TIMEZONE}). This is a phone call, so you cannot see "
        "anything the caller does."
    )
    model = (env.get("TALK_TWILIO_MODEL") or DEFAULT_MODEL).strip()
    voice = (env.get("TALK_TWILIO_VOICE") or DEFAULT_VOICE).strip()

    if is_owner(caller):
        parts = [(env.get("TALK_TWILIO_INSTRUCTIONS") or ASSISTANT_INSTRUCTIONS).strip(), clock]
        briefing = _briefing()
        if briefing:
            parts.append("Context you may rely on:\n" + briefing)
        return rt.SessionSetup(
            model=model,
            voice=voice,
            instructions="\n\n".join(parts),
            tools=OWNER_TOOLS,
        )

    parts = [
        (env.get("TALK_TWILIO_INSTRUCTIONS") or ASSISTANT_INSTRUCTIONS).strip(),
        STRANGER_SCOPE,
        clock,
    ]
    profile = _public_profile()
    if profile:
        parts.append("Public facts you may use:\n" + profile)
    return rt.SessionSetup(
        model=model,
        voice=voice,
        instructions="\n\n".join(parts),
        tools=STRANGER_TOOLS,
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


async def execute_tool(name: str, arguments: Mapping[str, Any], caller: str | None = None) -> str:
    """Run one tool call from the live session."""

    print(f"tool: {name}", flush=True)
    if name == "check_time":
        return datetime.now(ZoneInfo(TIMEZONE)).strftime("%A %d %B %Y, %H:%M %Z")
    if name == "handover_to_stranger":
        return (
            "Switched to public-facts-only rules. You are now speaking to someone Shaan "
            "has just met: use only the public profile, never mention anything private, "
            "and do not use memory or message tools."
        )
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
                "caller": caller,
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
                "caller": caller,
            },
        )
        return "noted"
    if name == "lookup_memory":
        query = str(arguments.get("query", "")).strip()
        if not query:
            return "error: query was empty"
        return await lookup_memory(query)
    if name == "ask_hermes":
        question = str(arguments.get("question", "")).strip()
        if not question:
            return "error: question was empty"
        _append_jsonl(
            DATA_DIR / "questions.jsonl",
            {
                "at": datetime.now(ZoneInfo(TIMEZONE)).isoformat(timespec="seconds"),
                "question": question,
                "caller": caller,
                "answered": False,
            },
        )
        print(f"question queued: {question[:120]}", flush=True)
        # Opportunistic in-call answer: memory is fast, Shaan is not.
        digest = await lookup_memory(question)
        if digest.startswith(("nothing in memory", "memory lookup failed", "error:")):
            return (
                "Shaan is not available right now. Tell the caller you have passed the "
                "question on and he will get back to them."
            )
        return digest
    return f"error: unknown tool {name!r}"


# --- pages ---------------------------------------------------------------


def login_page(error: bool = False) -> str:
    message = '<p style="color:#ff8080">Wrong password.</p>' if error else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Demo mode</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;padding:32px;background:#111;color:#eee;text-align:center}}
h1{{font-size:1.2rem;font-weight:600;margin:0 0 22px}}
input{{width:100%;box-sizing:border-box;padding:18px;font-size:1.1rem;border-radius:12px;
  border:1px solid #333;background:#1a1a1a;color:#eee;margin-bottom:12px}}
button{{width:100%;padding:18px;font-size:1.05rem;border-radius:12px;border:0;background:#2f6feb;color:#fff;font-weight:600}}
</style></head><body>
<h1>Phone line</h1>
{message}
<form method="post" action="/demo/login">
<input type="password" name="password" placeholder="Password"
  autocomplete="current-password" autofocus>
<button type="submit">Unlock</button>
</form>
</body></html>"""


def demo_page(state: Mapping[str, Any], active: bool) -> str:
    """Phone-sized switch page."""

    colour = "#1a7f37" if active else "#8b1d1d"
    label = "ON - strangers can call" if active else "OFF - only you get through"
    until = state.get("until") if active else None
    detail = f"Expires {html.escape(str(until))}" if until else "Nobody but you reaches the line."
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Demo mode</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;padding:28px;background:#111;color:#eee;text-align:center}}
h1{{font-size:1.3rem;margin:0 0 6px}}
.status{{background:{colour};padding:18px;border-radius:14px;font-size:1.15rem;
  font-weight:600;margin:22px 0 6px}}
.detail{{opacity:.7;font-size:.9rem;margin-bottom:26px}}
button{{width:100%;padding:20px;font-size:1.1rem;border-radius:14px;border:0;margin-bottom:12px;font-weight:600}}
.on{{background:#1a7f37;color:#fff}}
.off{{background:#3a3a3a;color:#eee}}
</style></head><body>
<h1>Phone line</h1>
<div class="status">{label}</div>
<div class="detail">{detail}</div>
<form method="post" action="/demo">
<button class="on" name="state" value="on">Turn ON ({DEMO_MINUTES} min)</button>
<button class="off" name="state" value="off">Turn OFF</button>
</form>
</body></html>"""


async def _serve(*, config: twilio.TwilioConfig, host: str, port: int) -> None:
    aiohttp = twilio._import_aiohttp()
    web = aiohttp.web

    secret = _cookie_secret()

    def authed(request) -> bool:
        return session_valid(secret, request.cookies.get(DEMO_COOKIE))

    async def demo(request):
        if request.method == "POST":
            if not authed(request):
                raise web.HTTPFound("/demo")
            form = await request.post()
            set_demo(str(form.get("state", "")).strip().lower() == "on")
            raise web.HTTPFound("/demo")
        if not authed(request):
            return web.Response(text=login_page(), content_type="text/html")
        return web.Response(text=demo_page(demo_state(), demo_active()), content_type="text/html")

    async def demo_login(request):
        form = await request.post()
        password = str(form.get("password", ""))
        stored = (os.environ.get("TALK_DEMO_PASSWORD_HASH") or "").strip()
        if not stored or not password_matches(password, stored):
            return web.Response(text=login_page(error=True), content_type="text/html", status=401)
        response = web.HTTPFound("/demo")
        response.set_cookie(
            DEMO_COOKIE,
            issue_session(secret),
            max_age=DEMO_SESSION_HOURS * 3600,
            httponly=True,
            secure=True,
            samesite="Lax",
            path="/",
        )
        raise response

    app = twilio.create_app(
        config=config,
        session_factory=build_session,
        setup_factory=build_setup,
        greeting=DEFAULT_GREETING,
        tool_executor=execute_tool,
        caller_policy=caller_policy,
        deny_message="Sorry, this line isn't taking calls right now.",
        aiohttp_module=aiohttp,
    )
    app.router.add_route("*", "/demo", demo)
    app.router.add_post("/demo/login", demo_login)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


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
        setup = build_setup(None)
        build_session(None)  # fail fast on a missing key rather than on the first call
    except ConfigError as exc:
        print(f"talk_twilio_server: {exc}", file=sys.stderr)
        return 2

    if args.check:
        print(f"voice webhook: {config.voice_url}")
        print(f"stream socket: {config.stream_url}")
        print(f"provider model: {setup.model} / voice {setup.voice}")
        print(f"owner tools: {', '.join(tool.name for tool in OWNER_TOOLS)}")
        print(f"stranger tools: {', '.join(tool.name for tool in STRANGER_TOOLS)}")
        print(f"briefing: {'yes' if _briefing() else 'none'}")
        print(f"public profile: {'yes' if _public_profile() else 'none'}")
        demo_line = "ON until " + str(demo_state().get("until")) if demo_active() else "OFF"
        print(f"demo mode: {demo_line}")
        print(f"demo password: {'set' if os.environ.get('TALK_DEMO_PASSWORD_HASH') else 'MISSING'}")
        return 0

    print(f"listening on http://{args.host}:{args.port}", flush=True)
    print(f"  voice webhook: {config.voice_url}", flush=True)
    print(f"  stream socket: {config.stream_url}", flush=True)
    try:
        asyncio.run(_serve(config=config, host=args.host, port=args.port))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
