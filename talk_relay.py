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
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

try:
    from . import talk_realtime as rt
    from .talk_tools import TalkToolError, execute_talk_tool
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_realtime as rt
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

#: How many settled response ids the relay remembers. Late deltas arrive within
#: seconds of a cancel, so a short memory is enough — and a long call must not
#: grow the ledger without bound.
MAX_SETTLED_RESPONSES = 32


def _noop(*_args) -> None:
    """Default callback: the relay works with no consumers attached."""


def _event_response_id(event: dict) -> str | None:
    """The response id an audio or transcript delta carries, if any."""

    response_id = event.get("response_id")
    return str(response_id) if response_id else None


def _nested_response_id(event: dict) -> str | None:
    """The response id on a lifecycle event, which nests it under ``response``."""

    response = event.get("response")
    if isinstance(response, dict) and response.get("id"):
        return str(response["id"])
    return None


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
        #: Only a response allowed to speak may write it, so a cancelled
        #: response's tail audio can no longer redirect the next truncate.
        self.last_audio_item_id: str | None = None
        # Response identity, as three named states rather than one boolean.
        # A cancelled response keeps emitting audio and transcript deltas —
        # OpenAI documents this — so "is anything in flight" cannot decide
        # whether a given delta may be spoken. Only its response_id can.
        #: The server has a response open. Gates the barge-in cancel: sending
        #: response.cancel while the model is idle earns a "Cancellation
        #: failed: no active response found" error on EVERY operator turn
        #: (live-session finding). Stays true from response.created until the
        #: matching terminal event, cancelled or not — the server is not done
        #: with a response merely because we asked it to stop.
        self._response_in_flight: bool = False
        #: Which response may speak. None means the provider did not name one,
        #: which is treated as "anything may speak" rather than silence.
        self._active_response_id: str | None = None
        #: Responses already cancelled or finished. Their late deltas are
        #: recognised as a tail and dropped instead of played over the next
        #: answer. Bounded; oldest evicted.
        self._settled_response_ids: OrderedDict[str, None] = OrderedDict()
        #: Mirrors ``_settled_response_ids`` for the unnamed case: an unnamed
        #: response has no id to remember, so its "already settled" state is
        #: this one flag instead of a ledger entry. Cleared on the next start.
        self._unnamed_response_cancelled: bool = False
        self._assistant_transcript: list[str] = []

    @property
    def response_active(self) -> bool:
        """Whether the server still has a response open.

        Read-only: the CLI polls this to gate announcements and the barge-in
        cancel, and the ledger below is the only thing allowed to move it.
        """

        return self._response_in_flight

    # -- response identity ----------------------------------------------------

    def _belongs_to_active(self, response_id: str | None) -> bool:
        """Whether events carrying ``response_id`` may still be spoken.

        Ambiguity fails open. An event the provider did not stamp, or one that
        arrives before any response was named, is played rather than dropped:
        muting real speech is the worse failure, and the fence only has to be
        exact about the case it exists for — a response known to be settled.
        An unnamed response can still be settled, though: once a barge-in has
        cancelled it, ``_unnamed_response_cancelled`` closes that same fence
        for its own tail, the same way a named response's id closes it.
        """

        if response_id is None:
            return not self._unnamed_response_cancelled
        if response_id in self._settled_response_ids:
            return False
        return self._active_response_id is None or response_id == self._active_response_id

    def _settle(self, response_id: str) -> None:
        """Retire a response so its late deltas stop counting as new speech."""

        if response_id in self._settled_response_ids:
            return
        self._settled_response_ids[response_id] = None
        if len(self._settled_response_ids) > MAX_SETTLED_RESPONSES:
            self._settled_response_ids.popitem(last=False)

    def _start_response(self, response_id: str | None) -> None:
        """Open a response, ignoring a start that replays a settled one.

        A replayed response.created must not be allowed to hand the microphone
        back to a response that was cancelled two turns ago. Nor may an
        unnamed start overwrite the identity of a response that is already
        known and still open — that would blind the ledger to a live
        response's own id and disable its fencing.
        """

        if response_id is not None and response_id in self._settled_response_ids:
            return
        if (
            response_id is None
            and self._response_in_flight
            and self._active_response_id is not None
        ):
            return  # an unnamed start cannot un-name an already-identified response
        self._unnamed_response_cancelled = False
        self._response_in_flight = True
        self._active_response_id = response_id

    def _cancel_active_response(self) -> bool:
        """Settle the response a barge-in interrupts; False if there is none.

        The response stays *in flight* — the server has not acknowledged the
        cancel, and announcing over it would still collide. If the response
        was named, it is settled, so nothing it emits from here on is played
        or transcribed. An unnamed response has no id to settle, so this sets
        ``_unnamed_response_cancelled`` instead — the same guarantee, and the
        same "one cancellation per response" idempotency, without an id to
        hang it on. The partial transcript goes with it rather than being
        folded into the next answer.
        """

        if not self._response_in_flight:
            return False
        if self._active_response_id is not None:
            if self._active_response_id in self._settled_response_ids:
                return False  # already cancelled: one cancellation per response
            self._settle(self._active_response_id)
        elif self._unnamed_response_cancelled:
            return False  # already cancelled: same guarantee for an unnamed response
        else:
            self._unnamed_response_cancelled = True
        self._assistant_transcript.clear()
        return True

    def _finish_response(self, response_id: str | None) -> None:
        """Close out a terminal event, ignoring one that lands too late.

        A terminal replayed after the next response has started names a
        response that is no longer active; it must close nothing. Anything the
        relay cannot positively attribute to some *other* response closes the
        current one, so an unnamed terminal never wedges the session open.
        """

        if response_id is not None:
            self._settle(response_id)
        if (
            response_id is not None
            and self._active_response_id is not None
            and response_id != self._active_response_id
        ):
            return
        self._response_in_flight = False
        self._active_response_id = None
        self._unnamed_response_cancelled = False
        self.last_audio_item_id = None
        self._assistant_transcript.clear()

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

    def handle_realtime_event(self, event: rt.RealtimeEvent) -> list[rt.RealtimeCommand]:
        """Handle one provider-neutral event on the transport loop."""

        if isinstance(event, rt.SessionReady):
            self.session_id = event.session_id
        elif isinstance(event, rt.ResponseStarted):
            self._start_response(event.response_id)
        elif isinstance(event, rt.SpeechStarted):
            self.on_barge_in()
            if self._cancel_active_response():
                return [rt.CancelResponse()]
        elif isinstance(event, rt.OutputAudio):
            if not self._belongs_to_active(event.response_id):
                return []  # tail audio from a cancelled or superseded response
            if event.item_id is not None:
                self.last_audio_item_id = event.item_id
            self.on_audio(event.data)
        elif isinstance(event, rt.Transcript):
            if event.provenance is rt.TranscriptProvenance.INPUT_AUDIO:
                if event.final and event.text.strip():
                    self.on_transcript_turn(rt.TranscriptRole.USER.value, event.text.strip())
            elif not self._belongs_to_active(event.response_id):
                pass  # interrupted text, arriving after its response was settled
            elif event.final:
                transcript = event.text or "".join(self._assistant_transcript)
                self._assistant_transcript.clear()
                if transcript.strip():
                    self.on_transcript_turn(
                        rt.TranscriptRole.ASSISTANT.value, transcript.strip()
                    )
            elif event.text:
                self._assistant_transcript.append(event.text)
                self.on_caption(event.text)
        elif isinstance(event, rt.ResponseFinished):
            self._finish_response(event.response_id)
        elif isinstance(event, rt.ProviderFailure):
            self._report_error(event.detail)
        return []

    async def handle_tool_call_async(self, event: dict) -> list[rt.RealtimeCommand]:
        """Execute one neutral function-call event off the transport loop."""

        try:
            messages = await run_bounded_on_daemon(
                self._neutral_function_call, event, timeout=TOOL_EXECUTION_WAIT_S
            )
            return [
                message for message in messages if not isinstance(message, rt.StartResponse)
            ]
        except ToolWorkerBusy:
            call_id = str(event.get("call_id") or "")
            name = str(event.get("name") or "tool")
            self._consume_tool_attempt(event)
            return self._neutral_function_output(
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
            return self._neutral_function_output(
                call_id, output, continue_response=False
            )

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

    def tool_queue_full_commands(self, event: dict) -> list[rt.RealtimeCommand]:
        """Neutral equivalent of :meth:`tool_queue_full_output`."""

        call_id = str(event.get("call_id") or "")
        name = str(event.get("name") or "tool")
        self._consume_tool_attempt(event)
        return self._neutral_function_output(
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

    def _on_response_created(self, event: dict) -> list[dict]:
        self._start_response(_nested_response_id(event))
        return []

    def _on_speech_started(self, _event: dict) -> list[dict]:
        # Barge-in. The callback drains whatever is still queued locally
        # (always safe); the cancel goes out only when a response is actually
        # in flight and not already cancelled — the model idling needs nothing
        # cancelled, and a second cancel for the same response is noise.
        self.on_barge_in()
        if not self._cancel_active_response():
            return []
        return [dict(BARGE_IN_MESSAGE)]

    def _on_audio_delta(self, event: dict) -> list[dict]:
        if not self._belongs_to_active(_event_response_id(event)):
            return []  # tail audio from a cancelled or superseded response
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
        if not self._belongs_to_active(_event_response_id(event)):
            return []  # interrupted text, arriving after its response settled
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
        if not self._belongs_to_active(_event_response_id(event)):
            return []  # interrupted text, arriving after its response settled
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

    def _tool_result(self, event: dict) -> tuple[str, str]:
        call_id = str(event.get("call_id") or "")
        name = str(event.get("name") or "")
        if self.tool_authorizer is not None:
            denial = self.tool_authorizer(name, event)
            if denial is not None:
                return call_id, denial
        raw = event.get("arguments")
        try:
            arguments = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        except ValueError:
            return call_id, UNPARSEABLE_ARGS_TEXT
        if not isinstance(arguments, dict):
            return call_id, UNPARSEABLE_ARGS_TEXT
        try:
            output = self.tool_executor(name, arguments)
        except TalkToolError as exc:
            # Unknown tool: the model asked for something that does not exist.
            # It still gets an answer, so the turn completes instead of hanging.
            output = f"That tool isn't available: {exc}"
        return call_id, output

    def _on_function_call(self, event: dict) -> list[dict]:
        call_id, output = self._tool_result(event)
        return self._function_output(call_id, output)

    def _neutral_function_call(self, event: dict) -> list[rt.RealtimeCommand]:
        call_id, output = self._tool_result(event)
        return self._neutral_function_output(call_id, output)

    def _on_error(self, event: dict) -> list[dict]:
        error = event.get("error")
        detail = ""
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("type") or "")
        elif isinstance(error, str):
            detail = error
        self._report_error(detail)
        return []

    def _report_error(self, detail: str) -> None:
        # A cancel that lost the race with response completion is not an error
        # the operator can act on.
        if "no active response" in detail.lower():
            return
        self.on_error(
            f"Something went wrong on the call: {detail}"
            if detail
            else "Something went wrong on the call."
        )

    def _on_response_done(self, event: dict) -> list[dict]:
        self._finish_response(_nested_response_id(event))
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

    @staticmethod
    def _neutral_function_output(
        call_id: str, output: str, *, continue_response: bool = True
    ) -> list[rt.RealtimeCommand]:
        messages: list[rt.RealtimeCommand] = [
            rt.SubmitToolResult(call_id=call_id, output=output)
        ]
        if continue_response:
            messages.append(rt.StartResponse())
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
    "MAX_SETTLED_RESPONSES",
    "TOOL_EXECUTION_WAIT_S",
    "UNPARSEABLE_ARGS_TEXT",
    "RealtimeRelay",
    "ToolExecutionTimeout",
    "ToolWorkerBusy",
    "run_bounded_on_daemon",
]
