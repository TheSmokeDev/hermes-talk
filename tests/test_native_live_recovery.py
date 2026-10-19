"""Offline transport latency, transcript batching, and exact operation recovery."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from test_native_controller import Audio, Session
from test_native_live import close, connected

import talk_realtime as rt
from talk_native_api import NativeTaskAPI, NativeTaskError
from talk_native_live import NativeLiveTaskController


def transcript(text, identity, *, start=0, end=10):
    return rt.Transcript(
        rt.TranscriptRole.USER,
        text,
        False,
        rt.TranscriptProvenance.INPUT_AUDIO,
        item_id="item-one",
        event_id=identity,
        finality="delta",
        start_ms=start,
        end_ms=end,
    )


async def controller_for(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = NativeTaskAPI("http://127.0.0.1", client=client)
    api.context = {"connection_id": "original", "generation": 1}
    session, notices = Session(), []
    controller = NativeLiveTaskController(
        api,
        session,
        {"task": dict(api.context)},
        Audio(),
        on_notice=notices.append,
        capture_store=False,
    )
    await controller.handle(rt.SessionReady("provider-session"))
    return SimpleNamespace(controller=controller, session=session, notices=notices, client=client)


def test_twenty_five_second_decision_does_not_block_capture_or_state_transport():
    async def scenario():
        entered = asyncio.Event()

        async def handler(request):
            if request.url.path.endswith("delegation"):
                entered.set()
                await asyncio.sleep(25)
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = NativeTaskAPI("http://127.0.0.1", client=client)
            api.context = {"connection_id": "original", "generation": 1}
            decision = asyncio.create_task(api.request("/live/delegation"))
            try:
                await entered.wait()
                started = time.monotonic()
                await asyncio.wait_for(
                    asyncio.gather(
                        api.request("/state"),
                        api.request("/live/transcript", {"fragments": []}),
                    ),
                    0.1,
                )
                assert time.monotonic() - started < 0.1
                assert not decision.done()
            finally:
                decision.cancel()
                await asyncio.gather(decision, return_exceptions=True)

    asyncio.run(scenario())


def test_transport_concurrency_is_bounded_without_attachment_mutex():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        active, peak = 0, 0

        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 4:
                entered.set()
            await release.wait()
            active -= 1
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = NativeTaskAPI("http://127.0.0.1", client=client)
            api.context = {"connection_id": "original", "generation": 1}
            requests = [asyncio.create_task(api.request("/state")) for _ in range(7)]
            await asyncio.wait_for(entered.wait(), 0.1)
            assert peak == 4 and active == 4
            release.set()
            await asyncio.gather(*requests)
            assert peak == 4

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,category,superseded", [(200, "stale", True), (403, "authorization", False)]
)
def test_inflight_old_response_never_becomes_new_target_data(status, category, superseded):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            if request.url.path.endswith("attach"):
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "task": {"connection_id": "new", "generation": 2},
                        "instructions": "Bounded context",
                        "tools": [],
                    },
                )
            entered.set()
            await release.wait()
            return httpx.Response(status, json={"ok": True, "private": "old task"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = NativeTaskAPI("http://127.0.0.1", client=client)
            api.context = {"connection_id": "old", "generation": 1}
            pending = asyncio.create_task(api.request("/state"))
            await entered.wait()
            await api.attach(target_id="new-task")
            release.set()
            with pytest.raises(NativeTaskError) as error:
                await pending
            assert error.value.category == category
            assert error.value.superseded is superseded

    asyncio.run(scenario())


def test_more_than_256_deltas_are_batched_exactly_and_capture_p95_is_bounded():
    async def scenario():
        writes, latencies, observed = [], [], {}

        async def handler(request):
            body = json.loads(request.content)
            if request.url.path.endswith("transcript"):
                writes.append(body["fragments"])
                now = time.monotonic()
                latencies.extend(now - observed[row["event_id"]] for row in body["fragments"])
            return httpx.Response(200, json={"ok": True})

        h = await controller_for(handler)
        texts = [" ", "repeat", "repeat", "\n", " words "] * 64
        for index, text in enumerate(texts):
            await h.controller.handle(transcript(text, f"delta-{index}"))
            identity = h.controller.fragments[-1][1]["event_id"]
            observed[identity] = time.monotonic()
        await h.controller.flush_captures()
        rows = [row for batch in writes for row in batch]
        assert [row["text"] for row in rows] == texts
        assert len({row["event_id"] for row in rows}) == 320
        assert all(row["item_id"] == "item-one" and row["finality"] == "delta" for row in rows)
        assert all(
            len(batch) <= 32 and sum(len(json.dumps(row).encode()) for row in batch) <= 8192
            for batch in writes
        )
        assert sorted(latencies)[int(len(latencies) * 0.95)] < 0.5
        assert not h.controller.capture_pending and len(h.controller.fragments) == 320
        await close(h)

    asyncio.run(scenario())


def test_low_volume_capture_flushes_on_timer_and_graceful_close():
    async def scenario():
        captured = asyncio.Event()
        writes = []

        async def handler(request):
            if request.url.path.endswith("transcript"):
                writes.extend(json.loads(request.content)["fragments"])
                captured.set()
            return httpx.Response(200, json={"ok": True})

        h = await controller_for(handler)
        await h.controller.handle(transcript(" first ", "one"))
        await asyncio.wait_for(captured.wait(), 0.5)
        await h.controller.handle(transcript("second", "two"))
        await h.controller.close()
        assert [row["text"] for row in writes] == [" first ", "second"]
        assert not h.controller.capture_pending
        await h.client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,attempts,category",
    [(503, 3, "transient"), (403, 1, "authorization"), (409, 1, "stale")],
)
def test_capture_retries_only_transient_errors_using_unchanged_ids(status, attempts, category):
    async def scenario():
        writes, fail = [], True

        async def handler(request):
            if request.url.path.endswith("transcript"):
                writes.append(json.loads(request.content))
                return httpx.Response(status if fail else 200, json={"ok": not fail})
            return httpx.Response(200, json={"ok": True})

        h = await controller_for(handler)
        h.controller.CAPTURE_RETRIES = (0, 0)
        await h.controller.handle(transcript(" untouched ", "one"))
        with pytest.raises(NativeTaskError) as error:
            await h.controller.flush_captures()
        assert error.value.category == category
        assert len(writes) == attempts and all(row == writes[0] for row in writes)
        assert len(h.controller.capture_pending) == 1
        if status == 503:
            fail = False
            await h.controller.flush_captures(retry=True)
            assert writes[-1] == writes[0] and not h.controller.capture_pending
        else:
            with pytest.raises(NativeTaskError):
                await h.controller.tick()
        await close(h)

    asyncio.run(scenario())


def test_late_eligible_fragment_does_not_consume_an_earlier_arriving_future_interval():
    async def scenario():
        h = await connected()
        await h.controller.handle(transcript("Future ", "future", start=50, end=60))
        await h.controller.handle(transcript("late earlier ", "late", start=10, end=20))
        await h.controller.handle(rt.DelegationRequested("earlier", offset_ms=30))
        await h.controller.drain()
        await h.controller.handle(rt.DelegationRequested("future", offset_ms=70))
        await h.controller.drain()
        bodies = [body for path, body in h.requests if path == "delegation"]
        assert [[row["text"] for row in body["fragments"]] for body in bodies] == [
            ["late earlier "],
            ["Future "],
        ]
        await close(h)

    asyncio.run(scenario())


def test_same_provider_delta_is_idempotent_but_repeated_words_have_distinct_identity():
    async def scenario():
        h = await connected()
        event = transcript("same ", "one")
        await h.controller.handle(event)
        await h.controller.handle(event)
        await h.controller.handle(transcript("same ", "two"))
        await h.controller.drain()
        rows = [
            row for path, body in h.requests if path == "transcript" for row in body["fragments"]
        ]
        assert [row["text"] for row in rows] == ["same ", "same "]
        assert rows[0]["event_id"] != rows[1]["event_id"]
        with pytest.raises(NativeTaskError, match="identity changed"):
            await h.controller.handle(transcript("changed", "one"))
        await close(h)

    asyncio.run(scenario())


@pytest.mark.parametrize("poll_status", [200, 503, 403])
def test_async_admission_polls_same_operation_without_repeating_dispatch(poll_status):
    async def scenario():
        admitted, release = asyncio.Event(), asyncio.Event()
        requests, polls = [], 0

        async def handler(request):
            nonlocal polls
            path = request.url.path.rsplit("/", 1)[-1]
            body = json.loads(request.content) if request.content else dict(request.url.params)
            requests.append((path, body))
            if path == "delegation":
                assert body["admission"] == "async"
                admitted.set()
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "operation_id": "exact-op",
                        "state": "admitted",
                        "pending": True,
                        "result": None,
                    },
                )
            if path == "operation":
                assert body["operation_id"] == "exact-op"
                polls += 1
                if polls == 1 and poll_status != 200:
                    return httpx.Response(poll_status, json={"error": "fixture"})
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "operation_id": "exact-op",
                        "state": "completed" if release.is_set() else "deciding",
                        "pending": not release.is_set(),
                        "result": {
                            "ok": True,
                            "kind": "commentary",
                            "output": "Exact original result",
                        }
                        if release.is_set()
                        else None,
                    },
                )
            return httpx.Response(200, json={"ok": True})

        h = await controller_for(handler)
        h.controller.OPERATION_POLL_S = 0.001
        await h.controller.handle(transcript("do this", "one"))
        await h.controller.handle(rt.DelegationRequested("original"))
        await admitted.wait()
        await h.controller.handle(transcript("keep chatting", "two"))
        await asyncio.wait_for(h.controller.flush_captures(), 0.1)
        await h.controller.send_audio(b"\x01\x00")
        assert any(isinstance(command, rt.AppendInputAudio) for command in h.session.sent)
        release.set()
        await h.controller.drain()
        assert len([path for path, body in requests if path == "delegation"]) == 1
        results = [
            command for command in h.session.sent if isinstance(command, rt.SubmitDelegationResult)
        ]
        if poll_status == 403:
            assert not results
            assert any(row.get("category") == "authorization" for row in h.notices)
        else:
            assert len(results) == 1 and results[0].delegation_id == "original"
            assert h.controller.delegations["original"].operation_id == "exact-op"
        await close(h)

    asyncio.run(scenario())


def test_completion_eligibility_ignores_capture_backlog_after_microphone_quiet():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            if request.url.path.endswith("transcript"):
                entered.set()
                await release.wait()
            return httpx.Response(200, json={"ok": True})

        h = await controller_for(handler)
        clock = [1.0]
        h.controller.clock = lambda: clock[0]
        await h.controller.handle(transcript("partial words", "one"))
        h.controller.capture_wake.set()
        await entered.wait()
        assert h.controller.timing()["input_pending"]
        clock[0] += 0.71
        assert h.controller._quiet()
        await h.controller._send_history_context("Original job completed")
        assert rt.AppendLiveContext("Original job completed", kind="context") in h.session.sent
        release.set()
        await h.controller.drain()
        assert h.controller.fragments[0][1]["text"] == "partial words"
        await close(h)

    asyncio.run(scenario())
