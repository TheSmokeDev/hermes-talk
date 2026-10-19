"""Live wire vocabulary, separate from the Realtime function-call protocol.

Subscription compatibility derives from OpenClaw 76378ddb (MIT),
realtime-quicksilver-wire.ts and realtime-quicksilver-delegation-controller.ts.
Public API shapes follow OpenAI Live session/delegation documentation.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping

try:
    from . import talk_realtime as rt
    from .talk_live_config import LiveConfig
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_realtime as rt
    from talk_live_config import LiveConfig

MAX_APPEND_BYTES = 500
MAX_EVENT_TEXT = 65536


def build_live_session(setup: rt.SessionSetup, config: LiveConfig, *, webrtc: bool) -> dict:
    if setup.model != config.model or setup.voice != config.voice:
        raise rt.RealtimeSessionError("Live model and voice must match the selected auth mode")
    if setup.text_output:
        raise rt.RealtimeSessionError("GPT-Live does not support cascade text-only output")
    if setup.turn_detection.mode is not rt.RealtimeTurnDetectionMode.PROVIDER_NATIVE:
        raise rt.RealtimeSessionError("GPT-Live uses its own continuous endpointing")
    if not setup.automatic_response and not setup.task_continuity:
        raise rt.RealtimeSessionError(
            "GPT-Live cannot disable automatic speech; use canonical task continuity"
        )
    audio = {"output": {"voice": config.voice}}
    if not webrtc:
        audio["format"] = {"type": "audio/pcm", "rate": 24000}
    return {
        "model": config.model,
        "instructions": setup.instructions,
        "audio": audio,
        "delegation": {"type": "client"},
    }


def _mapping(value) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _text(value, *, maximum=MAX_EVENT_TEXT) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("Invalid Live text")
    return value


def _offset(value) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("Invalid Live offset")
    return value


def decode_event(event: Mapping) -> rt.RealtimeEvent | None:
    """Preserve provider transcript finality and delegation identity, never invent it."""
    try:
        kind = event.get("type")
        if kind == "session.started":
            return rt.SessionReady(session_id=_mapping(event.get("session")).get("id"))
        if kind == "session.closed":
            reason = event.get("reason")
            failed = reason in {"connection_lost", "content"}
            return rt.SessionTerminated(
                state=rt.SessionState.FAILED if failed else rt.SessionState.CLOSED,
                detail=f"GPT-Live session finalized ({reason})" if reason else "",
            )
        if kind in {"session.input_transcript.delta", "session.output_transcript.delta"}:
            user = kind == "session.input_transcript.delta"
            return rt.Transcript(
                role=rt.TranscriptRole.USER if user else rt.TranscriptRole.ASSISTANT,
                text=_text(event.get("delta")),
                final=False,
                provenance=(
                    rt.TranscriptProvenance.INPUT_AUDIO
                    if user
                    else rt.TranscriptProvenance.OUTPUT_AUDIO
                ),
                item_id=event.get("item_id"),
                event_id=event.get("event_id"),
                finality="delta",
                start_ms=_offset(event.get("start_ms")),
                end_ms=_offset(event.get("end_ms")),
            )
        if kind in {"input_transcript.added", "output_transcript.added", "turn.done"}:
            item = _mapping(event.get("turn") if kind == "turn.done" else event.get("item"))
            if kind == "turn.done":
                if item.get("role") not in {"user", "assistant"}:
                    raise ValueError("Invalid Live turn role")
                user = item["role"] == "user"
                text = _text(item.get("transcript"))
            else:
                user = kind == "input_transcript.added"
                text = _text(item.get("text"))
            return rt.Transcript(
                role=rt.TranscriptRole.USER if user else rt.TranscriptRole.ASSISTANT,
                text=text,
                final=kind == "turn.done",
                provenance=(
                    rt.TranscriptProvenance.INPUT_AUDIO
                    if user
                    else rt.TranscriptProvenance.OUTPUT_AUDIO
                ),
                item_id=item.get("id"),
                event_id=event.get("event_id"),
                finality="turn" if kind == "turn.done" else "item",
                start_ms=_offset(item.get("start_ms")),
                end_ms=_offset(item.get("end_ms")),
            )
        if kind in {"session.delegation.created", "delegation.created"}:
            item = _mapping(
                event.get("delegation") if kind.startswith("session.") else event.get("item")
            )
            if item.get("target") != "client" or item.get("type") != "delegation":
                return None
            prompt = None
            if kind == "delegation.created" and isinstance(item.get("content"), list):
                prompt = "".join(
                    _text(part.get("text", ""), maximum=16000)
                    for part in item["content"]
                    if isinstance(part, Mapping) and part.get("type") == "input_text"
                )
            return rt.DelegationRequested(
                delegation_id=item.get("id"),
                offset_ms=_offset(event.get("offset_ms")),
                target="client",
                prompt=prompt,
            )
        if kind in {"session.output_audio.delta", "output_audio.delta"}:
            encoded = event.get("delta") if kind.startswith("session.") else event.get("audio")
            data = base64.b64decode(_text(encoded, maximum=2_000_000), validate=True)
            if len(data) % 2:
                raise ValueError("Invalid PCM byte alignment")
            return rt.OutputAudio(data=data)
        if kind == "error":
            error = _mapping(event.get("error"))
            code = error.get("code") or event.get("code")
            terminal = code in {
                "authentication_error",
                "invalid_api_key",
                "invalid_token",
                "token_expired",
            } or error.get("status", event.get("status")) in {401, 403}
            # Never relay provider text: it can echo authorization/account values.
            return rt.ProviderFailure(
                detail="GPT-Live rejected a session command", terminal=terminal
            )
    except (ValueError, TypeError, binascii.Error):
        return rt.ProviderFailure(detail="GPT-Live returned a malformed event", terminal=False)
    return None


def chunk_append_text(text: str) -> tuple[str, ...]:
    """Conservative UTF-8 byte cap also fits the public API's 500-token bound."""
    chunks = []
    current = ""
    size = 0
    for character in text:
        width = len(character.encode("utf-8"))
        if size + width > MAX_APPEND_BYTES:
            chunks.append(current)
            current, size = "", 0
        current += character
        size += width
    if current:
        chunks.append(current)
    return tuple(chunks)


def encode_command(command: rt.RealtimeCommand, *, subscription: bool = False) -> tuple[dict, ...]:
    if isinstance(command, rt.AppendInputAudio):
        if len(command.data) % 2:
            raise rt.RealtimeSessionError("Live input requires 24-kHz mono PCM16")
        return (
            {
                "type": "session.input_audio.append",
                "audio": base64.b64encode(command.data).decode("ascii"),
            },
        )
    if isinstance(command, rt.SubmitDelegationResult):
        if subscription:
            return tuple(
                {
                    "type": "delegation.context.append",
                    "delegation_item_id": command.delegation_id,
                    "channel": "speakable" if command.kind == "commentary" else "commentary",
                    "content": [{"type": "input_text", "text": chunk}],
                }
                for chunk in chunk_append_text(command.content)
            )
        kind = "commentary" if command.kind == "commentary" else "thinking"
        return tuple(
            {
                "type": f"session.{kind}.append",
                "content": chunk,
                "delegation_id": command.delegation_id,
            }
            for chunk in chunk_append_text(command.content)
        )
    if isinstance(command, rt.AppendLiveContext):
        kind = {"instructions": "instructions", "context": "thinking", "message": "commentary"}[
            command.kind
        ]
        if subscription and command.delegation_id:
            return encode_command(
                rt.SubmitDelegationResult(
                    command.delegation_id,
                    command.content,
                    kind="commentary" if command.kind == "message" else "context",
                ),
                subscription=True,
            )
        return tuple(
            {
                "type": f"session.{kind}.append",
                "content": chunk,
                "delegation_id": command.delegation_id,
            }
            for chunk in chunk_append_text(command.content)
        )
    if isinstance(command, rt.AddContext):
        return encode_command(rt.AppendLiveContext(command.text), subscription=subscription)
    if isinstance(command, rt.CancelResponse):
        return encode_command(
            rt.AppendLiveContext(
                "Stop speaking now and listen to the user. This does not cancel backend tasks."
            ),
            subscription=subscription,
        )
    if isinstance(command, rt.TruncateOutput):
        # Live has no truncatable response item. The caller owns local playback interruption.
        return ()
    raise rt.RealtimeSessionError(
        f"{type(command).__name__} is not a GPT-Live client-delegation command"
    )


__all__ = ["build_live_session", "chunk_append_text", "decode_event", "encode_command"]
