"""Host-tool classification and the steering deny receipts (capability bridge).

The classification table routes canonical Hermes tool names arriving at the
voice surface: curated read-only tools run inline, sensitive reads ride the
spoken-permit flow, and everything else delegates — with a denial that STEERS
to delegation instead of refusing flat.
"""

from __future__ import annotations

import json

import pytest

import talk_operator_auth

OPERATOR_ID = 586638048133906576
OTHER_ID = 123456789012345678


def _speaker(user_id: int | None) -> dict:
    return {"ssrc": 11, "user_id": user_id, "display_name": "display data"}


def _pcm(ms: int) -> bytes:
    return bytes(ms * 24 * 2)


def _bind_response(ledger, *, response_id="resp_1", start_ms=0, end_ms=20):
    ledger.note_speech_started({"item_id": "item_1", "audio_start_ms": start_ms})
    ledger.note_speech_stopped({"item_id": "item_1", "audio_end_ms": end_ms})
    create = ledger.response_for_commit({"item_id": "item_1"})
    ledger.note_response_created(
        {"response": {"id": response_id, "metadata": create["response"]["metadata"]}}
    )
    return create


def _bind_call(ledger, name, arguments, *, call_id="call_1"):
    return ledger.bind_tool_event(
        {
            "response_id": "resp_1",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(arguments),
        }
    )


# -- the table itself ------------------------------------------------------------


def test_every_inline_safe_entry_is_read_only_by_host_precedent():
    """The list is short and auditable: the host's own webhook-safe read class
    plus session_search, which the legacy lane already exposes read-only."""

    assert talk_operator_auth.VOICE_INLINE_SAFE == frozenset(
        {"session_search", "web_search", "web_extract", "vision_analyze"}
    )
    # No overlap with the permit class: a name routes to exactly one class.
    assert talk_operator_auth.VOICE_INLINE_SAFE.isdisjoint(
        talk_operator_auth.VOICE_PERMIT_GATED
    )


@pytest.mark.parametrize(
    "tool", sorted(talk_operator_auth.VOICE_INLINE_SAFE), ids=lambda t: t
)
def test_inline_safe_tools_classify_inline(tool):
    assert talk_operator_auth.classify_host_tool(tool) == "inline"
    assert talk_operator_auth.classify_host_tool(tool, {"query": "x"}) == "inline"


def test_computer_use_read_actions_classify_permit():
    for action in sorted(talk_operator_auth.COMPUTER_USE_VOICE_READ_ACTIONS):
        assert (
            talk_operator_auth.classify_host_tool("computer_use", {"action": action})
            == "permit"
        ), action


def test_computer_use_mutating_actions_classify_delegate():
    """The destructive half is never passed through on a spoken permit: its
    in-handler approval gate fails open without a bound approval context, so
    voice routes it to the delegate lane where the run approval loop gates."""

    for action in ("click", "type", "key", "scroll", "focus_app"):
        assert (
            talk_operator_auth.classify_host_tool("computer_use", {"action": action})
            == "delegate"
        ), action


def test_computer_use_with_a_missing_or_unparseable_action_fails_closed():
    assert talk_operator_auth.classify_host_tool("computer_use") == "delegate"
    assert talk_operator_auth.classify_host_tool("computer_use", {}) == "delegate"
    assert talk_operator_auth.classify_host_tool("computer_use", {"action": 42}) == "delegate"
    assert talk_operator_auth.classify_host_tool("computer_use", "not json") == "delegate"
    # Case/whitespace tolerate speech-shaped input; unknown verbs do not.
    assert (
        talk_operator_auth.classify_host_tool("computer_use", {"action": " Capture "})
        == "permit"
    )
    assert (
        talk_operator_auth.classify_host_tool("computer_use", {"action": "screenshot"})
        == "delegate"
    )


def test_unclassified_names_classify_delegate():
    """Real or invented, a name the table does not know delegates."""

    for name in ("terminal", "write_file", "tool_describe", "tool_call", "launch_missiles"):
        assert talk_operator_auth.classify_host_tool(name) == "delegate", name


# -- authorization through the ledger --------------------------------------------


def test_inline_safe_host_tool_passes_without_a_permit_even_for_non_operators(monkeypatch):
    """Provably read-only: no spoken permit required, same as the read-only
    talk tools. The room may look up the web; it may not mutate."""

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    monkeypatch.delenv("TALK_DISCORD_OPERATOR_USER_IDS", raising=False)

    assert ledger.authorize_tool("web_search", {"arguments": "{}"}) is None


def test_permit_gated_host_tool_passes_with_a_fresh_operator_permit(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _bind_call(ledger, "computer_use", {"action": "capture"})
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    assert ledger.authorize_tool("computer_use", event) is None


def test_permit_gated_host_tool_without_a_permit_gets_the_steering_denial(monkeypatch):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OTHER_ID), _pcm(20))
    _bind_response(ledger)
    event = _bind_call(ledger, "computer_use", {"action": "capture"})
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    denial = ledger.authorize_tool("computer_use", event)

    assert "not run" in denial
    assert "spin up an agent that can" in denial
    assert "want me to" in denial


def test_computer_use_destructive_action_is_never_permitted_by_voice(monkeypatch):
    """Even a fully-bound operator permit cannot carry a destructive action:
    classification happens BEFORE the permit check and routes it to delegate."""

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _bind_call(ledger, "computer_use", {"action": "click", "x": 10, "y": 20})
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    denial = ledger.authorize_tool("computer_use", event)

    assert denial is not None
    assert "not run" in denial
    assert "spin up an agent" in denial


def test_unclassified_host_tool_gets_the_steering_denial_even_for_operators(monkeypatch):
    """The tool_describe class of invention: denied, but the receipt offers the
    bridge instead of refusing flat — for the configured operator too, because
    the tool is not voice-callable at all."""

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    ledger.record_packet(_speaker(OPERATOR_ID), _pcm(20))
    _bind_response(ledger)
    event = _bind_call(ledger, "tool_describe", {"name": "computer_use"})
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    denial = ledger.authorize_tool("tool_describe", event)

    assert denial is not None
    assert "not run" in denial
    assert "isn't something I can do directly in a voice call" in denial
    assert "spin up an agent that can — want me to?" in denial


def test_unclassified_denial_does_not_leak_attribution_detail(monkeypatch):
    """The room hears the limit and the bridge; WHY verification failed stays
    in the log, where a stranger cannot argue with it."""

    ledger = talk_operator_auth.DiscordToolAuthorizationLedger()
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(OPERATOR_ID))

    denial = ledger.authorize_tool("terminal", {})

    assert denial is not None
    assert str(OPERATOR_ID) not in denial
    assert "attribution" not in denial
