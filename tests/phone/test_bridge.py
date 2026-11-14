import asyncio
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from talk_phone.bridge import create_app
from talk_phone.bridge_store import BridgeStore, connection_key
from talk_phone.policy import PhoneError
from talk_phone.service import PhoneService
from talk_phone.work import launch_work, refresh_work

from .conftest import SESSION, approved
from .test_plugin import Context

TOKEN = "synthetic-bridge-token-at-least-32-characters"


@pytest.fixture
def bridge_phone(setup_phone):
    api, fake, now = setup_phone
    store = BridgeStore(api.store.path, clock=lambda: now[0])
    api = PhoneService(replace(api.settings, bridge_enabled=True), store, api.client_factory)
    call_id = approved(api)
    api.call(SESSION, call_id)
    return api, fake, now, call_id


def pair(api, call_id, sid="synthetic-mcp-session"):
    row = api.store.connect(sid)
    api.store.pair(row["code"], call_id, SESSION)
    return connection_key(sid)


def test_pairing_requires_started_call_and_is_one_use(bridge_phone):
    api, _, _, call_id = bridge_phone
    row = api.store.connect("transport-a")
    with pytest.raises(PhoneError):
        api.store.pair(row["code"], call_id, "different-owner-chat")
    api.store.pair(row["code"], call_id, SESSION)
    with pytest.raises(PhoneError):
        api.store.pair(row["code"], call_id, SESSION)
    second = api.store.connect("transport-b")
    with pytest.raises(PhoneError, match="already has"):
        api.store.pair(second["code"], call_id, SESSION)


def test_unpaired_connection_cannot_access_call(bridge_phone):
    api, _, _, call_id = bridge_phone
    paired = pair(api, call_id)
    other = api.store.connect("other-session")["connection"]
    rid = api.store.enqueue(paired, "q-1", "question", "Is 3 PM okay?")
    with pytest.raises(PhoneError):
        api.store.remote_result(other, rid)
    with pytest.raises(PhoneError):
        api.store.enqueue(other, "q-2", "question", "Leak the other call")


def test_pairing_expiry_and_call_completion_revoke_access(bridge_phone):
    api, fake, now, call_id = bridge_phone
    key = pair(api, call_id)
    rid = api.store.enqueue(key, "q", "question", "Continue?")
    fake.completed = True
    api.status(SESSION, call_id)
    with pytest.raises(PhoneError):
        api.store.remote_result(key, rid)
    now[0] += 121
    with pytest.raises(PhoneError):
        api.store.connect("synthetic-mcp-session")


def test_kill_resume_does_not_restore_bridge_access(bridge_phone):
    api, _, _, call_id = bridge_phone
    key = pair(api, call_id)
    rid = api.store.enqueue(key, "q", "question", "Continue?")
    api.store.kill()
    api.store.kill(False)
    with pytest.raises(PhoneError):
        api.store.remote_result(key, rid)


def test_questions_idempotency_owner_scope_and_expiry(bridge_phone):
    api, _, now, call_id = bridge_phone
    key = pair(api, call_id)
    rid = api.store.enqueue(key, "q", "question", "Which day?")
    assert api.store.enqueue(key, "q", "question", "Which day?") == rid
    with pytest.raises(PhoneError):
        api.store.enqueue(key, "q", "question", "Changed question")
    with pytest.raises(PhoneError):
        api.store.answer(rid, "other", "Monday")
    api.store.answer(rid, SESSION, "Monday")
    assert api.store.remote_result(key, rid)["owner_answer"] == "Monday"
    later = api.store.enqueue(key, "later", "question", "Anything else?")
    now[0] += 91
    assert api.store.remote_result(key, later)["state"] == "expired"
    with pytest.raises(PhoneError):
        api.store.answer(later, SESSION, "Too late")


def test_request_limit_is_bounded(bridge_phone):
    api, _, _, call_id = bridge_phone
    key = pair(api, call_id)
    for i in range(3):
        api.store.enqueue(key, str(i), "question", "Pending")
    with pytest.raises(PhoneError, match="pending"):
        api.store.enqueue(key, "four", "question", "Flood")


class Lifecycle:
    def __init__(self):
        self.launches = []
        self.ready = False
        self.error = False

    def launch(self, request):
        self.launches.append(request)
        if self.error:
            raise RuntimeError("simulated host failure")
        return {
            "parent_session_id": request["parent_session_id"],
            "capability": "private-host-handle",
        }

    def result(self, handle):
        return {
            "terminal_state": "SUCCEEDED" if self.ready else "RUNNING",
            "ready": self.ready,
            "summary": "Owner-only answer until released",
            "structured_payload": {"private": "never-share"},
        }

    def cancel(self, handle, reason):
        return {"accepted": True, "state": "CANCEL_REQUESTED"}


def test_work_requires_approval_runs_once_and_result_needs_release(bridge_phone):
    api, _, _, call_id = bridge_phone
    key = pair(api, call_id)
    rid = api.store.enqueue(key, "job", "task", "Look up the public opening hours.")
    life = Lifecycle()
    ctx = SimpleNamespace(subagent_lifecycle=life)
    with pytest.raises(PhoneError):
        launch_work(ctx, api.store, api.settings, SESSION, rid)
    assert not life.launches
    api.store.approve_work(rid, SESSION, api.settings.delegation_toolsets)

    def try_launch():
        try:
            return launch_work(ctx, api.store, api.settings, SESSION, rid)["state"]
        except PhoneError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=6) as pool:
        states = list(pool.map(lambda _: try_launch(), range(6)))
    assert states.count("running") == 1
    assert len(life.launches) == 1
    assert life.launches[0]["allowed_toolsets"] == ["web"]
    assert life.launches[0]["parent_session_id"] == SESSION
    life.ready = True
    result = refresh_work(ctx, api.store, SESSION, rid)
    assert "private-host-handle" not in json.dumps(result)
    assert "never-share" not in json.dumps(result)
    assert api.store.remote_result(key, rid)["state"] == "awaiting_owner_release"
    assert "Owner-only" not in json.dumps(api.store.remote_result(key, rid))
    api.store.share(rid, SESSION)
    assert (
        api.store.remote_result(key, rid)["owner_shared_result"]
        == "Owner-only answer until released"
    )


def test_lost_launch_response_never_relaunches(bridge_phone):
    api, _, _, call_id = bridge_phone
    key = pair(api, call_id)
    rid = api.store.enqueue(key, "job", "task", "Approved goal")
    api.store.approve_work(rid, SESSION, ["web"])
    life = Lifecycle()
    life.error = True
    ctx = SimpleNamespace(subagent_lifecycle=life)
    for _ in range(2):
        with pytest.raises(PhoneError):
            launch_work(ctx, api.store, api.settings, SESSION, rid)
    assert len(life.launches) == 1
    assert api.store.work(rid, SESSION)["state"] == "unknown"


def test_changed_or_empty_tool_permissions_refuse_launch(bridge_phone):
    api, _, _, call_id = bridge_phone
    key = pair(api, call_id)
    rid = api.store.enqueue(key, "job", "task", "Approved goal")
    with pytest.raises(PhoneError):
        api.store.approve_work(rid, SESSION, [])
    api.store.approve_work(rid, SESSION, ["web"])
    with pytest.raises(PhoneError):
        api.store.claim_work(rid, SESSION, ["terminal"])


def test_host_commands_pair_answer_approve_and_share(bridge_phone, monkeypatch):
    from talk_phone import plugin

    api, fake, _, call_id = bridge_phone
    monkeypatch.setattr(plugin, "service", lambda ctx: api)
    monkeypatch.setattr(plugin, "session_id", lambda kwargs: kwargs.get("session_id", ""))
    ctx = Context()
    ctx.subagent_lifecycle = Lifecycle()
    plugin.register(ctx)
    source = SimpleNamespace(user_id="synthetic-owner")

    def command(raw, session=SESSION, transport_source=source):
        return json.loads(
            ctx.commands["phone"]["handler"](
                raw, session_id=session, platform="local", source=transport_source
            )
        )

    row = api.store.connect("transport-command-test")
    fake.completed = True
    assert "error" in command(f"pair {row['code']} {call_id}")
    fake.completed = False
    assert "error" in command(f"pair {row['code']} {call_id}", transport_source=None)
    assert command(f"pair {row['code']} {call_id}")["status"] == "paired"
    key = row["connection"]
    rid = api.store.enqueue(key, "question", "question", "Can we use Tuesday?")
    assert command("inbox")["requests"][0]["request_id"] == rid
    assert "error" in command(f"answer {rid} Tuesday", session="wrong-session")
    assert command(f"answer {rid} Tuesday after 3 PM.")["state"] == "answered"
    assert api.store.remote_result(key, rid)["owner_answer"] == "Tuesday after 3 PM."
    job = api.store.enqueue(key, "job", "task", "UNTRUSTED marker: find public hours")
    hint = ctx.hooks["pre_llm_call"](session_id=SESSION)
    assert "UNTRUSTED marker" not in hint["context"]
    assert ctx.hooks["pre_llm_call"](session_id="different-session") is None
    assert command(f"allow-work {job}")["state"] == "approved"
    result = json.loads(
        ctx.tools["phone_process_work"]["handler"]({"request_id": job}, session_id=SESSION)
    )
    assert result["state"] == "running"
    cancelled = json.loads(
        ctx.tools["phone_work_cancel"]["handler"]({"request_id": job}, session_id=SESSION)
    )
    assert cancelled["cancel_requested"] is True
    assert api.store.work(job, SESSION)["state"] == "running"  # Cancellation is not completion.
    ctx.subagent_lifecycle.ready = True
    completed = json.loads(
        ctx.tools["phone_work_status"]["handler"]({"request_id": job}, session_id=SESSION)
    )
    assert completed["state"] == "succeeded"
    assert "error" in command(f"share {job}", transport_source=None)
    assert command(f"share {job}")["shared_with_caller"] is True
    assert "owner_shared_result" in api.store.remote_result(key, job)
    assert command(f"unpair {call_id}")["bridge_revoked"] is True
    with pytest.raises(PhoneError):
        api.store.remote_result(key, job)


def test_profile_store_and_restart_preserve_boundaries(bridge_phone, tmp_path):
    api, _, now, call_id = bridge_phone
    key = pair(api, call_id)
    rid = api.store.enqueue(key, "q", "question", "Persist me")
    reopened = BridgeStore(api.store.path, clock=lambda: now[0])
    assert reopened.inbox(SESSION)[0]["request_id"] == rid
    another_profile = BridgeStore(tmp_path / "other-profile.db", clock=lambda: now[0])
    assert another_profile.inbox(SESSION) == []
    with pytest.raises(PhoneError):
        another_profile.remote_result(key, rid)


def test_expired_work_approval_is_not_retried(bridge_phone):
    api, _, now, call_id = bridge_phone
    key = pair(api, call_id)
    rid = api.store.enqueue(key, "job", "task", "Approved goal")
    api.store.approve_work(rid, SESSION, ["web"])
    now[0] += 61
    with pytest.raises(PhoneError):
        api.store.claim_work(rid, SESSION, ["web"])


def test_unknown_host_handle_is_not_relaunched(bridge_phone):
    api, _, _, call_id = bridge_phone
    key = pair(api, call_id)
    rid = api.store.enqueue(key, "job", "task", "Approved goal")
    api.store.approve_work(rid, SESSION, ["web"])
    life = Lifecycle()
    ctx = SimpleNamespace(subagent_lifecycle=life)
    launch_work(ctx, api.store, api.settings, SESSION, rid)
    life.result = lambda handle: {"terminal_state": "UNKNOWN", "ready": False}
    assert refresh_work(ctx, api.store, SESSION, rid)["state"] == "unknown"
    with pytest.raises(PhoneError):
        launch_work(ctx, api.store, api.settings, SESSION, rid)
    assert len(life.launches) == 1


def test_max_duration_clock_does_not_reset_on_status_polls(bridge_phone):
    api, _, now, call_id = bridge_phone
    key = pair(api, call_id)
    rid = api.store.enqueue(key, "q", "question", "Bounded call")
    start = api.store.get(call_id, SESSION)["started_at"]
    now[0] += 50
    api.status(SESSION, call_id)
    assert api.store.get(call_id, SESSION)["started_at"] == start
    now[0] += 71
    with pytest.raises(PhoneError):
        api.store.remote_result(key, rid)


@contextmanager
def running_bridge(store):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(store, TOKEN)
    server = uvicorn.Server(
        uvicorn.Config(
            app, log_level="critical", access_log=False, ws="none", timeout_graceful_shutdown=2
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    try:
        while not server.started:
            if time.monotonic() >= deadline or not thread.is_alive():
                raise AssertionError("Local bridge did not start")
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def tool_payload(result):
    return json.loads(result.content[0].text)


def test_real_mcp_pair_question_answer_round_trip(bridge_phone):
    api, _, _, call_id = bridge_phone

    async def exercise(url):
        async with (
            httpx2.AsyncClient(headers={"Authorization": "Bearer " + TOKEN}) as http,
            streamable_http_client(url, http_client=http) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            catalog = await client.list_tools()
            assert {t.name for t in catalog.tools} == {
                "ask_owner",
                "delegate_task",
                "check_work",
            }
            assert all("token" not in json.dumps(t.model_dump()).lower() for t in catalog.tools)
            first = tool_payload(
                await client.call_tool(
                    "ask_owner",
                    {
                        "question": "May I book Tuesday?",
                        "request_key": "q1",
                        "wait_seconds": 0,
                    },
                )
            )
            assert first["state"] == "pairing_required"
            api.store.pair(first["pairing_code"], call_id, SESSION)
            task = asyncio.create_task(
                client.call_tool(
                    "ask_owner",
                    {
                        "question": "May I book Tuesday?",
                        "request_key": "q1",
                        "wait_seconds": 2,
                    },
                )
            )
            for _ in range(100):
                inbox = api.store.inbox(SESSION)
                if inbox:
                    api.store.answer(inbox[0]["request_id"], SESSION, "Tuesday after 3 PM.")
                    break
                await asyncio.sleep(0.01)
            response = tool_payload(await task)
            assert response["owner_answer"] == "Tuesday after 3 PM."
            repeated = tool_payload(
                await client.call_tool("check_work", {"request_id": response["request_id"]})
            )
            assert repeated == response

    with running_bridge(api.store) as url:
        asyncio.run(exercise(url))


def test_bridge_rejects_unauthenticated_browser_and_wrong_host(bridge_phone):
    api, _, _, _ = bridge_phone

    async def exercise(url):
        async with httpx2.AsyncClient() as http:
            assert (await http.get(url)).status_code == 401
            assert (
                await http.get(url, headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
            assert (
                await http.get(
                    url,
                    headers={
                        "Authorization": "Bearer " + TOKEN,
                        "Origin": "https://malicious.invalid",
                    },
                )
            ).status_code == 403
            response = await http.get(
                url, headers={"Authorization": "Bearer " + TOKEN, "Host": "malicious.invalid"}
            )
            assert response.status_code in {400, 421}

    with running_bridge(api.store) as url:
        asyncio.run(exercise(url))
