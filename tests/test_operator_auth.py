"""Mutating-tool authorization and response/speaker binding."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

import talk_cli
import talk_operator_auth
import talk_relay

OPERATOR_ID = 586638048133906576
OTHER_ID = 123456789012345678


def _speaker(user_id: int | None) -> dict:
    return {"ssrc": 11, "user_id": user_id, "display_name": "display data"}


def _pcm(ms: int) -> bytes:
    return bytes(ms * 24 * 2)


def _bind_response(ledger, *, response_id="resp_1", start_ms=0, end_ms=20):
    ledger.note_speech_started(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": "item_1",
            "audio_start_ms": start_ms,
        }
    )
    ledger.note_speech_stopped(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": "item_1",
            "audio_end_ms": end_ms,
        }
    )
    create = ledger.response_for_commit(
        {"type": "input_audio_buffer.committed", "item_id": "item_1"}
    )
    ledger.note_response_created(
        {
            "type": "response.created",
            "response": {
                "id": response_id,
                "metadata": create["response"]["metadata"],
            },
        }
    )
    return create


def test_configured_speaker_can_run_mutating_tool_at_execution_time(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    create = _bind_response(ledger)
    event = ledger.bind_tool_event(
        {
            "response_id": "resp_1",
            "call_id": "call_delegate",
            "name": "delegate_task",
            "arguments": json.dumps({"task": "ship it"}),
        }
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    assert ledger.authorize_tool("delegate_task", event) is None
    assert create["type"] == "response.create"
    assert create["response"]["metadata"][talk_operator_auth.BINDING_METADATA_KEY]


def test_allowlist_is_re_read_when_the_mutating_tool_executes(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_stop"})

    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))
    assert ledger.authorize_tool("stop_work", event) is None
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OTHER_ID))
    assert "not run" in ledger.authorize_tool("stop_work", event)


@pytest.mark.parametrize("tool", sorted(talk_operator_auth.MUTATING_TALK_TOOLS))
def test_non_operator_is_denied_every_mutating_tool(monkeypatch, tool):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    _bind_response(ledger)
    event = ledger.bind_tool_event({"response_id": "resp_1"})
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    denial = ledger.authorize_tool(tool, event)

    assert "configured Discord operator" in denial
    assert "not run" in denial
    assert str(OPERATOR_ID) not in denial
    assert str(OTHER_ID) not in denial


def test_read_only_tools_remain_available_to_non_operators(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    monkeypatch.delenv("TALK_DISCORD_OPERATOR_USER_IDS", raising=False)

    for tool in talk_operator_auth.READ_ONLY_TALK_TOOLS:
        assert ledger.authorize_tool(tool, {}) is None


def test_unclassified_future_tool_fails_closed_even_for_configured_operator(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    denial = ledger.authorize_tool("future_state_changer", {})

    assert "not run" in denial


def test_two_speakers_in_one_vad_turn_fail_closed(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    _bind_response(ledger, end_ms=40)
    event = ledger.bind_tool_event({"response_id": "resp_1"})
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    assert "not run" in ledger.authorize_tool("delegate_task", event)


def test_missing_production_speech_started_event_fails_closed(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    ledger.note_speech_stopped({"item_id": "item_1", "audio_end_ms": 20})
    create = ledger.response_for_commit({"item_id": "item_1"})
    ledger.note_response_created(
        {"response": {"id": "resp_1", "metadata": create["response"]["metadata"]}}
    )
    event = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_1"})
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    assert "not run" in ledger.authorize_tool("delegate_task", event)


def test_duplicate_speech_start_cannot_narrow_a_mixed_turn_to_the_operator(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    ledger.note_speech_started({"item_id": "item_1", "audio_start_ms": 0})
    ledger.note_speech_started({"item_id": "item_1", "audio_start_ms": 20})
    ledger.note_speech_stopped({"item_id": "item_1", "audio_end_ms": 40})
    create = ledger.response_for_commit({"item_id": "item_1"})
    ledger.note_response_created(
        {"response": {"id": "resp_1", "metadata": create["response"]["metadata"]}}
    )
    event = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_1"})
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    assert "not run" in ledger.authorize_tool("delegate_task", event)


def test_recycled_vad_item_id_never_emits_a_second_response():
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(40))
    first = _bind_response(ledger)
    ledger.note_speech_started({"item_id": "item_1", "audio_start_ms": 20})
    ledger.note_speech_stopped({"item_id": "item_1", "audio_end_ms": 40})

    replay = ledger.response_for_commit({"item_id": "item_1"})

    assert first["type"] == "response.create"
    assert replay is None


def test_vad_item_tombstone_capacity_poison_revokes_delayed_authority(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger(max_seen_item_ids=1)
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(40))
    _bind_response(ledger)
    delayed = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_1"})
    ledger.note_speech_started({"item_id": "item_2", "audio_start_ms": 20})
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    assert "not run" in ledger.authorize_tool("delegate_task", delayed)


def test_unresolved_speaker_in_turn_fails_closed(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(None), _pcm(20))
    _bind_response(ledger)
    event = ledger.bind_tool_event({"response_id": "resp_1"})
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    assert "not run" in ledger.authorize_tool("steer_agent", event)


def test_missing_response_id_cannot_borrow_the_latest_speaker(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = ledger.bind_tool_event(
        {
            "response_id": "",
            "_talk_speaker_binding": "model-spoofed",
            "_talk_continuation": {"type": "response.create"},
        }
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    assert "not run" in ledger.authorize_tool("stop_work", event)
    assert event[talk_operator_auth.TRUSTED_BINDING_EVENT_KEY] is None
    assert event[talk_operator_auth.TRUSTED_CONTINUATION_EVENT_KEY] == {
        "type": "response.create"
    }


def test_response_metadata_token_is_required_not_just_response_order(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger, response_id="resp_good")
    ledger.note_response_created(
        {"response": {"id": "resp_unbound", "metadata": {}}}
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    good = ledger.bind_tool_event({"response_id": "resp_good", "call_id": "call_good"})
    unbound = ledger.bind_tool_event(
        {"response_id": "resp_unbound", "call_id": "call_unbound"}
    )

    assert ledger.authorize_tool("redirect_agent", good) is None
    assert "not run" in ledger.authorize_tool("redirect_agent", unbound)


def test_tool_continuation_preserves_the_same_opaque_binding():
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    first = _bind_response(ledger)
    event = ledger.bind_tool_event({"response_id": "resp_1"})

    continuation = event[talk_operator_auth.TRUSTED_CONTINUATION_EVENT_KEY]
    assert continuation != first
    assert continuation["response"]["metadata"][talk_operator_auth.BINDING_METADATA_KEY]


def test_copied_response_token_cannot_bind_a_forged_response_id(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    create = _bind_response(ledger, response_id="resp_expected")
    ledger.note_response_created(
        {
            "response": {
                "id": "resp_forged",
                "metadata": create["response"]["metadata"],
            }
        }
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    forged = ledger.bind_tool_event(
        {"response_id": "resp_forged", "call_id": "call_forged"}
    )

    assert "not run" in ledger.authorize_tool("redirect_agent", forged)


def test_reused_response_id_taints_delayed_authority_instead_of_rebinding(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger, response_id="resp_recycled")
    delayed = ledger.bind_tool_event(
        {"response_id": "resp_recycled", "call_id": "call_delayed"}
    )

    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    ledger.note_speech_started({"item_id": "item_2", "audio_start_ms": 20})
    ledger.note_speech_stopped(
        {
            "item_id": "item_2",
            "audio_end_ms": 40,
        }
    )
    create = ledger.response_for_commit({"item_id": "item_2"})
    ledger.note_response_created(
        {
            "response": {
                "id": "resp_recycled",
                "metadata": create["response"]["metadata"],
            }
        }
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    recycled = ledger.bind_tool_event(
        {"response_id": "resp_recycled", "call_id": "call_recycled"}
    )

    assert "not run" in ledger.authorize_tool("delegate_task", delayed)
    assert "not run" in ledger.authorize_tool("delegate_task", recycled)
    assert ledger.binding_for_response("resp_recycled") is None


def test_reused_response_id_with_invalid_token_still_revokes_delayed_authority(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger, response_id="resp_recycled")
    delayed = ledger.bind_tool_event(
        {"response_id": "resp_recycled", "call_id": "call_delayed_invalid_token"}
    )
    ledger.note_response_created(
        {
            "response": {
                "id": "resp_recycled",
                "metadata": {talk_operator_auth.BINDING_METADATA_KEY: "unknown-token"},
            }
        }
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    assert "not run" in ledger.authorize_tool("delegate_task", delayed)
    assert ledger.binding_for_response("resp_recycled") is None


def test_mutating_call_id_is_consumed_exactly_once(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    first = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_once"})
    replay = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_once"})

    assert ledger.authorize_tool("stop_work", first) is None
    assert "not run" in ledger.authorize_tool("stop_work", replay)


def test_same_bound_call_permit_cannot_authorize_mutation_twice(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))
    event = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_once"})

    assert ledger.authorize_tool("stop_work", event) is None
    assert "not run" in ledger.authorize_tool("stop_work", event)


def test_call_id_reused_across_response_ids_cannot_mutate(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(40))
    _bind_response(ledger, response_id="resp_1", end_ms=20)
    ledger.note_speech_started({"item_id": "item_2", "audio_start_ms": 20})
    ledger.note_speech_stopped({"item_id": "item_2", "audio_end_ms": 40})
    create = ledger.response_for_commit({"item_id": "item_2"})
    ledger.note_response_created(
        {"response": {"id": "resp_2", "metadata": create["response"]["metadata"]}}
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    first = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_shared"})
    replay = ledger.bind_tool_event({"response_id": "resp_2", "call_id": "call_shared"})

    assert ledger.authorize_tool("steer_agent", first) is None
    assert "not run" in ledger.authorize_tool("steer_agent", replay)


@pytest.mark.parametrize("call_id", [None, "", "  ", " call_1", 7, [], {}])
def test_missing_or_malformed_call_id_never_authorizes_mutation(monkeypatch, call_id):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    event = ledger.bind_tool_event({"response_id": "resp_1", "call_id": call_id})

    assert "not run" in ledger.authorize_tool("stop_work", event)


def test_continuation_token_is_fresh_shared_and_single_use(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    initial = _bind_response(ledger)
    first = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_1"})
    second = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_2"})
    continuation = first[talk_operator_auth.TRUSTED_CONTINUATION_EVENT_KEY]

    assert continuation == second[talk_operator_auth.TRUSTED_CONTINUATION_EVENT_KEY]
    assert continuation != initial

    ledger.note_response_created(
        {"response": {"id": "resp_2", "metadata": continuation["response"]["metadata"]}}
    )
    ledger.note_response_created(
        {"response": {"id": "resp_forged", "metadata": continuation["response"]["metadata"]}}
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    good = ledger.bind_tool_event({"response_id": "resp_2", "call_id": "call_3"})
    forged = ledger.bind_tool_event({"response_id": "resp_forged", "call_id": "call_4"})
    assert ledger.authorize_tool("redirect_agent", good) is None
    assert "not run" in ledger.authorize_tool("redirect_agent", forged)


def test_recycled_parent_response_revokes_unconsumed_continuation_chain(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    parent = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_parent"})
    continuation = parent[talk_operator_auth.TRUSTED_CONTINUATION_EVENT_KEY]
    ledger.note_response_created(
        {"response": {"id": "resp_1", "metadata": {}}}
    )
    ledger.note_response_created(
        {"response": {"id": "resp_2", "metadata": continuation["response"]["metadata"]}}
    )
    child = ledger.bind_tool_event({"response_id": "resp_2", "call_id": "call_child"})
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    assert "not run" in ledger.authorize_tool("redirect_agent", parent)
    assert "not run" in ledger.authorize_tool("redirect_agent", child)


def test_queue_rejection_consumes_bound_mutating_call_permit(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))
    executed = []
    relay = talk_relay.RealtimeRelay(
        tool_authorizer=ledger.authorize_tool,
        tool_executor=lambda name, arguments: executed.append((name, arguments)) or "ok",
    )
    event = ledger.bind_tool_event(
        {
            "type": "response.function_call_arguments.done",
            "response_id": "resp_1",
            "call_id": "call_queue_full",
            "name": "stop_work",
            "arguments": '{"run_id": 7}',
        }
    )

    relay.tool_queue_full_output(event)
    replay_output = relay.handle_event(event)

    assert executed == []
    assert "not run" in replay_output[0]["item"]["output"]


@pytest.mark.parametrize(
    "terminal_error",
    [
        talk_relay.ToolWorkerBusy("full"),
        talk_relay.ToolExecutionTimeout(started=False),
        talk_relay.ToolExecutionTimeout(started=True),
    ],
)
def test_daemon_rejection_or_timeout_consumes_bound_permit(
    monkeypatch, terminal_error
):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))
    executed = []
    relay = talk_relay.RealtimeRelay(
        tool_authorizer=ledger.authorize_tool,
        tool_executor=lambda name, arguments: executed.append((name, arguments)) or "ok",
    )
    event = ledger.bind_tool_event(
        {
            "type": "response.function_call_arguments.done",
            "response_id": "resp_1",
            "call_id": "call_daemon_rejected",
            "name": "stop_work",
            "arguments": '{"run_id": 7}',
        }
    )

    async def reject(*_args, **_kwargs):
        raise terminal_error

    monkeypatch.setattr(talk_relay, "run_bounded_on_daemon", reject)
    asyncio.run(relay.handle_event_async(event))
    replay_output = relay.handle_event(event)

    assert executed == []
    assert "not run" in replay_output[0]["item"]["output"]


def test_pending_event_is_revoked_across_discard_and_session_clear(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))
    executed = []
    relay = talk_relay.RealtimeRelay(
        tool_authorizer=ledger.authorize_tool,
        tool_executor=lambda name, arguments: executed.append((name, arguments)) or "ok",
    )
    event = ledger.bind_tool_event(
        {
            "type": "response.function_call_arguments.done",
            "response_id": "resp_1",
            "call_id": "call_pending_teardown",
            "name": "stop_work",
            "arguments": '{"run_id": 7}',
        }
    )

    async def send_batch(_messages):
        raise AssertionError("discarded work must not send")

    coordinator = talk_cli.ToolResponseCoordinator(relay, send_batch, max_pending=1)
    assert coordinator.admit(event)
    coordinator.discard_pending()
    ledger.clear()
    replay_output = relay.handle_event(event)

    assert executed == []
    assert "not run" in replay_output[0]["item"]["output"]


def test_read_only_handling_consumes_permit_before_name_mutation(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_1"})
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    assert ledger.authorize_tool("search_memory", event) is None
    assert "not run" in ledger.authorize_tool("stop_work", event)


def test_malformed_arguments_consume_permit_before_bound_event_replay(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))
    executed = []
    relay = talk_relay.RealtimeRelay(
        tool_authorizer=ledger.authorize_tool,
        tool_executor=lambda name, arguments: executed.append((name, arguments)) or "ok",
    )
    event = ledger.bind_tool_event(
        {
            "type": "response.function_call_arguments.done",
            "response_id": "resp_1",
            "call_id": "call_bad_args",
            "name": "stop_work",
            "arguments": "{",
        }
    )

    relay.handle_event(event)
    event["arguments"] = '{"run_id": 7}'
    replay_output = relay.handle_event(event)

    assert executed == []
    assert "not run" in replay_output[0]["item"]["output"]


def test_response_identity_capacity_exhaustion_poison_denies_all_mutation(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger(max_seen_response_ids=2)
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(60))
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))
    delayed = None
    for index in range(3):
        item_id = f"item_{index}"
        ledger.note_speech_started(
            {"item_id": item_id, "audio_start_ms": index * 20}
        )
        ledger.note_speech_stopped(
            {
                "item_id": item_id,
                "audio_end_ms": (index + 1) * 20,
            }
        )
        create = ledger.response_for_commit({"item_id": item_id})
        response_id = f"resp_{index}"
        ledger.note_response_created(
            {"response": {"id": response_id, "metadata": create["response"]["metadata"]}}
        )
        if index == 0:
            delayed = ledger.bind_tool_event(
                {"response_id": response_id, "call_id": "call_before_poison"}
            )

    after = ledger.bind_tool_event({"response_id": "resp_2", "call_id": "call_after"})
    assert "not run" in ledger.authorize_tool("delegate_task", delayed)
    assert "not run" in ledger.authorize_tool("delegate_task", after)


def test_call_identity_capacity_exhaustion_poison_denies_delayed_mutation(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger(max_seen_call_ids=1)
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))
    delayed = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_1"})

    overflow = ledger.bind_tool_event({"response_id": "resp_1", "call_id": "call_2"})

    assert "not run" in ledger.authorize_tool("stop_work", delayed)
    assert "not run" in ledger.authorize_tool("stop_work", overflow)


def test_clear_resets_response_call_and_poison_tombstones(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger(
        max_seen_response_ids=1, max_seen_call_ids=1
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger, response_id="resp_same")
    first = ledger.bind_tool_event(
        {"response_id": "resp_same", "call_id": "call_same"}
    )
    assert ledger.authorize_tool("stop_work", first) is None
    ledger.bind_tool_event({"response_id": "resp_same", "call_id": "overflow"})

    ledger.clear()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger, response_id="resp_same")
    after_teardown = ledger.bind_tool_event(
        {"response_id": "resp_same", "call_id": "call_same"}
    )

    assert ledger.authorize_tool("stop_work", after_teardown) is None


def test_replayed_done_event_never_executes_mutating_handler_twice(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))
    executed: list[tuple[str, dict]] = []
    relay = talk_relay.RealtimeRelay(
        tool_authorizer=ledger.authorize_tool,
        tool_executor=lambda name, arguments: executed.append((name, arguments)) or "ok",
    )
    raw = {
        "type": "response.function_call_arguments.done",
        "response_id": "resp_1",
        "call_id": "call_replayed",
        "name": "stop_work",
        "arguments": '{"run_id": 7}',
    }

    relay.handle_event(ledger.bind_tool_event(raw))
    replay_output = relay.handle_event(ledger.bind_tool_event(raw))

    assert executed == [("stop_work", {"run_id": 7})]
    assert "not run" in replay_output[0]["item"]["output"]


def test_call_id_replay_across_responses_never_reaches_mutating_handler(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(40))
    _bind_response(ledger, response_id="resp_1", end_ms=20)
    ledger.note_speech_started({"item_id": "item_2", "audio_start_ms": 20})
    ledger.note_speech_stopped({"item_id": "item_2", "audio_end_ms": 40})
    create = ledger.response_for_commit({"item_id": "item_2"})
    ledger.note_response_created(
        {"response": {"id": "resp_2", "metadata": create["response"]["metadata"]}}
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))
    executed: list[str] = []
    relay = talk_relay.RealtimeRelay(
        tool_authorizer=ledger.authorize_tool,
        tool_executor=lambda name, _arguments: executed.append(name) or "ok",
    )

    for response_id in ("resp_1", "resp_2"):
        relay.handle_event(
            ledger.bind_tool_event(
                {
                    "type": "response.function_call_arguments.done",
                    "response_id": response_id,
                    "call_id": "call_cross_response",
                    "name": "delegate_task",
                    "arguments": '{"task": "ship"}',
                }
            )
        )

    assert executed == ["delegate_task"]


def test_duplicate_commit_cannot_create_a_second_response():
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    ledger.note_speech_started({"item_id": "item_1", "audio_start_ms": 0})
    ledger.note_speech_stopped(
        {
            "item_id": "item_1",
            "audio_end_ms": 20,
        }
    )

    first = ledger.response_for_commit({"item_id": "item_1"})
    duplicate = ledger.response_for_commit({"item_id": "item_1"})

    assert first["type"] == "response.create"
    assert duplicate is None


def test_completion_and_teardown_clear_bounded_response_state():
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger(max_responses=2)
    for index in range(3):
        ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
        ledger.note_speech_started(
            {"item_id": f"item_{index}", "audio_start_ms": index * 20}
        )
        ledger.note_speech_stopped(
            {
                "item_id": f"item_{index}",
                "audio_end_ms": (index + 1) * 20,
            }
        )
        create = ledger.response_for_commit({"item_id": f"item_{index}"})
        ledger.note_response_created(
            {
                "response": {
                    "id": f"resp_{index}",
                    "metadata": create["response"]["metadata"],
                }
            }
        )

    # Active-capacity exhaustion denies all mutation rather than evicting an
    # older identity and reopening its response ID for replay.
    assert ledger.response_count == 0
    ledger.complete_response("resp_2", continued=False)
    assert ledger.binding_for_response("resp_2") is None
    ledger.clear()
    assert ledger.response_count == 0
    assert ledger.binding_count == 0
    assert ledger.segment_count == 0


def test_unchanged_arguments_still_authorize_within_ttl(monkeypatch):
    """The permit binds arguments, so the ordinary path — approve, then run
    exactly what was approved — must still go through on the first attempt.
    """

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    event = ledger.bind_tool_event(
        {
            "response_id": "resp_1",
            "call_id": "call_1",
            "name": "stop_work",
            "arguments": '{"target": "42"}',
        }
    )

    assert ledger.authorize_tool("stop_work", event) is None


def test_changed_arguments_after_mint_deny_execution(monkeypatch):
    """An approval covers one exact action. Arguments rewritten between the
    approved summary and execution are a different action, not the same one.
    """

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    event = ledger.bind_tool_event(
        {
            "response_id": "resp_1",
            "call_id": "call_1",
            "name": "stop_work",
            "arguments": '{"target": "42"}',
        }
    )
    event["arguments"] = '{"target": "43"}'

    assert "not run" in ledger.authorize_tool("stop_work", event)


def test_reordered_argument_keys_are_the_same_approved_action(monkeypatch):
    """Canonicalization is by value, not by serialization: a provider that
    re-emits the same arguments in another key order has not changed them.
    """

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    event = ledger.bind_tool_event(
        {
            "response_id": "resp_1",
            "call_id": "call_1",
            "name": "steer_agent",
            "arguments": '{"agent_id": "sa-0-a1b2c3d4", "text": "focus on pricing"}',
        }
    )
    event["arguments"] = '{"text": "focus on pricing", "agent_id": "sa-0-a1b2c3d4"}'

    assert ledger.authorize_tool("steer_agent", event) is None


def test_expired_permit_denies_execution(monkeypatch):
    """A permit is not valid forever. Once its window passes, the operator
    approves again rather than a stale yes firing into a moved-on conversation.
    """

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))
    monkeypatch.setenv("TALK_APPROVAL_PERMIT_TTL_S", "0.01")

    event = ledger.bind_tool_event(
        {
            "response_id": "resp_1",
            "call_id": "call_1",
            "name": "stop_work",
            "arguments": '{"target": "42"}',
        }
    )
    # sleep() guarantees a lower bound on elapsed time, so a 10ms window is
    # always past after 30ms — the expiry fires deterministically, not by luck.
    time.sleep(0.03)

    assert "not run" in ledger.authorize_tool("stop_work", event)


def test_permit_records_action_target_and_session_for_the_audit_trail():
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.bind_session("talk-session-abc")
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)

    event = ledger.bind_tool_event(
        {
            "response_id": "resp_1",
            "call_id": "call_1",
            "name": "steer_agent",
            "arguments": '{"agent_id": "sa-0-a1b2c3d4", "text": "focus on pricing"}',
        }
    )

    permit = event["_talk_call_permit"]
    assert permit.action == "steer_agent"
    assert permit.target == "sa-0-a1b2c3d4"
    assert permit.talk_session_id == "talk-session-abc"


def test_ambiguous_speaker_still_denies_even_with_a_minted_permit(monkeypatch):
    """The new argument and expiry checks are additive. A permit that is
    structurally valid must not become a way past the speaker-trust gate.
    """

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(10))
    ledger.record_packet(_speaker(OTHER_ID), _pcm(10))
    _bind_response(ledger, end_ms=20)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    event = ledger.bind_tool_event(
        {
            "response_id": "resp_1",
            "call_id": "call_1",
            "name": "stop_work",
            "arguments": '{"target": "42"}',
        }
    )

    assert "not run" in ledger.authorize_tool("stop_work", event)


def test_read_only_tools_are_unaffected_by_argument_binding(monkeypatch):
    """Read-only tools never carried an approval to begin with; the permit
    checks must not start gating the half of the surface that stays open.
    """

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    monkeypatch.setenv("TALK_APPROVAL_PERMIT_TTL_S", "0.01")

    event = ledger.bind_tool_event(
        {
            "response_id": "resp_1",
            "call_id": "call_1",
            "name": "search_memory",
            "arguments": '{"query": "pricing"}',
        }
    )
    time.sleep(0.03)
    event["arguments"] = '{"query": "something else"}'

    assert ledger.authorize_tool("search_memory", event) is None
