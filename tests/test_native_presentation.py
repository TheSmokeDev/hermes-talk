"""Exact native summary receipts follow local output consumption, never generation alone."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from test_native_controller import Session
from test_native_discord import room as room

import talk_audio
import talk_discord
import talk_realtime as rt
from talk_native_api import NativeTaskAPI, NativeTaskError
from talk_native_controller import NativeTaskController


async def connected(*, audio=None, fail_receipt=None):
    requests = []

    async def serve(request):
        body = json.loads(request.content)
        path = request.url.path.rsplit("/", 1)[-1]
        requests.append((path, body))
        if path == "speech":
            attempt = "attempt-" + str(sum(path == "speech" for path, _ in requests))
            return httpx.Response(200, json={
                "ok": True, "speak": True, "event_id": body["event_id"], "attempt_id": attempt,
                "response": {
                    "conversation": "none", "tools": [], "tool_choice": "none", "input": [],
                    "metadata": {"talk_presentation_id": attempt,
                                 "talk_event_id": body["event_id"]},
                },
            })
        if path == "receipt" and body["state"] == fail_receipt:
            return httpx.Response(409, json={"detail": "expired claim"})
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    api = NativeTaskAPI("http://127.0.0.1", client=client)
    api.context = {"connection_id": "owner-a", "generation": 3}
    session = Session()
    controller = NativeTaskController(
        api, session, {"task": dict(api.context)}, audio or talk_audio.DuplexAudio()
    )
    return SimpleNamespace(controller=controller, session=session, requests=requests, client=client)


def receipts(handle):
    return [body for path, body in handle.requests if path == "receipt"]


def states(handle):
    return [body["state"] for body in receipts(handle)]


async def start(handle):
    await handle.controller._speak({"event_id": "event-one"})
    await handle.controller.handle(
        rt.ResponseStarted("summary-one", handle.session.responses[-1].metadata)
    )


async def finish(handle):
    await handle.controller.handle(rt.ResponseFinished("summary-one", "completed", ()))


def consume(audio, frames=480):
    audio._output_callback(bytearray(frames * talk_audio.FRAME_BYTES), frames, None, None)


async def close(handle):
    await handle.controller.close()
    await handle.client.aclose()


def test_terminal_summary_finishes_after_generation_and_exact_local_consumption():
    async def scenario():
        h = await connected()
        await start(h)
        speech = next(body for path, body in h.requests if path == "speech")
        assert speech["presentation_protocol"] == 1 and speech["playback_supported"] is True
        await h.controller.handle(rt.OutputAudio(b"\x01\x00" * 960, "item", "summary-one"))
        assert states(h) == ["submitting", "context_submitted"]
        await finish(h)
        assert states(h) == ["submitting", "context_submitted"]
        consume(h.controller.audio)
        await h.controller.tick()
        assert states(h)[-1] == "playback_started"
        assert h.controller.presentation is not None
        consume(h.controller.audio)
        await h.controller.tick()
        assert states(h) == [
            "submitting", "context_submitted", "playback_started", "playback_finished"
        ]
        await finish(h)
        await h.controller.tick()
        assert len(receipts(h)) == 4
        assert all(row["response_id"] == "summary-one" for row in receipts(h)[2:])
        await close(h)

    asyncio.run(scenario())


def test_local_drain_before_provider_completion_is_started_only():
    async def scenario():
        h = await connected()
        await start(h)
        await h.controller.handle(rt.OutputAudio(b"\x01\x00" * 480, "item", "summary-one"))
        consume(h.controller.audio)
        await h.controller.tick()
        assert states(h)[-1] == "playback_started"
        await finish(h)
        assert states(h)[-1] == "playback_finished"
        await close(h)

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["no_audio", "overflow", "external_drain", "disconnect"])
def test_missing_or_dropped_output_never_finishes(outcome):
    async def scenario():
        h = await connected()
        await start(h)
        if outcome == "overflow":
            pcm = bytes(talk_audio.MAX_PLAYBACK_BYTES + 2)
            await h.controller.handle(rt.OutputAudio(pcm, "item", "summary-one"))
        elif outcome in {"external_drain", "disconnect"}:
            await h.controller.handle(rt.OutputAudio(bytes(960), "item", "summary-one"))
            if outcome == "external_drain":
                h.controller.audio.drain_playback()
        if outcome == "disconnect":
            await h.controller.close()
        else:
            await finish(h)
        assert states(h)[-1] == "unknown"
        assert "playback_finished" not in states(h)
        await close(h)

    asyncio.run(scenario())


def test_interrupted_summary_cannot_finish_or_accept_late_audio_and_replay_is_explicit():
    async def scenario():
        h = await connected()
        await start(h)
        await h.controller.handle(rt.OutputAudio(bytes(1920), "item", "summary-one"))
        consume(h.controller.audio)
        await h.controller.interrupt()
        assert states(h)[-2:] == ["playback_started", "interrupted"]
        await h.controller.handle(rt.OutputAudio(bytes(960), "late", "summary-one"))
        await finish(h)
        await h.controller.tick()
        assert states(h)[-1] == "interrupted" and not h.controller.audio.playback_pending
        await h.controller.command("/replay event-one")
        speech = [body for path, body in h.requests if path == "speech"]
        assert len(speech) == 2 and speech[-1]["replay"] is True
        assert speech[-1]["event_id"] == "event-one"
        assert {path for path, _ in h.requests} == {"speech", "receipt"}
        assert receipts(h)[-1]["attempt_id"] == "attempt-2"
        await close(h)

    asyncio.run(scenario())


def test_foreign_audio_and_duplicate_response_start_do_not_corrupt_summary():
    async def scenario():
        h = await connected()
        await start(h)
        await h.controller.handle(
            rt.ResponseStarted("summary-one", h.session.responses[-1].metadata)
        )
        await h.controller.handle(rt.OutputAudio(bytes(960), "other", "foreign"))
        assert not h.controller.audio.playback_pending
        await finish(h)
        assert states(h)[-1] == "unknown"
        await close(h)

    asyncio.run(scenario())


def test_submitting_must_be_persisted_before_any_provider_dispatch():
    async def scenario():
        h = await connected(fail_receipt="submitting")
        with pytest.raises(NativeTaskError, match="409"):
            await h.controller._speak({"event_id": "event-one"})
        assert not h.session.sent
        await close(h)

    asyncio.run(scenario())


def test_failed_provider_handoff_is_unknown_without_retry():
    async def scenario():
        h = await connected()

        async def fail(commands):
            assert states(h) == ["submitting"]
            raise OSError("provider disconnected during send")

        h.session.send = fail
        with pytest.raises(OSError, match="disconnected"):
            await h.controller._speak({"event_id": "event-one"})
        assert states(h) == ["submitting", "unknown"]
        await h.controller.tick()
        assert sum(path == "speech" for path, _ in h.requests) == 1
        await close(h)

    asyncio.run(scenario())


def test_discord_real_audio_source_finishes_only_when_player_consumes_frames(room):
    # The real library is not a dependency of the plugin; this case proves the
    # actual AudioSource contract where it is installed and skips where it is not.
    discord = pytest.importorskip("discord")

    async def scenario():
        room.audio._source = talk_discord._new_source(room.audio._outbound)
        assert isinstance(room.audio._source, discord.AudioSource)
        guard = room.audio.bind_native_surface(room.proof)
        h = await connected(audio=room.audio)
        h.controller.authorize_surface = guard
        await start(h)
        await h.controller.handle(rt.OutputAudio(bytes(1920), "item", "summary-one"))
        await finish(h)
        assert states(h)[-1] == "context_submitted"
        room.audio._source.read()
        await h.controller.tick()
        assert states(h)[-1] == "playback_started"
        room.audio._source.read()
        await h.controller.tick()
        assert states(h)[-1] == "playback_finished"
        await close(h)

    asyncio.run(scenario())


def test_discord_audience_revocation_drops_queued_private_summary_before_next_frame(room):
    discord = pytest.importorskip("discord")

    async def scenario():
        room.audio._source = talk_discord._new_source(room.audio._outbound)
        assert isinstance(room.audio._source, discord.AudioSource)
        h = await connected(audio=room.audio)
        h.controller.authorize_surface = room.audio.bind_native_surface(room.proof)
        await start(h)
        await h.controller.handle(rt.OutputAudio(b"\x01\x00" * 960, "item", "summary-one"))
        await finish(h)
        room.permitted.remove("202")
        assert room.audio._source.read() == talk_discord.SILENCE_FRAME
        with pytest.raises(NativeTaskError, match="audience authorization changed"):
            await h.controller.tick()
        assert "playback_finished" not in states(h) and not room.audio.playback_pending
        await close(h)

    asyncio.run(scenario())


def test_restored_discord_access_does_not_hide_audio_discarded_during_revocation(room):
    async def scenario():
        h = await connected(audio=room.audio)
        h.controller.authorize_surface = room.audio.bind_native_surface(room.proof)
        await start(h)
        await h.controller.handle(rt.OutputAudio(bytes(1920), "item", "summary-one"))
        room.audio._source.read()
        await h.controller.tick()
        assert states(h)[-1] == "playback_started"
        room.permitted.remove("202")
        room.audio._source.read()
        room.permitted.add("202")
        await finish(h)
        assert states(h)[-1] == "unknown"
        assert "playback_finished" not in states(h)
        await close(h)

    asyncio.run(scenario())


def test_generation_switch_while_dispatch_fence_is_pending_never_sends_old_summary():
    async def scenario():
        h = await connected()
        original = h.controller._speech_receipt

        async def switch(receipt, state, **kwargs):
            await original(receipt, state, **kwargs)
            if state == "submitting":
                h.controller.api.context = {"connection_id": "owner-b", "generation": 4}

        h.controller._speech_receipt = switch
        with pytest.raises(NativeTaskError, match="no longer current"):
            await h.controller._speak({"event_id": "event-one"})
        assert not h.session.sent
        await close(h)

    asyncio.run(scenario())


def test_operator_speaking_during_dispatch_fence_prevents_provider_summary():
    async def scenario():
        h = await connected()
        original = h.controller._speech_receipt

        async def interrupt(receipt, state, **kwargs):
            await original(receipt, state, **kwargs)
            if state == "submitting":
                h.controller.operator_speaking = True

        h.controller._speech_receipt = interrupt
        result = await h.controller._speak({"event_id": "event-one"})
        assert not result["speak"] and not h.session.sent
        assert states(h) == ["submitting", "unknown"]
        await close(h)

    asyncio.run(scenario())


@pytest.mark.parametrize("retired_state", ["interrupted", "unknown"])
def test_successful_handoff_after_retirement_still_reports_context_fact(retired_state):
    async def scenario():
        h = await connected()
        entered, release = asyncio.Event(), asyncio.Event()
        original = h.session.send

        async def held(commands):
            await original(commands)
            entered.set()
            await release.wait()

        h.session.send = held
        pending = asyncio.create_task(h.controller._speak({"event_id": "event-one"}))
        await entered.wait()
        presentation = h.controller.presentation
        if retired_state == "interrupted":
            await h.controller.interrupt()
        else:
            await h.controller._retire_presentation(presentation, retired_state)
        release.set()
        await pending
        assert states(h) == ["submitting", retired_state, "context_submitted"]
        assert presentation["retired"] and presentation["context_submitted"]
        assert h.controller.presentation is None
        await h.controller.tick()
        assert "playback_finished" not in states(h)
        await close(h)

    asyncio.run(scenario())
