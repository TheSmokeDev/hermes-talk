"""Durable Live context handoffs never imply correlated local playback."""

import asyncio

import pytest
from test_dashboard_tasks import join
from test_live_browser import FakeBrowser, registry_for, start, wait_for
from test_live_browser import environment as environment
from test_live_browser_repair import observation, quiet

import talk_realtime as rt
from talk_dashboard_gateway import DashboardTaskError
from talk_live_browser import BrowserBinding
from talk_live_coordinator import LiveLedger
from talk_task_sources import TaskEventError


def operation_view(fixture, operation_id):
    return fixture.registry.coordinator.operation(
        fixture.request, {**fixture.context, "operation_id": operation_id},
    )


def test_operation_handoff_is_durable_exact_and_never_marks_playback(environment):
    async def run():
        fixture = registry_for(environment)
        _, binding = await start(fixture)
        receipt = await binding.typed("Inspect this project", "typed-one")
        await wait_for(lambda: not binding.typed_pending)
        view = operation_view(fixture, receipt["operation_id"])
        presentation = view["presentation"]
        assert presentation["operation_id"] == receipt["operation_id"]
        assert presentation["state"] == "context_submitted"
        assert presentation["result_ready"] and presentation["context_submitted"]
        assert not presentation["playback_started"] and not presentation["playback_finished"]
        assert not presentation["replay_eligible"]
        assert "session_key" not in presentation and "generation" not in presentation
        events = [event for event, _ in binding.events if event["type"] == "result"]
        assert len(events) == 1 and events[0]["presentation"]["state"] == "result_ready"
        restored_browser = FakeBrowser()
        restored = BrowserBinding(fixture.registry, "restored", fixture.request,
                                  fixture.context, restored_browser, restored_browser.session)
        await restored.publish_result({**view["result"], "operation_id": view["operation_id"]})
        assert not restored_browser.session.commands
        assert len(fixture.host.jobs) == len(fixture.decision.calls) == 1
        await restored.close()
        await binding.close()
        final = operation_view(fixture, receipt["operation_id"])["presentation"]
        assert final["state"] == "unknown" and final["context_submitted"] and final["unknown"]
        assert final["attempt_id"] == presentation["attempt_id"]
        await fixture.registry.close_all()

    asyncio.run(run())


def test_send_failure_records_unknown_and_never_retries_an_uncertain_delivery(environment):
    async def run():
        fixture = registry_for(environment)
        _, binding = await start(fixture)
        sent = []

        async def uncertain_send(commands):
            sent.extend(commands)
            raise RuntimeError("handoff acknowledgment lost")

        fixture.browser.session.send = uncertain_send
        receipt = await binding.typed("Inspect this project", "typed-one")
        await wait_for(lambda: binding.closed)
        view = operation_view(fixture, receipt["operation_id"])
        assert view["presentation"]["state"] == "unknown"
        assert view["presentation"]["result_ready"] and view["presentation"]["unknown"]
        assert not view["presentation"]["context_submitted"]
        assert not binding.deliveries and len(sent) == 1
        refused = fixture.registry.coordinator.presentation(
            fixture.request, binding.body(operation_id=view["operation_id"], state="submitting"),
        )
        assert refused["ok"] and not refused["speak"]
        assert len(fixture.host.jobs) == len(fixture.decision.calls) == 1
        await fixture.registry.close_all()

    asyncio.run(run())


def test_operation_receipts_reject_foreign_attempt_session_and_stale_generation(environment):
    async def run():
        fixture = registry_for(environment)
        _, binding = await start(fixture)
        receipt = await binding.typed("Inspect this project", "typed-one")
        await wait_for(lambda: not binding.typed_pending)
        presentation = operation_view(fixture, receipt["operation_id"])["presentation"]
        body = binding.body(operation_id=receipt["operation_id"],
                            attempt_id=presentation["attempt_id"], state="context_submitted")
        for patch in ({"attempt_id": "another-attempt"},
                      {"provider_session_id": "another-session"},
                      {"operation_id": "another-operation"},
                      {"state": "playback_finished"}):
            with pytest.raises(DashboardTaskError):
                fixture.registry.coordinator.presentation(fixture.request, {**body, **patch})
        repeated = fixture.registry.coordinator.presentation(fixture.request, body)
        assert repeated["presentation"] == presentation
        await binding.close()
        _, renewed = join(environment)
        with pytest.raises(DashboardTaskError):
            fixture.registry.coordinator.presentation(fixture.request, body)
        current = fixture.registry.coordinator.operation(
            fixture.request, {**renewed, "operation_id": receipt["operation_id"]},
        )
        assert current["presentation"]["state"] == "unknown"
        assert len(fixture.host.jobs) == len(fixture.decision.calls) == 1
        await fixture.registry.close_all()

    asyncio.run(run())


def test_expired_operation_dispatch_is_unknown_without_a_second_attempt(environment, monkeypatch):
    async def run():
        fixture = registry_for(environment)
        _, binding = await start(fixture)
        binding.retired.add("held")
        await binding.transcript(observation("captured", "Inspect this project"))
        binding.delegate(rt.DelegationRequested("held", offset_ms=1000))
        await wait_for(lambda: not binding.pending)
        operation_id = next(iter(binding.operations))
        prepared = fixture.registry.coordinator.presentation(
            fixture.request, binding.body(operation_id=operation_id, state="submitting"),
        )
        assert prepared["speak"] and not fixture.browser.session.commands
        expires = prepared["presentation"]["claim_expires_at"]
        monkeypatch.setattr(fixture.bound.stages, "clock", lambda: expires + 1)
        view = operation_view(fixture, operation_id)
        assert view["presentation"]["state"] == "unknown"
        assert view["presentation"]["attempt_id"] == prepared["presentation"]["attempt_id"]
        await binding.proactive(quiet())
        assert not fixture.browser.session.commands
        assert len(fixture.host.jobs) == len(fixture.decision.calls) == 1
        await fixture.registry.close_all()

    asyncio.run(run())


def test_recorded_operation_result_publishes_while_transcript_capture_is_blocked(environment):
    async def run():
        fixture = registry_for(environment)
        _, binding = await start(fixture)
        fixture.decision.started, fixture.decision.release = asyncio.Event(), asyncio.Event()
        receipt = await binding.typed("Inspect this project", "typed-one")
        await asyncio.wait_for(fixture.decision.started.wait(), 5)
        await binding.capture_lock.acquire()
        try:
            await binding.transcript(observation("unrelated", "Unrelated transcript"))
            fixture.decision.release.set()
            await wait_for(lambda: bool(fixture.host.jobs))
            operation_id = receipt["operation_id"]
            await wait_for(lambda: LiveLedger(fixture.bound).operation(operation_id)["state"]
                           == "completed")
            await asyncio.wait_for(wait_for(lambda: not binding.typed_pending), 1)
            assert binding.persist_queue and binding.capture_lock.locked()
            assert len(fixture.browser.session.commands) == 1
            assert any(event["type"] == "result" and event["operation_id"] == operation_id
                       for event, _ in binding.events)
            assert operation_view(fixture, operation_id)["presentation"]["context_submitted"]
        finally:
            binding.capture_lock.release()
            await fixture.registry.close_all()

    asyncio.run(run())


def test_job_handoff_requires_persisted_dispatch_and_does_not_retry_unknown(environment):
    async def run():
        fixture = registry_for(environment)
        _, binding = await start(fixture)
        await binding.typed("Inspect this project", "typed-one")
        await wait_for(lambda: not binding.typed_pending)
        fixture.host.jobs["remote-1"].update(
            status="completed", updated_at=200.0, last_event="run.completed", output="Done",
        )
        commands = []

        async def uncertain_send(values):
            with fixture.bound.events._db(fixture.bound.token) as db:
                states = db.execute("SELECT state FROM task_event_speech").fetchall()
            assert [row[0] for row in states] == ["submitting"]
            commands.extend(values)
            raise RuntimeError("uncertain send")

        fixture.browser.session.send = uncertain_send
        with pytest.raises(RuntimeError, match="uncertain send"):
            await binding.proactive(quiet())
        with fixture.bound.events._db(fixture.bound.token) as db:
            states = db.execute("SELECT state FROM task_event_speech").fetchall()
        assert [row[0] for row in states] == ["unknown"]
        view = fixture.registry.manager.state(fixture.request, fixture.context)
        assert view["jobs"][0]["presentation"]["unknown"]
        assert not view["jobs"][0]["presentation"]["context_submitted"]
        await binding.proactive(quiet(2))
        assert len(commands) == 1 and len(fixture.host.jobs) == len(fixture.decision.calls) == 1
        await fixture.registry.close_all()

    asyncio.run(run())


def test_job_handoff_stops_when_dispatch_receipt_is_refused(environment, monkeypatch):
    async def run():
        fixture = registry_for(environment)
        _, binding = await start(fixture)
        await binding.typed("Inspect this project", "typed-one")
        await wait_for(lambda: not binding.typed_pending)
        fixture.host.jobs["remote-1"].update(
            status="completed", updated_at=200.0, last_event="run.completed", output="Done",
        )
        manager = fixture.registry.manager
        original = manager.speech_receipt
        states = []

        def refuse(request, body):
            states.append(body["state"])
            if body["state"] == "submitting":
                return {"ok": False}
            return original(request, body)

        monkeypatch.setattr(manager, "speech_receipt", refuse)
        await binding.proactive(quiet())
        assert states == ["submitting"]
        assert len(fixture.browser.session.commands) == 1
        assert len(fixture.host.jobs) == len(fixture.decision.calls) == 1
        await fixture.registry.close_all()

    asyncio.run(run())


def test_unsent_retired_result_waits_for_a_complete_fresh_quiet_observation(environment):
    async def run():
        fixture = registry_for(environment)
        _, binding = await start(fixture)
        binding.retired.add("held")
        await binding.transcript(observation("captured", "Inspect this project"))
        binding.delegate(rt.DelegationRequested("held", offset_ms=1000))
        await wait_for(lambda: not binding.pending)
        assert binding.deliveries and not fixture.browser.session.commands
        with pytest.raises(TaskEventError):
            await binding.proactive({})
        await binding.proactive({**quiet(3), "operator_speaking": True})
        await binding.proactive(quiet(2))
        assert binding.deliveries and not fixture.browser.session.commands
        await binding.proactive(quiet(4))
        assert not binding.deliveries and len(fixture.browser.session.commands) == 1
        await fixture.registry.close_all()

    asyncio.run(run())
