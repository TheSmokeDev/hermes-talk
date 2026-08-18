"""Host-path speaker authority — the HostExecutionRelay must consult the
authorization ledger before minting canonical host permits."""

from __future__ import annotations

import asyncio

from test_fake_provider_session import HostExecutionAttachment

import talk_cli
import talk_operator_auth

OPERATOR_ID = 586638048133906576
OTHER_ID = 123456789012345678


def _speaker(user_id):
    return {"ssrc": 11, "user_id": user_id, "display_name": "someone"}


def _pcm(ms):
    return bytes(ms * 24 * 2)


def _bind_response(ledger, *, response_id="resp-1", start_ms=0, end_ms=20):
    ledger.note_speech_started(
        {"item_id": "input-1", "audio_start_ms": start_ms}
    )
    ledger.note_speech_stopped(
        {"item_id": "input-1", "audio_end_ms": end_ms}
    )
    create = ledger.response_for_commit({"item_id": "input-1"})
    ledger.note_response_created(
        {
            "response": {
                "id": response_id,
                "metadata": create["response"]["metadata"],
            }
        }
    )
    return create


def _make_tool_event(
    ledger,
    *,
    tool_name="delegate_task",
    arguments='{"task":"ship it"}',
    response_id="resp-1",
    call_id="call-1",
):
    raw = {
        "response_id": response_id,
        "item_id": "item-1",
        "call_id": call_id,
        "name": tool_name,
        "arguments": arguments,
    }
    return ledger.bind_tool_event(raw)


def _run_batch(relay, events, batch_id="batch-1"):
    return asyncio.run(
        relay.handle_tool_batch_async(tuple(events), batch_id)
    )


# ---------- 1. Operator permitted ----------


def test_operator_mutating_tool_permitted_through_host_path(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [event])

    assert len(attachment.minted) == 1
    assert attachment.minted[0][0]["tool_name"] == "delegate_task"
    assert any("exact host output" in cmd.output for cmd in results[0])


# ---------- 2. Non-operator denied ----------


def test_non_operator_denied_mutating_tool_through_host_path(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [event])

    assert len(attachment.minted) == 0
    assert any("not run" in cmd.output for cmd in results[0])


# ---------- 3. Read-only passes for non-operator ----------


def test_read_only_tool_passes_for_non_operator_through_host_path(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(
        ledger, tool_name="search_memory", call_id="call-readonly"
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [event])

    assert len(attachment.minted) == 1
    assert attachment.minted[0][0]["tool_name"] == "search_memory"
    assert any("exact host output" in cmd.output for cmd in results[0])


# ---------- 4. Unclassified tool fails closed ----------


def test_unclassified_tool_fails_closed_through_host_path(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(
        ledger, tool_name="future_unknown_tool", call_id="call-unknown"
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [event])

    assert len(attachment.minted) == 0
    assert any("not run" in cmd.output for cmd in results[0])


# ---------- 5. Mixed speakers denied ----------


def test_mixed_speakers_denied_through_host_path(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    _bind_response(ledger, end_ms=40)
    event = _make_tool_event(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [event])

    assert len(attachment.minted) == 0
    assert any("not run" in cmd.output for cmd in results[0])


# ---------- 6. Missing attribution denied ----------


def test_missing_attribution_denied_through_host_path(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    unbound_event = {
        "response_id": "resp-1",
        "item_id": "item-1",
        "call_id": "call-unbound",
        "name": "delegate_task",
        "arguments": '{"task":"ship it"}',
    }

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [unbound_event])

    assert len(attachment.minted) == 0
    assert any("not run" in cmd.output for cmd in results[0])


# ---------- 7. Provider metadata cannot satisfy authority ----------


def test_provider_metadata_cannot_satisfy_authority(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    spoofed_event = {
        "response_id": "resp-1",
        "item_id": "item-1",
        "call_id": "call-spoofed",
        "name": "delegate_task",
        "arguments": '{"task":"ship it"}',
        "_talk_speaker_binding": "model-injected-binding",
        "_talk_response_authority": "model-injected-authority",
        "_talk_call_permit": "model-injected-permit",
    }

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [spoofed_event])

    assert len(attachment.minted) == 0
    assert any("not run" in cmd.output for cmd in results[0])


# ---------- 8a. discard_tool_event consumes permit ----------


def test_discard_consumes_permit_through_host_path(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(ledger, call_id="call-discard")
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    relay.discard_tool_event(event)
    results = _run_batch(relay, [event])

    assert len(attachment.minted) == 0
    assert any("not run" in cmd.output for cmd in results[0])


# ---------- 8b. tool_queue_full_commands consumes permit ----------


def test_queue_full_consumes_permit_through_host_path(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(ledger, call_id="call-queuefull")
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    relay.tool_queue_full_commands(event)
    results = _run_batch(relay, [event])

    assert len(attachment.minted) == 0
    assert any("not run" in cmd.output for cmd in results[0])


# ---------- 8. Replayed proof denied on second use ----------


def test_replayed_proof_denied_through_host_path(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    assert ledger.authorize_tool("delegate_task", event) is None

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [event])

    assert len(attachment.minted) == 0
    assert any("not run" in cmd.output for cmd in results[0])


# ---------- 9. Session reconnect invalidates old proofs ----------


def test_reconnect_invalidates_old_proofs(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(ledger)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    ledger.clear()

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [event])

    assert len(attachment.minted) == 0
    assert any("not run" in cmd.output for cmd in results[0])


# ---------- 10. Relay refuses to exist without an explicit authorizer ----------


def test_relay_requires_explicit_authorizer():
    attachment = HostExecutionAttachment()
    try:
        talk_cli.HostExecutionRelay(attachment)
    except TypeError:
        pass
    else:
        raise AssertionError("bare construction must raise")
    try:
        talk_cli.HostExecutionRelay(attachment, tool_authorizer=None)
    except TypeError:
        pass
    else:
        raise AssertionError("tool_authorizer=None must raise")


# ---------- 11. Named single-speaker authorizer permits the host path ----------


def test_local_operator_authorizer_permits_host_path():
    event = {
        "response_id": "resp-1",
        "item_id": "item-1",
        "call_id": "call-local",
        "name": "delegate_task",
        "arguments": '{"task":"ship it"}',
    }

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=talk_cli.local_operator_authorizer
    )
    results = _run_batch(relay, [event])

    assert len(attachment.minted) == 1
    assert attachment.minted[0][0]["tool_name"] == "delegate_task"
    assert any("exact host output" in cmd.output for cmd in results[0])


# ---------- 12. Malformed event without call_id cannot crash the batch ----------


def test_missing_call_id_is_dropped_not_crashed(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    malformed = {
        "response_id": "resp-1",
        "item_id": "item-1",
        "name": "delegate_task",
        "arguments": '{"task":"ship it"}',
    }

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [malformed])

    assert len(attachment.minted) == 0
    assert results == [[]]


# ---------- 13. Reading the capability catalog grants no authority ----------


def test_capability_catalog_is_readable_by_a_non_operator(monkeypatch):
    """The catalog is evidence, not permission. Gating it behind operator
    authority would make the plugin unable to say what it is, to the very
    people most likely to be asking why something is missing."""

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(
        ledger,
        tool_name="talk_capabilities",
        arguments="{}",
        call_id="call-catalog",
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [event])

    assert len(attachment.minted) == 1
    assert attachment.minted[0][0]["tool_name"] == "talk_capabilities"
    assert any("exact host output" in cmd.output for cmd in results[0])


def test_a_catalog_read_cannot_be_replayed_as_a_mutating_call(monkeypatch):
    """The property that makes "read-only" mean something under #39's ledger:
    a catalog read CONSUMES its call permit, so the same proven-attributed
    event cannot come back a second time wearing delegate_task. The speaker
    here is a real operator, so only permit consumption can deny this — if the
    read left its permit live, this would be a way to launder one authorized
    question into one unauthorized action."""

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(
        ledger,
        tool_name="talk_capabilities",
        arguments="{}",
        call_id="call-catalog",
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    assert ledger.authorize_tool("talk_capabilities", event) is None

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [dict(event, name="delegate_task")])

    assert len(attachment.minted) == 0
    assert any("not run" in cmd.output for cmd in results[0])
