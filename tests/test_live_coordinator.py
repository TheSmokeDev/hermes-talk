"""Real durable coordinator paths with controlled Hermes reasoning and gateway I/O."""

from __future__ import annotations

import asyncio

import pytest
from test_dashboard_tasks import environment as base_environment
from test_dashboard_tasks import join

from talk_dashboard_gateway import DashboardTaskError
from talk_live_coordinator import LiveCoordinator, LiveLedger


@pytest.fixture
def environment(tmp_path):
    return base_environment.__wrapped__(tmp_path)


class Decision:
    def __init__(self, *, name="delegate_task", arguments=None):
        self.calls = []
        self.name = name
        self.arguments = arguments or {"task": "Inspect the original requested project"}
        self.started = None
        self.release = None

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.started:
            self.started.set()
            await self.release.wait()
        return {"name": self.name, "arguments": self.arguments, "message": ""}


def setup(environment):
    manager, request, host, _ = environment
    bound, context = join(environment)
    decide = Decision()
    coordinator = LiveCoordinator(manager, None, lambda _: [{"name": "delegate_task"}],
                                  decide=decide)
    body = {**context, "provider_session_id": "provider-session", "delegation_id": "d-one",
            "offset_ms": 1000, "fragments": [{"event_id": "input-one", "role": "user",
                "text": "Start background work inspecting my project", "start_ms": 0,
                "end_ms": 900, "final": False}]}
    return coordinator, decide, request, host, bound, body


def test_partial_capture_duplicate_and_reconnect_share_one_worker(environment):
    async def run():
        coordinator, decide, request, host, bound, body = setup(environment)
        first = await coordinator.delegation(request, body)
        repeat = await coordinator.delegation(request, body)
        other_id = await coordinator.delegation(request, {**body, "delegation_id": "d-two"})
        ids = [item["action"]["run_id"] for item in (first, repeat, other_id)]
        assert len(set(ids)) == 1
        assert len(host.jobs) == len(decide.calls) == 1
        assert host.rows[("default", "task-a")][-1]["content"].startswith(
            "[Captured Live transcript fragments; this is not a finalized utterance.]"
        )
        records, _ = bound.stages.records(bound.token)
        assert records[0]["source_window"]["fragments"][0]["final"] is False
        assert records[0]["text"] == body["fragments"][0]["text"]
        _, context = join(environment)
        third = await coordinator.delegation(request, {**body, **context})
        assert third["action"]["run_id"] == first["action"]["run_id"]
        assert len(host.jobs) == len(decide.calls) == 1
        with pytest.raises(DashboardTaskError, match="no longer current"):
            await coordinator.delegation(request, body)
    asyncio.run(run())


def test_concurrent_event_does_not_repeat_reasoning_or_dispatch(environment):
    async def run():
        coordinator, decide, request, host, _, body = setup(environment)
        decide.started, decide.release = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(coordinator.delegation(request, body))
        await asyncio.wait_for(decide.started.wait(), timeout=10)
        repeat = await coordinator.delegation(request, body)
        assert "pending or unconfirmed" in repeat["output"]
        assert len(decide.calls) == 1 and not host.jobs
        decide.release.set()
        await first
        assert len(host.jobs) == 1
    asyncio.run(run())


def test_final_typed_origin_is_saved_once_and_keeps_its_modality(environment):
    async def run():
        coordinator, decide, request, host, bound, body = setup(environment)
        typed = {**body, "input_id": "typed-one", "text": "Inspect this exactly  "}
        await coordinator.typed(request, typed)
        await coordinator.typed(request, typed)
        records, _ = bound.stages.records(bound.token)
        assert len(records) == 1 and records[0]["input_type"] == "typed"
        assert records[0]["text"] == typed["text"]
        assert host.rows[("default", "task-a")][-1]["content"] == typed["text"]
        assert len(host.jobs) == len(decide.calls) == 1
    asyncio.run(run())


def test_outputs_future_input_and_conflicting_events_cannot_authorize_work(environment):
    async def run():
        coordinator, decide, request, host, bound, body = setup(environment)
        output = {**body["fragments"][0], "role": "assistant"}
        response = await coordinator.delegation(request, {**body, "fragments": [output]})
        assert "No new operator input" in response["output"]
        future = {**body["fragments"][0], "end_ms": 1200}
        await coordinator.delegation(request, {**body, "fragments": [future]})
        assert not host.jobs and not decide.calls
        await coordinator.delegation(request, body)
        with pytest.raises(DashboardTaskError, match="different content"):
            await coordinator.delegation(request, {**body, "fragments": [
                {**body["fragments"][0], "text": "Different request"}]})
        assert len(host.jobs) == 1
        assert LiveLedger(bound).recent()[0]["text"] == body["fragments"][0]["text"]
    asyncio.run(run())


def test_revocation_during_reasoning_prevents_action(environment):
    async def run():
        coordinator, decide, request, host, _, body = setup(environment)
        decide.started, decide.release = asyncio.Event(), asyncio.Event()
        pending = asyncio.create_task(coordinator.delegation(request, body))
        await asyncio.wait_for(decide.started.wait(), timeout=10)
        request.state.principal = "another-actor"
        decide.release.set()
        with pytest.raises(DashboardTaskError):
            await pending
        assert not host.jobs
    asyncio.run(run())
