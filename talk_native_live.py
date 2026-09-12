"""Live provider events use captured fragments and the same canonical native task API."""

from __future__ import annotations

import asyncio
import uuid
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


class NativeLiveTaskController(NativeTaskController):
    """Preserve Live's fragment evidence without inventing legacy response identity."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.provider_session_id = getattr(self.session, "session_id", None)
        self.fragments = []
        self.captured_fragments = set()
        self.final_fragments = {}
        self.delegations = {}
        self.fragment_sequence = 0
        self.claimed_sequence = 0
        self.synthetic_sequence = 0
        self.synthetic_output = False
        self.synthetic_cutoff_ms = None
        self.transcript_lock = asyncio.Lock()
        self.transcript_tasks = set()
        self.provider_response_active = False

    async def _capture(self, fragment):
        async with self.transcript_lock:
            result = await self.request(
                "/live/transcript",
                {
                    "provider_session_id": self.provider_session_id,
                    "fragments": [fragment],
                },
            )
            self.captured_fragments.add(fragment["event_id"])
            self._prune_fragments()
            return result

    def _prune_fragments(self):
        cutoff = max(self.claimed_sequence, self.synthetic_sequence)
        retained = []
        for sequence, fragment in self.fragments:
            if sequence <= cutoff and fragment["event_id"] in self.captured_fragments:
                self.captured_fragments.discard(fragment["event_id"])
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
                self.notice({"state": "refused", "reason": str(exc)})

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
            },
        )
        if not isinstance(result.get("output"), str):
            raise NativeTaskError("Typed input returned no canonical decision receipt")
        self.notice(
            {"state": "tool_receipt", "output": result["output"], "action": result.get("action")}
        )
        selection = result.get("selection")
        if selection and self.on_selection is not None and await self.on_selection(selection):
            return result
        self.synthetic_output = True
        self.synthetic_cutoff_ms = None
        if getattr(self.session, "supports_live_context", True):
            await self.send([rt.AppendLiveContext(result["output"], kind="message")])
        else:
            self.notice({"state": "text_only", "reason": "provider_context_append_unsupported"})
        return result

    def _remember_fragment(self, fragment):
        if self.provider_session_id is None:
            raise NativeTaskError("Live provider has not supplied its session identity")
        self._prune_fragments()
        if len(self.fragments) >= 256:
            raise NativeTaskError("Live transcript capacity reached; reconnect the selected task")
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
            self._background(self._capture(prior), transcript=True)
            return
        fragment = {
            "event_id": "fragment_" + uuid.uuid4().hex,
            "role": role,
            "text": event.text,
            "final": event.final,
        }
        if synthetic:
            fragment["synthetic"] = True
        for name in ("start_ms", "end_ms"):
            value = getattr(event, name)
            if value is not None:
                fragment[name] = value
        self._remember_fragment(fragment)
        if final_key is not None:
            self.final_fragments[final_key] = fragment
        if role == "user" and self.synthetic_output and event.end_ms is not None:
            self.synthetic_cutoff_ms = event.end_ms
        self._background(self._capture(fragment), transcript=True)
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
        cutoff = max(self.claimed_sequence, self.synthetic_sequence)
        eligible = []
        for sequence, fragment in self.fragments:
            if sequence <= cutoff:
                continue
            if (
                event.offset_ms is not None
                and fragment["role"] == "user"
                and fragment.get("end_ms") is not None
                and fragment["end_ms"] > event.offset_ms
            ):
                break
            eligible.append((sequence, fragment))
        originals = [fragment for _, fragment in eligible if fragment["role"] == "user"]
        if not originals:
            self.notice({"state": "refused", "reason": "delegation_has_no_new_operator_input"})
            return
        frozen = [dict(fragment) for _, fragment in eligible]
        body = {
            "provider_session_id": self.provider_session_id,
            "delegation_id": event.delegation_id,
            "offset_ms": event.offset_ms,
            "fragments": frozen,
        }
        if event.prompt is not None:
            body["prompt"] = event.prompt
        call = _Delegation(event, body)
        self.delegations[event.delegation_id] = call
        self.claimed_sequence = eligible[-1][0]
        self._background(self._dispatch_live(call))

    async def _dispatch_live(self, call):
        result = await self.request("/live/delegation", call.body)
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
        commands = [
            rt.SubmitDelegationResult(
                call.event.delegation_id, result["output"], kind=result.get("kind", "commentary")
            )
        ]
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
        self.synthetic_output = True
        self.synthetic_cutoff_ms = None
        await self.send(commands)

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
        elif isinstance(event, rt.OutputAudio):
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
            "operator_speaking": self.operator_speaking,
            "playback_active": bool(getattr(self.audio, "playback_pending", True)),
            "response_pending": self.provider_response_active,
            "input_pending": bool(self.transcript_tasks)
            or any(
                sequence > max(self.claimed_sequence, self.synthetic_sequence)
                and fragment["role"] == "user"
                for sequence, fragment in self.fragments
            ),
            "tools_pending": any(
                not value.finished and not value.retired for value in self.delegations.values()
            ),
        }

    async def tick(self):
        await super().tick()
        if not getattr(self.audio, "playback_pending", True):
            self.provider_response_active = False

    async def _send_history_context(self, text):
        if getattr(self.session, "supports_live_context", True):
            await self.send([rt.AppendLiveContext(text, kind="context")])
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
        self.synthetic_sequence = self.fragment_sequence
        self.synthetic_output = True
        self.synthetic_cutoff_ms = None
        if speech.get("result") is not None and self.on_result is not None:
            self.on_result(speech["result"])
        await self.send([rt.AppendLiveContext(commentary, kind="message")])
        await self.request("/speech/receipt", {**receipt, "state": "sent"})

    async def close(self):
        with suppress(NativeTaskError):
            if self.current:
                await self.interrupt()
        await super().close()


__all__ = ["NativeLiveTaskController"]
