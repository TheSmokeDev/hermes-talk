"""The Realtime event loop — transport-agnostic.

:class:`RealtimeRelay` takes one decoded Realtime server event and returns the
wire messages to send back. It never touches a socket or a microphone, which
lets the same loop serve the terminal session (aiohttp WebSocket) and, later,
a browser data channel — and lets the whole thing be tested against a scripted
transcript with no network. Its async entry point moves blocking tools off the
transport's event loop; the synchronous entry point remains for loop-free
callers and scripted tests.

Audio, captions, barge-in, and errors leave through injected callbacks. Tool
results come back as ``function_call_output`` items; a tool that fails speaks
its failure, and only an unknown tool name (a client bug) is reported as an
error string rather than a result.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextvars
import json
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

try:
    from .talk_tools import TalkToolError, execute_talk_tool
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    from talk_tools import TalkToolError, execute_talk_tool

BARGE_IN_MESSAGE: dict = {"type": "response.cancel"}
UNPARSEABLE_ARGS_TEXT = (
    "I couldn't parse that tool call — the arguments weren't valid JSON. "
    "Say what you wanted and I'll try again."
)

#: Courtesy wait for an ordinary tool. The worker is deliberately allowed to
#: finish after this bound (Python cannot safely kill a running thread), but
#: the Realtime turn gets an honest output instead of permanent silence.
TOOL_EXECUTION_WAIT_S = 5.0
TOOL_WORKERS = 2
TOOL_PENDING_JOBS = 4


def _noop(*_args) -> None:
    """Default callback: the relay works with no consumers attached."""


class ToolWorkerBusy(RuntimeError):
    """The bounded tool worker pool cannot admit another job."""


class ToolExecutionTimeout(TimeoutError):
    """A courtesy wait expired, with truthful worker-start state."""

    def __init__(self, *, started: bool) -> None:
        super().__init__("tool execution courtesy wait expired")
        self.started = started


@dataclass
class _ToolSubmission:
    future: concurrent.futures.Future
    started: threading.Event = field(default_factory=threading.Event)


class _DaemonWorkerPool:
    """A fixed daemon pool with bounded pending admission.

    Python cannot stop a running thread safely. A wedged tool can therefore
    occupy one worker, but it cannot create another worker or an unbounded
    backlog: once both fixed workers and the small queue are occupied, callers
    receive :class:`ToolWorkerBusy` immediately.
    """

    def __init__(self, *, max_workers: int, max_pending: int) -> None:
        self._max_workers = max_workers
        self._jobs: queue.Queue[tuple] = queue.Queue(maxsize=max_pending)
        self._threads: list[threading.Thread] = []
        self._start_lock = threading.Lock()

    @property
    def worker_count(self) -> int:
        return len(self._threads)

    def _ensure_started(self) -> None:
        if self._threads:
            return
        with self._start_lock:
            if self._threads:
                return
            for index in range(self._max_workers):
                worker = threading.Thread(
                    target=self._work,
                    daemon=True,
                    name=f"talk-tool-{index + 1}",
                )
                worker.start()
                self._threads.append(worker)

    def submit(self, fn: Callable[..., Any], *args: Any) -> _ToolSubmission:
        self._ensure_started()
        future: concurrent.futures.Future = concurrent.futures.Future()
        submission = _ToolSubmission(future)
        job = (submission, contextvars.copy_context(), fn, args)
        try:
            self._jobs.put_nowait(job)
        except queue.Full as exc:
            raise ToolWorkerBusy("tool worker capacity is full") from exc
        return submission

    def _work(self) -> None:
        while True:
            submission, context, fn, args = self._jobs.get()
            future = submission.future
            try:
                if not future.set_running_or_notify_cancel():
                    continue
                submission.started.set()
                try:
                    future.set_result(context.run(fn, *args))
                except BaseException as exc:  # noqa: BLE001 — Future preserves it
                    future.set_exception(exc)
            finally:
                self._jobs.task_done()


_TOOL_POOL = _DaemonWorkerPool(
    max_workers=TOOL_WORKERS,
    max_pending=TOOL_PENDING_JOBS,
)


async def _run_on_daemon(fn: Callable[..., Any], *args: Any) -> Any:
    """Run blocking work on the bounded daemon pool.

    The pool preserves ``contextvars`` like :func:`asyncio.to_thread`, while its
    fixed daemon workers cannot hold process exit or grow after timeouts.
    """

    submission = _TOOL_POOL.submit(fn, *args)
    return await asyncio.wrap_future(submission.future)


async def run_bounded_on_daemon(
    fn: Callable[..., Any], *args: Any, timeout: float
) -> Any:
    """Run one tool with a bounded wait and report whether it actually started."""

    submission = _TOOL_POOL.submit(fn, *args)
    try:
        return await asyncio.wait_for(
            asyncio.wrap_future(submission.future), timeout=timeout
        )
    except TimeoutError as exc:
        raise ToolExecutionTimeout(
            started=submission.started.is_set() or submission.future.running()
        ) from exc


class RealtimeRelay:
    """Turns Realtime server events into the messages that answer them."""

    def __init__(
        self,
        *,
        on_audio: Callable[[bytes], None] | None = None,
        on_caption: Callable[[str], None] | None = None,
        on_transcript_turn: Callable[[str, str], None] | None = None,
        on_barge_in: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        tool_executor: Callable[[str, dict], str] | None = None,
        tool_authorizer: Callable[[str, dict], str | None] | None = None,
    ) -> None:
        self.on_audio = on_audio or _noop
        self.on_caption = on_caption or _noop
        self.on_transcript_turn = on_transcript_turn or _noop
        self.on_barge_in = on_barge_in or _noop
        self.on_error = on_error or _noop
        self.tool_executor = tool_executor or execute_talk_tool
        self.tool_authorizer = tool_authorizer
        self.session_id: str | None = None
        #: Item the model is currently speaking — the CLI needs it to truncate
        #: the server-side transcript at the point playback actually reached.
        self.last_audio_item_id: str | None = None
        #: Whether a response is in flight. Gates the barge-in cancel: sending
        #: response.cancel while the model is idle earns a "Cancellation
        #: failed: no active response found" error on EVERY operator turn
        #: (live-session finding).
        self.response_active: bool = False
        self._assistant_transcript: list[str] = []

    def handle_event(self, event: dict) -> list[dict]:
        """Handle one server event; return messages to send back."""

        handler = self._DISPATCH.get(str(event.get("type") or ""))
        if handler is None:
            return []
        return handler(self, event)

    async def handle_event_async(self, event: dict) -> list[dict]:
        """Handle one event without running a blocking tool on the caller's loop.

        Tool messages contain only their function output. The transport owns
        response-level batching: it preserves call order and appends exactly
        one ``response.create`` after ``response.done`` and every call settles.
        Other event types stay on the loop because their callbacks are immediate
        and transport-agnostic.
        """

        handler = self._DISPATCH.get(str(event.get("type") or ""))
        if handler is None:
            return []
        if handler is self._DISPATCH["response.function_call_arguments.done"]:
            try:
                messages = await run_bounded_on_daemon(
                    handler, self, event, timeout=TOOL_EXECUTION_WAIT_S
                )
                # The synchronous relay owns its continuation for legacy callers.
                # Async transports batch every tool call in one Realtime response
                # and append exactly one response.create after the batch settles.
                return [
                    message
                    for message in messages
                    if message.get("type") != "response.create"
                ]
            except ToolWorkerBusy:
                call_id = str(event.get("call_id") or "")
                name = str(event.get("name") or "tool")
                self._consume_tool_attempt(event)
                return self._function_output(
                    call_id,
                    f"Earlier tools are still running, so the {name} tool was not "
                    "started. Wait for them to finish, then ask me to try again.",
                    continue_response=False,
                )
            except ToolExecutionTimeout as exc:
                call_id = str(event.get("call_id") or "")
                name = str(event.get("name") or "tool")
                self._consume_tool_attempt(event)
                if exc.started:
                    output = (
                        f"The {name} tool is still running after "
                        f"{TOOL_EXECUTION_WAIT_S:g} seconds. I detached it so the "
                        "call can continue, but its eventual result won't return "
                        "to this conversation. Ask me to check again if you still need it."
                    )
                else:
                    output = (
                        f"The {name} tool did not start within "
                        f"{TOOL_EXECUTION_WAIT_S:g} seconds because earlier tools "
                        "were still using the workers. Ask me to try again."
                    )
                return self._function_output(call_id, output, continue_response=False)
        return handler(self, event)

    def tool_queue_full_output(self, event: dict) -> list[dict]:
        """Answer a tool event that the session's bounded queue could not admit."""

        call_id = str(event.get("call_id") or "")
        name = str(event.get("name") or "tool")
        # Rejection is a terminal execution attempt. Consume any trusted
        # single-use mutating permit so the same bound event cannot execute
        # later when queue capacity becomes available.
        self._consume_tool_attempt(event)
        return self._function_output(
            call_id,
            f"Earlier tools are still running, so the {name} tool was not started. "
            "Wait for them to finish, then ask me to try again.",
            continue_response=False,
        )

    def _consume_tool_attempt(self, event: dict) -> None:
        """Consume a bound call permit on any terminal non-execution path."""

        if self.tool_authorizer is not None:
            self.tool_authorizer(str(event.get("name") or "tool"), event)

    def discard_tool_event(self, event: dict) -> None:
        """Revoke a queued tool event that session teardown will never execute."""

        self._consume_tool_attempt(event)

    # -- event handlers -------------------------------------------------------

    def _on_session(self, event: dict) -> list[dict]:
        session = event.get("session")
        if isinstance(session, dict) and session.get("id"):
            self.session_id = str(session["id"])
        return []

    def _on_response_created(self, _event: dict) -> list[dict]:
        self.response_active = True
        return []

    def _on_speech_started(self, _event: dict) -> list[dict]:
        # Barge-in. The callback drains whatever is still queued locally
        # (always safe); the cancel goes out only when a response is actually
        # in flight — the model idling needs nothing cancelled.
        self.on_barge_in()
        if not self.response_active:
            return []
        return [dict(BARGE_IN_MESSAGE)]

    def _on_audio_delta(self, event: dict) -> list[dict]:
        item_id = event.get("item_id")
        if item_id:
            self.last_audio_item_id = str(item_id)
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            try:
                pcm = base64.b64decode(delta)
            except (ValueError, TypeError):
                self.on_error("I dropped a piece of audio there.")
                return []
            self.on_audio(pcm)
        return []

    def _on_transcript_delta(self, event: dict) -> list[dict]:
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            self._assistant_transcript.append(delta)
            self.on_caption(delta)
        return []

    def _on_input_transcript_done(self, event: dict) -> list[dict]:
        transcript = event.get("transcript")
        if isinstance(transcript, str) and transcript.strip():
            self.on_transcript_turn("user", transcript.strip())
        return []

    def _on_output_transcript_done(self, event: dict) -> list[dict]:
        completed = event.get("transcript")
        transcript = (
            completed
            if isinstance(completed, str)
            else "".join(self._assistant_transcript)
        )
        self._assistant_transcript.clear()
        if transcript.strip():
            self.on_transcript_turn("assistant", transcript.strip())
        return []

    def _on_function_call(self, event: dict) -> list[dict]:
        call_id = str(event.get("call_id") or "")
        name = str(event.get("name") or "")
        if self.tool_authorizer is not None:
            denial = self.tool_authorizer(name, event)
            if denial is not None:
                return self._function_output(call_id, denial)
        raw = event.get("arguments")
        try:
            arguments = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        except ValueError:
            return self._function_output(call_id, UNPARSEABLE_ARGS_TEXT)
        if not isinstance(arguments, dict):
            return self._function_output(call_id, UNPARSEABLE_ARGS_TEXT)
        try:
            output = self.tool_executor(name, arguments)
        except TalkToolError as exc:
            # Unknown tool: the model asked for something that does not exist.
            # It still gets an answer, so the turn completes instead of hanging.
            output = f"That tool isn't available: {exc}"
        return self._function_output(call_id, output)

    def _on_error(self, event: dict) -> list[dict]:
        error = event.get("error")
        detail = ""
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("type") or "")
        elif isinstance(error, str):
            detail = error
        # A cancel that lost the race with response.done is not an error the
        # operator can act on — the state gate makes it rare, this makes the
        # residue silent instead of a line of noise per turn.
        if "no active response" in detail.lower():
            return []
        self.on_error(
            f"Something went wrong on the call: {detail}" if detail
            else "Something went wrong on the call."
        )
        return []

    def _on_response_done(self, _event: dict) -> list[dict]:
        self.last_audio_item_id = None
        self.response_active = False
        return []

    @staticmethod
    def _function_output(
        call_id: str, output: str, *, continue_response: bool = True
    ) -> list[dict]:
        messages = [
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        ]
        if continue_response:
            messages.append({"type": "response.create"})
        return messages

    _DISPATCH: ClassVar[dict[str, Callable[[RealtimeRelay, dict], list[dict]]]] = {
        "session.created": _on_session,
        "session.updated": _on_session,
        "response.created": _on_response_created,
        "input_audio_buffer.speech_started": _on_speech_started,
        "response.output_audio.delta": _on_audio_delta,
        "response.output_audio_transcript.delta": _on_transcript_delta,
        "response.output_audio_transcript.done": _on_output_transcript_done,
        "conversation.item.input_audio_transcription.completed": _on_input_transcript_done,
        "response.function_call_arguments.done": _on_function_call,
        "response.done": _on_response_done,
        "error": _on_error,
    }


__all__ = [
    "BARGE_IN_MESSAGE",
    "TOOL_EXECUTION_WAIT_S",
    "UNPARSEABLE_ARGS_TEXT",
    "RealtimeRelay",
    "ToolExecutionTimeout",
    "ToolWorkerBusy",
    "run_bounded_on_daemon",
]
