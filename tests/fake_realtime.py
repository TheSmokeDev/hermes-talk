"""Scripted Realtime transcripts — the offline stand-in for a live call.

Feed :func:`run_transcript` a list of server events exactly as OpenAI Realtime
would emit them; get back everything the relay did about it: the wire messages
it wanted sent, and every callback it fired. No socket, no key, no audio
device — which is the whole point.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from talk_relay import RealtimeRelay


@dataclass
class Recorder:
    """Everything one transcript produced."""

    sent: list[dict] = field(default_factory=list)
    audio: list[bytes] = field(default_factory=list)
    captions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    barge_ins: int = 0

    @property
    def sent_types(self) -> list[str]:
        return [message.get("type", "") for message in self.sent]

    @property
    def caption_text(self) -> str:
        return "".join(self.captions)

    def function_outputs(self) -> list[str]:
        return [
            message["item"]["output"]
            for message in self.sent
            if message.get("type") == "conversation.item.create"
            and message.get("item", {}).get("type") == "function_call_output"
        ]


def build_relay(recorder: Recorder, tool_executor=None) -> RealtimeRelay:
    """A relay wired to ``recorder``."""

    def bump_barge_in() -> None:
        recorder.barge_ins += 1

    return RealtimeRelay(
        on_audio=recorder.audio.append,
        on_caption=recorder.captions.append,
        on_barge_in=bump_barge_in,
        on_error=recorder.errors.append,
        tool_executor=tool_executor,
    )


def run_transcript(events: list[dict], *, tool_executor=None) -> Recorder:
    """Play a transcript through a fresh relay and return what happened."""

    recorder = Recorder()
    relay = build_relay(recorder, tool_executor)
    for event in events:
        recorder.sent.extend(relay.handle_event(event))
    return recorder


# -- event builders -----------------------------------------------------------


def audio_delta(pcm: bytes, item_id: str = "item_1") -> dict:
    return {
        "type": "response.output_audio.delta",
        "item_id": item_id,
        "delta": base64.b64encode(pcm).decode("ascii"),
    }


def transcript_delta(text: str) -> dict:
    return {"type": "response.output_audio_transcript.delta", "delta": text}


def speech_started() -> dict:
    return {"type": "input_audio_buffer.speech_started"}


def function_call(name: str, arguments: str, call_id: str = "call_1") -> dict:
    return {
        "type": "response.function_call_arguments.done",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def server_error(message: str) -> dict:
    return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
