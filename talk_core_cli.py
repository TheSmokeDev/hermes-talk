"""Terminal audio surface for the Hermes #95147 coordinator seam."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import threading
import uuid

from agent.realtime_voice import RealtimeEvent, RealtimeEventType
from agent.realtime_voice_coordinator import RealtimeVoiceCoordinator
from agent.realtime_voice_registry import get_provider

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
MAX_CONTROL_FRAME_CHARS = 65_536
MAX_PENDING_CONTROL_FRAMES = 64
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

class _StdinLineReader:
    """One owner for stdin buffering; POSIX stays event-driven."""

    def __init__(self, stream=None) -> None:
        self._stream = stream or sys.stdin
        self._loop = asyncio.get_running_loop()
        self._lines: asyncio.Queue[str | None] = asyncio.Queue(
            maxsize=MAX_PENDING_CONTROL_FRAMES + 1
        )
        self._eof = asyncio.Event()
        self._failure: RuntimeError | None = None
        self._descriptor: int | None = None
        self._text_buffer = ""
        self._thread: threading.Thread | None = None
        try:
            self._descriptor = self._stream.fileno()
            self._loop.add_reader(self._descriptor, self._read_ready)
        except (AttributeError, io.UnsupportedOperation, NotImplementedError):
            self._descriptor = None
            self._thread = threading.Thread(
                target=self._read_lines,
                name="hermes-talk-stdin",
                daemon=True,
            )
            self._thread.start()

    def _read_ready(self) -> None:
        assert self._descriptor is not None
        try:
            chunk = os.read(self._descriptor, 65_536)
        except OSError:
            chunk = b""
        if not chunk:
            self._mark_eof()
            return
        self._feed_text(chunk.decode("utf-8", errors="replace"))

    def _read_lines(self) -> None:
        try:
            for line in self._stream:
                self._loop.call_soon_threadsafe(self._feed_text, line)
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._mark_eof)

    def _feed_text(self, text: str) -> None:
        if self._eof.is_set():
            return
        self._text_buffer += text
        lines = self._text_buffer.split("\n")
        self._text_buffer = lines.pop()
        if self._text_buffer and len(self._text_buffer) > MAX_CONTROL_FRAME_CHARS:
            self._fail("Hermes TUI sent an oversized live delegation frame")
            return
        for line in lines:
            if len(line) > MAX_CONTROL_FRAME_CHARS:
                self._fail("Hermes TUI sent an oversized live delegation frame")
                return
            if self._lines.qsize() >= MAX_PENDING_CONTROL_FRAMES:
                self._fail("Hermes TUI flooded the live delegation channel")
                return
            self._lines.put_nowait(line.removesuffix("\r"))

    def _mark_eof(self) -> None:
        if self._eof.is_set():
            return
        if self._descriptor is not None:
            self._loop.remove_reader(self._descriptor)
        if self._text_buffer:
            if len(self._text_buffer) > MAX_CONTROL_FRAME_CHARS:
                self._failure = RuntimeError(
                    "Hermes TUI sent an oversized live delegation frame"
                )
            elif self._lines.qsize() >= MAX_PENDING_CONTROL_FRAMES:
                self._failure = RuntimeError(
                    "Hermes TUI flooded the live delegation channel"
                )
            else:
                self._lines.put_nowait(self._text_buffer)
            self._text_buffer = ""
        self._eof.set()
        self._lines.put_nowait(None)

    def _fail(self, message: str) -> None:
        self._failure = RuntimeError(message)
        self._text_buffer = ""
        while not self._lines.empty():
            self._lines.get_nowait()
        self._mark_eof()

    async def readline(self) -> str:
        line = await self._lines.get()
        if line is None:
            if self._failure is not None:
                raise self._failure
            return ""
        return line

    async def wait_for_parent_close(self, delegation_idle: asyncio.Event) -> None:
        await self._eof.wait()
        await delegation_idle.wait()
        if self._failure is not None:
            raise self._failure
        raise RuntimeError("Hermes TUI closed the live delegation channel")

    def close(self) -> None:
        if self._descriptor is not None:
            with contextlib.suppress(Exception):
                self._loop.remove_reader(self._descriptor)
        if self._thread is not None:
            with contextlib.suppress(Exception):
                self._stream.close()


async def run_core_talk_session(audio=None) -> int:
    """Run the TUI's client-delegated duplex session through Hermes core."""

    if os.environ.get(EVENT_STREAM_ENV) != "jsonl":
        print("talk: core realtime mode is reserved for the Hermes TUI", file=sys.stderr)
        return 1

    try:
        provider = get_provider(configured_provider_name())
    except Exception as exc:  # noqa: BLE001 - configuration boundary
        print(f"talk: {exc}", file=sys.stderr)
        return 1
    if provider is None:
        print("talk: no registered realtime voice provider", file=sys.stderr)
        return 1

    tools = [DELEGATE_TOOL]
    try:
        instructions = talk_identity.build_instructions(
            talk_host.host().identity_sections(),
            tools=tools,
            host_execution=True,
            lane="cli",
        )
        instructions = f"{instructions}\n\n{DELEGATE_INSTRUCTIONS}"
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
        audio = audio or talk_audio.DuplexAudio()
    except Exception as exc:  # noqa: BLE001 - startup configuration boundary
        print(f"talk: {exc}", file=sys.stderr)
        return 1

    coordinator: RealtimeVoiceCoordinator | None = None
    stdin_reader: _StdinLineReader | None = None
    runtime_tasks: list[asyncio.Task] = []
    audio_started = False
    delegation_idle = asyncio.Event()
    delegation_idle.set()

    async def dispatch_tool(name: str, arguments: dict) -> str:
        if name != "client_delegate":
            return f"Error: unsupported live voice tool {name!r}"
        request = str(arguments.get("request") or "").strip()
        if not request:
            return "Error: client_delegate requires a non-empty request"
        if stdin_reader is None or coordinator is None:
            raise RuntimeError("Hermes TUI delegation channel is unavailable")

        request_id = uuid.uuid4().hex
        progress_index = 0
        delegation_idle.clear()
        _emit_event({"type": "delegate", "id": request_id, "request": request})
        try:
            while True:
                line = await stdin_reader.readline()
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
        finally:
            delegation_idle.set()

    coordinator = RealtimeVoiceCoordinator(
        provider,
        dispatch_tool=dispatch_tool,
        max_in_flight_tool_calls=1,
    )
    last_audio_event: RealtimeEvent | None = None
    last_state: str | None = None

    def emit_state(state: str) -> None:
        nonlocal last_state
        if state == last_state:
            return
        last_state = state
        print(f"talk: state {state}", flush=True)

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
                emit_state("composing")
                audio.queue_playback(event.audio_bytes, item_id=event.item_id)
                last_audio_event = event
                continue
            if event.type is RealtimeEventType.TRANSCRIPT:
                if event.text:
                    if event.role in {"user", "assistant"}:
                        _emit_event(
                            {
                                "type": "transcript",
                                "role": event.role,
                                "text": event.text,
                                "final": event.final,
                            }
                        )
                    if event.final and event.role in {"user", "assistant"}:
                        capture.append_turn(event.role, event.text)
                continue
            if event.type is RealtimeEventType.TURN_ENDED:
                if event.role == "user":
                    emit_state("solving")
                elif event.role == "assistant":
                    emit_state("listening")
                continue
            if event.type is RealtimeEventType.TURN_STARTED:
                if event.role == "assistant":
                    emit_state("composing")
                elif event.role == "user" and last_audio_event is not None:
                    coordinator.report_audio_heard(
                        last_audio_event,
                        audio_end_ms=audio.played_ms,
                    )
                    audio.drain_playback()
                    await coordinator.cancel_response()
                    last_audio_event = None
                    emit_state("listening")
                continue
            if event.type is RealtimeEventType.ERROR:
                raise RuntimeError(event.text or "realtime voice provider failed")
        raise RuntimeError("realtime voice provider closed unexpectedly")

    try:
        audio.start()
        audio_started = True
        stdin_reader = _StdinLineReader()
        await coordinator.open(
            instructions=instructions,
            tools=tools,
            voice=voice,
        )
        print(
            f"talk: connected ({model}, voice {voice}). "
            "Ctrl+C to hang up.\n",
            flush=True,
        )
        emit_state("listening")

        runtime_tasks = [
            asyncio.create_task(send_microphone()),
            asyncio.create_task(receive_events()),
            asyncio.create_task(
                stdin_reader.wait_for_parent_close(delegation_idle)
            ),
        ]
        done, pending = await asyncio.wait(
            runtime_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
        return 0
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - terminal runtime boundary
        _emit_event(
            {
                "type": "error",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
        return 1
    finally:
        for task in runtime_tasks:
            task.cancel()
        if runtime_tasks:
            await asyncio.gather(*runtime_tasks, return_exceptions=True)
        if stdin_reader is not None:
            stdin_reader.close()
        with contextlib.suppress(Exception):
            await coordinator.close()
        if audio_started:
            audio.stop()
        capture.finish()
        talk_transcript.sweep_transcripts(talk_config.get_hermes_home())


__all__ = ["IDLE_POLL_S", "run_core_talk_session"]
