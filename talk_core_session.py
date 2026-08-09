"""Transport-neutral admission primitives for canonical Talk sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
        if type(pcm) is not bytes or not pcm:
            return None
        if speaker is None:
            return pcm
        if type(speaker) is not dict:
            return None
        user_id = speaker.get("user_id")
        if type(user_id) is not int or user_id != self.operator_user_id:
            return None
        return pcm


__all__ = ["OperatorPacketAdmission"]