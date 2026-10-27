"""GPT-Live subscription through a fake ``codex app-server``: no real Codex, no token."""

from __future__ import annotations

import asyncio
import json

import pytest
from test_live_config_protocol import setup_for

import talk_live_codex as codex
import talk_live_protocol as protocol
import talk_realtime as rt
from talk_live_config import LiveConfig

_MEDIA = "s=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
SDP_ANSWER = "v=0\r\no=- 2 2 IN IP4 127.0.0.1\r\n" + _MEDIA
OFFER = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n" + _MEDIA


class FakeStdin:
    def __init__(self, proc):
        self.proc = proc
        self.closed = False

    def write(self, data: bytes):
        for line in data.decode().splitlines():
            self.proc.received.append(json.loads(line))
            self.proc.handle(json.loads(line))

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True
        if self.proc.exit_on_stdin_close:
            self.proc.finish()


class FakeStdout:
    def __init__(self):
        self.lines = asyncio.Queue()

    async def readline(self):
        return await self.lines.get()


class FakeProcess:
    """Answers JSON-RPC requests from a script and records everything Talk sent."""

    def __init__(self, *, account="chatgpt", mcp=("upwork", "node_repl"), answer=SDP_ANSWER,
                 start_error=None, after_start=(), exit_on_stdin_close=True):
        self.received = []
        self.stdin = FakeStdin(self)
        self.stdout = FakeStdout()
        self.returncode = None
        self.killed = False
        self.exit_on_stdin_close = exit_on_stdin_close
        self.account = account
        self.mcp = mcp
        self.answer = answer
        self.start_error = start_error
        self.after_start = list(after_start)

    def emit(self, message):
        self.stdout.lines.put_nowait((json.dumps(message) + "\n").encode())

    def notify(self, method, **params):
        params = {"threadId": "thread-1", **params}
        self.emit({"jsonrpc": "2.0", "method": method, "params": params})

    def finish(self):
        if self.returncode is None:
            self.returncode = 0
            self.stdout.lines.put_nowait(b"")

    def handle(self, message):
        method, rid = message.get("method"), message.get("id")
        if rid is None or method is None:
            return
        result = {}
        if method == "account/read":
            result = {"account": {"type": self.account} if self.account else None}
        elif method == "config/read":
            result = {"config": {"mcp_servers": {name: {"command": "x"} for name in self.mcp}}}
        elif method == "thread/start":
            result = {"thread": {"id": "thread-1"}}
        elif method == "thread/realtime/start":
            if self.start_error:
                self.emit({"jsonrpc": "2.0", "id": rid,
                           "error": {"code": -32000, "message": self.start_error}})
                return
            self.emit({"jsonrpc": "2.0", "id": rid, "result": {}})
            self.notify("thread/realtime/started", realtimeSessionId="rts_live", version="v3")
            for item in self.after_start:
                self.notify(item[0], **item[1])
            if self.answer is not None:
                self.notify("thread/realtime/sdp", sdp=self.answer)
            return
        self.emit({"jsonrpc": "2.0", "id": rid, "result": result})

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode

    def kill(self):
        self.killed = True
        self.finish()


def spawner(proc, calls=None):
    async def spawn(*args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        return proc

    return spawn


def server_for(proc, calls=None):
    return codex.CodexAppServer(binary="codex-test", spawn=spawner(proc, calls))


def methods(proc):
    return [message.get("method") for message in proc.received if "method" in message]


def sent(proc, method):
    return next(message for message in proc.received if message.get("method") == method)


@pytest.mark.parametrize(
    "method,params,expected",
    [
        ("thread/realtime/transcript/delta", {"role": "user", "delta": "hi"},
         {"type": "input_transcript.added", "item": {"text": "hi"}}),
        ("thread/realtime/transcript/delta", {"role": "assistant", "delta": "yo"},
         {"type": "output_transcript.added", "item": {"text": "yo"}}),
        ("thread/realtime/transcript/done", {"role": "assistant", "text": "yo there"},
         {"type": "turn.done", "turn": {"role": "assistant", "transcript": "yo there"}}),
        ("thread/realtime/error", {"message": "secret account acct_123"},
         {"type": "error", "error": {"code": "codex_realtime_error"}}),
        ("thread/realtime/closed", {"reason": "requested"},
         {"type": "session.closed", "reason": None}),
        ("thread/realtime/closed", {"reason": "error"},
         {"type": "session.closed", "reason": "connection_lost"}),
        ("thread/realtime/started", {"realtimeSessionId": "x"}, None),
        ("turn/started", {}, None),
        ("thread/realtime/itemAdded", {"item": {"type": "other"}}, None),
    ],
)
def test_notification_translation_table(method, params, expected):
    assert codex._translate(method, params) == expected


def test_handoff_becomes_a_client_delegation_with_the_data_channel_item_id():
    event = codex._translate(
        "thread/realtime/itemAdded",
        {"item": {"type": "handoff_request", "handoff_id": "item_9", "item_id": "item_9",
                  "input_transcript": "check my calendar", "active_transcript": []}},
    )
    decoded = protocol.decode_event(event)
    assert isinstance(decoded, rt.DelegationRequested)
    assert decoded.delegation_id == "item_9"
    assert decoded.prompt == "check my calendar"


def test_error_event_never_carries_provider_text():
    decoded = protocol.decode_event(
        codex._translate("thread/realtime/error", {"message": "token sk-live-123"})
    )
    assert isinstance(decoded, rt.ProviderFailure)
    assert "sk-live" not in decoded.detail


def test_negotiation_fences_the_thread_and_strips_api_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "paid-key")
    monkeypatch.setenv("CODEX_API_KEY", "paid-codex-key")

    async def run():
        proc, calls = FakeProcess(), []
        config = LiveConfig()
        call = await codex.negotiate_codex_call(
            OFFER, setup_for(config), config, server=server_for(proc, calls)
        )
        assert call.answer_sdp == SDP_ANSWER
        assert call.session_id == "rts_live" and call.thread_id == "thread-1"
        args, kwargs = calls[0]
        assert args[:6] == ("codex-test", "app-server", "--listen", "stdio://",
                            "--enable", "realtime_conversation")
        overrides = [args[i + 1] for i, arg in enumerate(args) if arg == "-c"]
        assert set(codex.LOCKDOWN_OVERRIDES) <= set(overrides)
        assert "OPENAI_API_KEY" not in kwargs["env"] and "CODEX_API_KEY" not in kwargs["env"]
        assert methods(proc)[:6] == ["initialize", "initialized", "account/read", "config/read",
                                     "thread/start", "thread/realtime/start"]
        assert sent(proc, "initialize")["params"]["capabilities"] == {"experimentalApi": True}
        thread = sent(proc, "thread/start")["params"]
        assert thread["ephemeral"] is True
        assert thread["approvalPolicy"] == "untrusted"
        assert thread["sandbox"] == "read-only"
        assert thread["environments"] == []
        assert thread["developerInstructions"] == codex.SKIP_INSTRUCTIONS
        assert thread["config"] == {"model_reasoning_effort": "low",
                                    "mcp_servers.upwork.enabled": False,
                                    "mcp_servers.node_repl.enabled": False}
        start = sent(proc, "thread/realtime/start")["params"]
        assert start["transport"] == {"type": "webrtc", "sdp": OFFER}
        assert start["version"] == "v3" and start["voice"] == config.voice
        assert start["prompt"] == "Host policy"
        assert start["clientManagedHandoffs"] is True
        assert start["includeStartupContext"] is False
        assert "model" not in start
        assert "paid-key" not in json.dumps(proc.received)
        await call.stop()
        assert sent(proc, "thread/realtime/stop")["params"] == {"threadId": "thread-1"}
        assert proc.stdin.closed and proc.returncode is not None

    asyncio.run(run())


def test_api_key_codex_login_is_refused_before_any_thread():
    async def run():
        proc = FakeProcess(account="apiKey")
        with pytest.raises(codex.CodexLiveError, match="not signed in with ChatGPT"):
            await codex.negotiate_codex_call(OFFER, setup_for(), LiveConfig(),
                                             server=server_for(proc))
        assert "thread/start" not in methods(proc)
        assert proc.returncode is not None

    asyncio.run(run())


def test_missing_binary_names_the_install_step(monkeypatch):
    monkeypatch.setattr(codex, "codex_binary", lambda env=None: None)

    async def run():
        with pytest.raises(codex.CodexLiveError, match="codex login"):
            await codex.CodexAppServer().start()

    asyncio.run(run())


def test_start_errors_become_fixed_messages_without_provider_text():
    async def run():
        proc = FakeProcess(start_error="You've hit your usage limit (acct_secret_42)")
        with pytest.raises(codex.CodexLiveError, match="usage limit") as exc:
            await codex.negotiate_codex_call(OFFER, setup_for(), LiveConfig(),
                                             server=server_for(proc))
        assert "acct_secret_42" not in str(exc.value)
        assert proc.returncode is not None

    asyncio.run(run())


def test_realtime_error_during_setup_closes_the_app_server():
    async def run():
        proc = FakeProcess(answer=None,
                           after_start=[("thread/realtime/error", {"message": "boom acct_7"})])
        with pytest.raises(codex.CodexLiveError) as exc:
            await codex.negotiate_codex_call(OFFER, setup_for(), LiveConfig(),
                                             server=server_for(proc))
        assert "acct_7" not in str(exc.value)
        assert proc.returncode is not None

    asyncio.run(run())


def test_missing_answer_times_out_and_kills_a_stuck_process(monkeypatch):
    monkeypatch.setattr(codex, "ANSWER_TIMEOUT_S", 0.05)
    monkeypatch.setattr(codex, "STOP_TIMEOUT_S", 0.05)

    async def run():
        proc = FakeProcess(answer=None, exit_on_stdin_close=False)
        with pytest.raises(codex.CodexLiveError, match="did not answer"):
            await codex.negotiate_codex_call(OFFER, setup_for(), LiveConfig(),
                                             server=server_for(proc))
        assert proc.killed

    asyncio.run(run())


def test_thread_approvals_are_declined_and_other_requests_refused():
    async def run():
        proc = FakeProcess()
        server = server_for(proc)
        await server.start()
        for rid, method in [(901, "item/commandExecution/requestApproval"),
                            (902, "item/fileChange/requestApproval"),
                            (903, "item/permissions/requestApproval"),
                            (904, "item/tool/call")]:
            proc.emit({"jsonrpc": "2.0", "id": rid, "method": method, "params": {}})
        for _ in range(50):
            replies = {m["id"]: m for m in proc.received if m.get("id") in {901, 902, 903, 904}}
            if len(replies) == 4:
                break
            await asyncio.sleep(0.01)
        assert replies[901]["result"] == {"decision": "decline"}
        assert replies[902]["result"] == {"decision": "decline"}
        assert replies[903]["error"]["code"] == -32601
        assert replies[904]["error"]["code"] == -32601
        await server.close()

    asyncio.run(run())


def test_transport_announces_ready_relays_client_events_and_finalizes_on_close():
    async def run():
        proc = FakeProcess()
        config = LiveConfig()
        call = await codex.negotiate_codex_call(OFFER, setup_for(config), config,
                                                server=server_for(proc))
        relayed = []
        wire = codex.CodexLiveTransport(call, outbound=relayed.append)
        first = await anext(wire)
        assert first == {"type": "session.started", "session": {"id": "rts_live"}}
        proc.notify("thread/realtime/transcript/delta", role="user", delta="hello")
        proc.emit({"jsonrpc": "2.0", "method": "thread/realtime/transcript/delta",
                   "params": {"threadId": "other-thread", "role": "user", "delta": "foreign"}})
        proc.notify("turn/started")
        assert await anext(wire) == {"type": "input_transcript.added", "item": {"text": "hello"}}
        event = {"type": "delegation.context.append", "delegation_item_id": "i1",
                 "channel": "speakable", "content": [{"type": "input_text", "text": "ok"}]}
        await wire.send_json(event)
        assert relayed == [event]
        await wire.send_json({"type": "session.close"})
        assert await anext(wire) == {"type": "session.closed", "reason": None}
        assert wire.finalized
        assert "thread/realtime/stop" in methods(proc)
        await wire.close()

    asyncio.run(run())


def test_transport_without_data_channel_refuses_client_events():
    async def run():
        proc = FakeProcess()
        config = LiveConfig()
        call = await codex.negotiate_codex_call(OFFER, setup_for(config), config,
                                                server=server_for(proc))
        wire = codex.CodexLiveTransport(call)
        with pytest.raises(rt.RealtimeSessionError, match="data channel"):
            await wire.send_json({"type": "session.context.append"})
        await wire.close()
        with pytest.raises(rt.RealtimeSessionError, match="closed"):
            await wire.send_json({"type": "session.context.append"})

    asyncio.run(run())


def test_unexpected_app_server_exit_ends_events_without_a_clean_close(monkeypatch):
    # Stopping after a crash waits out STOP_TIMEOUT_S for an answer that cannot come.
    monkeypatch.setattr(codex, "STOP_TIMEOUT_S", 0.05)

    async def run():
        proc = FakeProcess()
        config = LiveConfig()
        call = await codex.negotiate_codex_call(OFFER, setup_for(config), config,
                                                server=server_for(proc))
        wire = codex.CodexLiveTransport(call, outbound=lambda _event: None)
        await anext(wire)
        proc.finish()
        with pytest.raises(StopAsyncIteration):
            await anext(wire)
        assert not wire.finalized
        await wire.close()

    asyncio.run(run())


def test_binary_override_and_path_lookup(monkeypatch):
    monkeypatch.setattr(codex.shutil, "which", lambda name: "C:/npm/codex.cmd")
    assert codex.codex_binary({}) == "C:/npm/codex.cmd"
    assert codex.codex_binary({codex.BINARY_ENV: " D:/codex.exe "}) == "D:/codex.exe"
