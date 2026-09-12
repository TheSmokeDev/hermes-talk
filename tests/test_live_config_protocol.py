"""Live configuration, credential isolation, and protocol schemas; no real credentials."""

from __future__ import annotations

import base64
from dataclasses import replace

import pytest

import talk_auth
import talk_live_config as config
import talk_live_protocol as protocol
import talk_realtime as rt


def fake_auth(mode="subscription"):
    return talk_auth.TalkAuth(
        token="test-subscription-token" if mode == "subscription" else "test-api-key",
        source=talk_auth.SOURCE_CODEX_OAUTH if mode == "subscription" else talk_auth.SOURCE_ENV,
        detail="test credential",
        account_id="test-account" if mode == "subscription" else None,
    )


def setup_for(settings=None, **overrides):
    settings = settings or config.LiveConfig()
    return rt.SessionSetup(
        model=settings.model, voice=settings.voice, instructions="Host policy", **overrides
    )


def test_default_subscription_and_explicit_api_have_separate_defaults():
    assert config.resolve_live_config({}) == config.LiveConfig(
        "subscription", "gpt-live-1-codex", "cove"
    )
    assert config.resolve_live_config({"TALK_LIVE_AUTH": "api"}) == config.LiveConfig(
        "api", "gpt-live-1", "marin"
    )


@pytest.mark.parametrize(
    "values",
    [
        {"TALK_LIVE_AUTH": ""},
        {"TALK_LIVE_AUTH": "prefer-subscription"},
        {"TALK_LIVE_MODEL": ""},
        {"TALK_LIVE_VOICE": ""},
        {"TALK_LIVE_VOICE": "marin"},
        {"TALK_LIVE_AUTH": "api", "TALK_LIVE_VOICE": "cove"},
        {"TALK_LIVE_AUTH": "api", "TALK_LIVE_MODEL": "gpt-live-1-codex"},
        {"TALK_LIVE_MODEL": "gpt-realtime"},
    ],
)
def test_invalid_configuration_fails_closed(values):
    with pytest.raises(config.LiveConfigError):
        config.resolve_live_config(values)


def test_subscription_ignores_all_api_keys_and_legacy_preference(monkeypatch):
    credential = fake_auth()
    monkeypatch.setattr(talk_auth, "_resolve_codex_oauth", lambda path: credential)
    result = config.resolve_live_auth(
        env={
            "TALK_OPENAI_API_KEY": "",
            "OPENAI_API_KEY": "paid",
            "TALK_PREFER_CODEX_OAUTH": "invalid",
        }
    )
    assert result is credential
    assert result.account_id == "test-account"
    assert result.token not in repr(result)
    assert result.account_id not in repr(result)


def test_missing_subscription_never_uses_available_key(monkeypatch):
    monkeypatch.setattr(talk_auth, "_resolve_codex_oauth", lambda path: None)
    with pytest.raises(talk_auth.TalkAuthError, match="API credentials were not used"):
        config.resolve_live_auth(env={"OPENAI_API_KEY": "paid"})


def test_failed_refresh_cannot_choose_api_key(monkeypatch):
    def fail(_):
        raise talk_auth.TalkAuthError("private provider detail")

    monkeypatch.setattr(talk_auth, "_resolve_codex_oauth", fail)
    with pytest.raises(talk_auth.TalkAuthError, match="subscription authentication failed") as exc:
        config.resolve_live_auth(env={"OPENAI_API_KEY": "paid"})
    assert "private" not in str(exc.value)


def test_api_auth_never_consults_oauth_and_preserves_scoped_key_precedence(monkeypatch):
    def forbid(_):
        pytest.fail("API mode consulted OAuth")

    monkeypatch.setattr(talk_auth, "_resolve_codex_oauth", forbid)
    result = config.resolve_live_auth(
        env={
            "TALK_LIVE_AUTH": "api",
            "TALK_OPENAI_API_KEY": "scoped",
            "OPENAI_API_KEY": "shared",
            "TALK_PREFER_CODEX_OAUTH": "true",
        }
    )
    assert result.source == talk_auth.SOURCE_CONFIGURED
    assert result.token == "scoped"
    assert result.account_id is None
    with pytest.raises(talk_auth.TalkAuthError, match="set but empty"):
        config.resolve_live_auth(
            env={"TALK_LIVE_AUTH": "api", "TALK_OPENAI_API_KEY": "", "OPENAI_API_KEY": "shared"}
        )
    with pytest.raises(talk_auth.TalkAuthError, match="Codex OAuth was not used"):
        config.resolve_live_auth(env={"TALK_LIVE_AUTH": "api"})


@pytest.mark.parametrize(
    "credential,settings",
    [
        (fake_auth("api"), config.LiveConfig()),
        (fake_auth(), config.LiveConfig("api")),
        (replace(fake_auth(), account_id=None), config.LiveConfig()),
        (replace(fake_auth(), account_id=" "), config.LiveConfig()),
        (replace(fake_auth(), account_id="a\nb"), config.LiveConfig()),
        (replace(fake_auth(), token="a\nb"), config.LiveConfig()),
    ],
)
def test_injected_credentials_cannot_cross_auth_modes_or_headers(credential, settings):
    with pytest.raises(talk_auth.TalkAuthError):
        config.validate_live_auth(credential, settings)


@pytest.mark.parametrize("webrtc", [True, False])
def test_startup_uses_client_delegation_and_native_audio_shape(webrtc):
    settings = config.LiveConfig("api")
    result = protocol.build_live_session(
        setup_for(settings, task_continuity=True, automatic_response=False), settings, webrtc=webrtc
    )
    assert result["delegation"] == {"type": "client"}
    assert "tools" not in result
    assert "responses" not in result
    assert ("format" in result["audio"]) is not webrtc
    if not webrtc:
        assert result["audio"]["format"] == {"type": "audio/pcm", "rate": 24000}


@pytest.mark.parametrize(
    "change",
    [
        {"text_output": True},
        {"model": "gpt-realtime"},
        {"voice": "marin"},
        {"automatic_response": False},
        {"turn_detection": rt.RealtimeTurnDetection(rt.RealtimeTurnDetectionMode.SERVER_VAD)},
    ],
)
def test_unsupported_setup_fails_before_transport(change):
    with pytest.raises(rt.RealtimeSessionError):
        protocol.build_live_session(
            replace(setup_for(), **change), config.LiveConfig(), webrtc=True
        )


@pytest.mark.parametrize("role", ["input", "output"])
def test_public_fragments_preserve_interval_and_never_become_final(role):
    event = protocol.decode_event(
        {
            "type": f"session.{role}_transcript.delta",
            "delta": "hello",
            "event_id": "fragment-7",
            "start_ms": 100,
            "end_ms": 160,
        }
    )
    assert isinstance(event, rt.Transcript)
    assert event.final is False
    assert (event.start_ms, event.end_ms, event.item_id) == (100, 160, "fragment-7")
    assert event.role is (
        rt.TranscriptRole.USER if role == "input" else rt.TranscriptRole.ASSISTANT
    )


def test_subscription_fragment_is_partial_and_explicit_turn_done_is_final():
    delta = protocol.decode_event({"type": "input_transcript.added", "item": {"text": "hel"}})
    final = protocol.decode_event(
        {"type": "turn.done", "turn": {"role": "user", "transcript": "hello"}}
    )
    assert delta.final is False
    assert final.final is True
    assert final.text == "hello"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "session.input_transcript.delta", "delta": "x", "start_ms": 9, "end_ms": 2},
        {"type": "session.input_transcript.delta", "delta": "x", "start_ms": True},
        {"type": "session.input_transcript.delta", "delta": []},
        {"type": "session.output_audio.delta", "delta": "invalid"},
        {"type": "session.output_audio.delta", "delta": base64.b64encode(b"x").decode()},
        {"type": "turn.done", "turn": {"role": "system", "transcript": "x"}},
    ],
)
def test_malformed_events_emit_nonterminal_failure(event):
    result = protocol.decode_event(event)
    assert isinstance(result, rt.ProviderFailure)
    assert not result.terminal


def test_delegation_metadata_does_not_masquerade_as_function_call_or_transcript():
    event = protocol.decode_event(
        {
            "type": "session.delegation.created",
            "offset_ms": 750,
            "delegation": {"type": "delegation", "target": "client", "id": "del-1"},
        }
    )
    assert event == rt.DelegationRequested("del-1", 750)
    legacy = protocol.decode_event(
        {
            "type": "delegation.created",
            "item": {
                "type": "delegation",
                "target": "client",
                "id": "del-2",
                "content": [{"type": "input_text", "text": "model summary"}],
            },
        }
    )
    assert legacy.prompt == "model summary"
    assert not isinstance(legacy, (rt.FunctionCall, rt.Transcript))
    assert (
        protocol.decode_event(
            {
                "type": "session.delegation.created",
                "delegation": {
                    "type": "delegation",
                    "target": "responses",
                    "id": "foreign",
                },
            }
        )
        is None
    )


def test_error_text_cannot_leak_credentials_or_account_ids():
    event = protocol.decode_event(
        {
            "type": "error",
            "error": {
                "code": "invalid_token",
                "message": "test-subscription-token test-account",
            },
        }
    )
    assert event.terminal
    assert "test-subscription-token" not in event.detail
    assert "test-account" not in event.detail


@pytest.mark.parametrize("subscription", [False, True])
def test_result_chunks_preserve_unicode_and_use_mode_specific_delegation_envelope(subscription):
    content = "This result " + "🌳你好" * 150
    events = protocol.encode_command(
        rt.SubmitDelegationResult("del-1", content), subscription=subscription
    )
    assert len(events) > 1
    if subscription:
        assert all(item["type"] == "delegation.context.append" for item in events)
        assert all(
            item["delegation_item_id"] == "del-1" and item["channel"] == "speakable"
            for item in events
        )
        chunks = [item["content"][0]["text"] for item in events]
    else:
        assert all(item["type"] == "session.commentary.append" for item in events)
        assert all(item["delegation_id"] == "del-1" for item in events)
        chunks = [item["content"] for item in events]
    assert "".join(chunks) == content
    assert all(len(chunk.encode()) <= 500 for chunk in chunks)


@pytest.mark.parametrize(
    "kind,wire",
    [("instructions", "instructions"), ("context", "thinking"), ("message", "commentary")],
)
def test_public_context_commands_require_nullable_delegation_id(kind, wire):
    assert protocol.encode_command(rt.AppendLiveContext("hello", kind=kind)) == (
        {"type": f"session.{wire}.append", "content": "hello", "delegation_id": None},
    )


@pytest.mark.parametrize(
    "command",
    [
        rt.StartResponse(),
        rt.SubmitToolResult("id", "value"),
        rt.RemoveContext("id"),
        rt.AddInputText("id", "hello"),
    ],
)
def test_legacy_commands_are_rejected(command):
    with pytest.raises(rt.RealtimeSessionError, match="not a GPT-Live"):
        protocol.encode_command(command)


def test_separate_billing_settings_survive_auth_switch():
    env = {"TALK_LIVE_SUBSCRIPTION_MODEL": "gpt-live-1-codex",
           "TALK_LIVE_SUBSCRIPTION_VOICE": "cove", "TALK_LIVE_API_MODEL": "gpt-live-1",
           "TALK_LIVE_API_VOICE": "marin"}
    from talk_live_config import resolve_live_config

    subscription = resolve_live_config(env)
    api = resolve_live_config({**env, "TALK_LIVE_AUTH": "api"})
    assert subscription.auth_mode == "subscription" and subscription.voice == "cove"
    assert api.auth_mode == "api" and api.voice == "marin"
    assert resolve_live_config(env) == subscription
