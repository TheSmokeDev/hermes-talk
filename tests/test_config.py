"""Config — fail-closed key resolution, voice validation, host paths."""

from __future__ import annotations

import sys

import pytest

import talk_config


def test_scoped_key_wins_over_shared(monkeypatch):
    monkeypatch.setenv("TALK_OPENAI_API_KEY", "sk-scoped")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")
    assert talk_config.resolve_openai_key() == "sk-scoped"


def test_shared_key_used_when_scoped_unset(monkeypatch):
    monkeypatch.delenv("TALK_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "  sk-shared  ")
    assert talk_config.resolve_openai_key() == "sk-shared"


def test_scoped_key_set_but_empty_is_a_refusal(monkeypatch):
    # The operator scoped a key on purpose. Falling through to the shared key
    # silently would use a credential they deliberately did not choose.
    monkeypatch.setenv("TALK_OPENAI_API_KEY", "   ")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared")
    with pytest.raises(talk_config.TalkConfigError, match="TALK_OPENAI_API_KEY"):
        talk_config.resolve_openai_key()


def test_shared_key_set_but_empty_is_a_refusal(monkeypatch):
    monkeypatch.delenv("TALK_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(talk_config.TalkConfigError, match="OPENAI_API_KEY"):
        talk_config.resolve_openai_key()


def test_no_key_at_all_raises(monkeypatch):
    monkeypatch.delenv("TALK_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(talk_config.TalkConfigError, match="no OpenAI key"):
        talk_config.resolve_openai_key()


def test_default_model_and_override(monkeypatch):
    monkeypatch.delenv("TALK_MODEL", raising=False)
    assert talk_config.talk_model() == talk_config.DEFAULT_TALK_MODEL
    monkeypatch.setenv("TALK_MODEL", " gpt-realtime-mini ")
    assert talk_config.talk_model() == "gpt-realtime-mini"


def test_voice_defaults_and_normalizes(monkeypatch):
    monkeypatch.delenv("TALK_VOICE", raising=False)
    assert talk_config.talk_voice() == talk_config.DEFAULT_TALK_VOICE
    monkeypatch.setenv("TALK_VOICE", " Marin ")
    assert talk_config.talk_voice() == "marin"


def test_unknown_voice_raises(monkeypatch):
    monkeypatch.setenv("TALK_VOICE", "morgan-freeman")
    with pytest.raises(talk_config.TalkConfigError, match="morgan-freeman"):
        talk_config.talk_voice()


def test_state_dir_follows_hermes_home(monkeypatch, tmp_path):
    # Block the host resolver so the env fallback is what gets exercised —
    # otherwise this passes or fails depending on whether hermes-agent happens
    # to be importable on the machine running the suite.
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    state = talk_config.state_dir()

    assert state == tmp_path / "home" / "state"
    assert state.is_dir()


def test_audio_device_overrides(monkeypatch):
    monkeypatch.delenv("TALK_INPUT_DEVICE", raising=False)
    monkeypatch.delenv("TALK_OUTPUT_DEVICE", raising=False)
    assert talk_config.audio_input_device() is None
    assert talk_config.audio_output_device() is None
    monkeypatch.setenv("TALK_INPUT_DEVICE", " 3 ")
    monkeypatch.setenv("TALK_OUTPUT_DEVICE", "Speakers (Realtek)")
    assert talk_config.audio_input_device() == "3"
    assert talk_config.audio_output_device() == "Speakers (Realtek)"
