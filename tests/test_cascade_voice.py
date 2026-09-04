"""Custom-voice cascade — chunker, stream lifecycle, barge-in, fail-closed config.

No network, no real keys: the ElevenLabs leg is a scripted fake WebSocket and
every key in this file is a literal placeholder. The real ``ELEVENLABS_API_KEY``
in an operator environment is explicitly deleted before any test that resolves
config, so a dev box cannot leak a credential into an assertion.
"""

from __future__ import annotations

import asyncio
import base64
import json

import aiohttp
import pytest

import talk_cascade_voice as cascade
import talk_cli
import talk_config
import talk_doctor
import talk_openai_realtime
import talk_realtime as rt
import talk_wire

FAKE_KEY = "fake-elevenlabs-key-for-tests"
FAKE_VOICE_ID = "fakeVoiceId0001"


def _scrub_elevenlabs_env(monkeypatch):
    """Make cascade key resolution hermetic on a box that HAS the real key."""

    monkeypatch.delenv("TALK_ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("TALK_ELEVENLABS_VOICE_ID", raising=False)
    monkeypatch.delenv("TALK_VOICE_MODE", raising=False)
    monkeypatch.delenv("TALK_CASCADE_TTS", raising=False)
    monkeypatch.delenv("TALK_ELEVENLABS_MODEL", raising=False)
    monkeypatch.delenv("TALK_CASCADE_SPEED", raising=False)


# ---------------------------------------------------------------------------
# Sentence chunker
# ---------------------------------------------------------------------------


def test_chunker_splits_on_terminal_punctuation():
    chunker = cascade.SentenceChunker()
    assert chunker.feed("Hello world. ") == ["Hello world."]
    # A terminal mark at the buffer's edge is held until more text proves it.
    assert chunker.feed("How are you?") == []
    assert chunker.flush() == ["How are you?"]


def test_chunker_splits_exclamation_question_and_ellipsis():
    chunker = cascade.SentenceChunker()
    pieces = chunker.feed("Wow! Really? Wait... yes. ")
    assert pieces == ["Wow!", "Really?", "Wait...", "yes."]
    assert chunker.flush() == []


def test_chunker_never_splits_decimals():
    chunker = cascade.SentenceChunker()
    assert chunker.feed("Pi is 3.14 exactly. ") == ["Pi is 3.14 exactly."]


def test_chunker_never_splits_abbreviations():
    chunker = cascade.SentenceChunker()
    assert chunker.feed("Dr. Smith and Mr. Jones arrived. ") == ["Dr. Smith and Mr. Jones arrived."]
    assert chunker.feed("Use tools, e.g. search, often. ") == ["Use tools, e.g. search, often."]


def test_chunker_never_splits_initials_or_acronyms():
    chunker = cascade.SentenceChunker()
    assert chunker.feed("J. Robert spoke. ") == ["J. Robert spoke."]
    assert chunker.feed("The U.S.A. team won. ") == ["The U.S.A. team won."]


def test_chunker_never_splits_dotted_words():
    chunker = cascade.SentenceChunker()
    assert chunker.feed("Visit example.com today. ") == ["Visit example.com today."]


def test_chunker_ellipsis_is_one_terminal_unit():
    chunker = cascade.SentenceChunker()
    # A naive splitter would emit "Wait." or cut between the dots.
    pieces = chunker.feed("Wait... really? ")
    assert pieces == ["Wait...", "really?"]


def test_chunker_long_sentence_splits_at_clause_break_past_budget():
    chunker = cascade.SentenceChunker(budget=20)
    # First comma sits INSIDE the budget and must not split early.
    assert chunker.feed("one two three four, five six seven") == []
    # The second comma is past the budget: split there, never mid-word.
    assert chunker.feed(", eight nine") == ["one two three four, five six seven,"]
    assert chunker.flush() == ["eight nine"]


def test_chunker_long_sentence_without_clause_break_waits_for_terminal():
    chunker = cascade.SentenceChunker(budget=20)
    long_clause = "word " * 30  # 150 chars, no punctuation at all
    assert chunker.feed(long_clause) == []
    assert chunker.feed("done. ") == [f"{long_clause}done."]


def test_chunker_reset_drops_partial_sentence():
    chunker = cascade.SentenceChunker()
    assert chunker.feed("an unfinished thought") == []
    chunker.reset()
    assert chunker.flush() == []


def test_chunker_terminal_at_buffer_edge_waits_for_next_delta():
    chunker = cascade.SentenceChunker()
    # "3." at the edge could be the start of "3.5" — hold it.
    assert chunker.feed("The answer is 3.") == []
    assert chunker.feed("5 exactly. ") == ["The answer is 3.5 exactly."]


# ---------------------------------------------------------------------------
# Fake ElevenLabs stream-input socket
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, data: str) -> None:
        self.type = aiohttp.WSMsgType.TEXT
        self.data = data


class _FakeWs:
    """A scripted stream-input socket: records sends, yields queued frames."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True

    # -- test scripting -------------------------------------------------------

    def feed(self, frame: dict) -> None:
        self.incoming.put_nowait(_FakeMessage(json.dumps(frame)))

    def feed_raw(self, raw: str) -> None:
        self.incoming.put_nowait(_FakeMessage(raw))

    def feed_audio(self, pcm: bytes) -> None:
        self.feed({"audio": base64.b64encode(pcm).decode("ascii")})

    def feed_final(self) -> None:
        self.feed({"isFinal": True})


class _FakeConnect:
    """The ``ws_connect`` seam: hands out scripted sockets, records dials."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.sockets: list[_FakeWs] = []

    async def __call__(self, *, url: str, headers: dict) -> _FakeWs:
        ws = _FakeWs()
        self.calls.append({"url": url, "headers": headers})
        self.sockets.append(ws)
        return ws


class _Harness:
    def __init__(self, **overrides) -> None:
        self.audio: list[bytes] = []
        self.errors: list[str] = []
        self.connect = _FakeConnect()
        self.voice = cascade.CascadeVoice(
            api_key=FAKE_KEY,
            voice_id=FAKE_VOICE_ID,
            model=talk_config.DEFAULT_ELEVENLABS_MODEL,
            on_audio=self.audio.append,
            on_error=self.errors.append,
            aiohttp_module=aiohttp,
            ws_connect=self.connect,
            **overrides,
        )
        self.voice.start()


async def _wait_for(condition, timeout: float = 2.0) -> None:
    """Poll the loop until ``condition`` holds; fail the test if it never does."""

    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0)


def _delta(text: str, response_id: str = "resp_1") -> rt.Transcript:
    return rt.Transcript(
        role=rt.TranscriptRole.ASSISTANT,
        text=text,
        final=False,
        provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
        response_id=response_id,
    )


def _final(text: str = "", response_id: str = "resp_1") -> rt.Transcript:
    return rt.Transcript(
        role=rt.TranscriptRole.ASSISTANT,
        text=text,
        final=True,
        provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
        response_id=response_id,
    )


def test_stream_lifecycle_bos_chunks_eos_isfinal():
    asyncio.run(_stream_lifecycle_bos_chunks_eos_isfinal())


async def _stream_lifecycle_bos_chunks_eos_isfinal():
    harness = _Harness()
    voice = harness.voice
    try:
        voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
        voice.handle_event(_delta("Hello there. "))
        await _wait_for(lambda: len(harness.connect.sockets) == 1)
        ws = harness.connect.sockets[0]
        await _wait_for(lambda: len(ws.sent) == 2)
        # BOS opens the stream with voice settings; the chunk is text ONLY —
        # no per-chunk generation trigger, so the buffer keeps its prosodic
        # context across the sentence boundary.
        assert ws.sent[0]["text"] == " "
        assert "voice_settings" in ws.sent[0]
        assert ws.sent[1] == {"text": "Hello there."}
        # The key rides the header; identifiers ride the URL; never the reverse.
        assert harness.connect.calls[0]["headers"] == {"xi-api-key": FAKE_KEY}
        assert FAKE_VOICE_ID in harness.connect.calls[0]["url"]
        assert "eleven_flash_v2_5" in harness.connect.calls[0]["url"]
        assert FAKE_KEY not in harness.connect.calls[0]["url"]

        voice.handle_event(_final(response_id="resp_1"))
        await _wait_for(lambda: len(ws.sent) == 3)
        # EOS ends the response's text AND carries the turn's one generation
        # trigger — the documented end-of-turn flush.
        assert ws.sent[2] == {"text": ""}

        ws.feed_audio(b"pcm-one")
        ws.feed_audio(b"pcm-two")
        ws.feed_final()
        await _wait_for(lambda: ws.closed)
        assert harness.audio == [b"pcm-one", b"pcm-two"]
        assert harness.errors == []
    finally:
        await voice.aclose()



def test_multi_sentence_response_pipelines_through_one_stream():
    asyncio.run(_multi_sentence_response_pipelines_through_one_stream())


async def _multi_sentence_response_pipelines_through_one_stream():
    harness = _Harness()
    voice = harness.voice
    try:
        voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
        voice.handle_event(_delta("First sentence. "))
        await _wait_for(lambda: len(harness.connect.sockets) == 1)
        ws = harness.connect.sockets[0]
        voice.handle_event(_delta("Second one. "))
        voice.handle_event(_final(response_id="resp_1"))
        await _wait_for(lambda: len(ws.sent) == 4)
        assert [m.get("text") for m in ws.sent] == [
            " ",
            "First sentence.",
            "Second one.",
            "",
        ]
        ws.feed_audio(b"audio-1")
        ws.feed_audio(b"audio-2")
        ws.feed_final()
        await _wait_for(lambda: ws.closed)
        assert harness.audio == [b"audio-1", b"audio-2"]
    finally:
        await voice.aclose()



def test_barge_in_aborts_stream_and_emits_zero_audio_after():
    asyncio.run(_barge_in_aborts_stream_and_emits_zero_audio_after())


async def _barge_in_aborts_stream_and_emits_zero_audio_after():
    harness = _Harness()
    voice = harness.voice
    try:
        voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
        voice.handle_event(_delta("A long answer begins. "))
        await _wait_for(lambda: len(harness.connect.sockets) == 1)
        ws = harness.connect.sockets[0]
        ws.feed_audio(b"heard-before-barge")
        await _wait_for(lambda: harness.audio == [b"heard-before-barge"])

        # The operator starts talking: the in-flight stream dies and the
        # response's remaining text is fenced off.
        voice.handle_event(rt.SpeechStarted(input_id="in_1", offset_ms=10))
        await _wait_for(lambda: ws.closed)
        audio_at_barge = list(harness.audio)
        ws.feed_audio(b"never-spoken")
        voice.handle_event(_delta("tail of the cancelled answer. "))
        voice.handle_event(_final(response_id="resp_1"))
        voice.handle_event(rt.ResponseFinished(response_id="resp_1"))
        await asyncio.sleep(0.05)
        assert harness.audio == audio_at_barge
        assert b"never-spoken" not in harness.audio

        # The NEXT response gets a fresh stream and speaks normally.
        voice.handle_event(rt.ResponseStarted(response_id="resp_2"))
        voice.handle_event(_delta("New answer. ", response_id="resp_2"))
        await _wait_for(lambda: len(harness.connect.sockets) == 2)
        ws2 = harness.connect.sockets[1]
        voice.handle_event(_final(response_id="resp_2"))
        await _wait_for(lambda: len(ws2.sent) == 3)
        ws2.feed_audio(b"fresh-audio")
        ws2.feed_final()
        await _wait_for(lambda: ws2.closed)
        assert harness.audio == [*audio_at_barge, b"fresh-audio"]
        assert harness.errors == []
    finally:
        await voice.aclose()



def test_tts_error_degrades_one_response_to_text_only():
    asyncio.run(_tts_error_degrades_one_response_to_text_only())


async def _tts_error_degrades_one_response_to_text_only():
    harness = _Harness()
    voice = harness.voice
    try:
        voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
        voice.handle_event(_delta("This answer loses its voice. "))
        await _wait_for(lambda: len(harness.connect.sockets) == 1)
        ws = harness.connect.sockets[0]
        ws.feed({"error": "upstream said no"})
        await _wait_for(lambda: ws.closed)
        # One logged receipt per response; the transcript lane is untouched.
        await _wait_for(lambda: len(harness.errors) == 1)
        assert "text-only" in harness.errors[0]
        assert harness.audio == []

        # The session survives: the next response speaks again.
        voice.handle_event(rt.ResponseStarted(response_id="resp_2"))
        voice.handle_event(_delta("Recovered. ", response_id="resp_2"))
        await _wait_for(lambda: len(harness.connect.sockets) == 2)
        ws2 = harness.connect.sockets[1]
        voice.handle_event(_final(response_id="resp_2"))
        await _wait_for(lambda: len(ws2.sent) == 3)
        ws2.feed_audio(b"recovered-audio")
        ws2.feed_final()
        await _wait_for(lambda: ws2.closed)
        assert harness.audio == [b"recovered-audio"]
        assert len(harness.errors) == 1  # no second receipt for a clean response
    finally:
        await voice.aclose()



def test_malformed_audio_frame_is_a_tts_error():
    asyncio.run(_malformed_audio_frame_is_a_tts_error())


async def _malformed_audio_frame_is_a_tts_error():
    harness = _Harness()
    voice = harness.voice
    try:
        voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
        voice.handle_event(_delta("Frame trouble. "))
        await _wait_for(lambda: len(harness.connect.sockets) == 1)
        ws = harness.connect.sockets[0]
        ws.feed({"audio": "!!!not-base64!!!"})
        await _wait_for(lambda: len(harness.errors) == 1)
        assert harness.audio == []
    finally:
        await voice.aclose()



def test_connect_failure_degrades_and_recovers():
    asyncio.run(_connect_failure_degrades_and_recovers())


async def _connect_failure_degrades_and_recovers():
    harness = _Harness()
    calls = 0

    async def flaky_connect(*, url, headers):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise cascade.CascadeTTSError("dial failed")
        return await harness.connect(url=url, headers=headers)

    voice = cascade.CascadeVoice(
        api_key=FAKE_KEY,
        voice_id=FAKE_VOICE_ID,
        model=talk_config.DEFAULT_ELEVENLABS_MODEL,
        on_audio=harness.audio.append,
        on_error=harness.errors.append,
        aiohttp_module=aiohttp,
        ws_connect=flaky_connect,
    )
    voice.start()
    try:
        voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
        voice.handle_event(_delta("No dial tone. "))
        voice.handle_event(_final(response_id="resp_1"))
        await _wait_for(lambda: len(harness.errors) == 1)
        voice.handle_event(rt.ResponseStarted(response_id="resp_2"))
        voice.handle_event(_delta("Second try. ", response_id="resp_2"))
        await _wait_for(lambda: len(harness.connect.sockets) == 1)
        ws = harness.connect.sockets[0]
        voice.handle_event(_final(response_id="resp_2"))
        await _wait_for(lambda: len(ws.sent) == 3)
        ws.feed_audio(b"ok")
        ws.feed_final()
        await _wait_for(lambda: ws.closed)
        assert harness.audio == [b"ok"]
    finally:
        await voice.aclose()



def test_unflushed_tail_speaks_at_response_finished():
    asyncio.run(_unflushed_tail_speaks_at_response_finished())


async def _unflushed_tail_speaks_at_response_finished():
    harness = _Harness()
    voice = harness.voice
    try:
        voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
        # No terminal punctuation, no final transcript — response.done ends it.
        voice.handle_event(_delta("a tail without punctuation"))
        voice.handle_event(rt.ResponseFinished(response_id="resp_1"))
        await _wait_for(lambda: len(harness.connect.sockets) == 1)
        ws = harness.connect.sockets[0]
        await _wait_for(lambda: len(ws.sent) == 3)
        assert ws.sent[1]["text"] == "a tail without punctuation"
        assert ws.sent[2] == {"text": ""}
    finally:
        await voice.aclose()



def test_stream_input_url_carries_identifiers_only():
    url = cascade.stream_input_url("voice abc", "model/1")
    assert "voice%20abc" in url
    assert "model_id=model%2F1" in url
    assert "output_format=pcm_24000" in url


def test_stream_input_url_states_ssml_parsing_explicitly():
    """The one query parameter ElevenLabs documents no default for.

    Left off, a ``<break time="0.4s" />`` is dropped rather than spoken, and
    nothing in the response says so — which is why the flag is sent either
    way instead of relying on an unstated default.
    """

    assert "enable_ssml_parsing=true" in cascade.stream_input_url("v", "m")
    assert "enable_ssml_parsing=false" in cascade.stream_input_url(
        "v", "m", enable_ssml=False
    )


# ---------------------------------------------------------------------------
# Prosody: one generation per TURN, never one per chunk
# ---------------------------------------------------------------------------


def test_no_chunk_ever_forces_its_own_generation():
    asyncio.run(_no_chunk_ever_forces_its_own_generation())


async def _no_chunk_ever_forces_its_own_generation():
    """Every text frame is text ONLY; the turn ends with exactly one flush.

    ``try_trigger_generation`` per chunk is what flattened prosody: it
    defeats the buffer whose entire purpose is giving the model enough
    context to carry intonation across a sentence boundary. An ellipsis-
    heavy line is the worst case — the chunker ends a chunk on each pause
    marker, so the old path produced a forced generation per marker.
    """

    harness = _Harness()
    voice = harness.voice
    try:
        voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
        voice.handle_event(_delta("First... second... third. "))
        await _wait_for(lambda: len(harness.connect.sockets) == 1)
        ws = harness.connect.sockets[0]
        voice.handle_event(_delta("And a fourth sentence. "))
        voice.handle_event(_final(response_id="resp_1"))
        await _wait_for(lambda: ws.sent and ws.sent[-1] == {"text": ""})

        assert not any("try_trigger_generation" in frame for frame in ws.sent)
        # No frame carries a `flush` key at all: the close IS the flush.
        assert not any("flush" in frame for frame in ws.sent)
        # The EOS is the LAST frame and appears exactly once.
        assert [i for i, f in enumerate(ws.sent) if f == {"text": ""}] == [
            len(ws.sent) - 1
        ]
        # Everything between BOS and EOS is a bare text frame.
        assert all(set(frame) == {"text"} for frame in ws.sent[1:-1])
    finally:
        await voice.aclose()


def test_voice_settings_are_a_caller_argument_with_unchanged_defaults():
    asyncio.run(_voice_settings_are_a_caller_argument())


async def _voice_settings_are_a_caller_argument():
    """The caller owns delivery; omitting it reproduces the old BOS exactly."""

    default = _Harness()
    try:
        default.voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
        default.voice.handle_event(_delta("Hello. "))
        await _wait_for(lambda: len(default.connect.sockets) == 1)
        ws = default.connect.sockets[0]
        await _wait_for(lambda: len(ws.sent) >= 1)
        assert ws.sent[0]["voice_settings"] == {
            "stability": 0.5,
            "similarity_boost": 0.75,
        }
    finally:
        await default.voice.aclose()

    custom = _Harness(voice_settings={"stability": 0.4, "speed": 1.1})
    try:
        custom.voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
        custom.voice.handle_event(_delta("Hello. "))
        await _wait_for(lambda: len(custom.connect.sockets) == 1)
        ws = custom.connect.sockets[0]
        await _wait_for(lambda: len(ws.sent) >= 1)
        assert ws.sent[0]["voice_settings"] == {"stability": 0.4, "speed": 1.1}
    finally:
        await custom.voice.aclose()


# ---------------------------------------------------------------------------
# TALK_CASCADE_SPEED
# ---------------------------------------------------------------------------


def test_speed_unset_sends_no_speed_field(monkeypatch):
    """Unset must be byte-identical on the wire, not an explicit 1.0."""

    _scrub_elevenlabs_env(monkeypatch)
    assert talk_config.elevenlabs_speed() is None
    assert talk_config.elevenlabs_voice_settings() == {
        "stability": 0.5,
        "similarity_boost": 0.75,
    }
    assert "speed" not in talk_config.elevenlabs_voice_settings()


@pytest.mark.parametrize("raw,expected", [("0.7", 0.7), ("1", 1.0), ("1.2", 1.2)])
def test_speed_in_range_is_threaded_into_voice_settings(monkeypatch, raw, expected):
    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("TALK_CASCADE_SPEED", raw)
    assert talk_config.elevenlabs_speed() == pytest.approx(expected)
    assert talk_config.elevenlabs_voice_settings()["speed"] == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["0.69", "1.21", "0", "-1", "4.0"])
def test_speed_out_of_range_refuses_rather_than_clamping(monkeypatch, raw):
    """A clamped pace would speak wrong on every word and never say why."""

    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("TALK_CASCADE_SPEED", raw)
    with pytest.raises(talk_config.TalkConfigError) as excinfo:
        talk_config.elevenlabs_speed()
    message = str(excinfo.value)
    assert "TALK_CASCADE_SPEED" in message
    assert "0.7" in message and "1.2" in message


@pytest.mark.parametrize("raw", ["fast", "1.0x", ""])
def test_speed_junk_refuses_except_blank_which_means_unset(monkeypatch, raw):
    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("TALK_CASCADE_SPEED", raw)
    if not raw.strip():
        assert talk_config.elevenlabs_speed() is None
        return
    with pytest.raises(talk_config.TalkConfigError, match="TALK_CASCADE_SPEED"):
        talk_config.elevenlabs_speed()


def test_speed_resolves_at_call_time_not_import_time(monkeypatch):
    """Rule 1: an operator's live edit takes effect on the next call."""

    _scrub_elevenlabs_env(monkeypatch)
    assert talk_config.elevenlabs_voice_settings().get("speed") is None
    monkeypatch.setenv("TALK_CASCADE_SPEED", "0.9")
    assert talk_config.elevenlabs_voice_settings()["speed"] == pytest.approx(0.9)
    monkeypatch.delenv("TALK_CASCADE_SPEED")
    assert "speed" not in talk_config.elevenlabs_voice_settings()


def test_the_two_default_voice_settings_cannot_drift_apart():
    """Two declarations of the same defaults, and nothing else keeps them equal.

    ``talk_cascade_voice`` deliberately does NOT import ``talk_config`` — it
    is a TTS transport, and config resolution belongs to the config module —
    so its fallback defaults are declared locally and the config module
    declares them again for the builder. That boundary is worth keeping, but
    it means a value changed in one place would silently disagree with the
    other: the CLI and dashboard would speak with the resolved settings
    while a caller that omitted them got the stale ones. This is the only
    thing enforcing that they agree.
    """

    assert cascade._VOICE_SETTINGS == talk_config.DEFAULT_ELEVENLABS_VOICE_SETTINGS


def test_voice_settings_builder_hands_back_a_fresh_dict(monkeypatch):
    """A caller mutating its copy must not poison the next session."""

    _scrub_elevenlabs_env(monkeypatch)
    first = talk_config.elevenlabs_voice_settings()
    first["stability"] = 0.99
    assert talk_config.elevenlabs_voice_settings()["stability"] == 0.5
    assert talk_config.DEFAULT_ELEVENLABS_VOICE_SETTINGS["stability"] == 0.5


# ---------------------------------------------------------------------------
# The stream-end hook (the dashboard relay lane's end-of-audio signal)
# ---------------------------------------------------------------------------


def test_stream_end_hook_fires_on_success_and_failure_but_not_barge_in():
    asyncio.run(_stream_end_hook_semantics())


async def _stream_end_hook_semantics():
    ended: list[str] = []
    harness = _Harness()
    voice = cascade.CascadeVoice(
        api_key=FAKE_KEY,
        voice_id=FAKE_VOICE_ID,
        model=talk_config.DEFAULT_ELEVENLABS_MODEL,
        on_audio=harness.audio.append,
        on_error=harness.errors.append,
        on_stream_end=lambda: ended.append("end"),
        aiohttp_module=aiohttp,
        ws_connect=harness.connect,
    )
    voice.start()
    try:
        # Success: the hook fires once the terminal frame settles the run.
        voice.handle_event(rt.ResponseStarted(response_id="resp_1"))
        voice.handle_event(_delta("Clean answer. "))
        voice.handle_event(_final(response_id="resp_1"))
        await _wait_for(lambda: len(harness.connect.sockets) == 1)
        ws = harness.connect.sockets[0]
        ws.feed_audio(b"pcm")
        ws.feed_final()
        await _wait_for(lambda: ended == ["end"])
        assert harness.audio == [b"pcm"]

        # Failure: the error receipt fires first, then the hook — a relay
        # draining a response stream must hear both, in that order.
        voice.handle_event(rt.ResponseStarted(response_id="resp_2"))
        voice.handle_event(_delta("Broken answer. ", response_id="resp_2"))
        await _wait_for(lambda: len(harness.connect.sockets) == 2)
        harness.connect.sockets[1].feed({"error": "upstream said no"})
        await _wait_for(lambda: len(harness.errors) == 1)
        await _wait_for(lambda: ended == ["end", "end"])

        # Barge-in: a CANCELLED run fires nothing — whoever aborted knows.
        voice.handle_event(rt.ResponseStarted(response_id="resp_3"))
        voice.handle_event(_delta("Interrupted answer. ", response_id="resp_3"))
        await _wait_for(lambda: len(harness.connect.sockets) == 3)
        voice.handle_event(rt.SpeechStarted(input_id="in_1", offset_ms=0))
        await _wait_for(lambda: harness.connect.sockets[2].closed)
        await asyncio.sleep(0.05)
        assert ended == ["end", "end"]
    finally:
        await voice.aclose()


def test_stream_end_hook_consumer_failure_does_not_kill_the_worker():
    asyncio.run(_stream_end_hook_consumer_failure())


async def _stream_end_hook_consumer_failure():
    harness = _Harness()

    def broken_hook() -> None:
        raise RuntimeError("consumer bug")

    voice = cascade.CascadeVoice(
        api_key=FAKE_KEY,
        voice_id=FAKE_VOICE_ID,
        model=talk_config.DEFAULT_ELEVENLABS_MODEL,
        on_audio=harness.audio.append,
        on_error=harness.errors.append,
        on_stream_end=broken_hook,
        aiohttp_module=aiohttp,
        ws_connect=harness.connect,
    )
    voice.start()
    try:
        for index in (1, 2):
            response_id = f"resp_{index}"
            voice.handle_event(rt.ResponseStarted(response_id=response_id))
            voice.handle_event(_delta("Still speaking. ", response_id=response_id))
            voice.handle_event(_final(response_id=response_id))
            await _wait_for(lambda index=index: len(harness.connect.sockets) == index)
            ws = harness.connect.sockets[index - 1]
            ws.feed_audio(f"pcm-{index}".encode())
            ws.feed_final()
            await _wait_for(lambda ws=ws: ws.closed)
        # The hook raised on BOTH runs; the worker survived to speak again.
        assert harness.audio == [b"pcm-1", b"pcm-2"]
        assert harness.errors == []
    finally:
        await voice.aclose()


# ---------------------------------------------------------------------------
# Fail-closed config lanes
# ---------------------------------------------------------------------------


def test_voice_mode_defaults_to_native(monkeypatch):
    monkeypatch.delenv("TALK_VOICE_MODE", raising=False)
    assert talk_config.voice_mode() == "native"


def test_voice_mode_cascade_accepted(monkeypatch):
    monkeypatch.setenv("TALK_VOICE_MODE", " Cascade ")
    assert talk_config.voice_mode() == "cascade"


def test_voice_mode_unknown_refuses(monkeypatch):
    monkeypatch.setenv("TALK_VOICE_MODE", "telepathy")
    with pytest.raises(talk_config.TalkConfigError, match="TALK_VOICE_MODE"):
        talk_config.voice_mode()


def test_cascade_tts_defaults_and_refuses_unknown(monkeypatch):
    monkeypatch.delenv("TALK_CASCADE_TTS", raising=False)
    assert talk_config.cascade_tts() == "elevenlabs"
    monkeypatch.setenv("TALK_CASCADE_TTS", "cartesia")
    with pytest.raises(talk_config.TalkConfigError, match="TALK_CASCADE_TTS"):
        talk_config.cascade_tts()


def test_elevenlabs_key_scoped_wins(monkeypatch):
    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("TALK_ELEVENLABS_API_KEY", "fake-scoped")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-shared")
    assert talk_config.resolve_elevenlabs_key() == "fake-scoped"


def test_elevenlabs_key_shared_fallback(monkeypatch):
    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("ELEVENLABS_API_KEY", " fake-shared ")
    assert talk_config.resolve_elevenlabs_key() == "fake-shared"


def test_elevenlabs_key_blank_is_a_refusal(monkeypatch):
    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("TALK_ELEVENLABS_API_KEY", "   ")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-shared")
    with pytest.raises(talk_config.TalkConfigError, match="TALK_ELEVENLABS_API_KEY"):
        talk_config.resolve_elevenlabs_key()
    monkeypatch.delenv("TALK_ELEVENLABS_API_KEY")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    with pytest.raises(talk_config.TalkConfigError, match="ELEVENLABS_API_KEY"):
        talk_config.resolve_elevenlabs_key()


def test_elevenlabs_key_missing_raises(monkeypatch):
    _scrub_elevenlabs_env(monkeypatch)
    with pytest.raises(talk_config.TalkConfigError, match="no ElevenLabs key"):
        talk_config.resolve_elevenlabs_key()


def test_elevenlabs_voice_id_required_with_remediation(monkeypatch):
    _scrub_elevenlabs_env(monkeypatch)
    with pytest.raises(talk_config.TalkConfigError, match="TALK_ELEVENLABS_VOICE_ID"):
        talk_config.elevenlabs_voice_id()
    monkeypatch.setenv("TALK_ELEVENLABS_VOICE_ID", "   ")
    with pytest.raises(talk_config.TalkConfigError, match="TALK_ELEVENLABS_VOICE_ID"):
        talk_config.elevenlabs_voice_id()
    monkeypatch.setenv("TALK_ELEVENLABS_VOICE_ID", f" {FAKE_VOICE_ID} ")
    assert talk_config.elevenlabs_voice_id() == FAKE_VOICE_ID


def test_elevenlabs_model_default_and_override(monkeypatch):
    monkeypatch.delenv("TALK_ELEVENLABS_MODEL", raising=False)
    assert talk_config.elevenlabs_model() == "eleven_flash_v2_5"
    monkeypatch.setenv("TALK_ELEVENLABS_MODEL", "eleven_multilingual_v2")
    assert talk_config.elevenlabs_model() == "eleven_multilingual_v2"


# ---------------------------------------------------------------------------
# OpenAI text-output mode (the cascade's provider leg)
# ---------------------------------------------------------------------------


def test_native_payload_is_byte_identical_without_text_output():
    payload = talk_wire.build_session_payload(
        model="m", voice="cedar", instructions="hi", tools=None
    )
    assert "output_modalities" not in payload
    assert payload["audio"]["output"] == {"voice": "cedar"}


def test_text_output_payload_drops_voice_and_requests_text():
    payload = talk_wire.build_session_payload(
        model="m", voice="cedar", instructions="hi", tools=None, text_output=True
    )
    assert payload["output_modalities"] == ["text"]
    assert "output" not in payload["audio"]  # no provider voice to configure
    # Listening is untouched: input audio, VAD, and transcription all stay.
    assert payload["audio"]["input"]["turn_detection"]["type"] == "server_vad"
    assert "transcription" in payload["audio"]["input"]


def test_session_update_carries_text_output_from_setup():
    setup = rt.SessionSetup(
        model="m", voice="cedar", instructions="hi", text_output=True
    )
    message = talk_openai_realtime.build_session_update(setup)
    assert message["session"]["output_modalities"] == ["text"]
    assert "output" not in message["session"]["audio"]

    native = talk_openai_realtime.build_session_update(
        rt.SessionSetup(model="m", voice="cedar", instructions="hi")
    )
    assert "output_modalities" not in native["session"]
    assert native["session"]["audio"]["output"] == {"voice": "cedar"}


def test_decode_output_text_events_into_assistant_transcripts():
    delta = talk_openai_realtime.decode_event(
        {"type": "response.output_text.delta", "delta": "Hello ", "response_id": "r1"}
    )
    assert isinstance(delta, rt.Transcript)
    assert delta.role is rt.TranscriptRole.ASSISTANT
    assert delta.final is False
    assert delta.text == "Hello "
    assert delta.response_id == "r1"

    done = talk_openai_realtime.decode_event(
        {"type": "response.output_text.done", "text": "Hello there.", "response_id": "r1"}
    )
    assert isinstance(done, rt.Transcript)
    assert done.final is True
    assert done.text == "Hello there."

    empty = talk_openai_realtime.decode_event({"type": "response.output_text.delta"})
    assert empty is None


# ---------------------------------------------------------------------------
# Doctor's cascade lane
# ---------------------------------------------------------------------------


def _doctor_cascade(report: dict) -> dict:
    return {check["id"]: check for check in report["checks"]}["cascade"]


def test_doctor_cascade_native_is_inactive_pass(monkeypatch):
    _scrub_elevenlabs_env(monkeypatch)
    check = _doctor_cascade(talk_doctor.collect_report())
    assert check["status"] == "pass"
    assert check["details"]["voice_mode"] == "native"


def test_doctor_cascade_rejects_unknown_voice_mode(monkeypatch):
    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("TALK_VOICE_MODE", "telepathy")
    check = _doctor_cascade(talk_doctor.collect_report())
    assert check["status"] == "fail"
    assert check["remediation"]


def test_doctor_cascade_missing_key_and_voice_id_fail(monkeypatch):
    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("TALK_VOICE_MODE", "cascade")
    check = _doctor_cascade(talk_doctor.collect_report())
    assert check["status"] == "fail"
    assert check["details"]["keys"] == {"scoped": "absent", "shared": "absent"}
    assert check["details"]["voice_id"] is None


def test_doctor_cascade_reports_presence_never_the_key(monkeypatch):
    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("TALK_VOICE_MODE", "cascade")
    monkeypatch.setenv("TALK_ELEVENLABS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TALK_ELEVENLABS_VOICE_ID", FAKE_VOICE_ID)
    check = _doctor_cascade(talk_doctor.collect_report())
    assert check["status"] == "pass"
    assert check["details"]["keys"]["scoped"] == "present"
    # The voice id is a semi-public identifier; the KEY never appears.
    assert check["details"]["voice_id"] == FAKE_VOICE_ID
    assert FAKE_KEY not in json.dumps(check)


def test_doctor_cascade_blank_key_refuses_closed(monkeypatch):
    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("TALK_VOICE_MODE", "cascade")
    monkeypatch.setenv("TALK_ELEVENLABS_API_KEY", "  ")
    check = _doctor_cascade(talk_doctor.collect_report())
    assert check["status"] == "fail"
    assert "blank" in check["summary"]


def test_doctor_cascade_gates_to_openai(monkeypatch):
    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("TALK_VOICE_MODE", "cascade")
    monkeypatch.setenv("TALK_ELEVENLABS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TALK_ELEVENLABS_VOICE_ID", FAKE_VOICE_ID)
    monkeypatch.setenv("TALK_PROVIDER", "grok")
    monkeypatch.setenv("XAI_API_KEY", "fake-xai-key")
    try:
        check = _doctor_cascade(talk_doctor.collect_report())
    finally:
        monkeypatch.delenv("TALK_PROVIDER", raising=False)
    assert check["status"] == "fail"
    assert "grok" in check["summary"]


# ---------------------------------------------------------------------------
# CLI startup gates (fail-closed before any socket or device opens)
# ---------------------------------------------------------------------------


def _no_audio_start(monkeypatch):
    import talk_audio

    def never(_self):  # pragma: no cover - must not be reached
        raise AssertionError("audio opened before cascade config was resolved")

    monkeypatch.setattr(talk_audio.DuplexAudio, "start", never)


def test_cli_cascade_refuses_non_openai_provider(monkeypatch, tmp_path, capsys):
    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("TALK_PROVIDER", "grok")
    monkeypatch.setenv("XAI_API_KEY", "fake-xai-key")
    monkeypatch.setenv("TALK_VOICE_MODE", "cascade")
    monkeypatch.setenv("TALK_ELEVENLABS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TALK_ELEVENLABS_VOICE_ID", FAKE_VOICE_ID)
    _no_audio_start(monkeypatch)

    assert asyncio.run(talk_cli.run_talk_session()) == 1
    err = capsys.readouterr().err
    assert "grok" in err
    assert "openai" in err


def test_cli_cascade_requires_voice_id(monkeypatch, tmp_path, capsys):
    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("TALK_VOICE_MODE", "cascade")
    monkeypatch.setenv("TALK_ELEVENLABS_API_KEY", FAKE_KEY)
    _no_audio_start(monkeypatch)

    assert asyncio.run(talk_cli.run_talk_session()) == 1
    assert "TALK_ELEVENLABS_VOICE_ID" in capsys.readouterr().err


def test_cli_native_mode_never_touches_cascade_config(monkeypatch, tmp_path, capsys):
    """Native mode must not even LOOK at cascade knobs: no voice id, no key,
    and a deliberately invalid TALK_CASCADE_TTS all stay inert — the refusal
    is that nothing refuses. (The session itself then fails on the missing
    audio stack, which is the native path behaving exactly as before.)"""

    _scrub_elevenlabs_env(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("TALK_CASCADE_TTS", "not-a-real-tts")

    import talk_audio

    def fail(_self):
        raise talk_audio.TalkAudioError('run: pip install "hermes-talk[audio]"')

    monkeypatch.setattr(talk_audio.DuplexAudio, "start", fail)

    assert asyncio.run(talk_cli.run_talk_session()) == 1
    err = capsys.readouterr().err
    assert "hermes-talk[audio]" in err  # the NATIVE failure, not a cascade one
    assert "cascade" not in err.lower()
