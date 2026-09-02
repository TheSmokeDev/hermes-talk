"""Terminal audio surface for the Hermes #95147 coordinator seam."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import uuid

from agent.realtime_voice import RealtimeEvent, RealtimeEventType
from agent.realtime_voice_coordinator import RealtimeVoiceCoordinator
from agent.realtime_voice_registry import get_provider
from model_tools import get_tool_definitions

try:
    from . import talk_audio, talk_config, talk_host, talk_identity, talk_transcript
    from .talk_core_realtime_contract import configured_provider_name
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_audio
    import talk_config
    import talk_host
    import talk_identity
    import talk_transcript
    from talk_core_realtime_contract import configured_provider_name

logger = logging.getLogger(__name__)

IDLE_POLL_S = 0.01
EVENT_STREAM_ENV = "HERMES_TALK_EVENT_STREAM"
MAX_PROVIDER_ITEM_ID_LENGTH = 32
EVENT_PREFIX = "talk: event "
DELEGATE_TOOL = {
    "type": "function",
    "function": {
        "name": "client_delegate",
        "description": (
            "Delegate tool use, coding, research, commands, or other substantive "
            "work to the Hermes text agent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "Complete plain-language request with relevant context.",
                }
            },
            "required": ["request"],
            "additionalProperties": False,
        },
    },
}
DELEGATE_INSTRUCTIONS = """

You are Hermes Live, the realtime voice surface of one unified assistant.
Respond directly, briefly, and conversationally. Never read long answers,
implementation detail, markdown, code, or tool output aloud.

You MUST call client_delegate promptly for research, tool use, commands,
coding, repository work, or any substantive factual task. The delegated text
agent owns execution and displays its detailed response to the user while it
works. You may give one short acknowledgement, then wait. Treat the returned
result as your own internal context and speak only a concise useful summary.
Never mention delegation, a backend, a tool protocol, or another assistant.
Answer greetings and ordinary conversation directly without delegation.
""".strip()


def _emit_event(payload: dict) -> None:
    print(f"{EVENT_PREFIX}{json.dumps(payload, separators=(',', ':'))}", flush=True)


def _progress_item_id(request_id: str, index: int) -> str:
    """Build a unique progress item id within provider wire limits."""

    return f"p{index:x}-{request_id}"[:MAX_PROVIDER_ITEM_ID_LENGTH]

async def _read_stdin_line() -> str:
    """Read one TUI command without leaving a blocking executor task on POSIX."""

    loop = asyncio.get_running_loop()
    try:
        descriptor = sys.stdin.fileno()
        result: asyncio.Future[str] = loop.create_future()

        def ready() -> None:
            if not result.done():
                result.set_result(sys.stdin.readline())

        loop.add_reader(descriptor, ready)
    except (AttributeError, io.UnsupportedOperation, NotImplementedError):
        return await asyncio.to_thread(sys.stdin.readline)
    try:
        return await result
    finally:
        loop.remove_reader(descriptor)


async def run_core_talk_session(audio=None) -> int:
    """Run duplex media through the registered provider and Hermes coordinator."""

    event_stream = os.environ.get(EVENT_STREAM_ENV) == "jsonl"
    provider = get_provider(configured_provider_name())
    if provider is None:
        print("talk: no registered realtime voice provider", file=sys.stderr)
        return 1
    ctx = talk_host.get_ctx()
    if not event_stream and (
        ctx is None or not callable(getattr(ctx, "dispatch_tool", None))
    ):
        print("talk: Hermes tool authority is unavailable", file=sys.stderr)
        return 1

    tools = [DELEGATE_TOOL] if event_stream else (get_tool_definitions(quiet_mode=True) or [])
    instructions = talk_identity.build_instructions(
        talk_host.host().identity_sections(),
        tools=tools,
        host_execution=True,
        lane="cli",
    )
    if event_stream:
        instructions = f"{instructions}\n\n{DELEGATE_INSTRUCTIONS}"
    if audio is None:
        audio = talk_audio.DuplexAudio()
    try:
        audio.start()
    except talk_audio.TalkAudioError as exc:
        print(f"talk: {exc}", file=sys.stderr)
        return 1

    async def dispatch_tool(name: str, arguments: dict) -> str:
        if not event_stream:
            return await asyncio.to_thread(ctx.dispatch_tool, name, arguments)
        if name != "client_delegate":
            return f"Error: unsupported live voice tool {name!r}"
        request = str(arguments.get("request") or "").strip()
        if not request:
            return "Error: client_delegate requires a non-empty request"
        request_id = uuid.uuid4().hex
        progress_index = 0
        _emit_event({"type": "delegate", "id": request_id, "request": request})
        while True:
            line = await _read_stdin_line()
            if not line:
                raise RuntimeError("Hermes TUI closed the live delegation channel")
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(command, dict) or command.get("id") != request_id:
                continue
            if command.get("type") == "delegate.progress":
                progress = str(command.get("text") or "").strip()
                if progress:
                    progress_index += 1
                    try:
                        await coordinator.add_context(
                            _progress_item_id(request_id, progress_index),
                            f"Silent Hermes text-agent progress:\n\n{progress}",
                        )
                    except Exception as exc:  # noqa: BLE001 - progress is optional
                        logger.warning(
                            "Realtime voice progress context was rejected; "
                            "continuing to await the final text-agent result: %s",
                            exc,
                        )
                continue
            if command.get("type") == "delegate.result":
                output = str(command.get("output") or "").strip()
                return f'"Agent Final Message":\n\n{output}'

    coordinator = RealtimeVoiceCoordinator(
        provider,
        dispatch_tool=dispatch_tool,
        max_in_flight_tool_calls=1 if event_stream else 16,
    )
    capture = talk_transcript.TranscriptCapture(talk_config.get_hermes_home())
    configured = talk_config.talk_provider()
    model = (
        talk_config.talk_grok_model()
        if configured == "grok"
        else talk_config.talk_model()
    )
    voice = (
        talk_config.talk_grok_voice()
        if configured == "grok"
        else talk_config.talk_voice()
    )
    try:
        await coordinator.open(
            instructions=instructions,
            tools=tools,
            voice=voice,
        )
    except Exception as exc:  # noqa: BLE001 - provider startup is a voice boundary
        audio.stop()
        capture.finish()
        print(f"talk: {exc}", file=sys.stderr)
        return 1

    print(
        f"talk: connected ({model}, voice {voice}). "
        "Ctrl+C to hang up.\n",
        flush=True,
    )
    last_audio_event: RealtimeEvent | None = None
    print("talk: state listening", flush=True)
    active_item_id: str | None = None

    async def send_microphone() -> None:
        while True:
            chunk = audio.read_input_chunk()
            if chunk is None:
                await asyncio.sleep(IDLE_POLL_S)
                continue
            await coordinator.send_audio(chunk)

    async def receive_events() -> None:
        nonlocal active_item_id, last_audio_event
        async for event in coordinator.events():
            if event.type is RealtimeEventType.AUDIO:
                if not event.audio_bytes:
                    continue
                if event.item_id != active_item_id:
                    active_item_id = event.item_id
                    last_audio_event = None
                    audio.reset_played_ms()
                print("talk: state composing", flush=True)
                audio.queue_playback(event.audio_bytes)
                last_audio_event = event
                continue
            if event.type is RealtimeEventType.TRANSCRIPT:
                if event.text:
                    if event_stream and event.role in {"user", "assistant"}:
                        _emit_event(
                            {
                                "type": "transcript",
                                "role": event.role,
                                "text": event.text,
                                "final": event.final,
                            }
                        )
                    elif not event_stream:
                        print(event.text, end="\n" if event.final else "", flush=True)
                    if event.final and event.role in {"user", "assistant"}:
                        capture.append_turn(event.role, event.text)
                continue
            if event.type is RealtimeEventType.TURN_ENDED:
                if event.role == "user":
                    print("talk: state solving", flush=True)
                elif event.role == "assistant":
                    print("talk: state listening", flush=True)
                continue
            if event.type is RealtimeEventType.TURN_STARTED:
                if event.role == "assistant":
                    print("talk: state composing", flush=True)
                elif event.role == "user" and last_audio_event is not None:
                    coordinator.report_audio_heard(
                        last_audio_event,
                        audio_end_ms=audio.played_ms,
                    )
                    audio.drain_playback()
                    await coordinator.cancel_response()
                    last_audio_event = None
                    active_item_id = None
                    print("talk: state listening", flush=True)
                continue
            if event.type is RealtimeEventType.ERROR:
                raise RuntimeError(event.text or "realtime voice provider failed")

    microphone = asyncio.create_task(send_microphone())
    receiver = asyncio.create_task(receive_events())
    try:
        done, pending = await asyncio.wait(
            (microphone, receiver),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            try:
                task.result()
            except Exception as exc:  # noqa: BLE001 - runtime boundary
                message = f"{type(exc).__name__}: {exc}"
                if event_stream:
                    _emit_event({"type": "error", "message": message})
                else:
                    print(f"talk: {message}", file=sys.stderr)
                return 1
        return 0
    finally:
        microphone.cancel()
        receiver.cancel()
        await asyncio.gather(microphone, receiver, return_exceptions=True)
        if event_stream:
            with contextlib.suppress(Exception):
                sys.stdin.close()
        with contextlib.suppress(Exception):
            await coordinator.close()
        audio.stop()
        capture.finish()
        talk_transcript.sweep_transcripts(talk_config.get_hermes_home())


__all__ = ["IDLE_POLL_S", "run_core_talk_session"]
