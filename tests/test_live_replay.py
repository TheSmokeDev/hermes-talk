"""Explicit Live replay re-announces a terminal result into the exact bound session."""

from __future__ import annotations

import asyncio

import pytest
from test_dashboard_tasks import environment as base_environment
from test_live_browser import delegate, wait_for
from test_live_browser_repair import quiet
from test_live_routes import application, create

import talk_realtime as rt
from talk_dashboard_store import DashboardStages
from talk_live_coordinator import LiveLedger

SECRETS = ("fixture-gateway-key", "fixture-live-api-key", "private-provider-session")


@pytest.fixture
def environment(tmp_path):
    return base_environment.__wrapped__(tmp_path)


async def finished_job(client, fixture):
    """A bound Live session whose one delegated job has completed and is announceable."""
    body = await create(client, fixture)
    await delegate(fixture)
    await wait_for(lambda: bool(fixture.browser.session.commands))
    binding = fixture.registry.bindings[body["binding_id"]]
    manager = fixture.registry.manager
    await asyncio.to_thread(manager.state, binding.lease, fixture.context)
    fixture.host.jobs["remote-1"].update(
        status="completed", output="Done", updated_at=200.0, last_event="run.completed",
    )
    state = await asyncio.to_thread(manager.state, binding.lease, fixture.context)
    assert state["announcements"], state
    return body, binding, state["announcements"][0]["event_id"]


async def retire_first_attempt(fixture, binding):
    """The proactive announcement loses its send: the first attempt lands as ``unknown``."""
    session = fixture.browser.session
    original = session.send

    async def lost(_commands):
        raise RuntimeError("handoff acknowledgment lost")

    session.send = lost
    try:
        with pytest.raises(RuntimeError, match="acknowledgment lost"):
            await binding.proactive(quiet(1))
    finally:
        session.send = original


def replay_body(body, event_id, sequence):
    return {
        **body, "event_id": event_id, "timing": quiet(sequence),
        "presentation_protocol": 1, "playback_supported": False, "replay": True,
    }


def presentation(fixture, event_id):
    return fixture.bound.events.presentation(fixture.bound.token, event_id)


def speech_rows(fixture):
    with fixture.bound.events._db(fixture.bound.token) as db:
        return [tuple(row) for row in db.execute(
            "SELECT attempt_id,state FROM task_event_speech ORDER BY attempt_order"
        ).fetchall()]


def table_counts(fixture):
    with fixture.bound.stages._db(fixture.bound.token, write=False) as db:
        return tuple(
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("dashboard_interactions", "live_operations")
        )


def announced(session):
    """Every command after the delegation's own result: proactive announcements and replays."""
    return list(session.commands[1:])


def appended(session):
    return [command for command in session.commands if isinstance(command, rt.AppendLiveContext)]


def test_live_replay_appends_summary_to_bound_session_and_persists_context_submitted(
    environment,
):
    async def run():
        async with application(environment) as (client, fixture):
            body, binding, event_id = await finished_job(client, fixture)
            await retire_first_attempt(fixture, binding)
            retired = presentation(fixture, event_id)
            assert retired["state"] == "unknown" and retired["unknown"]
            assert retired["replay_eligible"] and not retired["context_submitted"]
            assert announced(fixture.browser.session) == []
            reply = await client.post("/live/speech", json=replay_body(body, event_id, 2))
            assert reply.status_code == 200, reply.text
            data = reply.json()
            assert data["speak"] is True and data["event_id"] == event_id
            assert data["presentation"]["state"] == "context_submitted"
            assert data["presentation"]["context_submitted"] and not data["presentation"]["unknown"]
            assert data["presentation"]["attempt_id"] == data["attempt_id"]
            assert data["attempt_id"] != retired["attempt_id"]
            assert data["operation_id"] and data["run_id"]
            assert "completed" in data["content"] and "response" not in data
            assert not any(secret in reply.text for secret in SECRETS)
            added = announced(fixture.browser.session)
            assert len(added) == 1 and added[0].content == data["content"]
            # The job's delegation is still open, so the replay answers it there.
            assert isinstance(added[0], rt.SubmitDelegationResult)
            assert added[0].delegation_id == "delegation-one"
            assert presentation(fixture, event_id)["state"] == "context_submitted"
            assert speech_rows(fixture) == [(data["attempt_id"], "sent")]
            assert len(fixture.host.jobs) == 1
            assert not any(path.endswith("/stop") for _, path, _, _ in fixture.host.requests)

    asyncio.run(run())


def test_live_replay_rejects_foreign_binding_and_stale_generation(environment):
    async def run():
        async with application(environment) as (client, fixture):
            body, binding, event_id = await finished_job(client, fixture)
            await retire_first_attempt(fixture, binding)
            rows = speech_rows(fixture)
            for patch, headers, status in (
                ({"binding_id": "another-binding"}, {}, 409),
                ({"generation": body["generation"] - 1}, {}, 409),
                ({}, {"x-actor": "other"}, 403),
            ):
                reply = await client.post(
                    "/live/speech", json={**replay_body(body, event_id, 2), **patch},
                    headers=headers,
                )
                assert reply.status_code == status, reply.text
                if status == 409:
                    assert reply.json()["detail"]["code"] == "connection_stale"
                assert not any(secret in reply.text for secret in SECRETS)
            assert announced(fixture.browser.session) == []
            assert speech_rows(fixture) == rows
            assert presentation(fixture, event_id)["attempt_id"] == rows[0][0]
            assert not binding.closed

    asyncio.run(run())


def test_live_replay_refuses_non_terminal_or_active_attempt(environment):
    async def run():
        async with application(environment) as (client, fixture):
            body = await create(client, fixture)
            await delegate(fixture)
            await wait_for(lambda: bool(fixture.browser.session.commands))
            binding = fixture.registry.bindings[body["binding_id"]]
            manager = fixture.registry.manager
            state = await asyncio.to_thread(manager.state, binding.lease, fixture.context)
            running = state["events"]["events"][-1]
            assert running["state"] == "running"
            reply = await client.post(
                "/live/speech", json=replay_body(body, running["event_id"], 1),
            )
            assert reply.status_code == 200 and reply.json()["speak"] is False, reply.text
            assert announced(fixture.browser.session) == [] and speech_rows(fixture) == []
            fixture.host.jobs["remote-1"].update(
                status="completed", output="Done", updated_at=200.0, last_event="run.completed",
            )
            state = await asyncio.to_thread(manager.state, binding.lease, fixture.context)
            event_id = state["announcements"][0]["event_id"]
            commands = len(fixture.browser.session.commands)
            await binding.proactive(quiet(2))
            # The proactive announcement rides the job's own delegation, not a replay append.
            after = fixture.browser.session.commands[commands:]
            assert len(after) == 1 and isinstance(after[0], rt.SubmitDelegationResult)
            active = presentation(fixture, event_id)
            assert active["state"] == "context_submitted" and not active["replay_eligible"]
            reply = await client.post("/live/speech", json=replay_body(body, event_id, 3))
            assert reply.status_code == 200 and reply.json()["speak"] is False, reply.text
            assert len(fixture.browser.session.commands) == commands + 1
            assert speech_rows(fixture) == [(active["attempt_id"], "sent")]

    asyncio.run(run())


def test_live_replay_send_failure_persists_unknown_without_retry(environment):
    async def run():
        async with application(environment) as (client, fixture):
            body, binding, event_id = await finished_job(client, fixture)
            await retire_first_attempt(fixture, binding)
            first = presentation(fixture, event_id)["attempt_id"]
            session = fixture.browser.session
            original = session.send

            async def once(_commands):
                session.send = original
                raise RuntimeError("handoff acknowledgment lost")

            session.send = once
            reply = await client.post("/live/speech", json=replay_body(body, event_id, 2))
            assert reply.status_code == 502, reply.text
            assert reply.json()["detail"]["code"] == "live_unavailable"
            assert "acknowledgment lost" not in reply.text
            lost = presentation(fixture, event_id)
            assert lost["state"] == "unknown" and lost["unknown"] and lost["replay_eligible"]
            assert lost["attempt_id"] != first
            assert announced(session) == [] and not binding.speech_attempts
            assert not binding.closed
            again = await client.post("/live/speech", json=replay_body(body, event_id, 3))
            assert again.status_code == 200, again.text
            sent = again.json()["attempt_id"]
            assert sent not in {first, lost["attempt_id"]}
            assert again.json()["presentation"]["state"] == "context_submitted"
            assert len(announced(session)) == 1
            assert speech_rows(fixture) == [(sent, "sent")]
            assert len(fixture.host.jobs) == 1

    asyncio.run(run())


def test_live_replay_never_stages_input_or_coordinator_work(environment, monkeypatch):
    async def run():
        async with application(environment) as (client, fixture):
            body, binding, event_id = await finished_job(client, fixture)
            await retire_first_attempt(fixture, binding)

            def forbidden(*args, **kwargs):
                pytest.fail("replay must not stage input or coordinator work")

            coordinator = fixture.registry.coordinator
            for target, name in (
                (coordinator, "typed"), (coordinator, "delegation"), (LiveLedger, "admit"),
                (DashboardStages, "stage"), (DashboardStages, "stage_live"),
                (DashboardStages, "prepare_action"),
            ):
                monkeypatch.setattr(target, name, forbidden)
            counts, jobs = table_counts(fixture), dict(fixture.host.jobs)
            reply = await client.post("/live/speech", json=replay_body(body, event_id, 2))
            assert reply.status_code == 200 and reply.json()["speak"] is True, reply.text
            assert table_counts(fixture) == counts and fixture.host.jobs == jobs
            assert len(fixture.decision.calls) == 1
            assert len(announced(fixture.browser.session)) == 1
            assert not any(
                path == "/v1/runs" or path.endswith("/stop")
                for _, path, _, _ in fixture.host.requests[-6:]
            )

    asyncio.run(run())


def test_live_speech_without_replay_keeps_preparation_only_behaviour(environment):
    async def run():
        async with application(environment) as (client, fixture):
            body, _, event_id = await finished_job(client, fixture)
            reply = await client.post(
                "/live/speech", json={**fixture.context, "event_id": event_id, "timing": quiet(1)},
            )
            assert reply.status_code == 200, reply.text
            speech = reply.json()
            assert speech["speak"] is True and "completed" in speech["content"]
            assert "response" not in speech and speech["presentation"]["state"] == "claimed"
            assert announced(fixture.browser.session) == []
            assert speech_rows(fixture) == [(speech["attempt_id"], "queued")]
            for wrong in ({"replay": True},
                          {"replay": True, "presentation_protocol": 0,
                           "playback_supported": False},
                          {"replay": True, "presentation_protocol": 1,
                           "playback_supported": True},
                          {"replay": "yes", "presentation_protocol": 1,
                           "playback_supported": False}):
                refused = await client.post("/live/speech", json={
                    **body, "event_id": event_id, "timing": quiet(2), **wrong,
                })
                assert refused.status_code == 400, refused.text
                assert refused.json()["detail"]["code"] == "invalid_event"
            assert announced(fixture.browser.session) == []
            assert speech_rows(fixture) == [(speech["attempt_id"], "queued")]

    asyncio.run(run())


@pytest.mark.parametrize("retired", [False, True])
def test_live_replay_answers_an_open_delegation_and_appends_for_a_retired_one(
    environment, retired,
):
    async def run():
        async with application(environment) as (client, fixture):
            body, binding, event_id = await finished_job(client, fixture)
            await retire_first_attempt(fixture, binding)
            assert binding.job_delegations == {"1": "delegation-one"}
            if retired:
                binding.retired.add("delegation-one")
            reply = await client.post("/live/speech", json=replay_body(body, event_id, 2))
            assert reply.status_code == 200 and reply.json()["speak"] is True, reply.text
            added = announced(fixture.browser.session)
            assert len(added) == 1 and added[0].content == reply.json()["content"]
            if retired:
                assert isinstance(added[0], rt.AppendLiveContext)
                assert added[0].kind == "message" and added[0].delegation_id is None
            else:
                assert isinstance(added[0], rt.SubmitDelegationResult)
                assert added[0].delegation_id == "delegation-one"
            assert presentation(fixture, event_id)["state"] == "context_submitted"

    asyncio.run(run())
