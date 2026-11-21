"""Local (self-hosted OpenAI-Realtime-compatible) lane tests with a scripted socket.

No network and no real server: the socket is scripted, keys are fake short
strings, and the URL policy is exercised as pure configuration.
"""

from __future__ import annotations

import asyncio
import base64
import json
import types

import pytest

import talk_cli
import talk_config
import talk_doctor
import talk_local_realtime as local_rt
import talk_realtime as rt

LOCAL_ENV_NAMES = (
    "TALK_PROVIDER",
    "TALK_LOCAL_URL",
    "TALK_LOCAL_MODEL",
    "TALK_LOCAL_VOICE",
    "TALK_LOCAL_API_KEY",
    "TALK_TURN_DETECTION",
    "TALK_VOICE_MODE",
)


@pytest.fixture
def local_env(monkeypatch):
    for name in LOCAL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TALK_PROVIDER", "local")
    return monkeypatch


class _Message:
    def __init__(self, payload):
        self.type = "text"
        self.data = json.dumps(payload)


class _Socket:
    def __init__(self, events=()):
        self.events = iter(events)
        self.sent: list[dict] = []
        self.close_code = None

    async def send_json(self, message):
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return _Message(next(self.events))
        except StopIteration:
            raise StopAsyncIteration from None

    def exception(self):
        return None


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_exc):
        return None


class _Client:
    def __init__(self, socket):
        self.socket = socket
        self.connect_args = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    def ws_connect(self, *args, **kwargs):
        self.connect_args = (args, kwargs)
        return _Context(self.socket)


def _aiohttp(client):
    return types.SimpleNamespace(
        ClientSession=lambda: client,
        WSMsgType=types.SimpleNamespace(TEXT="text", ERROR="error"),
    )


def _setup(*, voice="default"):
    return rt.SessionSetup(
        model="local",
        voice=voice,
        instructions="Be brief.",
        tools=(
            rt.ToolDefinition(
                name="delegate_task",
                description="Hand work to a background agent",
                parameters={"type": "object", "properties": {"task": {"type": "string"}}},
            ),
        ),
    )


# -- configuration ---------------------------------------------------------------


def test_local_is_a_selectable_provider(local_env):
    assert talk_config.talk_provider() == "local"
    assert talk_config.talk_local_url() == talk_config.DEFAULT_LOCAL_URL
    assert talk_config.talk_local_model() == "local"
    assert talk_config.talk_local_voice() == "default"
    assert talk_config.resolve_local_key() == ""


@pytest.mark.parametrize(
    "url",
    [
        "ws://127.0.0.1:8765/v1/realtime",
        "ws://localhost:9000/v1/realtime",
        "ws://[::1]:8765/v1/realtime",
        "wss://voice.example.test/v1/realtime",
    ],
)
def test_local_url_accepts_loopback_ws_and_any_wss(local_env, url):
    local_env.setenv("TALK_LOCAL_URL", url)

    assert talk_config.talk_local_url() == url


@pytest.mark.parametrize(
    "url",
    [
        "ws://192.168.1.20:8765/v1/realtime",
        "ws://voice.example.test/v1/realtime",
        "http://127.0.0.1:8765/v1/realtime",
        "127.0.0.1:8765",
        "wss://voice.example.test/v1/realtime?model=x",
        "ws://",
    ],
)
def test_local_url_refuses_plaintext_off_loopback_and_malformed_urls(local_env, url):
    local_env.setenv("TALK_LOCAL_URL", url)

    with pytest.raises(talk_config.TalkConfigError):
        talk_config.talk_local_url()


def test_blank_local_key_refuses_closed(local_env):
    local_env.setenv("TALK_LOCAL_API_KEY", "  ")

    with pytest.raises(talk_config.TalkConfigError, match="set but empty"):
        talk_config.resolve_local_key()


def test_local_turn_detection_refuses_semantic_vad(local_env):
    local_env.setenv("TALK_TURN_DETECTION", "server_vad")
    assert talk_config.turn_detection("local").mode is rt.RealtimeTurnDetectionMode.SERVER_VAD

    local_env.setenv("TALK_TURN_DETECTION", "semantic_vad")
    with pytest.raises(talk_config.TalkConfigError):
        talk_config.turn_detection("local")


# -- session update ----------------------------------------------------------------


def test_session_update_drops_openai_hosted_ids_and_the_default_voice():
    update = local_rt.build_session_update(_setup())

    session = update["session"]
    assert update["type"] == "session.update"
    assert "model" not in session
    assert "transcription" not in session["audio"]["input"]
    assert "noise_reduction" not in session["audio"]["input"]
    assert "output" not in session["audio"]
    assert session["tools"][0]["name"] == "delegate_task"
    assert session["tool_choice"] == "auto"


def test_session_update_keeps_an_explicit_voice():
    update = local_rt.build_session_update(_setup(voice="Aiden"))

    assert update["session"]["audio"]["output"] == {"voice": "Aiden"}


# -- wire ----------------------------------------------------------------------------


def _connect(socket, *, token=""):
    client = _Client(socket)
    session = local_rt.LocalRealtimeSession(
        auth_token=token,
        auth_source="env",
        ws_url="ws://127.0.0.1:8765/v1/realtime",
        aiohttp_module=_aiohttp(client),
    )

    async def scenario():
        await session.connect(_setup())
        events = [event async for event in session]
        await session.close()
        return events

    return client, asyncio.run(scenario())


def test_keyless_connect_targets_the_local_url_without_an_auth_header():
    socket = _Socket()
    client, _events = _connect(socket)

    args, kwargs = client.connect_args
    assert args[0] == "ws://127.0.0.1:8765/v1/realtime?model=local"
    assert kwargs["headers"] == {}
    assert socket.sent[0]["type"] == "session.update"


def test_a_local_key_becomes_the_bearer():
    client, _events = _connect(_Socket(), token="local-test-key")

    _args, kwargs = client.connect_args
    assert kwargs["headers"] == {"Authorization": "Bearer local-test-key"}


def test_tool_call_and_audio_decode_through_the_openai_vocabulary():
    pcm = base64.b64encode(b"\x00\x01" * 8).decode("ascii")
    socket = _Socket(
        [
            {"type": "session.created", "session": {"id": "sess_local"}},
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_audio.delta", "delta": pcm, "item_id": "item_1",
             "response_id": "resp_1"},
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call_1",
                "name": "delegate_task",
                "arguments": json.dumps({"task": "Fix the tests"}),
                "response_id": "resp_1",
                "item_id": "item_2",
            },
            {"type": "response.done", "response": {"id": "resp_1", "status": "completed"}},
        ]
    )

    _client, events = _connect(socket)

    calls = [event for event in events if isinstance(event, rt.FunctionCall)]
    assert [call.name for call in calls] == ["delegate_task"]
    assert json.loads(calls[0].arguments) == {"task": "Fix the tests"}
    assert any(isinstance(event, rt.OutputAudio) for event in events)
    assert isinstance(events[-1], rt.SessionTerminated)


# -- CLI lane --------------------------------------------------------------------------


def test_cli_resolves_the_local_lane_and_builds_a_local_session(local_env):
    lane = talk_cli.resolve_provider_lane()

    assert (lane.provider, lane.model, lane.voice) == ("local", "local", "default")
    assert lane.auth.token == ""
    session = talk_cli._realtime_session(lane.auth, provider=lane.provider)
    assert isinstance(session, local_rt.LocalRealtimeSession)


def test_cli_lane_refuses_a_plaintext_remote_url(local_env):
    local_env.setenv("TALK_LOCAL_URL", "ws://10.0.0.5:8765/v1/realtime")

    with pytest.raises(talk_config.TalkConfigError):
        talk_cli.resolve_provider_lane()


# -- doctor ----------------------------------------------------------------------------


def _checks(report: dict) -> dict[str, dict]:
    return {check["id"]: check for check in report["checks"]}


def test_doctor_reports_the_local_lane_without_openai_auth(local_env):
    local_env.setenv("TALK_LOCAL_API_KEY", "local-test-key")

    checks = _checks(talk_doctor.collect_report())

    assert checks["provider"]["status"] == "pass"
    assert checks["provider"]["details"]["url"] == talk_config.DEFAULT_LOCAL_URL
    assert checks["provider"]["details"]["key"] == "present"
    assert checks["auth"]["status"] == "pass"
    assert checks["auth"]["details"]["provider"] == "local"
    assert checks["model"]["details"]["compatibility"] == "server-defined"
    assert checks["voice"]["details"]["provider"] == "local"
    assert "local-test-key" not in json.dumps(checks)


def test_doctor_fails_a_plaintext_remote_local_url(local_env):
    local_env.setenv("TALK_LOCAL_URL", "ws://10.0.0.5:8765/v1/realtime")

    check = _checks(talk_doctor.collect_report())["provider"]

    assert check["status"] == "fail"
    assert check["remediation"]


def test_doctor_fails_a_blank_local_key(local_env):
    local_env.setenv("TALK_LOCAL_API_KEY", "")

    checks = _checks(talk_doctor.collect_report())

    assert checks["provider"]["status"] == "fail"
    assert checks["auth"]["status"] == "fail"


def test_doctor_human_report_renders_the_local_lane(local_env, capsys):
    local_env.setenv("TALK_LOCAL_API_KEY", "local-test-key")

    text = talk_doctor.render_human(talk_doctor.collect_report())

    assert "receipt: provider=local, key=present" in text
    assert f"url={talk_config.DEFAULT_LOCAL_URL}" in text
    assert "local-test-key" not in text
