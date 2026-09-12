"""Async admission and exact transcript identity over the real SQLite stores."""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from test_dashboard_tasks import environment as base_environment
from test_dashboard_tasks import join
from test_live_coordinator import setup

from talk_dashboard_gateway import DashboardTaskError
from talk_live_coordinator import (
    LiveCoordinator,
    LiveLedger,
    normalize_fragments,
    resolve_fragments,
)


@pytest.fixture
def environment(tmp_path):
    return base_environment.__wrapped__(tmp_path)


def fragment(event, text, *, item=None, finality="delta", start=None, end=None):
    row = {"event_id": event, "text": text, "finality": finality, "start_ms": start, "end_ms": end}
    if item:
        row["item_id"] = item
    return row


async def settle(coordinator, request, body, receipt):
    pending = coordinator._tasks.get(receipt["operation_id"])
    if pending:
        await asyncio.wait_for(asyncio.shield(pending), 35)
    return coordinator.operation(request, {**body, "operation_id": receipt["operation_id"]})


def test_async_receipt_precedes_a_real_25_second_decision(environment):
    async def run():
        coordinator, decide, request, host, bound, body = setup(environment)

        async def slow(**kwargs):
            await asyncio.sleep(25)
            return await decide(**kwargs)

        coordinator.decide = slow
        started = time.monotonic()
        receipt = await asyncio.wait_for(
            coordinator.delegation(request, {**body, "admission": "async"}),
            5,
        )
        assert time.monotonic() - started < 5
        assert receipt["pending"] and receipt["result"] is None
        operation = LiveLedger(bound).operation(receipt["operation_id"])
        record = bound.stages.get(bound.token, operation["interaction_id"])
        assert record["text"] == body["fragments"][0]["text"]
        assert record["live_decision"]["state"] == "reasoning"
        assert not host.jobs
        completed = await settle(coordinator, request, body, receipt)
        assert time.monotonic() - started >= 25
        assert completed["state"] == "completed" and not completed["pending"]
        assert completed["result"]["action"]["state"] == "accepted"
        assert next(iter(host.jobs.values()))["status"] == "running"

    asyncio.run(run())


def test_duplicate_and_lost_async_receipts_reuse_one_operation(environment):
    async def run():
        coordinator, decide, request, host, _, body = setup(environment)
        decide.started, decide.release = asyncio.Event(), asyncio.Event()
        first = await coordinator.delegation(request, {**body, "admission": "async"})
        await asyncio.wait_for(decide.started.wait(), 5)
        repeats = await asyncio.gather(
            *(
                coordinator.delegation(
                    request,
                    {
                        **body,
                        "admission": "async",
                        "delegation_id": event,
                    },
                )
                for event in (body["delegation_id"], "duplicate-notice")
            )
        )
        assert {first["operation_id"], *(row["operation_id"] for row in repeats)} == {
            first["operation_id"]
        }
        assert len(decide.calls) == 1 and not host.jobs
        decide.release.set()
        result = await settle(coordinator, request, body, first)
        restarted = LiveCoordinator(coordinator.manager, None, coordinator.tools, decide=decide)
        assert (
            restarted.operation(request, {**body, "operation_id": first["operation_id"]}) == result
        )
        assert len(decide.calls) == len(host.jobs) == 1

    asyncio.run(run())


def test_uncertain_reasoning_survives_restart_without_replay(environment):
    async def run():
        coordinator, _, request, host, bound, body = setup(environment)
        calls = []

        async def failed(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("private provider response must never be returned")

        coordinator.decide = failed
        receipt = await coordinator.delegation(request, {**body, "admission": "async"})
        result = await settle(coordinator, request, body, receipt)
        assert result["state"] == "uncertain" and not result["pending"]
        assert "private provider" not in str(result)
        restarted = LiveCoordinator(coordinator.manager, None, coordinator.tools, decide=failed)
        retry = await restarted.delegation(request, {**body, "admission": "async"})
        assert retry["operation_id"] == receipt["operation_id"]
        assert retry["state"] == "uncertain"
        assert len(calls) == 1 and not host.jobs
        records, _ = bound.stages.records(bound.token)
        assert records[0]["live_decision"]["state"] == "reasoning"

    asyncio.run(run())


def test_poll_reconciles_accepted_receipt_without_dispatch(environment):
    async def run():
        coordinator, decide, request, host, bound, body = setup(environment)
        host.drop_run = True
        receipt = await coordinator.delegation(request, {**body, "admission": "async"})
        result = await settle(coordinator, request, body, receipt)
        assert result["state"] == "uncertain" and len(host.jobs) == 1
        _, actions = bound.stages.records(bound.token)
        action = actions[0]
        count = sum(path == "/v1/runs" for _, path, _, _ in host.requests)
        restarted = LiveCoordinator(coordinator.manager, None, coordinator.tools, decide=decide)
        for _ in range(3):
            assert (
                restarted.operation(
                    request,
                    {
                        **body,
                        "operation_id": receipt["operation_id"],
                    },
                )["state"]
                == "uncertain"
            )
        bound.stages.record_original_receipt(action, state="accepted", api_run_id="remote-1")
        restored = restarted.operation(request, {**body, "operation_id": receipt["operation_id"]})
        assert restored["state"] == "completed"
        assert restored["result"]["action"]["run_id"] == action["run_id"]
        assert count == sum(path == "/v1/runs" for _, path, _, _ in host.requests)
        assert len(decide.calls) == len(host.jobs) == 1

    asyncio.run(run())


def test_revoked_binding_cannot_read_or_dispatch_async_result(environment):
    async def run():
        coordinator, decide, request, host, _, body = setup(environment)
        decide.started, decide.release = asyncio.Event(), asyncio.Event()
        receipt = await coordinator.delegation(request, {**body, "admission": "async"})
        await asyncio.wait_for(decide.started.wait(), 5)
        request.state.principal = "another-actor"
        decide.release.set()
        task = coordinator._tasks[receipt["operation_id"]]
        await task
        with pytest.raises(DashboardTaskError) as error:
            coordinator.operation(request, {**body, "operation_id": receipt["operation_id"]})
        assert error.value.status == 403 and not host.jobs

    asyncio.run(run())


def test_operation_is_not_available_to_a_different_tab(environment):
    async def run():
        coordinator, _, request, _, _, body = setup(environment)
        receipt = await coordinator.delegation(request, {**body, "admission": "async"})
        await settle(coordinator, request, body, receipt)
        _, other = join(environment, tab="another-tab")
        with pytest.raises(DashboardTaskError) as error:
            coordinator.operation(request, {**other, "operation_id": receipt["operation_id"]})
        assert error.value.status == 404

    asyncio.run(run())


def test_claim_and_operation_are_atomic(environment, monkeypatch):
    async def run():
        coordinator, decide, request, host, bound, body = setup(environment)
        encode = bound.stages._encode

        def fail_operation(value):
            if "operation_id" in value:
                raise RuntimeError("simulated disk failure before admission commit")
            return encode(value)

        monkeypatch.setattr(bound.stages, "_encode", fail_operation)
        with pytest.raises(RuntimeError, match="disk failure"):
            await coordinator.delegation(request, {**body, "admission": "async"})
        records, actions = bound.stages.records(bound.token)
        assert not records and not actions and not host.jobs and not decide.calls
        assert LiveLedger(bound).recent()[0]["text"] == body["fragments"][0]["text"]

    asyncio.run(run())


def test_whitespace_and_repeated_deltas_are_exact_and_blank_is_not_an_action(environment):
    async def run():
        coordinator, decide, request, host, bound, body = setup(environment)
        blanks = [fragment("space", "  "), fragment("empty", ""), fragment("newline", "\n")]
        ack = coordinator.transcript(request, {**body, "fragments": blanks})
        assert ack["acked_event_ids"] == ["space", "empty", "newline"]
        assert [row["text"] for row in LiveLedger(bound).recent()] == ["  ", "", "\n"]
        assert (
            "No new operator input"
            in (
                await coordinator.delegation(
                    request,
                    {
                        **body,
                        "fragments": blanks,
                    },
                )
            )["output"]
        )
        assert not host.jobs and not decide.calls
        rows = [
            fragment("a", "Tell"),
            fragment("b", " "),
            fragment("c", "Codex"),
            fragment("d", " go"),
            fragment("e", " go"),
            fragment("f", "!  "),
        ]
        await coordinator.delegation(request, {**body, "fragments": rows})
        assert "".join(row["text"] for row in decide.calls[0]["source"]["fragments"]) == (
            "Tell Codex go go!  "
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    "rows,expected",
    [
        (
            [
                fragment("a", "Hel", item="one"),
                fragment("b", "Hello ", item="one", finality="item"),
                fragment("c", "world", item="two", finality="item"),
            ],
            "Hello world",
        ),
        (
            [fragment("a", "First ", finality="turn"), fragment("b", "second", finality="turn")],
            "First second",
        ),
        (
            [
                fragment("a", "late ", item="one", start=0, end=40),
                fragment("b", "other", item="two", start=100, end=200),
                fragment("c", "Final ", item="one", finality="item", start=0, end=80),
            ],
            "Final other",
        ),
        (
            [
                fragment("a", "old", item="one", start=0, end=40),
                fragment("b", " words", item="two", start=50, end=80),
                fragment("c", "whole turn", finality="turn", start=0, end=90),
            ],
            "whole turn",
        ),
    ],
)
def test_finals_replace_only_their_item_or_proven_interval(rows, expected):
    assert "".join(row["text"] for row in resolve_fragments(normalize_fragments(rows))) == expected


def test_late_final_inside_offset_replaces_item_but_does_not_replay(environment):
    async def run():
        coordinator, decide, request, host, bound, body = setup(environment)
        partial = fragment("partial", "Inspect ", item="utterance", start=0, end=100)
        final = fragment(
            "done",
            "Inspect this exact project",
            item="utterance",
            finality="item",
            start=0,
            end=900,
        )
        coordinator.transcript(request, {**body, "fragments": [partial, final]})
        await coordinator.delegation(request, {**body, "fragments": [partial]})
        source = decide.calls[0]["source"]
        assert source["offset_ms"] == 1000 and source["fragments"][0]["text"] == final["text"]
        repeat = await coordinator.delegation(
            request,
            {
                **body,
                "delegation_id": "late-notice",
                "fragments": [final],
            },
        )
        assert repeat["action"]["state"] == "accepted"
        assert len(decide.calls) == len(host.jobs) == 1
        assert bound.stages.records(bound.token)[0][-1]["text"] == final["text"]

    asyncio.run(run())


def test_late_final_past_offset_cannot_expand_an_accepted_capture(environment):
    async def run():
        coordinator, decide, request, host, bound, body = setup(environment)
        partial = fragment("partial", "Inspect this", item="utterance", start=0, end=900)
        final = fragment(
            "late-final",
            "Inspect this then delete it",
            item="utterance",
            finality="turn",
            start=0,
            end=1500,
        )
        coordinator.transcript(request, {**body, "fragments": [partial, final]})
        original = {**body, "fragments": [partial]}
        await coordinator.delegation(request, original)
        assert decide.calls[0]["source"]["fragments"][0]["text"] == partial["text"]
        no_action = await coordinator.delegation(
            request,
            {
                **body,
                "delegation_id": "late-notice",
                "offset_ms": 1600,
                "fragments": [final],
            },
        )
        assert "No new operator input" in no_action["output"]
        with pytest.raises(DashboardTaskError, match="different content"):
            await coordinator.delegation(
                request, {**original, "offset_ms": 1600, "fragments": [partial, final]}
            )
        assert len(decide.calls) == len(host.jobs) == 1
        rows, _ = bound.stages.records(bound.token)
        claimed = next(row for row in rows if "live_decision" in row)
        assert claimed["text"] == partial["text"]

    asyncio.run(run())


def test_final_replay_on_new_connection_reuses_canonical_receipt(environment):
    coordinator, _, request, host, bound, body = setup(environment)
    final = fragment("final", "Saved original words", item="utterance", finality="turn")
    first = coordinator.transcript(request, {**body, "fragments": [final]})
    # Model a lost ledger ACK after the host accepted the canonical input.
    with bound.stages._db(bound.token) as db:
        db.execute("UPDATE live_transcripts SET receipt=NULL")
    new_bound, context = join(environment, tab="new-native-connection")
    replay = coordinator.transcript(request, {**body, **context, "fragments": [final]})
    assert len(host.rows[("default", "task-a")]) == 2
    assert first["saved"][0]["state"] == replay["saved"][0]["state"] == "saved"
    assert not new_bound.stages.records(new_bound.token)[0][0].get("live_decision")
    with new_bound.stages._db(new_bound.token, write=False) as db:
        assert db.execute("SELECT count(*) FROM live_transcripts").fetchone()[0] == 1


def test_long_capture_batches_and_hot_reads_do_not_repeat_ddl_or_retention(
    environment, monkeypatch
):
    _, _, _, _, bound, _ = setup(environment)
    ledger = LiveLedger(bound)
    for batch in range(3):
        rows = normalize_fragments([fragment(f"{batch}-{index}", " x") for index in range(3000)])
        ledger.append("long-session", rows)
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced)
    for _ in range(3):
        assert len(LiveLedger(bound).recent()) == 40
        bound.stages.records(bound.token)
        bound.outbox.check(bound.token.owner, bound.token.connection_id, bound.token.generation)
    assert not any(
        row.startswith(("CREATE ", "DELETE ", "UPDATE ", "BEGIN IMMEDIATE")) for row in statements
    )
    with bound.stages._db(bound.token, write=False) as db:
        assert db.execute("SELECT count FROM live_transcript_totals").fetchone()[0] == 9000


def test_capture_batch_conflict_rolls_back_new_fragments(environment):
    _, _, _, _, bound, _ = setup(environment)
    ledger = LiveLedger(bound)
    ledger.append("session", normalize_fragments([fragment("one", "original")]))
    with pytest.raises(DashboardTaskError, match="different content"):
        ledger.append(
            "session",
            normalize_fragments(
                [
                    fragment("two", "new"),
                    fragment("one", "changed"),
                ]
            ),
        )
    assert [row["text"] for row in ledger.recent()] == ["original"]


def test_recipient_and_delegated_capability_context_reaches_decision(environment, monkeypatch):
    async def run():
        coordinator, decide, request, _, _, body = setup(environment)
        original = coordinator.manager.state
        recipients = {"selected": {"recipient_id": "native-codex-thread"}}
        capabilities = {"computer_use": {"available": True, "execution": "delegated"}}

        def state(*args, **kwargs):
            return {
                **original(*args, **kwargs),
                "recipients": recipients,
                "capabilities": capabilities,
            }

        monkeypatch.setattr(coordinator.manager, "state", state)
        decide.name = ""
        await coordinator.delegation(request, body)
        assert decide.calls[0]["context"]["recipients"] == recipients
        assert decide.calls[0]["context"]["capabilities"] == capabilities

    asyncio.run(run())
