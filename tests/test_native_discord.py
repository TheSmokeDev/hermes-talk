"""Immutable native Discord packet/output admission and authenticated command fencing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import sys
from types import SimpleNamespace

import pytest

import talk_discord
from talk_native_api import NativeTaskError


@pytest.fixture
def room(monkeypatch):
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", "101")
    operator = SimpleNamespace(id=101, display_name="Operator", bot=False, roles=[])
    guest = SimpleNamespace(id=202, display_name="Guest", bot=False, roles=[])
    guild = SimpleNamespace(id=7)
    channel = SimpleNamespace(id=55, guild=guild, members=[operator, guest])
    voice = SimpleNamespace(channel=channel, is_connected=lambda: True)
    receiver = object()
    permitted = {"101", "202"}
    authorizations = []

    def explicit_policy(adapter, member):
        authorizations.append(member.id)
        users = {str(value) for value in adapter._allowed_user_ids if str(value).isdigit()}
        roles = {str(value) for value in adapter._allowed_role_ids if str(value).isdigit()}
        return str(member.id) in users or any(str(role.id) in roles for role in member.roles)

    monkeypatch.setitem(
        sys.modules,
        "gateway.discord_task_context",
        SimpleNamespace(_explicitly_allowed=explicit_policy),
    )
    adapter = SimpleNamespace(
        _voice_clients={7: voice},
        _voice_receivers={7: receiver},
        _allowed_user_ids=permitted,
        _allowed_role_ids=set(),
        _task_voice_epoch="fixture-epoch",
        _task_voice_revisions={(7, 55): 0},
        _is_allowed_user=lambda *args, **kwargs: True,
    )
    audio = talk_discord.DiscordAudio(7)
    audio._bridge = {"guild_id": 7, "voice_client": voice, "receiver": receiver, "adapter": adapter}
    audio._source = talk_discord._RealtimeSource(audio._outbound)
    proof = {
        "surface": "discord",
        "guild_id": 7,
        "channel_id": 55,
        "operator_user_id": 101,
        "audience_user_ids": [101, 202],
        "audience_revision": hashlib.sha256(
            json.dumps(["fixture-epoch", 0, ["101", "202"]], separators=(",", ":")).encode()
        ).hexdigest(),
    }
    return SimpleNamespace(
        audio=audio,
        proof=proof,
        operator=operator,
        guest=guest,
        channel=channel,
        voice=voice,
        adapter=adapter,
        permitted=permitted,
        authorizations=authorizations,
    )


def test_only_immutable_operator_pcm_and_transport_silence_are_admitted(room):
    guard = room.audio.bind_native_surface(room.proof)
    assert guard()
    pcm = b"\x01\x00" * 480
    allowed = talk_discord.InputAudioPacket(
        pcm=pcm, speaker={"user_id": 101, "display_name": "Renamed"}
    )
    forged = talk_discord.InputAudioPacket(
        pcm=pcm, speaker={"user_id": 202, "display_name": "Operator"}
    )
    unresolved = talk_discord.InputAudioPacket(pcm=pcm, speaker={"display_name": "Operator"})
    assert room.audio.admit_native_packet(allowed) == pcm
    assert room.audio.admit_native_packet(forged) is None
    assert room.audio.admit_native_packet(unresolved) is None
    assert room.audio.admit_native_packet(
        talk_discord.InputAudioPacket(pcm=bytes(960), speaker=None)
    ) == bytes(960)
    assert (
        room.audio.admit_native_packet(talk_discord.InputAudioPacket(pcm=pcm, speaker=None)) is None
    )


@pytest.mark.parametrize("mutation", ["member", "channel", "operator", "bridge", "disconnected"])
def test_audience_or_operator_change_drains_already_queued_audio(room, monkeypatch, mutation):
    room.audio.bind_native_surface(room.proof)
    room.audio.queue_playback(b"\x01\x00" * 4800)
    assert not room.audio._outbound.empty()
    if mutation == "member":
        room.channel.members.append(SimpleNamespace(id=303, bot=False))
    elif mutation == "channel":
        room.channel.id = 56
    elif mutation == "operator":
        monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", "202")
    elif mutation == "bridge":
        room.adapter._voice_clients[7] = object()
    else:
        room.voice.is_connected = lambda: False
    assert room.audio._source.read() == talk_discord.SILENCE_FRAME
    assert room.audio._outbound.empty()
    with pytest.raises(talk_discord.talk_audio.TalkAudioError, match="authority changed"):
        room.audio.queue_playback(b"\x01\x00")


@pytest.mark.parametrize(
    "field,value",
    [
        ("operator_user_id", "101"),
        ("audience_user_ids", [101]),
        ("audience_revision", ""),
        ("surface", "cli"),
        ("guild_id", True),
    ],
)
def test_missing_or_forged_server_room_proof_refuses(room, field, value):
    proof = {**room.proof, field: value}
    with pytest.raises(NativeTaskError):
        room.audio.bind_native_surface(proof)


def test_plain_legacy_source_remains_unchanged():
    frames = queue.Queue()
    frames.put(b"\x01" * talk_discord.DISCORD_FRAME_BYTES)
    source = talk_discord._RealtimeSource(frames)
    assert source.read() == b"\x01" * talk_discord.DISCORD_FRAME_BYTES


def test_native_command_returns_full_inert_result_only_to_exact_caller(room):
    async def scenario():
        pending = asyncio.create_task(asyncio.Event().wait())
        result = {"output": "Full artifact\n" * 1000, "artifacts": [{"path": "private.txt"}]}
        calls = []

        async def command(text):
            calls.append(text)
            return result

        controller = SimpleNamespace(
            attachment={"surface_context": room.proof},
            guard=lambda: None,
            command=command,
        )
        with talk_discord._SESSION_LOCK:
            talk_discord._SESSION.update(task=pending, generation=77, controller=controller)
        try:
            with pytest.raises(NativeTaskError, match="immutable"):
                await talk_discord.native_command(
                    "/result 1", operator_user_id=202, guild_id=7, channel_id=55
                )
            assert not calls
            actual = await talk_discord.native_command(
                "/result 1", operator_user_id=101, guild_id=7, channel_id=55
            )
            assert actual == result and calls == ["/result 1"]
        finally:
            with talk_discord._SESSION_LOCK:
                talk_discord._SESSION.clear()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())


def test_listener_access_revocation_with_unchanged_membership_stops_next_playback_frame(room):
    room.audio.bind_native_surface(room.proof)
    room.audio.queue_playback(b"\x01\x00" * 480)
    assert room.authorizations[-1] == 202
    room.permitted.add("*")
    room.permitted.remove("202")
    frame = room.audio._source.read()
    assert not any(frame) and room.audio._outbound.empty()
    assert room.channel.members == [room.operator, room.guest]


def test_listener_role_revocation_and_voice_revision_change_revoke_capture(room):
    room.permitted.remove("202")
    room.guest.roles = [SimpleNamespace(id=303)]
    room.adapter._allowed_role_ids.add("303")
    guard = room.audio.bind_native_surface(room.proof)
    room.guest.roles.clear()
    assert guard() is False
    with pytest.raises(talk_discord.talk_audio.TalkAudioError, match="room authority changed"):
        room.audio.admit_native_packet(None)
    room.guest.roles = [SimpleNamespace(id=303)]
    assert guard() is True
    room.adapter._task_voice_revisions[(7, 55)] += 1
    assert guard() is False


def test_missing_host_explicit_listener_policy_fails_closed(room, monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.discord_task_context", None)
    with pytest.raises(NativeTaskError, match="explicit Discord audience policy"):
        room.audio.bind_native_surface(room.proof)


@pytest.mark.parametrize(
    "mode,done,expected",
    [
        ("native-task", False, True),
        ("native-task", True, False),
        ("legacy", False, False),
    ],
)
def test_native_active_includes_startup_before_controller_binding(
    monkeypatch, mode, done, expected
):
    monkeypatch.setattr(
        talk_discord,
        "_SESSION",
        {
            "task": SimpleNamespace(done=lambda: done),
            "mode": mode,
            "controller": None,
        },
    )
    assert talk_discord.native_session_active() is expected
