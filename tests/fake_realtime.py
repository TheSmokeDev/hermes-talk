"""Scripted Realtime transcripts — the offline stand-in for a live call.

Feed :func:`run_transcript` a list of server events exactly as OpenAI Realtime
would emit them; get back everything the relay did about it: the wire messages
it wanted sent, and every callback it fired. No socket, no key, no audio
device — which is the whole point.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import talk_realtime as rt
from talk_relay import RealtimeRelay


@dataclass
class Recorder:
    """Everything one transcript produced."""

    sent: list[dict] = field(default_factory=list)
    #: Neutral commands, for transcripts played through handle_realtime_event.
    commands: list = field(default_factory=list)
    audio: list[bytes] = field(default_factory=list)
    captions: list[str] = field(default_factory=list)
    turns: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    barge_ins: int = 0

    @property
    def sent_types(self) -> list[str]:
        return [message.get("type", "") for message in self.sent]

    @property
    def command_types(self) -> list[str]:
        return [type(command).__name__ for command in self.commands]

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
        on_transcript_turn=lambda role, text: recorder.turns.append((role, text)),
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


def play_neutral(relay: RealtimeRelay, recorder: Recorder, events: list) -> None:
    """Drive the path the live session actually runs: handle_realtime_event."""

    for event in events:
        recorder.commands.extend(relay.handle_realtime_event(event))


def run_neutral_transcript(events: list, *, tool_executor=None) -> Recorder:
    """Play a neutral transcript through a fresh relay and return what happened."""

    recorder = Recorder()
    play_neutral(build_relay(recorder, tool_executor), recorder, events)
    return recorder


# -- event builders -----------------------------------------------------------


def audio_delta(pcm: bytes, item_id: str = "item_1", response_id: str | None = None) -> dict:
    event = {
        "type": "response.output_audio.delta",
        "item_id": item_id,
        "delta": base64.b64encode(pcm).decode("ascii"),
    }
    if response_id is not None:
        event["response_id"] = response_id
    return event


def transcript_delta(text: str, response_id: str | None = None) -> dict:
    event = {"type": "response.output_audio_transcript.delta", "delta": text}
    if response_id is not None:
        event["response_id"] = response_id
    return event


def speech_started() -> dict:
    return {"type": "input_audio_buffer.speech_started"}


def response_created(response_id: str | None = None) -> dict:
    event: dict = {"type": "response.created"}
    if response_id is not None:
        event["response"] = {"id": response_id}
    return event


def response_done(response_id: str | None = None) -> dict:
    event: dict = {"type": "response.done"}
    if response_id is not None:
        event["response"] = {"id": response_id}
    return event


def function_call(name: str, arguments: str, call_id: str = "call_1") -> dict:
    return {
        "type": "response.function_call_arguments.done",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def server_error(message: str) -> dict:
    return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}


# -- neutral event builders ---------------------------------------------------
#
# The dict builders above drive handle_event/_DISPATCH; these drive
# handle_realtime_event, which is the path a real CLI or Discord call takes.


def rt_response_started(response_id: str | None = "resp_1") -> rt.ResponseStarted:
    return rt.ResponseStarted(response_id=response_id)


def rt_speech_started() -> rt.SpeechStarted:
    return rt.SpeechStarted(input_id="input_1", offset_ms=0)


def rt_audio(
    pcm: bytes, *, item_id: str | None = "item_1", response_id: str | None = "resp_1"
) -> rt.OutputAudio:
    return rt.OutputAudio(data=pcm, item_id=item_id, response_id=response_id)


def rt_transcript_delta(text: str, *, response_id: str | None = "resp_1") -> rt.Transcript:
    return rt.Transcript(
        role=rt.TranscriptRole.ASSISTANT,
        text=text,
        final=False,
        provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
        response_id=response_id,
    )


def rt_transcript_done(
    text: str = "", *, response_id: str | None = "resp_1"
) -> rt.Transcript:
    return rt.Transcript(
        role=rt.TranscriptRole.ASSISTANT,
        text=text,
        final=True,
        provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
        response_id=response_id,
    )


def rt_user_said(text: str) -> rt.Transcript:
    return rt.Transcript(
        role=rt.TranscriptRole.USER,
        text=text,
        final=True,
        provenance=rt.TranscriptProvenance.INPUT_AUDIO,
    )


def rt_response_finished(response_id: str | None = "resp_1") -> rt.ResponseFinished:
    return rt.ResponseFinished(response_id=response_id)
