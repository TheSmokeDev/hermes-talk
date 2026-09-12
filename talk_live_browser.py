"""Server-owned Live browser sessions and bounded task delegation pumps."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from collections import deque

try:
    from . import talk_capabilities, talk_identity
    from . import talk_realtime as rt
    from .talk_dashboard_gateway import DashboardTaskError
    from .talk_live_config import resolve_live_auth, resolve_live_config
    from .talk_live_coordinator import LiveCoordinator, protocol_id
    from .talk_live_transport import negotiate_live_browser
    from .talk_passive import digest
except ImportError:  # pragma: no cover - flat plugin load
    import talk_capabilities
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
CAPTURE_INTERVAL = 0.1
CAPTURE_FRAGMENTS = 32
CAPTURE_BYTES = 8192
MAX_CAPTURE_FRAGMENTS = 4096
MAX_CAPTURE_BYTES = 256 * 1024
OPERATION_POLL_SECONDS = 0.25


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
        self.fragments_seen = {}
        self.delegations_seen = set()
        self.pending = {}
        self.retired = set()
        self.persist_queue = deque()
        self.capture_bytes = 0
        self.capture_ready = asyncio.Event()
        self.capture_full = asyncio.Event()
        self.capture_lock = asyncio.Lock()
        self.send_lock = asyncio.Lock()
        self.operations = {}
        self.deliveries = {}
        self.delivered = set()
        self.typed_pending = set()
        self.job_delegations = {}
        self.closing = False
        self.tasks = set()
        self.closed = False
        self.failure = None
        self.polling = False
        self.last_state = 0.0
        self.results_seen = set()
        self.active_jobs = set()
        self.has_announcements = False
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
                if self.closed or self.closing:
                    return
                await self.authorize()
                if isinstance(event, rt.Transcript):
                    await self.transcript(event)
                elif isinstance(event, rt.DelegationRequested):
                    self.delegate(event)
                elif isinstance(event, rt.DelegationRetired):
                    self.retired.add(event.delegation_id)
                    # Speech interruption cannot retire an accepted Hermes operation.
                    self.emit("delivery", state="interrupted")
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
        identity = (digest([self.provider_session_id, event.event_id])
                    if event.event_id else secrets.token_hex(16))
        fragment = {
            "event_id": identity, "role": str(event.role), "text": event.text,
            "final": event.final, "start_ms": event.start_ms, "end_ms": event.end_ms,
        }
        if event.item_id is not None:
            fragment["item_id"] = event.item_id
        if event.finality is not None:
            fragment["finality"] = event.finality
        fingerprint = digest(fragment)
        previous = self.fragments_seen.get(identity)
        if previous is not None:
            if previous != fingerprint:
                raise DashboardTaskError("event_conflict", 409)
            return
        size = len(event.text.encode("utf-8"))
        if (len(self.persist_queue) >= MAX_CAPTURE_FRAGMENTS
                or self.capture_bytes + size > MAX_CAPTURE_BYTES):
            raise DashboardTaskError("capacity", 409)
        if event.role is rt.TranscriptRole.USER:
            if (len(self.pending_fragments) >= MAX_CAPTURE_FRAGMENTS
                    or sum(len(row["text"].encode("utf-8")) for row in self.pending_fragments)
                    + size > MAX_CAPTURE_BYTES):
                raise DashboardTaskError("capacity", 409)
            self.pending_fragments.append(fragment)
        self.fragments_seen[identity] = fingerprint
        if len(self.fragments_seen) > MAX_CAPTURE_FRAGMENTS * 2:
            self.fragments_seen.pop(next(iter(self.fragments_seen)))
        self.persist_queue.append(fragment)
        self.capture_bytes += size
        self.capture_ready.set()
        if len(self.persist_queue) >= CAPTURE_FRAGMENTS or self.capture_bytes >= CAPTURE_BYTES:
            self.capture_full.set()
        self.emit("transcript", **fragment)

    async def flush_capture(self):
        async with self.capture_lock:
            remaining = len(self.persist_queue)
            while remaining:
                batch, size = [], 0
                for fragment in self.persist_queue:
                    width = len(fragment["text"].encode("utf-8"))
                    if (len(batch) >= min(remaining, CAPTURE_FRAGMENTS)
                            or (batch and size + width > CAPTURE_BYTES)):
                        break
                    batch.append(fragment)
                    size += width
                if not batch:
                    raise DashboardTaskError("capacity", 409)
                await self.authorize(write=True)
                reply = await asyncio.to_thread(
                    self.registry.coordinator.transcript, self.lease, self.body(fragments=batch),
                )
                expected = {row["event_id"] for row in batch}
                acked = reply.get("acked_event_ids")
                if (acked is None and reply.get("ok") is True
                        and reply.get("captured") == len(batch)):
                    acked = list(expected)
                if (reply.get("ok") is not True or not isinstance(acked, list) or not acked
                        or any(not isinstance(value, str) for value in acked)
                        or not set(acked) <= expected):
                    raise DashboardTaskError("gateway_response_invalid", 502)
                acknowledged = set(acked)
                self.persist_queue = deque(row for row in self.persist_queue
                                           if row["event_id"] not in acknowledged)
                self.capture_bytes -= sum(len(row["text"].encode("utf-8")) for row in batch
                                          if row["event_id"] in acknowledged)
                remaining -= len(acknowledged)
            if not self.persist_queue:
                self.capture_ready.clear()
            if len(self.persist_queue) < CAPTURE_FRAGMENTS and self.capture_bytes < CAPTURE_BYTES:
                self.capture_full.clear()
        return {"ok": True}

    async def persist(self):
        try:
            while not self.closed and not self.closing:
                await self.capture_ready.wait()
                if not self.capture_full.is_set():
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.capture_full.wait(), CAPTURE_INTERVAL)
                await self.flush_capture()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - lost capture ownership closes audio, never claims a save
            await self.fail("Live transcript persistence failed. Rejoin the original task.")

    def delegate(self, event):
        if event.delegation_id in self.delegations_seen:
            return
        if (len(self.delegations_seen) >= MAX_DELEGATIONS
                or len(self.pending) + len(self.typed_pending) >= MAX_PENDING):
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

    async def operation_result(self, response, *, delegation_id=None):
        operation = response.get("operation_id")
        if operation is None:
            return response
        protocol_id(operation)
        current = response
        while True:
            await self.authorize()
            if self.closed or self.closing:
                raise asyncio.CancelledError
            if current.get("operation_id") != operation or type(current.get("pending")) is not bool:
                raise DashboardTaskError("gateway_response_invalid", 502)
            state = current.get("state")
            previous = self.operations.get(operation)
            self.operations[operation] = {"state": state, "delegation_id": delegation_id}
            if previous is None or previous["state"] != state:
                self.emit("operation", operation_id=operation, state=state,
                          pending=current["pending"])
            if not current["pending"]:
                if state not in {"completed", "uncertain", "failed"}:
                    raise DashboardTaskError("gateway_response_invalid", 502)
                result = current.get("result")
                if not isinstance(result, dict):
                    raise DashboardTaskError("gateway_response_invalid", 502)
                return {**result, "operation_id": operation, "operation_state": state}
            if state not in {"admitted", "deciding", "dispatching"}:
                raise DashboardTaskError("gateway_response_invalid", 502)
            await asyncio.sleep(OPERATION_POLL_SECONDS)
            current = await asyncio.to_thread(
                self.registry.coordinator.operation, self.lease, self.body(operation_id=operation),
            )

    async def deliver(self, key, commands, *, delegation_id=None):
        if key in self.delivered:
            return
        self.deliveries.setdefault(key, (commands, delegation_id))
        if delegation_id in self.retired:
            return
        async with self.send_lock:
            if key not in self.deliveries or delegation_id in self.retired:
                return
            await self.authorize(write=True)
            if self.closed or self.closing:
                return
            await self.session.send(commands)
            self.deliveries.pop(key, None)
            self.delivered.add(key)
            self.emit("delivery", operation_id=key, state="sent", playback_confirmed=False)

    async def publish_result(self, result, *, delegation_id=None, input_id=None):
        self.result_event(result)
        run_id = (result.get("action") or {}).get("run_id")
        if run_id is not None:
            self.active_jobs.add(str(run_id))
            if delegation_id is not None:
                self.job_delegations[str(run_id)] = delegation_id
        text = str(result.get("output") or "No task action was needed.")[:16000]
        command = (rt.SubmitDelegationResult(delegation_id, text) if delegation_id is not None
                   else rt.AppendLiveContext(text, kind="message"))
        await self.deliver(result.get("operation_id") or delegation_id or input_id,
                           [command], delegation_id=delegation_id)

    async def decide(self, delegation_id, body):
        try:
            await self.flush_capture()
            await self.authorize(write=True)
            result = await self.registry.coordinator.delegation(
                self.lease, {**body, "admission": "async"},
            )
            result = await self.operation_result(result, delegation_id=delegation_id)
            await self.authorize()
            if not self.closed and not self.closing:
                await self.publish_result(result, delegation_id=delegation_id)
        except asyncio.CancelledError:
            # Accepted coordinator operations keep running independently of this audio binding.
            raise
        except Exception:  # noqa: BLE001 - durable receipts remain available after failure
            if not self.closed:
                await self.fail("Live task decision failed. Inspect the task before retrying.")

    def result_event(self, result):
        values = {key: result[key] for key in (
            "output", "action", "selection", "operation_id", "operation_state",
        ) if key in result}
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
        active_typed = len(self.typed_pending)
        if (
            len(self.input_results) >= MAX_DELEGATIONS
            or len(self.pending) + active_typed >= MAX_PENDING
        ):
            raise DashboardTaskError("capacity", 409)
        self.typed_pending.add(input_id)
        task = self.launch(self._typed(text, input_id))
        self.input_results[input_id] = text, task
        task.add_done_callback(
            lambda done: self.typed_pending.discard(input_id)
            if done.cancelled() or done.exception() else None
        )
        return await asyncio.shield(task)

    async def _typed(self, text, input_id):
        await self.flush_capture()
        await self.authorize(write=True)
        result = await self.registry.coordinator.typed(
            self.lease, self.body(input_id=input_id, text=text, admission="async"),
        )
        await self.authorize()
        if self.closed or self.closing:
            raise DashboardTaskError("connection_stale", 409)
        self.emit("transcript", role="user", text=text, final=True, finality="turn",
                  event_id=input_id, item_id=input_id)
        if result.get("operation_id") is not None:
            self.launch(self.finish_typed(result, input_id))
            return {key: result[key] for key in ("ok", "operation_id", "state", "pending")}
        await self.publish_result(result, input_id=input_id)
        self.typed_pending.discard(input_id)
        return {"ok": True}

    async def finish_typed(self, result, input_id):
        try:
            result = await self.operation_result(result)
            await self.publish_result(result, input_id=input_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - accepted work survives transport failure
            if not self.closed:
                await self.fail("Live task decision failed. Inspect the task before retrying.")
        finally:
            self.typed_pending.discard(input_id)

    async def poll(self, after, timing=None):
        if type(after) is not int or after < 0 or after > self.sequence:
            raise DashboardTaskError("invalid_event", 400)
        if self.events and after < self.events[0][0]["sequence"] - 1:
            raise DashboardTaskError("connection_stale", 409)
        while self.events and self.events[0][0]["sequence"] <= after:
            _, size = self.events.popleft()
            self.event_bytes -= size
        active = (self.active_jobs or self.pending or self.typed_pending or self.deliveries
                  or self.has_announcements)
        interval = 0.25 if active else 5.0
        if (not self.closed and not self.polling
                and self.registry.clock() - self.last_state >= interval):
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
        self.active_jobs = {
            str(job["run_id"]) for job in state.get("jobs", [])
            if job.get("status") not in {"completed", "failed", "cancelled", "lost"}
        }
        self.has_announcements = bool(state.get("announcements"))
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
        if timing is None:
            return
        if not any(value for key, value in timing.items() if key != "sequence"):
            self.retired.clear()
            for key, (commands, delegation_id) in list(self.deliveries.items()):
                await self.deliver(key, commands, delegation_id=delegation_id)
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
                delegation_id = self.job_delegations.get(str(speech["run_id"]))
                command = (rt.SubmitDelegationResult(delegation_id, speech["content"])
                           if delegation_id else
                           rt.AppendLiveContext(speech["content"], kind="message"))
                async with self.send_lock:
                    await self.session.send([command])
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
        if self.closed or self.closing:
            return
        self.closing = True
        with contextlib.suppress(Exception):
            async with asyncio.timeout(5):
                await self.flush_capture()
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
        catalog=None,
        coordinator=None,
        negotiate=None,
        env=None,
        clock=time.monotonic,
    ):
        self.manager, self.require_auth = manager, require_auth
        self.coordinator = coordinator or LiveCoordinator(manager, targets, tools)
        self.catalog = catalog or talk_capabilities.status
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
            tools=[],
            task_continuity=True,
            instructions=talk_identity.build_live_instructions(
                lane="dashboard",
                capabilities=self.capabilities(bound),
                task_context=self.task_context(bound),
            ),
        )

    def capabilities(self, bound):
        builder = getattr(self.manager, "live_capabilities", None)
        if callable(builder):
            return builder(bound)
        return talk_identity.live_capabilities(self.catalog())

    def task_context(self, bound):
        builder = getattr(self.manager, "live_instructions", None)
        if callable(builder):
            return builder(bound)
        history = self.manager._history(bound)
        rows = [{"role": row["role"], "content": row["content"]}
                for row in history.get("messages", []) if row.get("role") in {"user", "assistant"}
                and isinstance(row.get("content"), str) and not row.get("hidden")][-12:]
        return json.dumps({"selected_task": bound.selected_context, "history": rows},
                          ensure_ascii=False)[:12000]

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
