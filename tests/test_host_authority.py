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


def _bind_response(
    ledger, *, response_id="resp-1", start_ms=0, end_ms=20, input_item="input-1"
):
    ledger.note_speech_started(
        {"item_id": input_item, "audio_start_ms": start_ms}
    )
    ledger.note_speech_stopped(
        {"item_id": input_item, "audio_end_ms": end_ms}
    )
    create = ledger.response_for_commit({"item_id": input_item})
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
    item_id="item-1",
):
    raw = {
        "response_id": response_id,
        "item_id": item_id,
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


# ---------- 8. discard_tool_event consumes permit ----------


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


# ---------- 9. tool_queue_full_commands consumes permit ----------


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


# ---------- 10. Replayed proof denied on second use ----------


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


# ---------- 11. Session reconnect invalidates old proofs ----------


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


# ---------- 12. Relay refuses to exist without an explicit authorizer ----------


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


# ---------- 13. Named single-speaker authorizer permits the host path ----------


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


# ---------- 14. Malformed event without call_id cannot crash the batch ----------


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


# ---------- 16. One batch carrying both an authorized and a denied event ----------


def test_a_mixed_batch_authorizes_each_event_on_its_own_merits(monkeypatch):
    """Authorization is per EVENT, not per batch (Archon LOW, #47).

    Every other batch test carries events that all pass or all fail, so a
    relay that decided once and applied the verdict to the whole tuple would
    have looked identical. Here one event is the operator's and one is a
    stranger's, in the same batch, under the same permit-minting span.
    """

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(
        ledger, response_id="resp-1", start_ms=0, end_ms=20, input_item="in-1"
    )
    permitted = _make_tool_event(
        ledger, response_id="resp-1", call_id="call-ok", item_id="item-1"
    )

    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    _bind_response(
        ledger, response_id="resp-2", start_ms=20, end_ms=40, input_item="in-2"
    )
    denied = _make_tool_event(
        ledger, response_id="resp-2", call_id="call-no", item_id="item-2"
    )

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [permitted, denied])

    # Exactly one permit reached the host, and it is the operator's.
    assert len(attachment.minted) == 1
    assert attachment.minted[0][0]["call_id"] == "call-ok"

    assert any("exact host output" in cmd.output for cmd in results[0])
    assert results[1], "the denied event produced no result at all"
    assert not any("exact host output" in cmd.output for cmd in results[1])
    # Denied for its SPEAKER, not incidentally: the same wording every other
    # non-operator denial in this file asserts.
    assert any("not run" in cmd.output for cmd in results[1])
    assert any("Discord operator" in cmd.output for cmd in results[1])
    # Each result is addressed to its own call, not merged.
    assert results[0][0].call_id == "call-ok"
    assert results[1][0].call_id == "call-no"


def test_a_mixed_batch_consumes_the_denied_events_permit_too(monkeypatch):
    """A denied event must not leave a replayable permit behind."""

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(
        ledger, response_id="resp-1", start_ms=0, end_ms=20, input_item="in-1"
    )
    permitted = _make_tool_event(
        ledger, response_id="resp-1", call_id="call-ok", item_id="item-1"
    )

    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    _bind_response(
        ledger, response_id="resp-2", start_ms=20, end_ms=40, input_item="in-2"
    )
    denied = _make_tool_event(
        ledger, response_id="resp-2", call_id="call-no", item_id="item-2"
    )

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    _run_batch(relay, [permitted, denied])

    # Replay the once-denied event now that the operator has spoken again:
    # its permit was consumed on the denial, so it cannot come back.
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    replay = _run_batch(relay, [denied], batch_id="batch-2")

    assert len(attachment.minted) == 1, "a consumed permit was replayed"
    assert not any("exact host output" in cmd.output for cmd in replay[0])


# ---------- 17. An unnamed call is matched as the unknown it is ----------


def test_an_unnamed_call_is_revoked_under_the_same_name_it_is_authorized_under():
    """#47 item 3: the two authorizer call sites disagreed on the fallback.

    `_consume_tool_attempt` said "tool" and the batch path said "" for the
    same nameless event, so the identity used to revoke a permit was not the
    identity used to authorize it. One owner now answers for both.
    """

    seen: list[str] = []

    def recording_authorizer(name, _event):
        seen.append(name)
        return None

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=recording_authorizer
    )
    nameless = {"call_id": "call-1", "arguments": "{}", "response_id": "r", "item_id": "i"}

    relay.discard_tool_event(dict(nameless))
    relay.tool_queue_full_commands(dict(nameless))
    _run_batch(relay, [dict(nameless)])

    assert seen == ["", "", ""], "an unnamed call took a different identity per path"
    assert talk_cli._event_tool_name({}) == ""
    assert talk_cli._event_tool_name({"name": None}) == ""
    assert talk_cli._event_tool_name({"name": "delegate_task"}) == "delegate_task"


# ---------- 15. Reading the capability catalog grants no authority ----------


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


# ---------- Transport-independent classification (capability bridge, F1) ----------


def _local_event(name, arguments, call_id="call-1", item_id="item-1"):
    return {
        "response_id": "resp-1",
        "item_id": item_id,
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def test_local_lane_never_dispatches_a_delegate_class_host_tool():
    """The classification gate rides ABOVE the transport authorizer: even the
    all-permitting local single-speaker authorizer cannot dispatch a
    destructive or unclassified host tool bare — its in-handler approval
    gates fail open on the plugin thread. The denial steers to delegation
    instead of refusing flat."""

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=talk_cli.local_operator_authorizer
    )
    events = [
        _local_event(
            "computer_use", '{"action": "click", "coordinate": [5, 5]}', "call-1", "item-1"
        ),
        _local_event("terminal_command", '{"command": "rm -rf ./x"}', "call-2", "item-2"),
        _local_event("tool_describe", '{"name": "anything"}', "call-3", "item-3"),
    ]

    results = _run_batch(relay, events)

    assert attachment.minted == []  # nothing reached the canonical host batch
    for result in results:
        assert "was not run" in result[0].output
        assert "spin up an agent" in result[0].output


def test_local_lane_still_dispatches_inline_safe_permit_reads_and_talk_tools():
    """What the gate does NOT catch: curated read-only host tools, the
    permit-class read actions (the local single speaker IS the operator),
    and talk tools — delegate_task is the steering receipt's destination and
    must never be classification-denied."""

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=talk_cli.local_operator_authorizer
    )
    events = [
        _local_event("web_search", '{"query": "hermes"}', "call-1", "item-1"),
        _local_event("computer_use", '{"action": "capture"}', "call-2", "item-2"),
        _local_event("delegate_task", '{"task": "ship it"}', "call-3", "item-3"),
    ]

    results = _run_batch(relay, events)

    minted = [entry[0]["tool_name"] for entry in attachment.minted]
    assert minted == ["web_search", "computer_use", "delegate_task"]
    for result in results:
        assert "exact host output" in result[0].output


def test_discord_lane_classification_is_unchanged_by_the_relay_gate(monkeypatch):
    """Belt and suspenders on Discord: the ledger already classifies (it also
    guards the legacy lane), and the relay gate agrees with it — a
    permit-less destructive computer_use never mints, with the steering
    denial spoken."""

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _make_tool_event(
        ledger,
        tool_name="computer_use",
        arguments='{"action": "click"}',
    )
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    attachment = HostExecutionAttachment()
    relay = talk_cli.HostExecutionRelay(
        attachment, tool_authorizer=ledger.authorize_tool
    )
    results = _run_batch(relay, [event])

    assert attachment.minted == []
    assert any("not run" in cmd.output for cmd in results[0])
