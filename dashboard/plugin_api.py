"""Browser voice surface — the backend half, mounted by the Hermes dashboard.

Routes land at ``/api/plugins/hermes-talk/`` via
``hermes_cli/web_server.py::_mount_plugin_api_routes``, which imports THIS FILE
BY PATH (``importlib.util.spec_from_file_location``). There is no parent
package at that point, so the plugin's flat ``talk_*`` modules are reached by
putting the plugin root on ``sys.path`` — the usual ``from . import x`` shim
cannot work from a subdirectory that is not a package.

Three invariants this layer exists to hold:

- **The mint answers with the ephemeral secret and nothing else.** The raw API
  key or OAuth token is spent inside :mod:`talk_wire` on exactly one OpenAI
  endpoint; it never enters a response body, a header, or a log line.
- **The tool contract is the relay's contract, verbatim.** An UNKNOWN tool is a
  client bug and answers 400. A KNOWN tool that fails answers 200 with
  speakable failure text, because a live call must hear what broke instead of
  dying on a stack trace.
- **The guard never fails open.** Dashboard plugin routes ride the host's own
  session auth, but this one mints real credentials, so it carries a second,
  independent gate: ``TALK_DASHBOARD_TOKEN`` when set, loopback-only when it is
  not, and a refusal — never a pass — for a peer this process cannot identify.

Out-of-process caveat: the dashboard imports this file into the WEB SERVER
process, which has no bound plugin context. Agent-loop-only tools
(``session_search``, in-loop ``delegate_task``) degrade to their announced
fallbacks there; see the README's Dashboard section.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import sys
from pathlib import Path

#: The dashboard loads this module by path, so ``talk_*`` is not importable by
#: name until the plugin root is on the path. Done once, before the imports
#: below, and deliberately as the ONE mechanism — a relative import has no
#: package to resolve against here, and a name guessed from the host's module
#: registry would break the moment the loader's slug changed.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

import talk_apiserver  # noqa: E402
import talk_auth  # noqa: E402
import talk_capabilities  # noqa: E402
import talk_config  # noqa: E402
import talk_host  # noqa: E402
import talk_identity  # noqa: E402
import talk_relay  # noqa: E402
import talk_runs  # noqa: E402
import talk_tools  # noqa: E402
import talk_wire  # noqa: E402

try:
    from fastapi import APIRouter, HTTPException, Request
except ImportError:  # pragma: no cover - offline tests run without the dashboard deps

    class APIRouter:  # type: ignore[no-redef]
        """Decorator no-op so handlers stay directly callable under pytest."""

        def get(self, *_args, **_kwargs):
            return lambda fn: fn

        def post(self, *_args, **_kwargs):
            return lambda fn: fn

    class HTTPException(Exception):  # type: ignore[no-redef]
        """Same two fields the real one carries, so assertions match either way."""

        def __init__(self, status_code: int, detail=None) -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class Request:  # type: ignore[no-redef]
        """Annotation target only — never instantiated on this path."""


router = APIRouter()

DASHBOARD_TOKEN_ENV = "TALK_DASHBOARD_TOKEN"
DASHBOARD_TOKEN_HEADER = "x-talk-token"

#: Peers this process will serve when no token is configured. ``::ffff:127.0.0.1``
#: is the IPv4-mapped form a dual-stack listener reports.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"})

TOKEN_REQUIRED_MESSAGE = (
    f"hermes-talk dashboard routes require the {DASHBOARD_TOKEN_ENV} token: send it as "
    f"`{DASHBOARD_TOKEN_HEADER}: <token>` or `Authorization: Bearer <token>`."
)
LOOPBACK_ONLY_MESSAGE = (
    "hermes-talk dashboard routes serve loopback only. This request came from "
    f"somewhere else — set {DASHBOARD_TOKEN_ENV} and send it as "
    f"`{DASHBOARD_TOKEN_HEADER}: <token>` to reach them remotely."
)

#: How many runs the panel asks for. Fixed rather than caller-supplied: the
#: registry is a status board, not a query surface.
RUNS_LIMIT = 20
TOOL_EXECUTION_WAIT_S = talk_relay.TOOL_EXECUTION_WAIT_S


# -- auth ---------------------------------------------------------------------


def dashboard_token() -> str | None:
    """The configured token, resolved at CALL time. Set-but-blank reads as unset."""

    return (os.environ.get(DASHBOARD_TOKEN_ENV) or "").strip() or None


def _presented_token(request) -> str:
    """The token this request carries, from either accepted header."""

    getter = getattr(getattr(request, "headers", None), "get", None)
    if getter is None:
        return ""
    direct = (getter(DASHBOARD_TOKEN_HEADER) or "").strip()
    if direct:
        return direct
    authorization = (getter("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[len("bearer ") :].strip()
    return ""


def _is_loopback(request) -> bool:
    """True only for a peer this process can positively identify as local.

    An absent or unparseable client address reads as REMOTE. Failing open here
    would hand the mint to whichever proxy stripped the peer address.
    """

    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    if not isinstance(host, str) or not host.strip():
        return False
    return host.strip().lower() in LOOPBACK_HOSTS


def require_dashboard_auth(request) -> None:
    """Gate one request. Returns on success, raises otherwise — never a bool.

    A bool would let a handler forget to check it; a raise cannot be ignored.
    """

    configured = dashboard_token()
    if configured is not None:
        presented = _presented_token(request)
        # compare_digest over bytes: unequal lengths return False rather than
        # raising, and the comparison does not short-circuit on first mismatch.
        if presented and hmac.compare_digest(
            presented.encode("utf-8"), configured.encode("utf-8")
        ):
            return
        raise HTTPException(status_code=401, detail=TOKEN_REQUIRED_MESSAGE)
    if _is_loopback(request):
        return
    raise HTTPException(status_code=403, detail=LOOPBACK_ONLY_MESSAGE)


# -- helpers ------------------------------------------------------------------


async def _json_body(request) -> dict:
    """Decode a JSON object body. Anything else is one 400, not a stack trace."""

    try:
        payload = await request.json()
    except Exception as exc:
        # Every decode failure is the same 400 — a torn body, a wrong
        # content-type, and a client that sent nothing are one bug to the caller.
        raise HTTPException(status_code=400, detail="request body must be JSON") from exc
    return payload if isinstance(payload, dict) else {}


def _warm_agent_lane() -> str:
    """Resolve the agent lane, paying for a cold probe. Worker thread only."""

    talk_apiserver.warm()
    # The catalog rides the same paid-for wait: it is the only place in the
    # codebase that warms a lane at session start, and the alternative is the
    # session's first "what can you do?" answering "still checking".
    talk_capabilities.warm()
    return talk_host.host().agent_lane()


def _mint(auth_token: str, voice: str):
    """Assemble instructions and mint. Blocking — called on a worker thread."""

    tools = talk_tools.default_talk_tools()
    return talk_wire.mint_ephemeral_session(
        auth_token=auth_token,
        model=talk_config.talk_model(),
        voice=voice,
        instructions=talk_identity.build_instructions(
            talk_host.host().identity_sections(), tools=tools
        ),
        tools=tools,
    )


def _resolve_voice(requested) -> str:
    """The voice for this session — the operator's override, or the configured one."""

    if isinstance(requested, str) and requested.strip():
        voice = requested.strip().lower()
        if voice not in talk_config.OPENAI_REALTIME_VOICES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{voice}' is not a built-in Realtime voice "
                    f"({', '.join(talk_config.OPENAI_REALTIME_VOICES)})"
                ),
            )
        return voice
    try:
        return talk_config.talk_voice()
    except talk_config.TalkConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# -- routes -------------------------------------------------------------------


@router.get("/status")
async def talk_status(request: Request) -> dict:
    """Readiness for the page: which auth lane, which model, which voices.

    ``auth_status`` reports the LANE and never the credential, which is what
    lets this route answer before anything has been minted.
    """

    require_dashboard_auth(request)
    try:
        voice = talk_config.talk_voice()
    except talk_config.TalkConfigError as exc:
        voice = ""
        detail_suffix = f" (TALK_VOICE unusable: {exc})"
    else:
        detail_suffix = ""
    status = talk_auth.auth_status()
    return {
        "ok": True,
        "configured": bool(status.get("configured")),
        "source": status.get("source"),
        "detail": f"{status.get('detail') or ''}{detail_suffix}",
        "model": talk_config.talk_model(),
        "voice": voice,
        "voices": list(talk_config.OPENAI_REALTIME_VOICES),
        "version": talk_tools.plugin_version(),
        # Tri-state, not a bool: no plugin context is ever bound in the web
        # server process, so the only question that matters here is whether the
        # api_server lane can reach a real agent. This route is the page's
        # first call, which makes it the right place to PAY for the probe —
        # off the event loop, so the tile is already right when it first
        # paints and every later tool call reads a warm verdict.
        "agentLoop": await asyncio.to_thread(_warm_agent_lane),
    }


@router.post("/session")
async def create_session(request: Request) -> dict:
    """Mint one ephemeral Realtime session for the browser to dial directly.

    The response is the descriptor's wire form plus which lane paid for it.
    ``to_wire()`` carries the EPHEMERAL secret; the credential that minted it
    stays in this process.
    """

    require_dashboard_auth(request)
    body = await _json_body(request)
    voice = _resolve_voice(body.get("voice"))
    try:
        auth = talk_auth.resolve_auth()
    except talk_auth.TalkAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        # Off the event loop: the mint is a 30s-timeout HTTP call and identity
        # assembly reads files and may initialize a memory provider. On the
        # loop, one slow mint freezes the WHOLE Hermes dashboard.
        descriptor = await asyncio.to_thread(
            _mint,
            auth.token,
            voice,
        )
    except talk_wire.TalkWireError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, **descriptor.to_wire(), "authSource": auth.source}


@router.post("/tool")
async def run_tool(request: Request) -> dict:
    """Relay one model function call into the plugin's real tool surface.

    Mirrors :mod:`talk_relay`: an unknown name is a CLIENT bug (400), and a
    known tool that fails answers 200 with the text the model should speak.
    """

    require_dashboard_auth(request)
    body = await _json_body(request)
    name = str(body.get("name") or "").strip()
    arguments = body.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        # Off the event loop. A tool can probe the api_server, start a run, or
        # reach a bound agent — none of that may run where the dashboard's own
        # request loop lives.
        output = await talk_relay.run_bounded_on_daemon(
            talk_tools.execute_talk_tool,
            name,
            arguments,
            timeout=TOOL_EXECUTION_WAIT_S,
        )
    except talk_relay.ToolWorkerBusy:
        output = (
            f"Earlier tools are still running, so the {name or 'tool'} tool was not "
            "started. Wait for them to finish, then ask me to try again."
        )
    except talk_relay.ToolExecutionTimeout as exc:
        if exc.started:
            output = (
                f"The {name or 'tool'} tool is still running after "
                f"{TOOL_EXECUTION_WAIT_S:g} seconds. I detached it so the call can "
                "continue, but its eventual result won't return to this conversation. "
                "Ask me to check again if you still need it."
            )
        else:
            output = (
                f"The {name or 'tool'} tool did not start within "
                f"{TOOL_EXECUTION_WAIT_S:g} seconds because earlier tools were still "
                "using the workers. Ask me to try again."
            )
    except talk_tools.TalkToolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "output": output}


@router.get("/runs")
async def list_runs(request: Request) -> dict:
    """Background runs for the panel and the WORK_STARTED watcher.

    ``include_history`` so a run from a previous process surfaces as ``lost``
    instead of vanishing — this process cannot know how a detached child ended.
    """

    require_dashboard_auth(request)
    return {
        "ok": True,
        "runs": talk_runs.list_runs(limit=RUNS_LIMIT, include_history=True),
    }


#: Every route in this plugin, for the guard-coverage invariant test. A new
#: route that is not listed here (or not gated) fails that test rather than
#: shipping open.
ROUTE_HANDLERS = (talk_status, create_session, run_tool, list_runs)


__all__ = [
    "DASHBOARD_TOKEN_ENV",
    "DASHBOARD_TOKEN_HEADER",
    "LOOPBACK_HOSTS",
    "LOOPBACK_ONLY_MESSAGE",
    "ROUTE_HANDLERS",
    "RUNS_LIMIT",
    "TOKEN_REQUIRED_MESSAGE",
    "create_session",
    "dashboard_token",
    "list_runs",
    "require_dashboard_auth",
    "router",
    "run_tool",
    "talk_status",
]
