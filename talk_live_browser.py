"""Server-owned Live browser sessions and bounded task delegation pumps."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from collections import deque

try:
    from . import talk_identity
    from . import talk_realtime as rt
    from .talk_dashboard_gateway import DashboardTaskError
    from .talk_live_config import resolve_live_auth, resolve_live_config
    from .talk_live_coordinator import LiveCoordinator, protocol_id
    from .talk_live_transport import negotiate_live_browser
    from .talk_passive import digest
except ImportError:  # pragma: no cover - flat plugin load
    import talk_identity
    import talk_realtime as rt
    from talk_dashboard_gateway import DashboardTaskError
    from talk_live_config import resolve_live_auth, resolve_live_config
    from talk_live_coordinator import LiveCoordinator, protocol_id
    from talk_live_transport import negotiate_live_browser
    from talk_passive import digest

LEASE_SECONDS = 15.0
MAX_BINDINGS = 16
MAX_EVENTS = 512
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_PENDING = 8
MAX_DELEGATIONS = 1024


class RequestLease:
    """A sideband decision sees the most recent authenticated browser request."""

    def __init__(self, request, require_auth, *, clock=time.monotonic):
        self._clock, self._require_auth = clock, require_auth
        self.refresh(request)

    def refresh(self, request):
        self._request = request
        self.expires = self._clock() + LEASE_SECONDS

    def current(self):
        if self._clock() >= self.expires:
            raise DashboardTaskError("connection_stale", 409)
        self._require_auth(self._request)
        return self._request

    def __getattr__(self, name):
        return getattr(self.current(), name)


class BrowserBinding:
    def __init__(self, registry, key, request, body, browser, session):
        self.registry, self.key = registry, key
        self.context = {name: body[name] for name in ("connection_id", "generation")}
        self.lease = RequestLease(request, registry.require_auth, clock=registry.clock)
        self.browser, self.session = browser, session
        self.provider_session_id = browser.session_id
        self.events = deque()
        self.sequence = self.event_bytes = 0
        self.pending_fragments = []
        self.fragments_seen = set()
        self.delegations_seen = set()
        self.pending = {}
        self.retired = set()
        self.persist_queue = asyncio.Queue(maxsize=256)
        self.tasks = set()
        self.closed = False
        self.failure = None
        self.polling = False
        self.last_state = 0.0
        self.results_seen = set()
        self.input_results = {}

    def body(self, **values):
        return {**values, **self.context, "provider_session_id": self.provider_session_id}

    def launch(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def start(self):
        self.launch(self.pump())
        self.launch(self.persist())
        self.launch(self.watch_lease())

    def emit(self, kind, **values):
        event = {"sequence": self.sequence + 1, "type": kind, **values}
        encoded = len(json.dumps(event, ensure_ascii=False).encode())
        if len(self.events) >= MAX_EVENTS or self.event_bytes + encoded > MAX_EVENT_BYTES:
            raise DashboardTaskError("capacity", 409)
        self.events.append((event, encoded))
        self.event_bytes += encoded
        self.sequence += 1

    async def authorize(self, *, write=False):
        self.lease.current()
        return await asyncio.to_thread(
            self.registry.manager.binding,
            self.lease,
            self.context,
            write=write,
        )

    async def pump(self):
        try:
            async for event in self.session:
                if self.closed:
                    return
                await self.authorize()
                if isinstance(event, rt.Transcript):
                    await self.transcript(event)
                elif isinstance(event, rt.DelegationRequested):
                    self.delegate(event)
                elif isinstance(event, rt.DelegationRetired):
                    self.retired.add(event.delegation_id)
                    pending = self.pending.get(event.delegation_id)
                    if pending:
                        pending.cancel()
                elif isinstance(event, rt.ProviderFailure):
                    await self.fail("GPT-Live rejected a session event. Rejoin the task.")
                    return
                elif isinstance(event, rt.SessionTerminated):
                    await self.fail("GPT-Live audio ended. Rejoin the task to reconnect.")
                    return
            if not self.closed:
                await self.fail("GPT-Live sideband disconnected. Rejoin the task.")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - no upstream details or credentials reach the browser
            await self.fail("GPT-Live task connection failed. Rejoin the task.")

    async def transcript(self, event):
        if not event.text:
            return
        identity = (
            digest(
                [
                    event.item_id,
                    str(event.role),
                    event.text,
                    event.final,
                    event.start_ms,
                    event.end_ms,
                ]
            )
            if event.item_id
            else (secrets.token_hex(16))
        )
        if identity in self.fragments_seen:
            return
        if len(self.fragments_seen) >= 4096:
            raise DashboardTaskError("capacity", 409)
        self.fragments_seen.add(identity)
        fragment = {
            "event_id": identity,
            "role": str(event.role),
            "text": event.text,
            "final": event.final,
            "start_ms": event.start_ms,
            "end_ms": event.end_ms,
        }
        if event.role is rt.TranscriptRole.USER:
            if len(self.pending_fragments) >= 256:
                raise DashboardTaskError("capacity", 409)
            self.pending_fragments.append(fragment)
        self.persist_queue.put_nowait(fragment)
        self.emit("transcript", role=str(event.role), text=event.text, final=event.final)

    async def persist(self):
        try:
            while not self.closed:
                fragment = await self.persist_queue.get()
                await self.authorize(write=True)
                await asyncio.to_thread(
                    self.registry.coordinator.transcript,
                    self.lease,
                    self.body(fragments=[fragment]),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - fail closed on lost canonical transcript ownership
            await self.fail("Live transcript persistence failed. Rejoin the original task.")

    def delegate(self, event):
        if event.delegation_id in self.delegations_seen:
            return
        if len(self.delegations_seen) >= MAX_DELEGATIONS or len(self.pending) >= MAX_PENDING:
            raise DashboardTaskError("capacity", 409)
        self.delegations_seen.add(event.delegation_id)
        captured, future = [], []
        for fragment in self.pending_fragments:
            end = fragment.get("end_ms")
            (
                future
                if event.offset_ms is not None and end is not None and end > event.offset_ms
                else captured
            ).append(fragment)
        self.pending_fragments = future
        body = self.body(
            delegation_id=event.delegation_id, offset_ms=event.offset_ms, fragments=captured
        )
        task = self.launch(self.decide(event.delegation_id, body))
        self.pending[event.delegation_id] = task
        task.add_done_callback(lambda _: self.pending.pop(event.delegation_id, None))

    async def decide(self, delegation_id, body):
        try:
            await self.authorize(write=True)
            async with asyncio.timeout(35):
                result = await self.registry.coordinator.delegation(self.lease, body)
            await self.authorize()
            if self.closed:
                return
            self.result_event(result)
            if delegation_id not in self.retired:
                await self.session.send(
                    [
                        rt.SubmitDelegationResult(
                            delegation_id,
                            str(result.get("output") or "No task action was needed.")[:16000],
                        )
                    ]
                )
        except asyncio.CancelledError:
            # Cancelling this coroutine never issues stop/cancel to accepted Hermes work.
            raise
        except Exception:  # noqa: BLE001 - durable task receipts remain available after failure
            if not self.closed:
                await self.fail("Live task decision failed. Inspect the task before retrying.")

    def result_event(self, result):
        values = {key: result[key] for key in ("output", "action", "selection") if key in result}
        action = result.get("action") or {}
        if action.get("run_id") is not None:
            values["run_id"] = action["run_id"]
        self.emit("result", **values)

    async def typed(self, text, input_id):
        if self.closed:
            raise DashboardTaskError("connection_stale", 409)
        protocol_id(input_id)
        if input_id in self.input_results:
            original, pending = self.input_results[input_id]
            if text != original:
                raise DashboardTaskError("event_conflict", 409)
            return await asyncio.shield(pending)
        active_typed = sum(not task.done() for _, task in self.input_results.values())
        if (
            len(self.input_results) >= MAX_DELEGATIONS
            or len(self.pending) + active_typed >= MAX_PENDING
        ):
            raise DashboardTaskError("capacity", 409)
        task = self.launch(self._typed(text, input_id))
        self.input_results[input_id] = text, task
        return await asyncio.shield(task)

    async def _typed(self, text, input_id):
        await self.authorize(write=True)
        async with asyncio.timeout(35):
            result = await self.registry.coordinator.typed(
                self.lease,
                self.body(input_id=input_id, text=text),
            )
        await self.authorize()
        if self.closed:
            raise DashboardTaskError("connection_stale", 409)
        self.emit("transcript", role="user", text=text, final=True)
        self.result_event(result)
        await self.session.send(
            [
                rt.AppendLiveContext(
                    str(result.get("output") or "Typed input received.")[:16000],
                    kind="message",
                )
            ]
        )
        return {"ok": True}

    async def poll(self, after, timing=None):
        if type(after) is not int or after < 0 or after > self.sequence:
            raise DashboardTaskError("invalid_event", 400)
        if self.events and after < self.events[0][0]["sequence"] - 1:
            raise DashboardTaskError("connection_stale", 409)
        while self.events and self.events[0][0]["sequence"] <= after:
            _, size = self.events.popleft()
            self.event_bytes -= size
        if not self.closed and not self.polling and self.registry.clock() - self.last_state >= 5:
            self.polling = True
            self.last_state = self.registry.clock()
            try:
                await self.proactive(timing)
            finally:
                self.polling = False
        return {"ok": True, "events": [event for event, _ in self.events], "cursor": self.sequence}

    async def proactive(self, timing):
        await self.authorize()
        state = await asyncio.to_thread(self.registry.manager.state, self.lease, self.context)
        for job in state.get("jobs", []):
            key = (job.get("run_id"), job.get("status"))
            if not job.get("result_available") or key in self.results_seen:
                continue
            result = await asyncio.to_thread(
                self.registry.manager.result,
                self.lease,
                {**self.context, "run_id": job["run_id"]},
            )
            if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_EVENT_BYTES // 2:
                self.emit("result", run_id=job["run_id"], result_available=True)
            else:
                self.emit("result", result=result)
            self.results_seen.add(key)
        typing = any(not task.done() for _, task in self.input_results.values())
        if timing is None or self.pending or typing:
            return
        for announcement in state.get("announcements", [])[:1]:
            prepared = await asyncio.to_thread(
                self.registry.manager.speech,
                self.lease,
                {**self.context, "event_id": announcement["event_id"], "timing": timing},
            )
            speech = speech_context(prepared)
            if not speech.get("speak"):
                continue
            receipt = {
                **self.context,
                "event_id": speech["event_id"],
                "attempt_id": speech["attempt_id"],
            }
            try:
                await self.authorize(write=True)
                await self.session.send([rt.AppendLiveContext(speech["content"], kind="message")])
            except BaseException:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        self.registry.manager.speech_receipt,
                        self.lease,
                        {**receipt, "state": "unknown"},
                    )
                raise
            await asyncio.to_thread(
                self.registry.manager.speech_receipt, self.lease, {**receipt, "state": "sent"}
            )

    async def watch_lease(self):
        try:
            while not self.closed:
                await asyncio.sleep(1)
                await self.authorize()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - lease/auth failures revoke audio, never task work
            await self.fail("Live authorization expired. Rejoin the task.")

    async def fail(self, message):
        if self.closed:
            return
        self.failure = message
        if len(self.events) >= MAX_EVENTS or self.event_bytes > MAX_EVENT_BYTES - 1024:
            self.events.clear()
            self.event_bytes = 0
        self.emit("error", message=message)
        await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        current = asyncio.current_task()
        pending = [task for task in self.tasks if task is not current]
        for task in pending:
            task.cancel()
        with contextlib.suppress(Exception):
            async with asyncio.timeout(5):
                await self.browser.close()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def speech_context(prepared):
    """Convert trusted status fields, never worker prose, into a bounded spoken update."""
    if not prepared.get("speak"):
        return prepared
    try:
        response = prepared["response"]
        if (
            response["conversation"] != "none"
            or response["tool_choice"] != "none"
            or response["tools"]
        ):
            raise ValueError
        raw = response["input"][0]["content"][0]["text"]
        data = json.loads(raw)
        status = {
            "queued": "queued",
            "running": "running",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
            "waiting_for_approval": "waiting for your approval",
            "lost": "lost; its outcome is unconfirmed",
        }[data["status"]]
        run_id = str(prepared["run_id"])
        content = f"Hermes observed job {run_id} as {status}."
        if data.get("full_result_available"):
            content += " The full result is available in the task panel."
        if data["status"] == "cancelled":
            content += " Cancellation does not mean prior effects were rolled back."
        if len(content) > 1000:
            raise ValueError
    except (KeyError, TypeError, ValueError, IndexError):
        raise DashboardTaskError("gateway_response_invalid", 502) from None
    return {key: value for key, value in prepared.items() if key != "response"} | {
        "content": content,
        "kind": "commentary",
    }


class LiveBrowserRegistry:
    def __init__(
        self,
        manager,
        targets,
        tools,
        require_auth,
        *,
        setup_factory=None,
        coordinator=None,
        negotiate=None,
        env=None,
        clock=time.monotonic,
    ):
        self.manager, self.require_auth = manager, require_auth
        self.coordinator = coordinator or LiveCoordinator(manager, targets, tools)
        self.setup_factory = setup_factory or self.default_setup
        self.negotiate = negotiate or negotiate_live_browser
        self.env, self.clock = env, clock
        self.bindings = {}
        self.creating = set()
        self.opening = set()
        self.closed = False

    def default_setup(self, bound, config):
        return rt.SessionSetup(
            model=config.model,
            voice=config.voice,
            instructions=talk_identity.build_instructions(
                None,
                tools=[],
                lane="dashboard",
                canonical_task=True,
                capabilities="Hermes owns task delegation; speak briefly while work continues.",
            )
            + "\n\n"
            + self.manager.instructions(bound),
        )

    async def create(self, request, body):
        if self.closed:
            raise DashboardTaskError("connection_stale", 409)
        bound = await asyncio.to_thread(self.manager.binding, request, body, write=True)
        context = {"connection_id": bound.connection_id, "generation": bound.generation}
        connection = bound.connection_id
        if connection in self.creating:
            raise DashboardTaskError("busy", 409)
        for key, previous in list(self.bindings.items()):
            if previous.closed or previous.lease.expires <= self.clock():
                await previous.close()
                self.bindings.pop(key, None)
        if len(self.bindings) + len(self.creating) >= MAX_BINDINGS:
            raise DashboardTaskError("capacity", 409)
        self.creating.add(connection)
        creating_task = asyncio.current_task()
        self.opening.add(creating_task)
        browser = None
        try:
            config = resolve_live_config(self.env)
            auth = await asyncio.to_thread(resolve_live_auth, env=self.env, config=config)
            setup = await asyncio.to_thread(self.setup_factory, bound, config)
            async with asyncio.timeout(30):
                browser = await self.negotiate(body.get("sdp"), setup, auth, config)
                session = await browser.open_session()
            if self.closed:
                raise DashboardTaskError("connection_stale", 409)
            await asyncio.to_thread(self.manager.binding, request, context, write=True)
            for key, previous in list(self.bindings.items()):
                if previous.context == context:
                    await previous.close()
                    self.bindings.pop(key, None)
            key = secrets.token_hex(24)
            binding = BrowserBinding(self, key, request, context, browser, session)
            self.bindings[key] = binding
            binding.start()
            return {"ok": True, **browser.public_response(key)}
        except BaseException:
            if browser is not None:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(5):
                        await browser.close()
            raise
        finally:
            self.creating.discard(connection)
            self.opening.discard(creating_task)

    async def binding(self, request, body, *, refresh=False):
        await asyncio.to_thread(self.manager.binding, request, body)
        binding = self.bindings.get(protocol_id(body.get("binding_id")))
        context = {key: body.get(key) for key in ("connection_id", "generation")}
        if binding is None or binding.context != context:
            raise DashboardTaskError("connection_stale", 409)
        if refresh and not binding.closed:
            binding.lease.refresh(request)
        return binding

    async def close_all(self):
        self.closed = True
        opening = [task for task in self.opening if task is not asyncio.current_task()]
        for task in opening:
            task.cancel()
        await asyncio.gather(
            *opening,
            *(binding.close() for binding in self.bindings.values()),
            return_exceptions=True,
        )
        self.bindings.clear()
