"""Live semantic session and lifecycle tests using fake transports."""

from __future__ import annotations

import asyncio

import pytest
from test_live_config_protocol import fake_auth, setup_for
from test_live_transport import SDP, Socket, aiohttp_peer

import talk_live_realtime as live
import talk_realtime as rt
from talk_live_config import LiveConfig
from talk_live_transport import LiveBrowserSession


class ScriptedTransport:
    def __init__(self, *, auth=None, config=None, ready=True, finalize=True):
        self.auth = auth
        self.config = config
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.ready = ready
        self.finalize = finalize
        self.final_usage = None
        self.setup = None

    async def connect(self, setup):
        self.setup = setup
        if self.ready:
            self.emit({"type": "session.started", "session": {"id": "live_test"}})

    def emit(self, event):
        self.queue.put_nowait(event)

    async def send_json(self, event):
        self.sent.append(event)
        if event["type"] == "session.close" and self.finalize:
            self.final_usage = {"seconds": 7}
            self.emit({"type": "session.closed", "reason": "close_requested"})

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item


def session_peer(mode="api", **kwargs):
    config = LiveConfig(mode)
    wire = ScriptedTransport(**kwargs)
    session = live.LiveRealtimeSession(
        auth=fake_auth(mode), config=config, transport_factory=lambda **_: wire
    )
    return session, wire, setup_for(config)


def delegation(identifier="del-1", *, legacy=False):
    item = {"type": "delegation", "target": "client", "id": identifier}
    return {
        "type": "delegation.created" if legacy else "session.delegation.created",
        "item" if legacy else "delegation": item,
    }


def test_native_api_factory_end_to_end_sends_pcm_and_finalizes_usage():
    async def run():
        module, client, socket = aiohttp_peer()
        config = LiveConfig("api")
        session = live.LiveRealtimeSession(
            auth=fake_auth("api"), config=config, aiohttp_module=module
        )
        await session.connect(setup_for(config))
        assert isinstance(await anext(session), rt.SessionReady)
        await session.send([rt.AppendInputAudio(b"\x01\x00\x02\x00")])
        assert socket.sent[-1] == {"type": "session.input_audio.append", "audio": "AQACAA=="}
        socket.emit(
            {
                "type": "session.input_transcript.delta",
                "delta": "hello",
                "start_ms": 0,
                "end_ms": 100,
            }
        )
        event = await anext(session)
        assert isinstance(event, rt.Transcript) and not event.final
        await session.close()
        assert session.state is rt.SessionState.CLOSED
        assert session.finalized
        assert session.final_usage == {"seconds": 3}
        assert isinstance(await anext(session), rt.SessionTerminated)
        with pytest.raises(StopAsyncIteration):
            await anext(session)
        assert socket.closed and client.closed

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["subscription", "api"])
def test_delegation_dedup_and_result_validation(mode):
    async def run():
        session, wire, setup = session_peer(mode)
        await session.connect(setup)
        await anext(session)
        wire.emit(delegation(legacy=mode == "subscription"))
        event = await anext(session)
        assert event.delegation_id == "del-1"
        wire.emit(delegation(legacy=mode == "subscription"))
        wire.emit({"type": "session.input_transcript.delta", "delta": "next"})
        assert isinstance(await anext(session), rt.Transcript)
        with pytest.raises(rt.RealtimeSessionError, match="unknown or retired"):
            await session.send([rt.SubmitDelegationResult("foreign", "bad")])
        assert not wire.sent
        await session.send([rt.SubmitDelegationResult("del-1", "Work is done")])
        assert wire.sent[0]["type"] == (
            "delegation.context.append" if mode == "subscription" else "session.commentary.append"
        )
        await session.close()

    asyncio.run(run())


def test_subscription_superseding_delegation_retires_old_result_authority():
    async def run():
        session, wire, setup = session_peer("subscription")
        await session.connect(setup)
        await anext(session)
        wire.emit(delegation("one", legacy=True))
        await anext(session)
        wire.emit(delegation("two", legacy=True))
        assert await anext(session) == rt.DelegationRetired("one")
        assert (await anext(session)).delegation_id == "two"
        with pytest.raises(rt.RealtimeSessionError, match="retired"):
            await session.send([rt.SubmitDelegationResult("one", "late result")])
        await session.send([rt.SubmitDelegationResult("two", "current result")])
        assert wire.sent[0]["delegation_item_id"] == "two"
        await session.close()

    asyncio.run(run())


def test_public_delegations_remain_separately_addressable():
    async def run():
        session, wire, setup = session_peer()
        await session.connect(setup)
        await anext(session)
        for name in ("one", "two"):
            wire.emit(delegation(name))
            assert (await anext(session)).delegation_id == name
        await session.send(
            [rt.SubmitDelegationResult("one", "first"), rt.SubmitDelegationResult("two", "second")]
        )
        assert [event["delegation_id"] for event in wire.sent] == ["one", "two"]
        await session.close()

    asyncio.run(run())


def test_no_commands_escape_invalid_batch_or_mutate_subscription_context():
    async def run():
        session, wire, setup = session_peer("subscription")
        await session.connect(setup)
        await anext(session)
        with pytest.raises(rt.RealtimeSessionError):
            await session.send([rt.AppendLiveContext("not sent"), rt.StartResponse()])
        assert not wire.sent
        await session.send([rt.AppendLiveContext("now sent")])
        assert "not sent" not in wire.sent[0]["session"]["instructions"]
        assert "Host policy" in wire.sent[0]["session"]["instructions"]
        assert "now sent" in wire.sent[0]["session"]["instructions"]
        await session.close()

    asyncio.run(run())


def test_disconnect_is_failure_and_not_final_usage_proof():
    async def run():
        session, wire, setup = session_peer()
        await session.connect(setup)
        await anext(session)
        wire.emit(EOFError("test-api-key"))
        failure = await anext(session)
        terminal = await anext(session)
        assert isinstance(failure, rt.ProviderFailure) and failure.terminal
        assert "test-api-key" not in failure.detail
        assert terminal.state is rt.SessionState.FAILED
        assert not session.finalized and session.final_usage is None
        await session.close()
        assert wire.closed

    asyncio.run(run())


def test_close_timeout_is_explicit_incomplete_finalization(monkeypatch):
    async def run():
        monkeypatch.setattr(live, "CLOSE_TIMEOUT_S", 0.01)
        session, wire, setup = session_peer(finalize=False)
        await session.connect(setup)
        await anext(session)
        await session.close()
        failure = await anext(session)
        assert failure.terminal and "unconfirmed" in failure.detail
        assert session.state is rt.SessionState.FAILED
        assert not session.finalized and wire.closed
        assert isinstance(await anext(session), rt.SessionTerminated)

    asyncio.run(run())


def test_cancellation_during_startup_closes_all_resources():
    async def run():
        module, client, socket = aiohttp_peer(Socket(auto_start=False))
        config = LiveConfig("api")
        session = live.LiveRealtimeSession(
            auth=fake_auth("api"), config=config, aiohttp_module=module
        )
        task = asyncio.create_task(session.connect(setup_for(config)))
        while not socket.sent:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.state is rt.SessionState.CLOSED
        assert socket.closed and client.closed

    asyncio.run(run())


def test_foreign_session_id_cannot_replace_bound_identity():
    async def run():
        session, wire, setup = session_peer()
        await session.connect(setup)
        await anext(session)
        wire.emit({"type": "session.started", "session": {"id": "foreign"}})
        assert (await anext(session)).terminal
        assert session.session_id == "live_test"
        await session.close()

    asyncio.run(run())


def test_browser_sideband_never_restarts_session_or_duplicates_media():
    async def run():
        module, client, socket = aiohttp_peer()
        config = LiveConfig("api")
        broker = LiveBrowserSession(
            answer_sdp=SDP,
            session_id="live_test",
            sideband_url="wss://api.openai.com/v1/live/sessions/live_test/attach",
            auth=fake_auth("api"),
            config=config,
            setup=setup_for(config),
            aiohttp_module=module,
        )
        session = await broker.open_session()
        assert await broker.open_session() is session
        assert isinstance(await anext(session), rt.SessionReady)
        assert not socket.sent
        with pytest.raises(rt.RealtimeSessionError, match="media track"):
            await session.send([rt.AppendInputAudio(b"\x00\x00")])
        socket.emit({"type": "session.output_audio.delta", "delta": "AAAAAA=="})
        socket.emit({"type": "session.output_transcript.delta", "delta": "hello"})
        assert isinstance(await anext(session), rt.Transcript)
        await broker.close()
        assert socket.sent == [{"type": "session.close"}]
        assert client.closed and socket.closed

    asyncio.run(run())


def test_provider_close_before_ready_cannot_resurrect_session():
    async def run():
        session, wire, setup = session_peer("subscription", ready=False)
        wire.emit({"type": "session.closed", "reason": "expired"})
        with pytest.raises(rt.RealtimeSessionError, match="before session readiness"):
            await session.connect(setup)
        assert session.state is rt.SessionState.FAILED
        assert wire.closed

    asyncio.run(run())


def test_close_during_connect_cancels_pending_connection_and_never_reopens():
    async def run():
        module, client, socket = aiohttp_peer(Socket(auto_start=False))
        config = LiveConfig("api")
        session = live.LiveRealtimeSession(
            auth=fake_auth("api"), config=config, aiohttp_module=module
        )
        task = asyncio.create_task(session.connect(setup_for(config)))
        while not socket.sent:
            await asyncio.sleep(0)
        await session.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.state is rt.SessionState.CLOSED
        assert socket.closed and client.closed
        with pytest.raises(rt.RealtimeSessionError, match="only run once"):
            await session.connect(setup_for(config))

    asyncio.run(run())


def test_subscription_browser_accepts_idless_started_event_using_negotiated_identity():
    async def run():
        module, _, socket = aiohttp_peer()
        broker = LiveBrowserSession(
            answer_sdp=SDP,
            session_id="rtc_test",
            sideband_url="wss://api.openai.com/v1/live/rtc_test",
            auth=fake_auth(),
            config=LiveConfig(),
            setup=setup_for(),
            aiohttp_module=module,
        )
        session = await broker.open_session()
        await anext(session)
        socket.emit({"type": "session.started", "session": {"expires_at": 9000}})
        socket.emit({"type": "input_transcript.added", "item": {"text": "hello"}})
        assert isinstance(await anext(session), rt.Transcript)
        await broker.close()
        with pytest.raises(rt.RealtimeSessionError, match="binding is closed"):
            await broker.open_session()

    asyncio.run(run())
