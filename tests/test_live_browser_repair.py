"""Browser batching and asynchronous decision regressions over the real SQLite coordinator."""

import asyncio
import json
import threading
import time

import pytest
from test_live_browser import delegate, registry_for, start, wait_for
from test_live_browser import environment as environment

import talk_realtime as rt
from talk_dashboard_gateway import DashboardTaskError
from talk_live_browser import BrowserBinding


def quiet(sequence=1):
    return {"sequence": sequence, "operator_speaking": False, "playback_active": False,
            "response_pending": False, "input_pending": False, "tools_pending": False}


def observation(identity, text, *, end=900, item="utterance", finality="delta", role="user"):
    return rt.Transcript(rt.TranscriptRole(role), text, finality == "turn",
                         rt.TranscriptProvenance.INPUT_AUDIO if role == "user"
                         else rt.TranscriptProvenance.OUTPUT_AUDIO,
                         item_id=item, event_id=identity, finality=finality,
                         start_ms=0, end_ms=end)


def ledger_rows(bound):
    with bound.stages._db(bound.token) as db:
        return [json.loads(row[0]) for row in db.execute(
            "SELECT record FROM live_transcripts ORDER BY rowid").fetchall()]


def test_capture_batches_preserve_320_repeated_deltas_and_late_offsets(environment):
    async def run():
        fixture = registry_for(environment)
        _, binding = await start(fixture)
        batches = []
        original = fixture.registry.coordinator.transcript

        def capture(request, body):
            batches.append(list(body["fragments"]))
            return original(request, body)

        fixture.registry.coordinator.transcript = capture
        texts = ["go", " ", "go", "\n", ""] * 64
        for number, text in enumerate(texts):
            await binding.transcript(observation(f"e-{number}", text))
        await binding.transcript(observation("future", "Future words", end=5000, item="future"))
        await binding.transcript(observation("late", " late ", end=950, item="late"))
        await binding.flush_capture()
        assert not binding.persist_queue and binding.capture_bytes == 0
        assert all(len(batch) <= 32 for batch in batches)
        assert all(sum(len(row["text"].encode()) for row in batch) <= 8192 for batch in batches)
        saved = ledger_rows(fixture.bound)
        assert [row["text"] for row in saved[:320]] == texts
        assert len(saved) == len({row["event_id"] for row in saved}) == 322
        assert all(row["item_id"] == "utterance" and not row["final"] for row in saved[:320])
        binding.delegate(rt.DelegationRequested("frozen", offset_ms=1000))
        await wait_for(lambda: bool(fixture.host.jobs))
        captured = fixture.decision.calls[0]["source"]["fragments"]
        assert len(captured) == 321 and captured[-1]["text"] == " late "
        assert "".join(row["text"] for row in captured) == "".join(texts) + " late "
        assert [row["text"] for row in binding.pending_fragments] == ["Future words"]
        await binding.transcript(observation("late-item", "go go\n", finality="item"))
        await binding.close()
        saved = ledger_rows(fixture.bound)
        assert saved[-1]["finality"] == "item" and saved[-1]["final"] is False
        assert len(fixture.decision.calls[0]["source"]["fragments"]) == 321
        assert len(fixture.host.jobs) == 1
        await fixture.registry.close_all()

    asyncio.run(run())


def test_capture_ack_failure_retains_evidence_and_large_atomic_item_flushes_alone(environment):
    async def run():
        fixture = registry_for(environment)
        binding = BrowserBinding(fixture.registry, "capture-only", fixture.request,
                                 fixture.context, fixture.browser, fixture.browser.session)
        original = fixture.registry.coordinator.transcript
        fixture.registry.coordinator.transcript = lambda request, body: {
            "ok": True, "acked_event_ids": ["not-in-batch"]}
        await binding.transcript(observation("atomic", "x" * 9000, finality="item"))
        with pytest.raises(DashboardTaskError):
            await binding.flush_capture()
        assert len(binding.persist_queue) == 1 and binding.capture_bytes == 9000
        fixture.registry.coordinator.transcript = original
        await binding.close()
        assert not binding.persist_queue and binding.capture_bytes == 0
        assert ledger_rows(fixture.bound)[0]["text"] == "x" * 9000
        assert fixture.browser.closed

    asyncio.run(run())


def test_async_25_second_decision_preserves_poll_lease_captions_and_parallel_request(environment):
    async def run():
        started = asyncio.Event()
        calls = []

        async def decide(**values):
            calls.append(values)
            if len(calls) == 1:
                started.set()
                await asyncio.sleep(25)
            return {"name": "delegate_task", "arguments": {"task": "Inspect captured request"},
                    "message": ""}

        fixture = registry_for(environment, decision=decide)
        _, binding = await start(fixture)
        start_time = time.monotonic()
        receipt = await binding.typed("First exact request  ", "slow-typed")
        assert receipt["pending"] and time.monotonic() - start_time < 1
        assert await binding.typed("First exact request  ", "slow-typed") == receipt
        await asyncio.wait_for(started.wait(), 5)
        parallel = await binding.typed("Second exact request", "parallel-typed")
        assert parallel["operation_id"] != receipt["operation_id"]
        cursor = 0
        poll_count = 0
        seen = []
        while binding.operations.get(receipt["operation_id"], {}).get("state") != "completed":
            assert time.monotonic() - start_time < 32
            await fixture.registry.binding(fixture.request, {
                **fixture.context, "binding_id": binding.key}, refresh=True)
            await binding.transcript(observation(f"output-{poll_count}", " Still here ",
                                                 item=None, role="assistant"))
            response = await binding.poll(cursor)
            seen.extend(response["events"])
            cursor = response["cursor"]
            poll_count += 1
            await asyncio.sleep(0.25)
        await wait_for(lambda: not binding.typed_pending)
        assert time.monotonic() - start_time >= 25
        assert poll_count > 50 and not binding.closed
        assert len(calls) == len(fixture.host.jobs) == 2
        assert any(event.get("text") == " Still here " for event in seen)
        assert any(event.get("operation_id") == parallel["operation_id"]
                   and event.get("state") == "completed" for event in seen)
        completed_receipts = len([event for event, _ in binding.events
                                  if event.get("operation_id") == receipt["operation_id"]])
        command_count = len(fixture.browser.session.commands)
        await asyncio.sleep(0.35)
        assert len(fixture.browser.session.commands) == command_count == 2
        assert len([event for event, _ in binding.events
                    if event.get("operation_id") == receipt["operation_id"]]) == completed_receipts
        assert all(job["status"] == "running" for job in fixture.host.jobs.values())
        await fixture.registry.close_all()

    asyncio.run(run())


def test_queued_capture_does_not_delay_exact_job_completion_or_repeat_its_result(
    environment, record_property,
):
    async def run():
        fixture = registry_for(environment)
        _, binding = await start(fixture)
        await delegate(fixture, identity="original-delegation")
        await wait_for(lambda: not binding.pending)
        await binding.poll(0, quiet())
        entered, release = threading.Event(), threading.Event()
        original = fixture.registry.coordinator.transcript

        def slow_capture(request, body):
            entered.set()
            assert release.wait(5)
            return original(request, body)

        fixture.registry.coordinator.transcript = slow_capture
        await binding.transcript(observation("storage-wait", " queued output ", role="assistant"))
        binding.capture_full.set()
        await wait_for(entered.is_set)
        try:
            fixture.host.jobs["remote-1"].update(
                status="completed", updated_at=200.0, last_event="run.completed",
                output="Verified worker result")
            binding.last_state = 0
            recorded = time.monotonic()
            response = await asyncio.wait_for(binding.poll(binding.sequence, quiet(2)), 1)
            eligible_ms = (time.monotonic() - recorded) * 1000
            record_property("completion_eligibility_ms", round(eligible_ms, 2))
            record_property("audio_played_confirmed", False)
            summaries = [command for command in fixture.browser.session.commands
                         if isinstance(command, rt.SubmitDelegationResult)
                         and "completed" in command.content]
            assert eligible_ms < 1000 and not release.is_set() and binding.capture_lock.locked()
            assert len(summaries) == 1
            assert summaries[0].delegation_id == "original-delegation"
            assert any(event.get("result", {}).get("output") == "Verified worker result"
                       for event in response["events"])
            assert not binding.active_jobs
            with fixture.bound.events._db(fixture.bound.token) as db:
                states = db.execute(
                    "SELECT state,playback_supported FROM task_event_speech").fetchall()
            assert all(tuple(row) == ("sent", 0) for row in states)
            binding.last_state = 0
            again = await binding.poll(response["cursor"], quiet(3))
            assert not any(event["type"] == "result" for event in again["events"])
            assert len([command for command in fixture.browser.session.commands
                        if isinstance(command, rt.SubmitDelegationResult)
                        and "completed" in command.content]) == 1
        finally:
            release.set()
            await fixture.registry.close_all()

    asyncio.run(run())


def test_sync_coordinator_reply_remains_compatible_and_duplicate_delivery_is_inert(environment):
    async def run():
        fixture = registry_for(environment)
        original = fixture.registry.coordinator.typed

        async def legacy(request, body):
            return await original(request, {key: value for key, value in body.items()
                                            if key != "admission"})

        fixture.registry.coordinator.typed = legacy
        _, binding = await start(fixture)
        assert await binding.typed("Legacy request", "legacy-typed") == {"ok": True}
        assert await binding.typed("Legacy request", "legacy-typed") == {"ok": True}
        command = fixture.browser.session.commands[0]
        await binding.deliver("legacy-typed", [command])
        assert len(fixture.browser.session.commands) == len(fixture.host.jobs) == 1
        assert not binding.typed_pending
        await fixture.registry.close_all()

    asyncio.run(run())
