"""GPT-Live on the ChatGPT subscription through the Codex CLI's own app-server.

Catalog admission rule 11 lets a plugin use a subscription sign-in only through
Hermes, and Hermes signs HTTP requests, not the WebSocket a subscription call
needs. So Talk never holds that token: ``codex app-server`` (the user's Codex
CLI, on its own login) negotiates the WebRTC call and owns the token-bearing
connection. Talk reads the call's events from the app-server's notifications
and sends delegation results over the call's own WebRTC data channel
(``oai-events``), which carries no credential.

Codex routes every voice delegation into its thread as a turn and has no
switch to stop that, so the thread is fenced off: ephemeral, no environment, a
read-only sandbox, untrusted approvals that Talk always declines, apps, shell,
web search, plugins, sub-agents and every configured MCP server disabled, and
instructions to reply only ``skip``. Client-managed handoffs keep its replies
away from the voice. Hermes does the work, under Hermes approvals.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping

try:
    from . import talk_realtime as rt
    from .talk_live_config import LiveConfig
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_realtime as rt
    from talk_live_config import LiveConfig

_log = logging.getLogger("hermes_talk.live_codex")

BINARY_ENV = "TALK_CODEX_BINARY"
INSTALL_HINT = (
    "GPT-Live on the ChatGPT subscription runs through the Codex CLI: install it "
    "(`npm install -g @openai/codex`) and sign in with `codex login`"
)
REQUEST_TIMEOUT_S = 30.0
ANSWER_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 5.0
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_NOTIFICATIONS = 1024
MAX_DELEGATION_TEXT = 16000
DEFAULT_MODEL = "gpt-live-1-codex"
SKIP_INSTRUCTIONS = (
    "You are a silent relay for a voice session. Another agent, Hermes, carries out "
    "every request from that session. When you receive a <realtime_delegation> message "
    "or any other request, do not run anything, do not use tools, do not read files: "
    "reply with only the word: skip"
)
# The thread never needs a tool. Disable every tool family Codex can offer it.
LOCKDOWN_OVERRIDES = (
    "features.apps=false",
    "features.shell_tool=false",
    "features.multi_agent=false",
    "features.plugins=false",
    "features.browser_use=false",
    "features.computer_use=false",
    'web_search="disabled"',
    "tools.view_image=false",
    "suppress_unstable_features_warning=true",
)
# Credentials the CLI would otherwise prefer over its ChatGPT login.
_API_KEY_ENV = frozenset({"OPENAI_API_KEY", "CODEX_API_KEY"})
_APPROVAL_REQUESTS = frozenset(
    {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
)


class CodexLiveError(rt.RealtimeSessionError):
    """The Codex app-server could not start, sign in, or negotiate the call."""


def codex_binary(env: Mapping[str, str] | None = None) -> str | None:
    """The Codex CLI to launch: ``TALK_CODEX_BINARY`` when set, else ``codex`` on PATH."""

    source = os.environ if env is None else env
    configured = (source.get(BINARY_ENV) or "").strip()
    if configured:
        return configured
    return shutil.which("codex")


def _child_env() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if name not in _API_KEY_ENV}


def _translate(method: str, params: Mapping) -> dict | None:
    """One app-server notification as the Live wire event ``decode_event`` reads."""

    if method == "thread/realtime/transcript/delta":
        # v3 sends whole transcript items through Codex's "delta" notification.
        user = params.get("role") == "user"
        kind = "input_transcript.added" if user else "output_transcript.added"
        return {"type": kind, "item": {"text": params.get("delta")}}
    if method == "thread/realtime/transcript/done":
        role = "user" if params.get("role") == "user" else "assistant"
        return {"type": "turn.done", "turn": {"role": role, "transcript": params.get("text")}}
    if method == "thread/realtime/itemAdded":
        item = params.get("item")
        if not isinstance(item, Mapping) or item.get("type") != "handoff_request":
            return None
        text = item.get("input_transcript")
        return {
            "type": "delegation.created",
            "item": {
                "id": item.get("item_id") or item.get("handoff_id"),
                "type": "delegation",
                "target": "client",
                "content": [
                    {
                        "type": "input_text",
                        "text": text[:MAX_DELEGATION_TEXT] if isinstance(text, str) else "",
                    }
                ],
            },
        }
    if method == "thread/realtime/error":
        # Never relay the text: it can carry account details.
        return {"type": "error", "error": {"code": "codex_realtime_error"}}
    if method == "thread/realtime/closed":
        reason = params.get("reason")
        failed = reason and reason != "requested"
        return {"type": "session.closed", "reason": "connection_lost" if failed else None}
    return None


class _ThreadedPipe:
    """asyncio-shaped wrapper over a blocking pipe, for loops that cannot spawn."""

    def __init__(self, handle):
        self._handle = handle

    def write(self, data: bytes) -> None:
        self._handle.write(data)

    async def drain(self) -> None:
        await asyncio.to_thread(self._handle.flush)

    async def readline(self) -> bytes:
        return await asyncio.to_thread(self._handle.readline)

    def close(self) -> None:
        self._handle.close()


class _ThreadedProcess:
    """``subprocess.Popen`` behind the slice of the asyncio process API used here.

    A Windows selector event loop cannot create subprocesses; some hosts run one.
    """

    def __init__(self, popen):
        self._popen = popen
        self.stdin = _ThreadedPipe(popen.stdin)
        self.stdout = _ThreadedPipe(popen.stdout)

    @property
    def returncode(self):
        return self._popen.returncode

    async def wait(self):
        return await asyncio.to_thread(self._popen.wait)

    def kill(self) -> None:
        self._popen.kill()


async def _spawn_with_threads(*args, env, cwd, **_ignored):
    popen = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=cwd,
    )
    return _ThreadedProcess(popen)


class CodexAppServer:
    """One ``codex app-server`` child speaking JSON-RPC over stdio, for one Live call."""

    def __init__(self, *, binary: str | None = None, spawn: Callable | None = None):
        self._binary = binary
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._proc = None
        self._reader = None
        self._workdir = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.notifications: asyncio.Queue = asyncio.Queue(maxsize=MAX_NOTIFICATIONS)
        self._closed = False

    async def start(self) -> None:
        binary = self._binary or codex_binary()
        if not binary:
            raise CodexLiveError(INSTALL_HINT)
        self._workdir = tempfile.TemporaryDirectory(prefix="hermes-talk-codex-")
        args = [binary, "app-server", "--listen", "stdio://", "--enable", "realtime_conversation"]
        for override in LOCKDOWN_OVERRIDES:
            args += ["-c", override]
        spawn_options = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.DEVNULL,
            "env": _child_env(),
            "cwd": self._workdir.name,
            "limit": MAX_LINE_BYTES,
        }
        try:
            try:
                self._proc = await self._spawn(*args, **spawn_options)
            except NotImplementedError:
                self._proc = await _spawn_with_threads(*args, **spawn_options)
        except OSError:
            await self.close()
            raise CodexLiveError(INSTALL_HINT) from None
        self._reader = asyncio.create_task(self._read())
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": "hermes-talk", "version": "0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._write({"jsonrpc": "2.0", "method": "initialized"})

    @property
    def workdir(self) -> str:
        return self._workdir.name

    async def _write(self, message: dict) -> None:
        if self._proc is None or self._proc.stdin is None or self._closed:
            raise CodexLiveError("Codex app-server is not running")
        self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def request(self, method: str, params: dict, *, timeout: float = REQUEST_TIMEOUT_S):
        if self._reader is not None and self._reader.done():
            raise CodexLiveError("Codex app-server exited")
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError:
            raise CodexLiveError(f"Codex app-server did not answer {method}") from None
        finally:
            self._pending.pop(request_id, None)

    async def _answer(self, message: Mapping) -> None:
        """Decline every approval and refuse every other request the thread makes."""

        if message.get("method") in _APPROVAL_REQUESTS:
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": {"decision": "decline"}}
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "refused by hermes-talk"},
            }
        with contextlib.suppress(Exception):
            await self._write(reply)

    async def _read(self) -> None:
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    await self._answer(message)
                elif "id" in message:
                    future = self._pending.get(message.get("id"))
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error = message.get("error")
                        text = error.get("message") if isinstance(error, Mapping) else None
                        future.set_exception(CodexLiveError(_safe_error(text)))
                    else:
                        future.set_result(message.get("result"))
                elif isinstance(message.get("method"), str):
                    if self.notifications.full():
                        self.notifications.get_nowait()
                    self.notifications.put_nowait(message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken pipe ends the call below
            _log.debug("codex app-server reader stopped", exc_info=True)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CodexLiveError("Codex app-server exited"))
            if not self.notifications.full():
                self.notifications.put_nowait(None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
            try:
                async with asyncio.timeout(STOP_TIMEOUT_S):
                    await proc.wait()
            except (TimeoutError, Exception):  # noqa: BLE001 - make sure the child is gone
                with contextlib.suppress(ProcessLookupError, Exception):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._workdir is not None:
            with contextlib.suppress(Exception):
                self._workdir.cleanup()


def _safe_error(text) -> str:
    """A fixed message for the failures users can act on; never the raw text."""

    low = str(text or "").lower()
    if "usage limit" in low or "hit your usage" in low:
        return (
            "GPT-Live on the ChatGPT subscription hit the plan's usage limit; "
            "use Realtime (TALK_VOICE_MODE=native) or wait for the limit to reset"
        )
    if "login" in low or "auth" in low or "unauthorized" in low:
        return f"The Codex CLI is not signed in to ChatGPT. {INSTALL_HINT}"
    if "unknown variant" in low or "unknown field" in low:
        return "This Codex CLI is too old for GPT-Live; update it (`npm install -g @openai/codex`)"
    return "Codex app-server refused the GPT-Live request"


class CodexLiveCall:
    """A negotiated subscription call: the app-server, its thread, and the SDP answer."""

    def __init__(self, server: CodexAppServer, thread_id: str, answer_sdp: str, session_id: str):
        self.server = server
        self.thread_id = thread_id
        self.answer_sdp = answer_sdp
        self.session_id = session_id
        self._stopped = False

    async def stop(self) -> None:
        if not self._stopped:
            self._stopped = True
            with contextlib.suppress(Exception):
                await self.server.request(
                    "thread/realtime/stop", {"threadId": self.thread_id}, timeout=STOP_TIMEOUT_S
                )
        await self.server.close()


async def negotiate_codex_call(
    sdp: str,
    setup: rt.SessionSetup,
    config: LiveConfig,
    *,
    server: CodexAppServer | None = None,
) -> CodexLiveCall:
    """Start a fenced Codex thread and exchange the WebRTC offer for its answer."""

    server = server or CodexAppServer()
    try:
        await server.start()
        account = (await server.request("account/read", {}) or {}).get("account")
        if not isinstance(account, Mapping) or account.get("type") != "chatgpt":
            raise CodexLiveError(
                "The Codex CLI is not signed in with ChatGPT, so GPT-Live would not bill the "
                f"subscription. {INSTALL_HINT}. API credentials were not used."
            )
        effective = (await server.request("config/read", {}) or {}).get("config")
        servers = effective.get("mcp_servers") if isinstance(effective, Mapping) else None
        mcp_off = {
            f"mcp_servers.{name}.enabled": False
            for name in (servers if isinstance(servers, Mapping) else {})
        }
        thread = await server.request(
            "thread/start",
            {
                "cwd": server.workdir,
                "ephemeral": True,
                "approvalPolicy": "untrusted",
                "sandbox": "read-only",
                "environments": [],
                "developerInstructions": SKIP_INSTRUCTIONS,
                "config": {"model_reasoning_effort": "low", **mcp_off},
            },
        )
        thread_id = ((thread or {}).get("thread") or {}).get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexLiveError("Codex app-server did not create a thread")
        params = {
            "threadId": thread_id,
            "transport": {"type": "webrtc", "sdp": sdp},
            "outputModality": "audio",
            "version": "v3",
            "voice": config.voice,
            "prompt": setup.instructions,
            "realtimeStartInstructions": SKIP_INSTRUCTIONS,
            "clientManagedHandoffs": True,
            "includeStartupContext": False,
        }
        if config.model != DEFAULT_MODEL:
            params["model"] = config.model
        await server.request("thread/realtime/start", params)
        answer = session_id = None
        async with asyncio.timeout(ANSWER_TIMEOUT_S):
            while answer is None:
                message = await server.notifications.get()
                if message is None:
                    raise CodexLiveError("Codex app-server exited during GPT-Live setup")
                method, body = message.get("method"), message.get("params") or {}
                if body.get("threadId") not in {None, thread_id}:
                    continue
                if method == "thread/realtime/started":
                    session_id = body.get("realtimeSessionId") or session_id
                elif method == "thread/realtime/sdp":
                    answer = body.get("sdp")
                elif method in {"thread/realtime/error", "thread/realtime/closed"}:
                    raise CodexLiveError(_safe_error(body.get("message") or body.get("reason")))
        if not isinstance(answer, str) or not answer:
            raise CodexLiveError("Codex app-server returned no SDP answer")
        return CodexLiveCall(server, thread_id, answer, session_id or thread_id)
    except TimeoutError:
        await server.close()
        raise CodexLiveError("Codex app-server did not answer the GPT-Live offer") from None
    except BaseException:
        await server.close()
        raise


class CodexLiveTransport:
    """Live transport over a negotiated Codex call.

    Events come from the app-server's notifications. ``send_json`` hands each
    client event to ``outbound``, which puts it on the call's WebRTC data channel:
    a local aiortc channel on terminal/Discord, the browser's on dashboard
    surfaces. ``session.close`` stops the call through the app-server instead.
    """

    def __init__(self, call: CodexLiveCall, outbound: Callable | None = None):
        self.call = call
        self.session_id = call.session_id
        self._outbound = outbound
        self.finalized = False
        self.final_usage = None
        self._closed = False
        self._closing = False
        self._announced = False

    def attach_outbound(self, outbound: Callable) -> None:
        self._outbound = outbound

    async def send_json(self, event: dict) -> None:
        if self._closed:
            raise rt.RealtimeSessionError("GPT-Live call is closed")
        if event.get("type") == "session.close":
            self._closing = True
            await self.call.stop()
            return
        if self._outbound is None:
            raise rt.RealtimeSessionError("GPT-Live data channel is not attached")
        result = self._outbound(event)
        if asyncio.iscoroutine(result):
            await result

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict:
        if not self._announced:
            # The call is live once negotiated; Codex's own start receipt can precede it.
            self._announced = True
            return {"type": "session.started", "session": {"id": self.session_id}}
        while True:
            if self._closed or (self._closing and self.finalized):
                raise StopAsyncIteration
            message = await self.call.server.notifications.get()
            if message is None:
                if self._closing and not self.finalized:
                    # We stopped the call; the app-server exits before saying so.
                    self.finalized = True
                    return {"type": "session.closed", "reason": None}
                raise StopAsyncIteration
            body = message.get("params") or {}
            if body.get("threadId") not in {None, self.call.thread_id}:
                continue
            event = _translate(message.get("method"), body)
            if event is None:
                continue
            if event["type"] == "session.closed":
                self.finalized = True
            return event

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self.call.stop()


__all__ = [
    "BINARY_ENV",
    "INSTALL_HINT",
    "CodexAppServer",
    "CodexLiveCall",
    "CodexLiveError",
    "CodexLiveTransport",
    "codex_binary",
    "negotiate_codex_call",
]
