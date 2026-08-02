"""CLI — session.update shaping and the fail-closed startup paths."""

from __future__ import annotations

import argparse
import asyncio

import talk_audio
import talk_cli


def test_session_update_drops_type_and_model():
    message = talk_cli.build_session_update(
        model="gpt-realtime-2.1", voice="cedar", instructions="be brief", tools=None
    )

    assert message["type"] == "session.update"
    session = message["session"]
    # Both are set at connect time; the Realtime session object does not take
    # either on an update, so leaving them in is a rejected session.
    assert "type" not in session
    assert "model" not in session
    assert session["instructions"] == "be brief"
    assert session["audio"]["output"]["voice"] == "cedar"
    assert session["audio"]["input"]["turn_detection"]["interrupt_response"] is True


def test_session_update_carries_tools():
    tools = [{"type": "function", "name": "talk_status", "parameters": {}}]
    session = talk_cli.build_session_update(
        model="m", voice="cedar", instructions="hi", tools=tools
    )["session"]

    assert session["tools"] == tools
    assert session["tool_choice"] == "auto"


def test_missing_credentials_exit_one_without_opening_audio(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("TALK_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Hermetic OAuth lane: a dev box's real ~/.codex login must not leak in.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    def never(_self):  # pragma: no cover - must not be reached
        raise AssertionError("audio opened before auth was resolved")

    monkeypatch.setattr(talk_audio.DuplexAudio, "start", never)

    assert asyncio.run(talk_cli.run_talk_session()) == 1
    assert "codex login" in capsys.readouterr().err


def test_unusable_voice_exits_one(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("TALK_VOICE", "not-a-voice")

    assert asyncio.run(talk_cli.run_talk_session()) == 1
    assert "not-a-voice" in capsys.readouterr().err


def test_missing_audio_stack_exits_one_before_dialling(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("TALK_VOICE", raising=False)

    def fail(_self):
        raise talk_audio.TalkAudioError('run: pip install "hermes-talk[audio]"')

    monkeypatch.setattr(talk_audio.DuplexAudio, "start", fail)

    def never():  # pragma: no cover - must not be reached
        raise AssertionError("dialled OpenAI without a working audio device")

    monkeypatch.setattr(talk_cli, "_import_aiohttp", never)

    assert asyncio.run(talk_cli.run_talk_session()) == 1
    assert "hermes-talk[audio]" in capsys.readouterr().err


def test_keyboard_interrupt_hangs_up_cleanly(monkeypatch, capsys):
    async def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(talk_cli, "run_talk_session", interrupted)

    assert talk_cli.cli_entry() == 0
    assert "hung up" in capsys.readouterr().out


def test_setup_cli_adds_no_required_arguments():
    parser = argparse.ArgumentParser()
    talk_cli.setup_cli(parser)

    assert parser.parse_args([]).talk_command == "session"
