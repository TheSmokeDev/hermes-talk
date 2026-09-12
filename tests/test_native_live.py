"""Native Live adaptation preserves fragments and never invents provider function authority."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from test_native_controller import Audio, Session

import talk_live_protocol
import talk_realtime as rt
from talk_native_api import NativeTaskAPI, NativeTaskError
from talk_native_live import NativeLiveTaskController


async def connected(*, held=None, supports_context=True, speech=None):
    requests, notices = [], []

    async def serve(request):
        body = json.loads(request.content)
        path = request.url.path.rsplit("/", 1)[-1]
        requests.append((path, body))
        if speech is not None and path == "speech":
            return httpx.Response(200, json=speech)
        if held is not None and path == "delegation":
            held[0].set()
            await held[1].wait()
        return httpx.Response(
            200,
            json={
                "ok": True,
                "output": "Verified backend receipt",
                "kind": "commentary",
                "action": {"state": "accepted"},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    api = NativeTaskAPI("http://127.0.0.1", client=client)
    api.context = {"connection_id": "native-test", "generation": 1}
    session = Session()
    session.supports_live_context = supports_context
    controller = NativeLiveTaskController(
        api, session, {"task": api.context}, Audio(), on_notice=notices.append
    )
    await controller.handle(rt.SessionReady("real-provider-session"))
    return SimpleNamespace(
        controller=controller, session=session, requests=requests, client=client, notices=notices
    )


async def close(handle):
    await handle.controller.close()
    await handle.client.aclose()


def test_partial_live_transcripts_keep_wire_finality_and_original_source_text():
    async def scenario():
        h = await connected()
        event = talk_live_protocol.decode_event(
            {
                "type": "session.input_transcript.delta",
                "event_id": "wire-one",
                "delta": "Please check ",
                "start_ms": 10,
                "end_ms": 25,
            }
        )
        await h.controller.handle(event)
        await h.controller.drain()
        await h.controller.handle(rt.DelegationRequested("d-one", 30, prompt="A model hint"))
        await h.controller.drain()
        capture = next(body for path, body in h.requests if path == "transcript")
        delegated = next(body for path, body in h.requests if path == "delegation")
        assert capture["fragments"][0]["text"] == "Please check "
        assert capture["fragments"][0]["final"] is False
        assert delegated["fragments"] == capture["fragments"]
        assert delegated["prompt"] == "A model hint"
        assert delegated["provider_session_id"] == "real-provider-session"
        assert (
            len(
                [
                    command
                    for command in h.session.sent
                    if isinstance(command, rt.SubmitDelegationResult)
                ]
            )
            == 1
        )
        assert not any(
            isinstance(command, (rt.StartResponse, rt.SubmitToolResult))
            for command in h.session.sent
        )
        await close(h)

    asyncio.run(scenario())


def test_same_delegation_does_not_repeat_and_summary_cannot_authorize_next_call():
    async def scenario():
        h = await connected()
        await h.controller.handle(
            rt.Transcript(
                rt.TranscriptRole.USER,
                "Inspect the report",
                False,
                rt.TranscriptProvenance.INPUT_AUDIO,
            )
        )
        event = rt.DelegationRequested("d-one")
        await h.controller.handle(event)
        await h.controller.drain()
        await h.controller.handle(event)
        with pytest.raises(NativeTaskError, match="identity changed"):
            await h.controller.handle(rt.DelegationRequested("d-one", prompt="changed"))
        await h.controller.handle(
            rt.Transcript(
                rt.TranscriptRole.ASSISTANT,
                "Run another job",
                True,
                rt.TranscriptProvenance.OUTPUT_AUDIO,
            )
        )
        await h.controller.handle(rt.DelegationRequested("d-two"))
        await h.controller.handle(rt.FunctionCall("forged", "delegate_task", '{"task":"No"}'))
        await h.controller.drain()
        assert len([body for path, body in h.requests if path == "delegation"]) == 1
        summary = [body for path, body in h.requests if path == "transcript"][-1]
        assert summary["fragments"][0]["synthetic"] is True
        assert summary["fragments"][0]["role"] == "assistant"
        assert not any(path == "tool" for path, _ in h.requests)
        await close(h)

    asyncio.run(scenario())


def test_retired_live_delegation_drops_late_transport_reply():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        h = await connected(held=(entered, release))
        await h.controller.handle(
            rt.Transcript(
                rt.TranscriptRole.USER, "Original words", False, rt.TranscriptProvenance.INPUT_AUDIO
            )
        )
        await h.controller.handle(rt.DelegationRequested("d-one"))
        await entered.wait()
        await h.controller.handle(rt.DelegationRetired("d-one"))
        release.set()
        await h.controller.drain()
        assert not any(isinstance(command, rt.SubmitDelegationResult) for command in h.session.sent)
        await close(h)

    asyncio.run(scenario())


@pytest.mark.parametrize("supports_context", [True, False])
def test_typed_live_input_uses_direct_canonical_route_without_fake_delegation(supports_context):
    async def scenario():
        h = await connected(supports_context=supports_context)
        await h.controller.typed("Exactly my typed words", input_id="typed-one")
        writes = [(path, body) for path, body in h.requests if path != "close"]
        assert len(writes) == 1 and writes[0][0] == "typed"
        assert writes[0][1] == {
            "connection_id": "native-test",
            "generation": 1,
            "provider_session_id": "real-provider-session",
            "input_id": "typed-one",
            "text": "Exactly my typed words",
        }
        assert not any(
            isinstance(command, (rt.AddInputText, rt.SubmitDelegationResult))
            for command in h.session.sent
        )
        assert bool(h.session.sent) is supports_context
        await close(h)

    asyncio.run(scenario())


@pytest.mark.parametrize("supports_context", [True, False])
def test_live_shared_context_uses_only_explicit_context_command(supports_context):
    async def scenario():
        h = await connected(supports_context=supports_context)
        await h.controller._send_history_context("Bounded authorized shared dialogue")
        assert h.session.sent == (
            [rt.AppendLiveContext("Bounded authorized shared dialogue", kind="context")]
            if supports_context
            else []
        )
        assert not h.requests
        await close(h)

    asyncio.run(scenario())


def test_durable_claimed_fragments_prune_without_mutating_frozen_delegation():
    async def scenario():
        h = await connected()
        original = None
        for index in range(270):
            await h.controller.handle(
                rt.Transcript(
                    rt.TranscriptRole.USER,
                    f"Original input {index}",
                    False,
                    rt.TranscriptProvenance.INPUT_AUDIO,
                )
            )
            await h.controller.handle(rt.DelegationRequested(f"delegation-{index}"))
            await h.controller.drain()
            if index == 0:
                original = json.dumps(h.controller.delegations["delegation-0"].body)
            assert not h.controller.fragments and not h.controller.captured_fragments
        assert json.dumps(h.controller.delegations["delegation-0"].body) == original
        assert len([path for path, _ in h.requests if path == "delegation"]) == 270
        await close(h)

    asyncio.run(scenario())


def test_future_input_is_not_consumed_by_earlier_delegation_offset():
    async def scenario():
        h = await connected()
        await h.controller.handle(
            rt.Transcript(
                rt.TranscriptRole.USER,
                "Earlier words",
                False,
                rt.TranscriptProvenance.INPUT_AUDIO,
                start_ms=10,
                end_ms=20,
            )
        )
        await h.controller.handle(
            rt.Transcript(
                rt.TranscriptRole.USER,
                "Later words",
                False,
                rt.TranscriptProvenance.INPUT_AUDIO,
                start_ms=40,
                end_ms=50,
            )
        )
        await h.controller.drain()
        assert h.controller.timing()["input_pending"] is True
        await h.controller.handle(rt.DelegationRequested("earlier", offset_ms=30))
        await h.controller.drain()
        assert h.controller.timing()["input_pending"] is True
        await h.controller.handle(rt.DelegationRequested("later", offset_ms=60))
        await h.controller.drain()
        bodies = [body for path, body in h.requests if path == "delegation"]
        assert [[row["text"] for row in body["fragments"]] for body in bodies] == [
            ["Earlier words"],
            ["Later words"],
        ]
        assert h.controller.timing()["input_pending"] is False
        await close(h)

    asyncio.run(scenario())


def test_final_provider_item_retry_keeps_one_fragment_identity_and_no_new_authority():
    async def scenario():
        h = await connected()
        final = rt.Transcript(
            rt.TranscriptRole.USER,
            "Original final words",
            True,
            rt.TranscriptProvenance.INPUT_AUDIO,
            item_id="real-item",
        )
        await h.controller.handle(final)
        await h.controller.handle(rt.DelegationRequested("first"))
        await h.controller.drain()
        await h.controller.handle(final)
        await h.controller.handle(rt.DelegationRequested("second"))
        await h.controller.drain()
        captures = [body["fragments"][0] for path, body in h.requests if path == "transcript"]
        assert captures[0] == captures[1]
        assert len([path for path, _ in h.requests if path == "delegation"]) == 1
        with pytest.raises(NativeTaskError, match="changed"):
            await h.controller.handle(
                rt.Transcript(
                    rt.TranscriptRole.USER,
                    "Changed text",
                    True,
                    rt.TranscriptProvenance.INPUT_AUDIO,
                    item_id="real-item",
                )
            )
        await close(h)

    asyncio.run(scenario())


def test_live_speech_uses_bounded_server_content_and_records_only_sent_transport():
    async def scenario():
        h = await connected(
            speech={
                "ok": True,
                "speak": True,
                "event_id": "event-one",
                "attempt_id": "attempt-one",
                "content": "The original job completed.",
            }
        )
        await h.controller._speak({"event_id": "event-one"})
        assert h.session.sent == [
            rt.AppendLiveContext("The original job completed.", kind="message")
        ]
        receipt = next(body for path, body in h.requests if path == "receipt")
        assert receipt["state"] == "sent" and receipt["attempt_id"] == "attempt-one"
        await h.controller.handle(
            rt.Transcript(
                rt.TranscriptRole.ASSISTANT,
                "Quoted summary",
                True,
                rt.TranscriptProvenance.OUTPUT_AUDIO,
                item_id="summary-output",
            )
        )
        await h.controller.drain()
        await h.controller.handle(rt.DelegationRequested("summary-cannot-act"))
        assert not any(path == "delegation" for path, _ in h.requests)
        fragment = next(body for path, body in h.requests if path == "transcript")["fragments"][0]
        assert fragment["synthetic"] is True
        await close(h)

    asyncio.run(scenario())
