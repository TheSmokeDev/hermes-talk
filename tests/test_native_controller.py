"""Native adapters exercise the real task HTTP routes, selector, and SQLite coordinator."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import fixture_data
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from test_target_switching import fleet as fleet

import talk_openai_realtime
import talk_realtime as rt
from talk_native_api import NativeTaskAPI, NativeTaskError
from talk_native_controller import NativeTaskController


class Audio:
    playback_pending = False
    played_ms = 0
    input_paused = False

    def __init__(self):
        self.output = []
        self.drains = 0
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def read_input_chunk(self):
        return None

    def queue_playback(self, pcm):
        self.output.append(pcm)

    def drain_playback(self):
        self.drains += 1
        self.output.clear()

    def reset_played_ms(self):
        self.played_ms = 0

    def pause_input(self):
        self.input_paused = True

    def resume_input(self):
        self.input_paused = False


class Session:
    def __init__(self):
        self.sent = []
        self.setup = None
        self.events = asyncio.Queue()
        self.closed = False

    async def connect(self, setup):
        self.setup = setup

    async def send(self, commands):
        self.sent.extend(commands)

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.events.get()
        if event is None:
            raise StopAsyncIteration
        return event

    async def close(self):
        self.closed = True
        self.events.put_nowait(None)

    @property
    def responses(self):
        return [command for command in self.sent if isinstance(command, rt.StartResponse)]


@pytest.fixture
def native(fleet, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("native_route_fixture", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "TASKS", fleet.manager)
    monkeypatch.setattr(module, "TARGETS", fleet.selection)
    monkeypatch.setenv("TALK_DASHBOARD_TOKEN", "fake-native-token")
    routes = {
        "/native/attach": module.native_task_attach,
        "/targets": module.task_targets,
        "/event": module.task_event,
        "/tool": module.run_tool,
        "/state": module.task_state,
        "/result": module.task_result,
        "/preference": module.task_preference,
        "/speech": module.task_speech,
        "/speech/receipt": module.task_speech_receipt,
        "/close": module.task_close,
    }

    def endpoint(handler):
        async def call(request):
            request.state.principal = "actor-one"
            try:
                return JSONResponse(await handler(request))
            except module.HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

        return call

    app = Starlette(
        routes=[
            Route(
                "/api/plugins/hermes-talk" + path,
                endpoint(handler),
                methods=["GET"] if path == "/result" else ["POST"],
            )
            for path, handler in routes.items()
        ]
    )

    async def connect(*, tab="native-test", target="task-a", session=None):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
        api = NativeTaskAPI("http://127.0.0.1", talk_token="fake-native-token", client=client)
        catalog = await api.catalog()
        selected = next(
            row["target_id"] for row in catalog["targets"] if row["session_id"] == target
        )
        attachment = await api.attach(target_id=selected, tab_id=tab)
        audio, session = Audio(), session or Session()
        notices, states, results = [], [], []
        controller = NativeTaskController(
            api,
            session,
            attachment,
            audio,
            on_notice=notices.append,
            on_state=states.append,
            on_result=results.append,
        )
        return SimpleNamespace(
            api=api,
            client=client,
            controller=controller,
            audio=audio,
            session=session,
            notices=notices,
            states=states,
            results=results,
        )

    return SimpleNamespace(connect=connect, app=app, fleet=fleet)


async def started(handle, response_id="response-one", *, request=None):
    request = request or handle.session.responses[-1]
    await handle.controller.handle(
        talk_openai_realtime.decode_event(
            {
                "type": "response.created",
                "response": {"id": response_id, "metadata": dict(request.metadata)},
            }
        )
    )


async def finished(
    handle, response_id="response-one", *, text="The answer.", calls=(), status="completed"
):
    output = [
        {"id": "fc_" + call_id, "type": "function_call", "call_id": call_id} for call_id in calls
    ]
    if text:
        output.append(
            {
                "id": "answer_" + response_id,
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_audio", "transcript": text}],
            }
        )
    await handle.controller.handle(
        talk_openai_realtime.decode_event(
            {
                "type": "response.done",
                "response": {"id": response_id, "status": status, "output": output},
            }
        )
    )
    await handle.controller.drain()


async def user(handle, text, *, input_id="input-one", response_id="response-one"):
    receipt = await handle.controller.typed(text, input_id=input_id)
    await started(handle, response_id)
    return receipt


async def tool(handle, name, arguments, *, call_id="call-one", response_id="response-one"):
    await handle.controller.handle(
        rt.FunctionCall(call_id, name, json.dumps(arguments), response_id, "fc_" + call_id)
    )
    await finished(handle, response_id, text="", calls=[call_id])


async def close(handle):
    await handle.controller.close()
    await handle.api.close()
    await handle.client.aclose()


def test_real_http_auth_refuses_missing_token(native):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=native.app)) as client:
            api = NativeTaskAPI("http://127.0.0.1", client=client)
            with pytest.raises(NativeTaskError, match="HTTP 401"):
                await api.catalog()

    asyncio.run(scenario())


def test_raw_voice_origin_saved_once_before_its_response_and_not_from_arguments(native):
    async def scenario():
        h = await native.connect()
        original = "Please build it.\nKeep the exact $42 budget."
        await h.controller.handle(
            talk_openai_realtime.decode_event(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "mic-one",
                    "transcript": original,
                }
            )
        )
        assert not h.session.responses
        await h.controller.handle(rt.InputAudioCommitted("mic-one"))
        await started(h)
        await h.controller.handle(
            rt.FunctionCall(
                "call-one",
                "delegate_task",
                '{"task":"A generated execution goal"}',
                "response-one",
                "fc_call-one",
            )
        )
        assert not native.fleet.hosts["local"].jobs
        await finished(h, text="", calls=["call-one"])
        host = native.fleet.hosts["local"]
        submitted = next(body for method, path, body, _ in host.requests if path == "/v1/runs")
        assert submitted["input"] == original
        assert submitted["child"]["goal"] == "A generated execution goal"
        assert (
            len([row for row in host.rows[("default", "task-a")] if row["content"] == original])
            == 1
        )
        continuation = h.session.responses[-1]
        assert continuation.metadata["talk_previous_response_id"] == "response-one"
        await started(h, "response-two")
        await finished(h, "response-two", text="The original task has started.")
        assert h.controller.inputs["mic-one"].settled
        await close(h)

    asyncio.run(scenario())


def test_commit_before_transcript_and_out_of_order_voice_inputs_keep_exact_references(native):
    async def scenario():
        h = await native.connect()
        await h.controller.handle(rt.InputAudioCommitted("mic-a"))
        await h.controller.handle(rt.InputAudioCommitted("mic-b"))
        for item_id, text in (("mic-b", "Second words"), ("mic-a", "First words")):
            await h.controller.handle(
                rt.Transcript(
                    rt.TranscriptRole.USER,
                    text,
                    True,
                    rt.TranscriptProvenance.INPUT_AUDIO,
                    item_id=item_id,
                )
            )
        assert [list(response.input)[-1]["id"] for response in h.session.responses] == [
            "mic-b",
            "mic-a",
        ]
        assert [response.metadata["talk_input_id"] for response in h.session.responses] == [
            "mic-b",
            "mic-a",
        ]
        await close(h)

    asyncio.run(scenario())


def test_changed_origin_retry_and_foreign_response_do_not_execute(native):
    async def scenario():
        h = await native.connect()
        await user(h, "Original typed instruction")
        with pytest.raises(NativeTaskError, match="changed"):
            await h.controller.stage("input-one", "typed", "Rewritten instruction")
        await h.controller.handle(rt.ResponseStarted("foreign", {"talk_request_id": "invented"}))
        await h.controller.handle(
            rt.FunctionCall(
                "foreign-call", "delegate_task", '{"task":"No"}', "foreign", "foreign-item"
            )
        )
        await h.controller.handle(rt.OutputAudio(b"\x01\x00", "foreign-item", "foreign"))
        assert not native.fleet.hosts["local"].jobs
        assert not h.audio.output
        assert any(
            isinstance(item, rt.CancelResponse) and item.response_id == "foreign"
            for item in h.session.sent
        )
        await close(h)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["cancelled", "failed", "incomplete"])
def test_noncompleted_response_never_dispatches_work(native, status):
    async def scenario():
        h = await native.connect()
        await user(h, "Never turn a cancelled response into an action")
        await h.controller.handle(
            rt.FunctionCall(
                "call-one", "delegate_task", '{"task":"No"}', "response-one", "fc_call-one"
            )
        )
        await finished(h, calls=["call-one"], status=status)
        assert not native.fleet.hosts["local"].jobs
        assert h.controller.inputs["input-one"].incomplete
        await close(h)

    asyncio.run(scenario())


def test_late_declared_call_can_finish_but_undeclared_call_cannot(native):
    async def scenario():
        h = await native.connect()
        await user(h, "Launch one job")
        await finished(h, text="", calls=["call-one"])
        assert not native.fleet.hosts["local"].jobs
        await h.controller.handle(
            rt.FunctionCall(
                "call-one", "delegate_task", '{"task":"One job"}', "response-one", "fc_call-one"
            )
        )
        await h.controller.drain()
        assert len(native.fleet.hosts["local"].jobs) == 1
        await h.controller.handle(
            rt.FunctionCall(
                "extra-call", "delegate_task", '{"task":"Wrong"}', "response-one", "fc_extra"
            )
        )
        assert len(native.fleet.hosts["local"].jobs) == 1
        await close(h)

    asyncio.run(scenario())


def test_full_result_preferences_silent_reconnect_and_generation_fence(native):
    async def scenario():
        h = await native.connect()
        await user(h, "Make the full artifact")
        await tool(h, "delegate_task", {"task": "Produce artifact"})
        await started(h, "response-two")
        await finished(h, "response-two")
        await h.controller.preference("completion")
        host = native.fleet.hosts["local"]
        full = "Full artifact\n" * 2500
        host.jobs["remote-1"].update(
            status="completed", output=full, artifacts=[{"path": "result.txt"}], updated_at=103
        )
        await h.controller.refresh(announce=False)
        result = await h.controller.result(1)
        assert result["output"] == full and result["truncated"] is False
        h2 = await native.connect()
        view = await h2.controller.refresh()
        assert view["preferences"] == {"update_mode": "completion"}
        assert not view["announcements"] and not h2.session.sent
        with pytest.raises(NativeTaskError, match="HTTP 409"):
            await h.controller.request("/state")
        await close(h)
        await close(h2)

    asyncio.run(scenario())


def test_summary_is_isolated_and_cannot_call_tools_or_write_history(native):
    async def scenario():
        h = await native.connect()
        await user(h, "Run the job")
        await tool(h, "delegate_task", {"task": "Compute the answer"})
        await started(h, "response-two")
        await finished(h, "response-two")
        await h.controller.refresh(announce=False)
        native.fleet.hosts["local"].jobs["remote-1"].update(
            status="completed", output="Untrusted result", updated_at=104
        )
        await h.controller.refresh()
        command = h.session.responses[-1]
        assert command.conversation == "none" and command.allow_tools is False
        before = list(native.fleet.hosts["local"].rows[("default", "task-a")])
        await started(h, "summary", request=command)
        await h.controller.handle(
            rt.FunctionCall(
                "attack", "delegate_task", '{"task":"Do more"}', "summary", "summary-item"
            )
        )
        await h.controller.handle(
            rt.Transcript(
                rt.TranscriptRole.ASSISTANT,
                "A short summary",
                True,
                rt.TranscriptProvenance.OUTPUT_AUDIO,
                "summary",
                "s1",
            )
        )
        await h.controller.handle(rt.SpeechStarted("interrupt-summary"))
        await h.controller.handle(rt.OutputAudio(b"\x01\x00", "late", "summary"))
        assert native.fleet.hosts["local"].rows[("default", "task-a")] == before
        assert len(native.fleet.hosts["local"].jobs) == 1 and not h.audio.output
        await close(h)

    asyncio.run(scenario())


def test_http_response_cannot_cross_client_generation_switch():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"ok": True, "private": "old task"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = NativeTaskAPI("http://127.0.0.1", client=client)
            api.context = {"connection_id": "old", "generation": 1}
            pending = asyncio.create_task(api.request("/state"))
            await entered.wait()
            api.context = {"connection_id": "new", "generation": 2}
            release.set()
            with pytest.raises(NativeTaskError, match="older connection"):
                await pending
            with pytest.raises(NativeTaskError, match="older connection"):
                await api.request(
                    "/state", expected_context={"connection_id": "old", "generation": 1}
                )

    asyncio.run(scenario())


def test_steering_uses_original_correction_and_same_job_with_concurrent_typed_history(native):
    from test_dashboard_steering import SteeringHost

    async def scenario():
        catalog_host = native.fleet.hosts["local"]
        host = SteeringHost()
        host.store_id = catalog_host.store_id
        host.rows.update(catalog_host.rows)

        def route(request):
            if "/api/sessions" in request.url.path:
                return catalog_host(request)
            return host(request)

        native.fleet.hosts["local"] = route
        h = await native.connect()
        host.busy_commit = True
        await user(h, "Start the long job")
        await tool(h, "delegate_task", {"task": "Keep working"})
        await started(h, "acknowledgement")
        await finished(h, "acknowledgement")
        host.rows[("default", "task-a")].append(
            {"id": 20, "role": "user", "content": "Concurrent typed turn"}
        )
        correction = "Use exactly $42.\nPlease."
        await user(h, correction, input_id="correction", response_id="correct-response")
        await tool(h, "steer_work", {"run_id": 1}, response_id="correct-response")
        assert len(host.jobs) == 1 and len(host.deliveries) == 1
        delivered = host.deliveries[0]
        assert delivered["input"] == correction
        assert (
            delivered["control"]["origin"]["origin_turn_id"]
            == h.controller.inputs["correction"].receipt["origin_turn_id"]
        )
        assert (
            len([row for row in host.rows[("default", "task-a")] if row["content"] == correction])
            == 1
        )
        await close(h)

    asyncio.run(scenario())


def test_current_approval_and_cancel_keep_original_job(native):
    async def scenario():
        h = await native.connect()
        await user(h, "Run it")
        await tool(h, "delegate_task", {"task": "One job"})
        await started(h, "ack")
        await finished(h, "ack")
        host = native.fleet.hosts["local"]
        host.pending = [{"request_id": "approval-one", "choices": ["once", "deny"]}]
        await user(h, "Allow this once", input_id="approval-input", response_id="approval-response")
        await tool(
            h,
            "resolve_approval",
            {"run_id": 1, "approval_id": "approval-one", "choice": "once"},
            response_id="approval-response",
        )
        assert not host.pending
        await started(h, "approval-ack")
        await finished(h, "approval-ack")
        await user(h, "Stop the original job", input_id="stop-input", response_id="stop-response")
        await tool(h, "stop_work", {"run_id": 1}, response_id="stop-response")
        assert len(host.jobs) == 1 and host.jobs["remote-1"]["status"] == "cancelled"
        await close(h)

    asyncio.run(scenario())


def test_shared_history_is_bounded_current_reference_context_and_silent_on_reconnect(native):
    async def scenario():
        h = await native.connect()
        await h.controller.refresh(announce=False)
        assert not h.session.sent
        rows = native.fleet.hosts["local"].rows[("default", "task-a")]
        rows.extend(
            {
                "id": 100 + i,
                "role": "user" if i % 2 else "assistant",
                "content": f"Shared update {i}",
            }
            for i in range(15)
        )
        await h.controller.refresh(announce=False)
        context = h.session.sent[-1]
        assert isinstance(context, rt.AddContext)
        payload = json.loads(context.text.split("\n", 1)[1])
        assert len(payload) == 12 and payload[-1]["content"] == "Shared update 14"
        assert len(context.text) <= 12000 and not native.fleet.hosts["local"].jobs
        await h.controller.refresh(announce=False)
        assert h.session.sent[-1] is context
        rows.append({"id": 150, "role": "assistant", "content": "x" * 20000})
        await h.controller.refresh(announce=False)
        newer = h.session.sent[-2]
        assert isinstance(newer, rt.AddContext) and len(newer.text) <= 12000
        assert h.session.sent[-1] == rt.RemoveContext(context.item_id)
        await h.controller.typed("My new exact input", input_id="typed-context")
        assert h.session.responses[-1].input[0]["id"] == newer.item_id
        assert h.session.responses[-1].metadata["talk_input_id"] == "typed-context"
        await close(h)

    asyncio.run(scenario())


def test_shared_history_allowlist_excludes_hidden_non_dialogue_and_credentials(native):
    async def scenario():
        h = await native.connect()
        state = await h.controller.request("/state")
        state["history"]["messages"].extend(
            [
                {"id": 70, "role": "system", "content": "Hidden system"},
                {"id": 71, "role": "tool", "content": "Hidden tool"},
                {"id": 72, "role": "user", "content": "Hidden flag", "hidden": True},
                {"id": 73, "role": "user",
                 "content": "Key " + fixture_data.fake_credential("doctor-api")},
                {"id": 74, "role": "assistant", "content": "Bearer fixture-secret-value"},
                {"id": 75, "role": "user", "content": "Visible saved dialogue"},
            ]
        )
        await h.controller._refresh_history(state)
        payload = json.loads(h.session.sent[-1].text.split("\n", 1)[1])
        assert payload == [{"role": "user", "content": "Visible saved dialogue"}]
        assert not native.fleet.hosts["local"].jobs
        await close(h)

    asyncio.run(scenario())


def test_history_cannot_cross_selected_task_or_audience_during_send(native):
    async def scenario():
        h = await native.connect()
        state = await h.controller.request("/state")
        state["history"]["messages"].append(
            {"id": 99, "role": "user", "content": "Latest shared input"}
        )
        state["history"]["session_id"] = "other-task"
        with pytest.raises(NativeTaskError, match="different task"):
            await h.controller._refresh_history(state)
        state["history"]["session_id"] = h.controller.attachment["task"]["session_id"]
        allowed, entered = [True], asyncio.Event()
        h.controller.authorize_surface = lambda: allowed[0]
        original = h.controller._send_history_context

        async def gated(text):
            entered.set()
            await original(text)

        h.controller._send_history_context = gated
        await h.controller.send_lock.acquire()
        task = asyncio.create_task(h.controller._refresh_history(state))
        await entered.wait()
        allowed[0] = False
        h.controller.send_lock.release()
        with pytest.raises(NativeTaskError, match="audience authorization changed"):
            await task
        assert not h.session.sent and not h.controller.history_rows
        allowed[0] = True
        await close(h)

    asyncio.run(scenario())


def test_terminal_runner_actual_http_selects_peer_returns_and_reconnects_silently(
    native, monkeypatch
):
    import talk_cli

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=native.app))
        api = NativeTaskAPI("http://127.0.0.1", talk_token="fake-native-token", client=client)
        selected, sessions = asyncio.Queue(), []
        monkeypatch.setattr(
            talk_cli,
            "resolve_provider_lane",
            lambda: SimpleNamespace(
                provider="openai", model="fixture", voice="fixture", auth=object()
            ),
        )

        def factory(auth):
            session = Session()
            sessions.append(session)
            return session

        running = asyncio.create_task(
            talk_cli.run_native_talk_session(
                audio=Audio(),
                session_factory=factory,
                task={"target_id": "task-a"},
                api=api,
                on_controller=selected.put_nowait,
            )
        )
        first = await asyncio.wait_for(selected.get(), 5)
        local = await api.catalog()
        target_b = next(
            row["target_id"] for row in local["targets"] if row["session_id"] == "task-b"
        )
        await first.command("/select " + target_b)
        second = await asyncio.wait_for(selected.get(), 5)
        east = await api.catalog(peer_id="east")
        target_east = next(
            row["target_id"] for row in east["targets"] if row["session_id"] == "task-a"
        )
        await second.command("/select " + target_east)
        remote = await asyncio.wait_for(selected.get(), 5)
        assert remote.attachment["task"]["peer_id"] == "east"
        await remote.command("/return")
        second_again = await asyncio.wait_for(selected.get(), 5)
        assert second_again.attachment["task"]["session_id"] == "task-b"
        await second_again.command("/return")
        first_again = await asyncio.wait_for(selected.get(), 5)
        assert first_again.attachment["task"]["session_id"] == "task-a"
        await first_again.command("/reconnect")
        final = await asyncio.wait_for(selected.get(), 5)
        assert final.context != first_again.context
        assert final.attachment["task"]["return_depth"] == 0
        assert all(not session.sent for session in sessions)
        await final.session.close()
        assert await asyncio.wait_for(running, 5) == 0
        assert not any(host.jobs for host in native.fleet.hosts.values())
        await client.aclose()

    asyncio.run(scenario())
