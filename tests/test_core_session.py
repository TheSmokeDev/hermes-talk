"""Admission rules for pumping Discord capture into a canonical core session."""

import asyncio
import time
import types
from dataclasses import FrozenInstanceError

import pytest

import talk_core_session
from talk_core_session import OperatorPacketAdmission
from talk_discord import InputAudioPacket


def _requires_core_api():
    if not talk_core_session.talk_core_realtime.core_provider_available():
        pytest.skip("Hermes realtime voice API-v2 is not installed in this test environment")


def test_operator_pcm_is_admitted_only_for_the_exact_integer_user_id():
    admission = OperatorPacketAdmission(42)
    pcm = b"\x01\x00"

    assert admission.admit(InputAudioPacket(speaker={"user_id": 42}, pcm=pcm)) == pcm
    for lookalike in (None, 7, True, False, "42", 42.0):
        assert admission.admit(InputAudioPacket(speaker={"user_id": lookalike}, pcm=pcm)) is None


def test_synthesized_silence_is_admitted_to_close_the_operator_turn():
    admission = OperatorPacketAdmission(42)
    silence = bytes(960)

    assert admission.admit(InputAudioPacket(speaker=None, pcm=silence)) == silence


def test_unattributed_nonzero_pcm_is_not_treated_as_synthesized_silence():
    admission = OperatorPacketAdmission(42)

    assert admission.admit(InputAudioPacket(speaker=None, pcm=b"\x01\x00")) is None


@pytest.mark.parametrize("speaker", [{"user_id": 42}, None])
def test_odd_length_pcm_is_rejected_before_identity_or_silence_admission(speaker):
    admission = OperatorPacketAdmission(42)

    assert admission.admit(InputAudioPacket(speaker=speaker, pcm=b"\x00")) is None


def test_display_name_ssrc_and_provider_metadata_never_grant_authority():
    admission = OperatorPacketAdmission(42)
    packet = InputAudioPacket(
        speaker={
            "user_id": None,
            "display_name": "operator",
            "ssrc": 42,
            "provider": {"discord_user_id": 42},
        },
        pcm=b"\x01\x00",
    )

    assert admission.admit(packet) is None


@pytest.mark.parametrize(
    "packet",
    [
        None,
        object(),
        InputAudioPacket(speaker={}, pcm=b"\x01\x00"),
        InputAudioPacket(speaker="not metadata", pcm=b"\x01\x00"),
        InputAudioPacket(speaker={"user_id": 42}, pcm=b""),
        InputAudioPacket(speaker={"user_id": 42}, pcm=bytearray(b"\x01\x00")),
        InputAudioPacket(speaker=None, pcm="not pcm"),
    ],
)
def test_malformed_packets_and_pcm_fail_closed(packet):
    assert OperatorPacketAdmission(42).admit(packet) is None


@pytest.mark.parametrize("operator_user_id", [0, -1, True, False, "42", 42.0, None])
def test_operator_id_must_be_an_immutable_positive_integer(operator_user_id):
    with pytest.raises((TypeError, ValueError)):
        OperatorPacketAdmission(operator_user_id)


def test_operator_id_cannot_be_rebound():
    admission = OperatorPacketAdmission(42)

    with pytest.raises(FrozenInstanceError):
        admission.operator_user_id = 7

    admitted = admission.admit_record(InputAudioPacket(speaker={"user_id": 42}, pcm=b"\x01\x00"))
    with pytest.raises(FrozenInstanceError):
        admitted.speaker_user_id = 7


def test_core_setup_is_input_only_and_requests_only_commit_transcription():
    _requires_core_api()
    setup = talk_core_session.build_core_setup()

    assert setup.model == talk_core_session.talk_config.talk_model()
    assert setup.voice == talk_core_session.talk_config.talk_voice()
    assert setup.instructions == ""
    assert setup.tools == ()
    assert setup.audio == talk_core_session.talk_core_realtime.SUPPORTED_AUDIO_FORMAT
    assert setup.automatic_response is False
    assert setup.provider_options == {
        "capabilities": (
            "input_transcription",
            "input_commit_events",
        ),
    }
    assert "automatic_response" not in setup.provider_options


def test_core_runner_opens_exact_provider_and_routes_only_authorized_pcm():
    _requires_core_api()
    operator_pcm = b"\x01\x00"
    silence = b"\x00\x00"
    packets = iter(
        (
            InputAudioPacket(speaker={"user_id": 42}, pcm=operator_pcm),
            InputAudioPacket(speaker={"user_id": 7}, pcm=b"\x02\x00"),
            InputAudioPacket(speaker=None, pcm=silence),
        )
    )

    class Audio:
        started = False
        stopped = False

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

        def read_input_packet(self):
            try:
                return next(packets)
            except StopIteration as exc:
                raise RuntimeError("bridge lost") from exc

    class Attachment:
        operator_user_id = 42

        def __init__(self):
            self.operator_audio = []
            self.silence = []
            self.closed = 0

        def feed_audio(self, pcm, *, speaker_user_id, mime_type=None):
            self.operator_audio.append((pcm, speaker_user_id, mime_type))
            return "accepted"

        def feed_synthesized_silence(self, pcm):
            self.silence.append(pcm)
            return "accepted"

        async def close(self):
            self.closed += 1

    attachment = Attachment()

    class Factory:
        def __init__(self):
            self.opened = []

        async def open(self, provider_name, setup, **kwargs):
            self.opened.append((provider_name, setup, kwargs))
            return attachment

    factory = Factory()
    audio = Audio()

    with pytest.raises(RuntimeError, match="bridge lost"):
        asyncio.run(talk_core_session.run_core_session(factory, guild_id=9, audio=audio))

    assert audio.started and audio.stopped
    assert factory.opened[0][0] == talk_core_session.talk_core_realtime.PROVIDER_NAME
    assert factory.opened[0][2]["required_capabilities"] == (
        talk_core_session.talk_core_realtime.CORE_CAPABILITIES
    )
    assert isinstance(factory.opened[0][2]["provider_session_id"], str)
    assert attachment.operator_audio == [(operator_pcm, 42, "audio/pcm")]
    assert attachment.silence == [silence]
    assert attachment.closed == 1


def test_core_runner_snapshots_packet_attribution_once_before_forwarding(monkeypatch):
    operator_pcm = b"\x01\x00"

    class MutablePacket:
        pcm = operator_pcm

        def __init__(self):
            self.speaker_reads = 0

        @property
        def speaker(self):
            self.speaker_reads += 1
            if self.speaker_reads == 1:
                return {"user_id": 42}
            return {"user_id": 7}

    packet = MutablePacket()
    packets = iter((packet,))

    class Audio:
        def start(self):
            return None

        def stop(self):
            return None

        def read_input_packet(self):
            try:
                return next(packets)
            except StopIteration as exc:
                raise RuntimeError("bridge lost") from exc

    class Attachment:
        operator_user_id = 42

        def __init__(self):
            self.operator_audio = []
            self.silence = []

        def feed_audio(self, pcm, *, speaker_user_id, mime_type=None):
            self.operator_audio.append((pcm, speaker_user_id, mime_type))
            return "accepted"

        def feed_synthesized_silence(self, pcm):
            self.silence.append(pcm)
            return "accepted"

        async def close(self):
            return None

    attachment = Attachment()

    class Factory:
        async def open(self, *_args, **_kwargs):
            return attachment

    monkeypatch.setattr(talk_core_session, "build_core_setup", lambda: object())

    with pytest.raises(RuntimeError, match="bridge lost"):
        asyncio.run(talk_core_session.run_core_session(Factory(), guild_id=9, audio=Audio()))

    assert packet.speaker_reads == 1
    assert attachment.operator_audio == [(operator_pcm, 42, "audio/pcm")]
    assert attachment.silence == []


def test_core_runner_projects_listening_only_from_attachment_lifecycle(monkeypatch):
    statuses = []

    class Attachment:
        operator_user_id = 42

        def __init__(self):
            self.lifecycle_events = (
                types.SimpleNamespace(
                    lifecycle="connecting",
                    provider_event=types.SimpleNamespace(session_id="untrusted-provider-metadata"),
                ),
            )

        async def close(self):
            return None

    attachment = Attachment()

    class Factory:
        async def open(self, *_args, **_kwargs):
            return attachment

    class Audio:
        reads = 0

        def start(self):
            return None

        def stop(self):
            return None

        def read_input_packet(self):
            self.reads += 1
            if self.reads == 1:
                assert statuses == []
                attachment.lifecycle_events += (
                    types.SimpleNamespace(lifecycle="ready"),
                    types.SimpleNamespace(lifecycle="listening"),
                )
                return None
            raise RuntimeError("bridge lost")

    monkeypatch.setattr(talk_core_session, "build_core_setup", lambda: object())

    with pytest.raises(RuntimeError, match="bridge lost"):
        asyncio.run(
            talk_core_session.run_core_session(
                Factory(), guild_id=9, audio=Audio(), status_callback=statuses.append
            )
        )

    assert statuses == ["listening"]


def test_core_audio_read_does_not_block_the_gateway_event_loop(monkeypatch):
    class Attachment:
        operator_user_id = 42

        async def close(self):
            return None

    class Factory:
        async def open(self, *_args, **_kwargs):
            return Attachment()

    class BlockingAudio:
        def start(self):
            return None

        def stop(self):
            return None

        def read_input_packet(self):
            time.sleep(0.2)
            raise RuntimeError("bridge lost")

    monkeypatch.setattr(talk_core_session, "build_core_setup", lambda: object())

    async def scenario():
        started = time.perf_counter()
        task = asyncio.create_task(
            talk_core_session.run_core_session(Factory(), guild_id=9, audio=BlockingAudio())
        )
        await asyncio.sleep(0.01)
        elapsed = time.perf_counter() - started
        result = await asyncio.gather(task, return_exceptions=True)
        return elapsed, result

    elapsed, result = asyncio.run(scenario())
    assert elapsed < 0.1
    assert isinstance(result[0], RuntimeError)
    assert "bridge lost" in str(result[0])
