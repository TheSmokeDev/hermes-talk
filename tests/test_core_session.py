"""Admission rules for pumping Discord capture into a canonical core session."""

from dataclasses import FrozenInstanceError

import pytest

from talk_core_session import OperatorPacketAdmission
from talk_discord import InputAudioPacket


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
