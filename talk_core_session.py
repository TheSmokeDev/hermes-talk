"""Transport-neutral admission primitives for canonical Talk sessions."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

try:
    from . import talk_config, talk_core_realtime
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_config
    import talk_core_realtime

CORE_AUDIO_POLL_S = 0.01


def build_core_setup():
    """Build the exact input-only Hermes realtime setup for canonical ingress."""

    if not talk_core_realtime.core_provider_available():
        raise RuntimeError("Hermes realtime voice API-v2 is unavailable")
    return talk_core_realtime.RealtimeVoiceSetup(
        model=talk_config.talk_model(),
        voice=talk_config.talk_voice(),
        instructions="",
        tools=(),
        audio=talk_core_realtime.SUPPORTED_AUDIO_FORMAT,
        provider_options={
            "automatic_response": False,
            "capabilities": (
                "input_transcription",
                "input_commit_events",
            ),
        },
    )


@dataclass(frozen=True, slots=True)
class OperatorPacketAdmission:
    """Admit PCM only from one immutable Discord operator identity.

    A packet whose speaker is ``None`` is transport-synthesized silence and
    remains admissible so server-side VAD can close an already-open operator
    turn. All malformed or unresolved input fails closed.
    """

    operator_user_id: int

    def __post_init__(self) -> None:
        if type(self.operator_user_id) is not int:
            raise TypeError("operator_user_id must be a positive integer")
        if self.operator_user_id <= 0:
            raise ValueError("operator_user_id must be a positive integer")

    def admit(self, packet: Any) -> bytes | None:
        """Return authorized PCM, otherwise ``None``."""

        try:
            speaker = packet.speaker
            pcm = packet.pcm
        except Exception:  # noqa: BLE001 - malformed transport objects fail closed
            return None
        if type(pcm) is not bytes or not pcm or len(pcm) % 2:
            return None
        if speaker is None:
            return pcm if not any(pcm) else None
        if type(speaker) is not dict:
            return None
        user_id = speaker.get("user_id")
        if type(user_id) is not int or user_id != self.operator_user_id:
            return None
        return pcm


async def run_core_session(
    factory: object,
    *,
    guild_id: int,
    audio: object | None = None,
) -> None:
    """Pump one capture-only Discord lane into a host-owned attachment."""

    if audio is None:
        try:
            from .talk_discord import DiscordAudio
        except ImportError:  # pragma: no cover - flat-module fallback
            from talk_discord import DiscordAudio

        audio = DiscordAudio(guild_id, capture_only=True)

    attachment = None
    try:
        attachment = await factory.open(
            talk_core_realtime.PROVIDER_NAME,
            build_core_setup(),
            provider_session_id=f"talk-discord-{uuid.uuid4().hex}",
            required_capabilities=talk_core_realtime.CORE_CAPABILITIES,
        )
        admission = OperatorPacketAdmission(attachment.operator_user_id)
        audio.start()
        while True:
            # Discord capture is a paced synchronous bridge. Keep that blocking
            # read off the gateway loop so text turns, stop, and teardown remain
            # responsive while voice capture is idle.
            packet = await asyncio.to_thread(audio.read_input_packet)
            if packet is None:
                await asyncio.sleep(CORE_AUDIO_POLL_S)
                continue
            pcm = admission.admit(packet)
            if pcm is None:
                continue
            if packet.speaker is None:
                result = attachment.feed_synthesized_silence(pcm)
            else:
                result = attachment.feed_audio(
                    pcm,
                    speaker_user_id=packet.speaker["user_id"],
                    mime_type="audio/pcm",
                )
            if str(result) != "accepted":
                raise RuntimeError(f"canonical realtime audio feed failed closed: {result}")
    finally:
        try:
            audio.stop()
        finally:
            if attachment is not None:
                await attachment.close()


__all__ = ["OperatorPacketAdmission", "build_core_setup", "run_core_session"]
