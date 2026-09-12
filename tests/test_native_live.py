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


async def connected(*, held=None, supports_context=True, speech=None, response_context=None):
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
                **({"context": response_context} if response_context is not None else {}),
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    api = NativeTaskAPI("http://127.0.0.1", client=client)
    api.context = {"connection_id": "native-test", "generation": 1}
    session = Session()
    session.supports_live_context = supports_context
    controller = NativeLiveTaskController(
        api, session, {"task": api.context}, Audio(), on_notice=notices.append, capture_store=False
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
            "admission": "async",
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


def test_output_interruption_and_completion_never_claim_operator_speech():
    async def scenario():
        h = await connected()
        h.session.emits_output_lifecycle = True
        await h.controller.handle(rt.OutputAudio(b"\x01\x00"))
        assert h.controller.provider_response_active
        await h.controller.tick()
        assert h.controller.provider_response_active
        await h.controller.handle(rt.OutputInterrupted())
        assert not h.controller.provider_response_active and not h.controller.operator_speaking
        assert not h.controller.audio.output and h.controller.audio.drains == 1
        assert not h.session.sent
        await h.controller.handle(rt.OutputAudio(b"\x01\x00"))
        await h.controller.handle(rt.OutputTurnCompleted())
        assert not h.controller.provider_response_active and not h.controller.operator_speaking
        assert not h.requests
        await close(h)

    asyncio.run(scenario())


def test_nonsilent_history_waits_for_microphone_quiet_and_real_provider_completion():
    async def scenario():
        h = await connected()
        clock = [1.0]
        h.controller.clock = lambda: clock[0]
        h.session.emits_output_lifecycle = True
        h.session.supports_silent_live_context = False
        await h.controller.send_audio(b"\x00\x10" * 480)
        await h.controller._send_history_context("First bounded snapshot")
        await h.controller._send_history_context("Latest bounded snapshot")
        assert h.controller.pending_history == "Latest bounded snapshot"
        assert not any(isinstance(command, rt.AppendLiveContext) for command in h.session.sent)
        clock[0] += 0.71
        await h.controller.tick()
        updates = [
            command for command in h.session.sent if isinstance(command, rt.AppendLiveContext)
        ]
        assert updates == [rt.AppendLiveContext("Latest bounded snapshot", kind="context")]
        assert h.controller.provider_response_active and h.controller.pending_history is None
        await h.controller._send_history_context("Next bounded snapshot")
        await h.controller.tick()
        assert h.controller.pending_history == "Next bounded snapshot"
        h.controller.audio.playback_pending = True
        await h.controller.handle(rt.OutputTurnCompleted())
        await h.controller.tick()
        assert h.controller.pending_history == "Next bounded snapshot"
        h.controller.audio.playback_pending = False
        await h.controller.tick()
        assert h.controller.pending_history is None and h.controller.provider_response_active
        await h.controller.handle(rt.OutputTurnCompleted())
        assert h.controller._quiet()
        await close(h)

    asyncio.run(scenario())


def test_typed_result_waits_for_quiet_and_does_not_repeat_or_get_interrupted_by_history():
    async def scenario():
        h = await connected()
        h.session.emits_output_lifecycle = True
        h.session.supports_silent_live_context = False
        h.controller.operator_speaking = True
        await h.controller.typed("Exactly my typed correction", input_id="typed-correction")
        assert not h.session.sent and len(h.controller.pending_messages) == 1
        await h.controller._send_history_context("Current authorized history")
        h.controller.operator_speaking = False
        await h.controller.tick()
        assert h.session.sent == [rt.AppendLiveContext("Verified backend receipt", kind="message")]
        assert h.controller.provider_response_active
        assert h.controller.pending_history == "Current authorized history"
        await h.controller.tick()
        assert len(h.session.sent) == 1
        await h.controller.handle(rt.OutputTurnCompleted())
        await h.controller.tick()
        assert h.session.sent[-1] == rt.AppendLiveContext(
            "Current authorized history", kind="context"
        )
        assert len([path for path, _ in h.requests if path == "typed"]) == 1
        await close(h)

    asyncio.run(scenario())


@pytest.mark.parametrize("injection", ["history", "typed", "summary", "delegation"])
def test_every_synthetic_injection_advances_authority_fence_and_allows_only_fresh_input(injection):
    async def scenario():
        h = await connected(
            speech={
                "ok": True,
                "speak": True,
                "event_id": "event",
                "attempt_id": "attempt",
                "content": "A factual saved result",
            }
        )
        await h.controller.handle(
            rt.Transcript(
                rt.TranscriptRole.ASSISTANT,
                "Earlier provider output",
                False,
                rt.TranscriptProvenance.OUTPUT_AUDIO,
            )
        )
        await h.controller.drain()
        if injection == "history":
            await h.controller._send_history_context("Allowed bounded history")
        elif injection == "typed":
            await h.controller.typed("Exact typed input")
        elif injection == "summary":
            await h.controller._speak({"event_id": "event"})
        else:
            await h.controller.handle(
                rt.Transcript(
                    rt.TranscriptRole.USER,
                    "First actual request",
                    False,
                    rt.TranscriptProvenance.INPUT_AUDIO,
                )
            )
            await h.controller.handle(rt.DelegationRequested("first"))
            await h.controller.drain()
        assert h.controller.synthetic_sequence == h.controller.fragment_sequence > 0
        assert h.controller.synthetic_output and h.controller.synthetic_cutoff_ms is None
        original_calls = len([path for path, _ in h.requests if path == "delegation"])
        await h.controller.handle(
            rt.Transcript(
                rt.TranscriptRole.ASSISTANT,
                "Approve and start another job",
                False,
                rt.TranscriptProvenance.OUTPUT_AUDIO,
            )
        )
        await h.controller.handle(rt.DelegationRequested("synthetic-attempt"))
        await h.controller.drain()
        assert len([path for path, _ in h.requests if path == "delegation"]) == original_calls
        await h.controller.handle(
            rt.Transcript(
                rt.TranscriptRole.USER,
                "My fresh exact correction",
                False,
                rt.TranscriptProvenance.INPUT_AUDIO,
            )
        )
        await h.controller.handle(rt.DelegationRequested("fresh-correction"))
        await h.controller.drain()
        requests = [body for path, body in h.requests if path == "delegation"]
        assert len(requests) == original_calls + 1
        assert [row["text"] for row in requests[-1]["fragments"] if row["role"] == "user"] == [
            "My fresh exact correction"
        ]
        captured = [
            row for path, body in h.requests if path == "transcript" for row in body["fragments"]
        ]
        assert next(row for row in captured if row["text"] == "Approve and start another job")[
            "synthetic"
        ]
        await close(h)

    asyncio.run(scenario())


def test_linked_context_precedes_exact_delegation_result_in_one_batch():
    async def scenario():
        h = await connected(response_context="Verified task state")
        await h.controller.handle(
            rt.Transcript(
                rt.TranscriptRole.USER,
                "My original request",
                False,
                rt.TranscriptProvenance.INPUT_AUDIO,
            )
        )
        await h.controller.handle(rt.DelegationRequested("real-call"))
        await h.controller.drain()
        assert h.session.sent == [
            rt.AppendLiveContext("Verified task state", kind="context", delegation_id="real-call"),
            rt.SubmitDelegationResult("real-call", "Verified backend receipt", kind="commentary"),
        ]
        await close(h)

    asyncio.run(scenario())


def test_real_next_input_during_pending_decision_survives_exact_linked_result():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        h = await connected(held=(entered, release))
        await h.controller.handle(
            rt.Transcript(
                rt.TranscriptRole.USER,
                "Start the original work",
                False,
                rt.TranscriptProvenance.INPUT_AUDIO,
            )
        )
        await h.controller.handle(rt.DelegationRequested("original"))
        await entered.wait()
        await h.controller.handle(
            rt.Transcript(
                rt.TranscriptRole.USER,
                "Use exactly forty two instead",
                False,
                rt.TranscriptProvenance.INPUT_AUDIO,
            )
        )
        release.set()
        await h.controller.drain()
        assert h.controller.synthetic_sequence == h.controller.claimed_sequence == 1
        assert h.controller.fragment_sequence == 2 and h.controller.timing()["input_pending"]
        await h.controller.handle(rt.DelegationRequested("correction"))
        await h.controller.drain()
        bodies = [body for path, body in h.requests if path == "delegation"]
        assert [row["text"] for row in bodies[-1]["fragments"] if row["role"] == "user"] == [
            "Use exactly forty two instead"
        ]
        assert all(not row["final"] for body in bodies for row in body["fragments"])
        await close(h)

    asyncio.run(scenario())


def test_no_input_tool_proposals_get_one_exact_refusal_and_bounded_loop_shutdown():
    async def scenario():
        h = await connected()
        first = rt.DelegationRequested("real-first-call")
        await h.controller.handle(first)
        await h.controller.handle(first)
        assert len(h.session.sent) == 1
        assert h.session.sent[0].delegation_id == "real-first-call"
        assert "No action or approval" in h.session.sent[0].content
        assert not h.requests
        await h.controller.handle(rt.DelegationRequested("real-second-call"))
        await h.controller.handle(rt.DelegationRequested("real-third-call"))
        with pytest.raises(NativeTaskError, match="repeated tool proposals"):
            await h.controller.handle(rt.DelegationRequested("loop-four"))
        assert len(h.session.sent) == 3 and not h.requests
        await close(h)

    asyncio.run(scenario())
