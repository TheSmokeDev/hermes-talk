"""Wire layer — session payload shape and ephemeral-secret handling."""

from __future__ import annotations

import json
import types

import pytest

import talk_realtime as rt
import talk_wire


def test_session_payload_enables_server_vad_and_barge_in():
    payload = talk_wire.build_session_payload(
        model="gpt-realtime-2.1", voice="cedar", instructions="be brief"
    )
    turn_detection = payload["audio"]["input"]["turn_detection"]
    assert payload["type"] == "realtime"
    assert payload["model"] == "gpt-realtime-2.1"
    assert payload["instructions"] == "be brief"
    assert turn_detection["type"] == "server_vad"
    assert turn_detection["create_response"] is True
    # Barge-in is the whole differentiator: without this the model talks over
    # the operator and every interrupt is cosmetic.
    assert turn_detection["interrupt_response"] is True
    assert (
        payload["audio"]["input"]["transcription"]["model"] == talk_wire.INPUT_TRANSCRIPTION_MODEL
    )
    assert payload["audio"]["output"]["voice"] == "cedar"


def test_turn_detection_encoder_has_exact_native_server_and_semantic_shapes():
    expected_server = {
        "type": "server_vad",
        "create_response": False,
        "interrupt_response": True,
    }
    assert (
        talk_wire.encode_turn_detection(
            rt.RealtimeTurnDetection(),
            automatic_response=False,
        )
        == expected_server
    )
    assert (
        talk_wire.encode_turn_detection(
            rt.RealtimeTurnDetection(mode=rt.RealtimeTurnDetectionMode.SERVER_VAD),
            automatic_response=False,
        )
        == expected_server
    )
    assert talk_wire.encode_turn_detection(
        rt.RealtimeTurnDetection(mode=rt.RealtimeTurnDetectionMode.SEMANTIC_VAD),
        automatic_response=False,
    ) == {
        "type": "semantic_vad",
        "eagerness": "auto",
        "create_response": False,
        "interrupt_response": True,
    }
    assert talk_wire.encode_turn_detection(
        rt.RealtimeTurnDetection(
            mode=rt.RealtimeTurnDetectionMode.SEMANTIC_VAD,
            semantic_eagerness=rt.RealtimeSemanticEagerness.HIGH,
        ),
        automatic_response=True,
    ) == {
        "type": "semantic_vad",
        "eagerness": "high",
        "create_response": True,
        "interrupt_response": True,
    }


def test_invalid_endpointing_is_refused_before_mint_network(monkeypatch):
    called = False

    def fake_post(_auth_token, _session):
        nonlocal called
        called = True
        return {"value": "must-not-be-used"}

    monkeypatch.setattr(talk_wire, "post_client_secret", fake_post)
    invalid = types.SimpleNamespace(
        mode=rt.RealtimeTurnDetectionMode.SERVER_VAD,
        semantic_eagerness=rt.RealtimeSemanticEagerness.LOW,
    )
    with pytest.raises(ValueError, match="only for semantic"):
        talk_wire.mint_ephemeral_session(
            auth_token="secret",
            model="gpt-realtime-test",
            voice="cedar",
            instructions="brief",
            turn_detection=invalid,
        )
    assert called is False


def test_input_only_payload_disables_automatic_response_at_mint_time():
    payload = talk_wire.build_session_payload(
        model="gpt-realtime-2.1",
        voice="cedar",
        instructions="transcribe only",
        automatic_response=False,
    )

    assert payload["audio"]["input"]["turn_detection"]["create_response"] is False
    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_session_payload_omits_tools_when_none_passed():
    payload = talk_wire.build_session_payload(
        model="m", voice="cedar", instructions="hi", tools=None
    )
    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_session_payload_carries_tools_and_auto_choice():
    tools = [{"type": "function", "name": "talk_status", "parameters": {}}]
    payload = talk_wire.build_session_payload(
        model="m", voice="cedar", instructions="hi", tools=tools
    )
    assert payload["tools"] == tools
    assert payload["tool_choice"] == "auto"


def test_parse_client_secret_flat_shape():
    value, expires_ms = talk_wire.parse_client_secret({"value": "ek_flat", "expires_at": 1_700})
    assert value == "ek_flat"
    assert expires_ms == 1_700_000


def test_parse_client_secret_nested_shape():
    value, expires_ms = talk_wire.parse_client_secret(
        {"client_secret": {"value": "ek_nested", "expires_at": 42}}
    )
    assert value == "ek_nested"
    assert expires_ms == 42_000


def test_parse_client_secret_without_expiry_is_none():
    value, expires_ms = talk_wire.parse_client_secret({"value": "ek_only"})
    assert value == "ek_only"
    assert expires_ms is None


@pytest.mark.parametrize(
    "payload",
    [{}, {"value": ""}, {"client_secret": {}}, {"client_secret": "not-a-dict"}],
)
def test_parse_client_secret_missing_value_raises(payload):
    with pytest.raises(talk_wire.TalkUpstreamError):
        talk_wire.parse_client_secret(payload)


def test_mint_never_returns_the_auth_token(monkeypatch):
    seen: dict = {}

    def fake_post(auth_token, session):
        seen["auth_token"] = auth_token
        seen["session"] = session
        return {"value": "ek_minted", "expires_at": 100}

    monkeypatch.setattr(talk_wire, "post_client_secret", fake_post)

    descriptor = talk_wire.mint_ephemeral_session(
        auth_token="sk-super-secret",
        model="gpt-realtime-2.1",
        voice="cedar",
        instructions="be brief",
        tools=[{"type": "function", "name": "talk_status", "parameters": {}}],
    )

    assert seen["auth_token"] == "sk-super-secret"
    assert seen["session"]["tool_choice"] == "auto"
    assert descriptor.client_secret == "ek_minted"
    assert descriptor.expires_at_ms == 100_000
    assert descriptor.offer_url == talk_wire.OPENAI_REALTIME_OFFER_URL

    # The token that minted the secret must not survive into anything a client
    # can see. Serialize the whole descriptor and grep it.
    wire = json.dumps(descriptor.to_wire())
    assert "sk-super-secret" not in wire
    assert "ek_minted" in wire


def test_input_only_mint_passes_false_into_the_http_payload(monkeypatch):
    seen = {}

    def fake_post(_auth_token, session):
        seen.update(session)
        return {"value": "ephemeral"}

    monkeypatch.setattr(talk_wire, "post_client_secret", fake_post)

    talk_wire.mint_ephemeral_session(
        auth_token="secret",
        model="gpt-realtime-2.1",
        voice="cedar",
        instructions="transcribe only",
        automatic_response=False,
    )

    assert seen["audio"]["input"]["turn_detection"]["create_response"] is False
