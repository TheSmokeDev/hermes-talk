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


@pytest.mark.parametrize(
    "model",
    ["gpt-realtime-2.1", "gpt-realtime-2.1-mini", "gpt-realtime-mini"],
)
def test_model_policy_explicitly_allows_known_duplex_tool_models(model):
    assert talk_config.realtime_model_compatibility(model) == "compatible"
    assert talk_config.realtime_model_valid(model) is True


@pytest.mark.parametrize(
    "model",
    ["gpt-realtime-whisper", "gpt-realtime-translate", "gpt-5.6"],
)
def test_model_policy_rejects_known_incompatible_models(model):
    assert talk_config.realtime_model_compatibility(model) == "incompatible"
    assert talk_config.realtime_model_valid(model) is False


@pytest.mark.parametrize(
    "model",
    ["gpt-realtime-totally-fake", "gpt-realtime-2099-01-01"],
)
def test_model_policy_labels_unlisted_realtime_syntax_as_unknown(model):
    assert talk_config.realtime_model_compatibility(model) == "unknown"
    assert talk_config.realtime_model_valid(model) is False


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


def test_discord_operator_ids_are_immutable_numeric_ids(monkeypatch):
    monkeypatch.setenv(
        "TALK_DISCORD_OPERATOR_USER_IDS",
        " 586638048133906576, 123456789012345678 ",
    )

    assert talk_config.discord_operator_user_ids() == frozenset(
        {586638048133906576, 123456789012345678}
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "586638048133906576,",
        ",586638048133906576",
        "586638048133906576,,123456789012345678",
        "586638048133906576,not-an-id",
        "-1",
        "+586638048133906576",
        "18446744073709551616",
        "9" * 5_000,
    ],
)
def test_blank_or_malformed_discord_operator_config_authorizes_nobody(
    monkeypatch, raw
):
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", raw)

    assert talk_config.discord_operator_user_ids() == frozenset()


def test_unset_discord_operator_config_authorizes_nobody(monkeypatch):
    monkeypatch.delenv("TALK_DISCORD_OPERATOR_USER_IDS", raising=False)

    assert talk_config.discord_operator_user_ids() == frozenset()


# --- identity_char_limit ------------------------------------------------------


def _home_with_config(monkeypatch, tmp_path, body: str):
    """An isolated Hermes home holding one config.yaml."""

    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_identity_char_limit_reads_the_memory_block(monkeypatch, tmp_path):
    _home_with_config(
        monkeypatch,
        tmp_path,
        "model:\n  default: gpt-5.5\nmemory:\n  memory_char_limit: 2200\n"
        "  user_char_limit: 1375\n",
    )

    assert talk_config.identity_char_limit("memory_char_limit") == 2200
    assert talk_config.identity_char_limit("user_char_limit") == 1375


def test_identity_char_limit_is_zero_when_unset(monkeypatch, tmp_path):
    """Zero means "no host opinion" — the caller applies its own cap. Callers
    must never read it as "emit nothing"."""

    _home_with_config(monkeypatch, tmp_path, "memory:\n  memory_enabled: true\n")

    assert talk_config.identity_char_limit("user_char_limit") == 0


def test_identity_char_limit_ignores_the_key_outside_the_memory_block(
    monkeypatch, tmp_path
):
    """Same discipline as the model scan: a matching key nested under some
    OTHER top-level section is not this key."""

    _home_with_config(
        monkeypatch,
        tmp_path,
        "voice:\n  user_char_limit: 99\nmemory:\n  memory_enabled: true\n",
    )

    assert talk_config.identity_char_limit("user_char_limit") == 0


def test_identity_char_limit_survives_a_missing_config(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "nowhere"))

    assert talk_config.identity_char_limit("memory_char_limit") == 0


def test_identity_char_limit_is_zero_for_a_key_no_host_declares(monkeypatch, tmp_path):
    """What makes ``working_char_limit`` shippable with no host-side change:
    an unknown key reads as "no host opinion", so the plugin cap applies and
    the section still travels."""

    _home_with_config(monkeypatch, tmp_path, "memory:\n  memory_char_limit: 6000\n")

    assert talk_config.identity_char_limit("working_char_limit") == 0


def test_session_key_is_none_when_unset(monkeypatch):
    monkeypatch.delenv("TALK_SESSION_KEY", raising=False)

    assert talk_config.session_key() is None


def test_session_key_returns_the_configured_value(monkeypatch):
    monkeypatch.setenv("TALK_SESSION_KEY", "  operator-pedro  ")

    assert talk_config.session_key() == "operator-pedro"


def test_session_key_blank_means_none(monkeypatch):
    # Set-but-blank sends no header, the same reading TALK_API_SERVER_KEY and
    # TALK_AGENT_PROFILE give it. There is nothing else to fall back to here.
    monkeypatch.setenv("TALK_SESSION_KEY", "   ")

    assert talk_config.session_key() is None
