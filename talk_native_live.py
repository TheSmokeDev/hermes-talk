"""Live provider events use captured fragments and the same canonical native task API."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass

try:
    from . import talk_realtime as rt
    from .talk_native_api import NativeTaskError
    from .talk_native_controller import NativeTaskController
except ImportError:
    import talk_realtime as rt
    from talk_native_api import NativeTaskError
    from talk_native_controller import NativeTaskController


@dataclass
class _Delegation:
    event: rt.DelegationRequested
    body: dict
    retired: bool = False
    finished: bool = False
    admitted: bool = False
    operation_id: str | None = None


class NativeLiveTaskController(NativeTaskController):
    """Preserve Live's fragment evidence without inventing legacy response identity."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.provider_session_id = getattr(self.session, "session_id", None)
        self.fragments = []
        self.captured_fragments = set()
        self.claimed_fragments = set()
        self.final_fragments = {}
        self.delegations = {}
        self.fragment_sequence = 0
        self.claimed_sequence = 0
        self.synthetic_sequence = 0
        self.synthetic_output = False
        self.synthetic_cutoff_ms = None
        self.transcript_lock = asyncio.Lock()
        self.transcript_tasks = set()
        self.capture_pending = {}
        self.capture_task = None
        self.capture_wake = asyncio.Event()
        self.capture_error = None
        self.capture_accepting = True
        self.seen_fragments = {}
        self.last_user_fragment_at = None
        self.provider_response_active = False
        self.pending_history = None
        self.pending_messages = deque()
        self.update_lock = asyncio.Lock()
        self.no_input_proposals = 0

    CAPTURE_DELAY = 0.1
    CAPTURE_COUNT = 32
    CAPTURE_BYTES = 8192
    CAPTURE_PENDING_COUNT = 4096
    CAPTURE_PENDING_BYTES = 256 * 1024
    CAPTURE_RETRIES = (0.1, 0.25)
    OPERATION_POLL_S = 0.25
    OPERATION_TIMEOUT_S = 75

    def _queue_capture(self, fragment):
        if not self.capture_accepting:
            raise NativeTaskError("Transcript capture is closing")
        identity = fragment["event_id"]
        if identity not in self.capture_pending:
            if (
                len(self.capture_pending) >= self.CAPTURE_PENDING_COUNT
                or sum(len(json.dumps(row).encode()) for row in self.capture_pending.values())
                + len(json.dumps(fragment).encode())
                > self.CAPTURE_PENDING_BYTES
            ):
                raise NativeTaskError(
                    "Live transcript queue is full; unsaved input remains pending"
                )
            self.capture_pending[identity] = dict(fragment)
        if fragment.get("final") or len(self.capture_pending) >= self.CAPTURE_COUNT:
            self.capture_wake.set()
        self._start_capture()

    def _start_capture(self):
        if not self.capture_pending or self.capture_error is not None:
            return
        if self.capture_task is None or self.capture_task.done():
            self.capture_task = asyncio.create_task(self._capture_writer())
            self.tasks.add(self.capture_task)
            self.transcript_tasks.add(self.capture_task)
            self.capture_task.add_done_callback(self.tasks.discard)
            self.capture_task.add_done_callback(self.transcript_tasks.discard)

    async def _capture_writer(self):
        async with self.transcript_lock:
            while self.capture_pending and self.current:
                if not self.capture_wake.is_set():
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self.capture_wake.wait(), self.CAPTURE_DELAY)
                self.capture_wake.clear()
                batch, size = [], 0
                for fragment in self.capture_pending.values():
                    amount = len(json.dumps(fragment, ensure_ascii=False).encode())
                    if batch and (
                        len(batch) >= self.CAPTURE_COUNT or size + amount > self.CAPTURE_BYTES
                    ):
                        break
                    batch.append(dict(fragment))
                    size += amount
                body = {"provider_session_id": self.provider_session_id, "fragments": batch}
                for attempt in range(len(self.CAPTURE_RETRIES) + 1):
                    try:
                        await self.request("/live/transcript", body)
                        break
                    except NativeTaskError as exc:
                        if not exc.retryable or attempt == len(self.CAPTURE_RETRIES):
                            self.capture_error = exc
                            self.notice(
                                {
                                    "state": "capture_pending",
                                    "reason": str(exc),
                                    "category": exc.category,
                                    "count": len(self.capture_pending),
                                }
                            )
                            return
                        await asyncio.sleep(self.CAPTURE_RETRIES[attempt])
                for fragment in batch:
                    self.capture_pending.pop(fragment["event_id"], None)
                    self.captured_fragments.add(fragment["event_id"])
                self._prune_fragments()
                if len(self.capture_pending) >= self.CAPTURE_COUNT:
                    self.capture_wake.set()

    async def flush_captures(self, *, retry=False):
        if retry and self.capture_error is not None and self.capture_error.retryable:
            self.capture_error = None
        self.capture_wake.set()
        self._start_capture()
        pending = self.capture_task
        if pending is not None and pending is not asyncio.current_task():
            await asyncio.shield(pending)
        if self.capture_error is not None:
            raise self.capture_error

    async def _capture(self, fragment):
        self._queue_capture(fragment)
        await self.flush_captures()
        return {"ok": True}

    def _prune_fragments(self):
        retained = []
        for sequence, fragment in self.fragments:
            identity = fragment["event_id"]
            if (
                identity in self.claimed_fragments or fragment["role"] == "assistant"
            ) and identity in self.captured_fragments:
                self.captured_fragments.discard(identity)
                self.claimed_fragments.discard(identity)
            else:
                retained.append((sequence, fragment))
        self.fragments = retained
        self.captured_fragments.intersection_update(
            fragment["event_id"] for _, fragment in retained
        )

    def _background(self, coroutine, *, transcript=False):
        async def guarded():
            try:
                await coroutine
            except NativeTaskError as exc:
                self.notice({"state": "refused", "reason": str(exc), "category": exc.category})

        task = asyncio.create_task(guarded())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        if transcript:
            self.transcript_tasks.add(task)
            task.add_done_callback(self.transcript_tasks.discard)

    async def typed(self, text, *, input_id=None):
        self.guard()
        if self.provider_session_id is None:
            raise NativeTaskError("Provider session identity is not available yet")
        if not isinstance(text, str) or not text.strip() or len(text) > 65536:
            raise NativeTaskError("Typed input must be bounded non-empty text")
        await self.interrupt()
        input_id = input_id or "input_" + uuid.uuid4().hex
        result = await self.request(
            "/live/typed",
            {
                "provider_session_id": self.provider_session_id,
                "input_id": input_id,
                "text": text,
                "admission": "async",
            },
        )
        result = await self._operation_result(result)
        if not isinstance(result.get("output"), str):
            raise NativeTaskError("Typed input returned no canonical decision receipt")
        self.notice(
            {"state": "tool_receipt", "output": result["output"], "action": result.get("action")}
        )
        selection = result.get("selection")
        if selection and self.on_selection is not None and await self.on_selection(selection):
            return result
        if getattr(self.session, "supports_live_context", True):
            if len(self.pending_messages) >= 16:
                raise NativeTaskError("Voice update queue is full; the canonical result is saved")
            self.pending_messages.append(rt.AppendLiveContext(result["output"], kind="message"))
            await self._flush_updates()
        else:
            self.notice({"state": "text_only", "reason": "provider_context_append_unsupported"})
        return result

    def _remember_fragment(self, fragment):
        if self.provider_session_id is None:
            raise NativeTaskError("Live provider has not supplied its session identity")
        self._prune_fragments()
        if (
            len(self.fragments) >= 4096
            or sum(len(row["text"].encode()) for _, row in self.fragments)
            + len(fragment["text"].encode())
            > 60000
        ):
            raise NativeTaskError(
                "Live input window is full; captured input remains in task history"
            )
        self.fragment_sequence += 1
        self.fragments.append((self.fragment_sequence, fragment))

    async def _transcript(self, event):
        if not event.text:
            return
        role = "user" if event.provenance is rt.TranscriptProvenance.INPUT_AUDIO else "assistant"
        if role == "assistant" and self.synthetic_output:
            # Timing-free Live output cannot prove that it belongs to a new real turn.
            if (
                self.synthetic_cutoff_ms is None
                or event.start_ms is None
                or event.start_ms < self.synthetic_cutoff_ms
            ):
                synthetic = True
            else:
                self.synthetic_output = False
                synthetic = False
        else:
            synthetic = False
        final_key = (role, event.item_id) if event.final and event.item_id else None
        if final_key is not None and final_key in self.final_fragments:
            prior = self.final_fragments[final_key]
            if (prior["text"], prior.get("start_ms"), prior.get("end_ms")) != (
                event.text,
                event.start_ms,
                event.end_ms,
            ):
                raise NativeTaskError("Final Live transcript changed for its provider item")
            self._queue_capture(prior)
            return
        event_identity = getattr(event, "event_id", None)
        fragment = {
            "event_id": (
                "fragment_"
                + uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    self.provider_session_id + ":" + event_identity,
                ).hex
            )
            if event_identity
            else "fragment_" + uuid.uuid4().hex,
            "role": role,
            "text": event.text,
            "final": event.final,
        }
        item_id = getattr(event, "item_id", None)
        finality = getattr(event, "finality", None)
        if item_id is not None:
            fragment["item_id"] = item_id
        if finality is not None:
            fragment["finality"] = finality
        if synthetic:
            fragment["synthetic"] = True
        for name in ("start_ms", "end_ms"):
            value = getattr(event, name)
            if value is not None:
                fragment[name] = value
        previous = self.seen_fragments.get(fragment["event_id"])
        if previous is not None:
            if previous != fragment:
                raise NativeTaskError(
                    "Live transcript identity changed during retry", category="validation"
                )
            return
        self.seen_fragments[fragment["event_id"]] = dict(fragment)
        if len(self.seen_fragments) > 8192:
            self.seen_fragments.pop(next(iter(self.seen_fragments)))
        self._remember_fragment(fragment)
        if role == "user":
            self.last_user_fragment_at = self.clock()
        if final_key is not None:
            self.final_fragments[final_key] = fragment
        if role == "user" and self.synthetic_output and event.end_ms is not None:
            self.synthetic_cutoff_ms = event.end_ms
        self._queue_capture(fragment)
        if self.on_caption is not None:
            self.on_caption(event)

    async def _delegate(self, event):
        if event.target != "client":
            raise NativeTaskError("Live delegated to an unsupported execution target")
        prior = self.delegations.get(event.delegation_id)
        if prior is not None:
            if prior.event != event:
                raise NativeTaskError("Live delegation identity changed during retry")
            return
        eligible = []
        for sequence, fragment in self.fragments:
            if fragment["event_id"] in self.claimed_fragments:
                continue
            if (
                event.offset_ms is not None
                and fragment["role"] == "user"
                and fragment.get("end_ms") is not None
                and fragment["end_ms"] > event.offset_ms
            ):
                continue
            eligible.append((sequence, fragment))
        originals = [fragment for _, fragment in eligible if fragment["role"] == "user"]
        if not originals:
            self.no_input_proposals += 1
            if self.no_input_proposals > 3:
                raise NativeTaskError("Provider repeated tool proposals without new operator input")
            self.delegations[event.delegation_id] = _Delegation(event, {}, finished=True)
            self.notice({"state": "refused", "reason": "delegation_has_no_new_operator_input"})
            await self._send_synthetic(
                [
                    rt.SubmitDelegationResult(
                        event.delegation_id,
                        "No new operator input was captured. No action or approval was authorized. "
                        "Continue listening; do not propose another tool "
                        "until the operator speaks.",
                    )
                ],
                source_sequence=self.claimed_sequence,
            )
            return
        self.no_input_proposals = 0
        frozen = [dict(fragment) for _, fragment in eligible]
        body = {
            "provider_session_id": self.provider_session_id,
            "delegation_id": event.delegation_id,
            "offset_ms": event.offset_ms,
            "fragments": frozen,
            "admission": "async",
        }
        if event.prompt is not None:
            body["prompt"] = event.prompt
        call = _Delegation(event, body)
        self.delegations[event.delegation_id] = call
        self.claimed_sequence = max(self.claimed_sequence, eligible[-1][0])
        self.claimed_fragments.update(fragment["event_id"] for _, fragment in eligible)
        self._background(self._dispatch_live(call))

    async def _operation_result(self, result, call=None):
        operation_id = result.get("operation_id")
        if operation_id is None:
            return result
        if not isinstance(operation_id, str) or not operation_id:
            raise NativeTaskError("Live operation receipt has no identity")
        if call is not None:
            call.operation_id, call.admitted = operation_id, True
        self.notice({"state": "operation_accepted", "operation_id": operation_id})
        deadline = asyncio.get_running_loop().time() + self.OPERATION_TIMEOUT_S
        failures = 0
        while result.get("pending") is True:
            if asyncio.get_running_loop().time() >= deadline:
                raise NativeTaskError(
                    "Live operation remains pending; inspect its existing receipt"
                )
            await asyncio.sleep(self.OPERATION_POLL_S)
            try:
                self.guard()
                result = await self.api.request(
                    "/live/operation",
                    method="GET",
                    params={"operation_id": operation_id},
                    expected_context=self.context,
                )
                self.guard()
                failures = 0
            except NativeTaskError as exc:
                failures += 1
                if not exc.retryable or failures >= 3:
                    raise
                continue
            if result.get("operation_id") != operation_id:
                raise NativeTaskError("Live operation response belongs to another request")
        value = result.get("result")
        if not isinstance(value, dict):
            raise NativeTaskError("Live operation has no decision receipt")
        return value

    async def _dispatch_live(self, call):
        try:
            await self.flush_captures()
            result = await self.request("/live/delegation", call.body)
            result = await self._operation_result(result, call)
        except NativeTaskError:
            call.finished = True
            raise
        if not isinstance(result.get("output"), str):
            raise NativeTaskError("Live delegation returned no canonical decision receipt")
        self.notice(
            {"state": "tool_receipt", "output": result["output"], "action": result.get("action")}
        )
        call.finished = True
        for fragment in call.body["fragments"]:
            self.captured_fragments.add(fragment["event_id"])
        self._prune_fragments()
        if call.retired or not self.current:
            return
        selection = result.get("selection")
        if selection and self.on_selection is not None and await self.on_selection(selection):
            return
        commands = []
        if (
            getattr(self.session, "supports_live_context", True)
            and isinstance(result.get("context"), str)
            and result["context"]
        ):
            commands.append(
                rt.AppendLiveContext(
                    result["context"], kind="context", delegation_id=call.event.delegation_id
                )
            )
        commands.append(
            rt.SubmitDelegationResult(
                call.event.delegation_id, result["output"], kind=result.get("kind", "commentary")
            )
        )
        await self._send_synthetic(commands, source_sequence=self.claimed_sequence)

    async def handle(self, event):
        self.guard()
        if isinstance(event, rt.SessionReady):
            if self.provider_session_id not in {None, event.session_id}:
                raise NativeTaskError("Live provider session identity changed without reattachment")
            self.provider_session_id = event.session_id
        elif isinstance(event, rt.Transcript):
            await self._transcript(event)
        elif isinstance(event, rt.DelegationRequested):
            await self._delegate(event)
        elif isinstance(event, rt.DelegationRetired):
            if event.delegation_id in self.delegations:
                self.delegations[event.delegation_id].retired = True
        elif isinstance(event, rt.OutputInterrupted):
            self.audio.drain_playback()
            self.provider_response_active = False
            self.presentation = None
        elif isinstance(event, rt.OutputTurnCompleted):
            self.provider_response_active = False
        elif isinstance(event, rt.OutputAudio):
            if self.service_paused:
                return
            self.spoken_item = event.item_id
            self.provider_response_active = True
            self.audio.queue_playback(event.data)
        elif isinstance(event, rt.SpeechStarted):
            self.operator_speaking = True
            self.speech_started_at = self.clock()
            await self.interrupt()
        elif isinstance(event, rt.SpeechStopped):
            self.operator_speaking = False
        elif isinstance(event, rt.FunctionCall):
            self.notice({"state": "refused", "reason": "live_function_calls_are_not_authority"})
        elif isinstance(event, rt.ProviderFailure) and event.terminal:
            raise NativeTaskError("Live provider failed; canonical task work remains owned")
        elif isinstance(event, rt.SessionTerminated) and event.state is rt.SessionState.FAILED:
            raise NativeTaskError("Live provider disconnected; canonical task work remains owned")

    async def interrupt(self, *, presentation_only=False):
        self.guard()
        self.audio.drain_playback()
        if self.provider_response_active:
            await self.send([rt.CancelResponse()])
        self.provider_response_active = False
        self.presentation = None

    def timing(self):
        self.sequence += 1
        return {
            "sequence": self.sequence,
            "operator_speaking": self.operator_speaking
            or (
                self.last_input_activity is not None
                and self.clock() - self.last_input_activity < 0.7
            ),
            "playback_active": bool(getattr(self.audio, "playback_pending", True)),
            "response_pending": self.provider_response_active,
            "input_pending": self.last_user_fragment_at is not None
            and self.clock() - self.last_user_fragment_at < 0.7
            and any(
                fragment["event_id"] not in self.claimed_fragments and fragment["role"] == "user"
                for _, fragment in self.fragments
            ),
            "tools_pending": any(
                not value.finished and not value.retired and not value.admitted
                for value in self.delegations.values()
            ),
        }

    async def tick(self):
        if self.capture_error is not None and not self.capture_error.retryable:
            raise self.capture_error
        await super().tick()
        if not getattr(self.session, "emits_output_lifecycle", False) and not getattr(
            self.audio, "playback_pending", True
        ):
            self.provider_response_active = False
        await self._flush_updates()

    def _quiet(self):
        return not self.service_paused and not any(
            value for key, value in self.timing().items() if key != "sequence"
        )

    async def _send_synthetic(self, commands, *, quiet=False, source_sequence=None):
        async with self.send_lock:
            self.guard()
            if quiet and not self._quiet():
                return False
            self.synthetic_sequence = max(
                self.synthetic_sequence,
                self.fragment_sequence if source_sequence is None else source_sequence,
            )
            self.synthetic_output = True
            self.synthetic_cutoff_ms = None
            self._prune_fragments()
            if getattr(self.session, "emits_output_lifecycle", False) and any(
                isinstance(command, rt.SubmitDelegationResult)
                or (
                    isinstance(command, rt.AppendLiveContext)
                    and (
                        command.kind == "message"
                        or not getattr(self.session, "supports_silent_live_context", True)
                    )
                )
                for command in commands
            ):
                self.provider_response_active = True
            await self.session.send(tuple(commands))
            self.guard()
            return True

    async def _flush_updates(self):
        async with self.update_lock:
            self.guard()
            if not self._quiet():
                return
            if self.pending_messages:
                command = self.pending_messages.popleft()
                if not await self._send_synthetic([command], quiet=True):
                    self.pending_messages.appendleft(command)
            elif self.pending_history is not None:
                text, self.pending_history = self.pending_history, None
                if (
                    not await self._send_synthetic(
                        [rt.AppendLiveContext(text, kind="context")], quiet=True
                    )
                    and self.pending_history is None
                ):
                    self.pending_history = text

    async def _send_history_context(self, text):
        if getattr(self.session, "supports_live_context", True):
            self.pending_history = text
            await self._flush_updates()
        else:
            self.notice({"state": "text_only", "reason": "provider_context_append_unsupported"})

    async def _speak(self, candidate):
        if not getattr(self.session, "supports_live_context", True):
            self.notice({"state": "text_only", "reason": "provider_summary_speech_unsupported"})
            return
        speech = await self.request(
            "/live/speech",
            {
                "event_id": candidate["event_id"],
                "timing": self.timing(),
            },
        )
        if speech.get("speak") is not True:
            return
        commentary = speech.get("content")
        if not isinstance(commentary, str) or not commentary.strip() or len(commentary) > 4000:
            raise NativeTaskError("Live summary has no bounded server commentary")
        receipt = {"event_id": speech["event_id"], "attempt_id": speech["attempt_id"]}
        if any(value for key, value in self.timing().items() if key != "sequence"):
            await self.request("/speech/receipt", {**receipt, "state": "deferred"})
            return
        if speech.get("result") is not None and self.on_result is not None:
            self.on_result(speech["result"])
        sent = await self._send_synthetic(
            [rt.AppendLiveContext(commentary, kind="message")], quiet=True
        )
        await self.request("/speech/receipt", {**receipt, "state": "sent" if sent else "deferred"})

    async def drain(self):
        await self.flush_captures()
        await super().drain()

    async def close(self):
        if self.closed:
            return
        if self.current:
            try:
                await asyncio.wait_for(self.flush_captures(retry=True), 2)
            except (NativeTaskError, TimeoutError):
                self.notice({"state": "capture_incomplete", "count": len(self.capture_pending)})
        self.capture_accepting = False
        with suppress(NativeTaskError):
            if self.current:
                await self.interrupt()
        await super().close()


__all__ = ["NativeLiveTaskController"]
