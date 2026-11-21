"""Experimental OpenAI-compatible cascade TTS backend (hermes-talk#151).

No network and no real keys: the speech endpoint is a scripted fake POST and
every key here is a literal placeholder. Operator env is scrubbed first so a
dev box with real keys cannot leak one into an assertion.
"""

from __future__ import annotations

import asyncio
import io
import wave

import pytest
import wallclock

import talk_cascade_voice as cascade
import talk_config
import talk_doctor
import talk_realtime as rt

FAKE_KEY = "fake-gateway-key-for-tests"
BASE = "http://127.0.0.1:4000/v1"

_ENV = (
    "TALK_VOICE_MODE", "TALK_PROVIDER", "TALK_CASCADE_TTS", "TALK_CASCADE_SPEED",
    "TALK_ELEVENLABS_API_KEY", "ELEVENLABS_API_KEY", "TALK_ELEVENLABS_VOICE_ID",
    "TALK_ELEVENLABS_MODEL", "TALK_CASCADE_OPENAI_BASE_URL", "TALK_CASCADE_OPENAI_API_KEY",
    "TALK_CASCADE_OPENAI_MODEL", "TALK_CASCADE_OPENAI_VOICE", "TALK_CASCADE_OPENAI_FORMAT",
    "TALK_OPENAI_API_KEY", "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def _scrub(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def _openai_env(monkeypatch, **extra):
    monkeypatch.setenv("TALK_CASCADE_TTS", "openai")
    monkeypatch.setenv("TALK_CASCADE_OPENAI_BASE_URL", BASE)
    monkeypatch.setenv("TALK_CASCADE_OPENAI_MODEL", "sopro")
    monkeypatch.setenv("TALK_CASCADE_OPENAI_VOICE", "alloy")
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


# -- config -------------------------------------------------------------------


def test_default_backend_is_unchanged_elevenlabs_triple(monkeypatch):
    monkeypatch.setenv("TALK_ELEVENLABS_API_KEY", "fake-eleven")
    monkeypatch.setenv("TALK_ELEVENLABS_VOICE_ID", "voice1")
    assert talk_config.cascade_tts() == "elevenlabs"
    config = talk_config.cascade_voice_config("openai")
    assert config == ("fake-eleven", "voice1", talk_config.DEFAULT_ELEVENLABS_MODEL)
    assert config.backend == "elevenlabs"
    voice = cascade.build_cascade_voice(config, on_audio=lambda _b: None, on_error=print)
    assert type(voice) is cascade.CascadeVoice


def test_openai_backend_resolves_and_never_borrows_the_openai_key(monkeypatch):
    _openai_env(monkeypatch, OPENAI_API_KEY="real-openai-key", TALK_OPENAI_API_KEY="real-too")
    config = talk_config.cascade_voice_config("openai")
    assert config.backend == "openai"
    assert config.base_url == BASE
    assert (config.api_key, config.voice_id, config.model) == (None, "alloy", "sopro")
    assert config.response_format == "pcm"
    monkeypatch.setenv("TALK_CASCADE_OPENAI_API_KEY", FAKE_KEY)
    config = talk_config.cascade_voice_config("openai")
    assert config.api_key == FAKE_KEY
    assert FAKE_KEY not in repr(config)


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("http://tts.example.com/v1", "plain http"),
        ("ftp://127.0.0.1/v1", "http:// or https://"),
        ("https://user:pw@tts.example.com/v1", "credentials"),
        ("https://tts.example.com/v1?key=x", "credentials"),
    ],
)
def test_base_url_refusals(monkeypatch, url, match):
    _openai_env(monkeypatch, TALK_CASCADE_OPENAI_BASE_URL=url)
    with pytest.raises(talk_config.TalkConfigError, match=match):
        talk_config.cascade_voice_config("openai")


def test_https_remote_and_http_loopback_are_accepted(monkeypatch):
    _openai_env(monkeypatch, TALK_CASCADE_OPENAI_BASE_URL="https://tts.example.com/v1/")
    assert talk_config.cascade_openai_base_url() == "https://tts.example.com/v1"
    monkeypatch.setenv("TALK_CASCADE_OPENAI_BASE_URL", "http://localhost:8880/v1")
    assert talk_config.cascade_openai_base_url() == "http://localhost:8880/v1"


@pytest.mark.parametrize(
    "missing",
    ["TALK_CASCADE_OPENAI_BASE_URL", "TALK_CASCADE_OPENAI_MODEL", "TALK_CASCADE_OPENAI_VOICE"],
)
def test_required_openai_knobs_refuse_by_name(monkeypatch, missing):
    _openai_env(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(talk_config.TalkConfigError, match=missing):
        talk_config.cascade_voice_config("openai")


def test_blank_key_bad_format_and_unknown_backend_refuse(monkeypatch):
    _openai_env(monkeypatch, TALK_CASCADE_OPENAI_API_KEY="  ")
    with pytest.raises(talk_config.TalkConfigError, match="set but empty"):
        talk_config.cascade_voice_config("openai")
    monkeypatch.delenv("TALK_CASCADE_OPENAI_API_KEY")
    monkeypatch.setenv("TALK_CASCADE_OPENAI_FORMAT", "mp3")
    with pytest.raises(talk_config.TalkConfigError, match="TALK_CASCADE_OPENAI_FORMAT"):
        talk_config.cascade_voice_config("openai")
    monkeypatch.setenv("TALK_CASCADE_TTS", "edge")
    with pytest.raises(talk_config.TalkConfigError, match="TALK_CASCADE_TTS"):
        talk_config.cascade_tts()


def test_saved_experimental_settings_apply_when_env_is_unset(monkeypatch):
    saved = {
        "cascade_tts": "openai",
        "cascade_openai_base_url": BASE,
        "cascade_openai_model": "sopro",
        "cascade_openai_voice": "alloy",
    }
    monkeypatch.setattr(talk_config, "plugin_setting", saved.get)
    config = talk_config.cascade_voice_config("openai")
    assert (config.backend, config.base_url, config.model) == ("openai", BASE, "sopro")
    monkeypatch.setenv("TALK_CASCADE_TTS", "elevenlabs")  # env wins
    assert talk_config.cascade_tts() == "elevenlabs"


def test_doctor_reports_the_openai_backend(monkeypatch):
    monkeypatch.setenv("TALK_VOICE_MODE", "cascade")
    _openai_env(monkeypatch, TALK_CASCADE_OPENAI_API_KEY=FAKE_KEY)
    check = talk_doctor._cascade_check()
    assert check["status"] == "pass", check
    assert FAKE_KEY not in repr(check)
    monkeypatch.delenv("TALK_CASCADE_OPENAI_BASE_URL")
    check = talk_doctor._cascade_check()
    assert check["status"] == "fail"
    assert FAKE_KEY not in repr(check)


# -- the speech leg -----------------------------------------------------------


class _FakePost:
    """Scripted ``POST /audio/speech``: each call yields the next body's parts."""

    def __init__(self, bodies, *, hold: asyncio.Event | None = None, error=None) -> None:
        self.bodies = list(bodies)
        self.calls: list[dict] = []
        self.hold = hold
        self.error = error

    def __call__(self, url, headers, payload):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        parts = self.bodies.pop(0) if self.bodies else [b""]
        return self._body(parts)

    async def _body(self, parts):
        if self.error is not None:
            raise self.error
        for index, part in enumerate(parts):
            if index and self.hold is not None:
                await self.hold.wait()
            yield part


def _voice(post, *, key=FAKE_KEY, fmt="pcm", speed=None):
    audio: list[bytes] = []
    errors: list[str] = []
    voice = cascade.OpenAICompatibleCascadeVoice(
        base_url=BASE, api_key=key, voice_id="alloy", model="sopro",
        response_format=fmt, speed=speed, http_post=post,
        on_audio=audio.append, on_error=errors.append,
    )
    voice.start()
    return voice, audio, errors


def _text(text, final=False):
    return rt.Transcript(
        role=rt.TranscriptRole.ASSISTANT, text=text, final=final,
        provenance=rt.TranscriptProvenance.OUTPUT_AUDIO, response_id="resp_1",
    )


async def _wait_for(condition, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + wallclock.stretch(timeout)
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0)


def _wav(rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(b"\x01\x00\x02\x00")
    return buffer.getvalue()


def test_each_chunk_is_one_post_and_pcm_reaches_the_sink():
    async def run():
        post = _FakePost([[b"\x01\x00\x02"], [b"\x03\x00"]])
        voice, audio, errors = _voice(post, speed=1.1)
        try:
            voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
            voice.handle_event(_text("Hello there. How are you?"))
            voice.handle_event(_text("", final=True))
            await _wait_for(lambda: len(post.calls) == 2 and len(audio) >= 1)
            await asyncio.sleep(0)
        finally:
            await voice.aclose()
        call = post.calls[0]
        assert call["url"] == f"{BASE}/audio/speech"
        assert call["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
        assert FAKE_KEY not in call["url"]
        assert call["payload"] == {
            "model": "sopro", "voice": "alloy", "input": "Hello there.",
            "response_format": "pcm", "speed": 1.1,
        }
        assert post.calls[1]["payload"]["input"] == "How are you?"
        # An odd trailing byte never splits a 16-bit sample.
        assert audio[0] == b"\x01\x00"
        assert errors == []

    asyncio.run(run())


def test_keyless_server_gets_no_authorization_header():
    async def run():
        post = _FakePost([[b"\x00\x00"]])
        voice, audio, _errors = _voice(post, key=None)
        try:
            voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
            voice.handle_event(_text("Hi.", final=True))
            await _wait_for(lambda: audio)
        finally:
            await voice.aclose()
        assert "Authorization" not in post.calls[0]["headers"]

    asyncio.run(run())


def test_wav_is_decoded_and_a_wrong_rate_degrades_to_text():
    async def run():
        post = _FakePost([[_wav(24_000)], [_wav(16_000)]])
        voice, audio, errors = _voice(post, fmt="wav")
        try:
            voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
            voice.handle_event(_text("One.", final=True))
            await _wait_for(lambda: audio)
            assert audio == [b"\x01\x00\x02\x00"]
            voice.handle_event(rt.ResponseStarted(response_id="resp_2"))
            voice.handle_event(rt.Transcript(
                role=rt.TranscriptRole.ASSISTANT, text="Two.", final=True,
                provenance=rt.TranscriptProvenance.OUTPUT_AUDIO, response_id="resp_2",
            ))
            await _wait_for(lambda: errors)
        finally:
            await voice.aclose()
        assert len(audio) == 1
        assert errors == [
            "cascade voice failed for that answer — staying text-only (CascadeTTSError)"
        ]

    asyncio.run(run())


def test_barge_in_cancels_the_request_and_drops_late_audio():
    async def run():
        hold = asyncio.Event()
        post = _FakePost([[b"\x01\x00", b"\x02\x00"]], hold=hold)
        voice, audio, errors = _voice(post)
        try:
            voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
            voice.handle_event(_text("Long answer.", final=True))
            await _wait_for(lambda: audio == [b"\x01\x00"])
            voice.handle_event(rt.SpeechStarted())
            hold.set()
            for _ in range(5):
                await asyncio.sleep(0)
        finally:
            await voice.aclose()
        assert audio == [b"\x01\x00"]
        assert errors == []

    asyncio.run(run())


def test_http_failure_receipt_never_carries_the_key():
    async def run():
        post = _FakePost([], error=cascade.CascadeTTSError("TTS endpoint answered HTTP 401"))
        voice, audio, errors = _voice(post)
        try:
            voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
            voice.handle_event(_text("Hi.", final=True))
            await _wait_for(lambda: errors)
        finally:
            await voice.aclose()
        assert audio == []
        assert len(errors) == 1 and FAKE_KEY not in errors[0]

    asyncio.run(run())
