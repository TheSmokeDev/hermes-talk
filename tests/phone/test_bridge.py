"""Phone is another input to the same Hermes session; no manual pairing or task gate."""

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

import httpx2
import pytest
import uvicorn

from talk_phone.bridge import create_app
from talk_phone.bridge_store import BridgeStore
from talk_phone.policy import PhoneError
from talk_phone.service import PhoneService

from .conftest import SESSION, approved, prepared

TOKEN = "synthetic-bridge-token-at-least-32-characters"


@pytest.fixture
def bridge_phone(setup_phone):
    api, fake, now = setup_phone
    store = BridgeStore(api.store.path, clock=lambda: now[0])
    api = PhoneService(replace(api.settings, bridge_enabled=True), store, api.client_factory)
    call_id = approved(api)
    api.call(SESSION, call_id)
    return api, fake, now, call_id


def route(api, fake):
    return api.store.route(fake.posts[-1]["bridge_capability"])


def test_call_connects_automatically_without_a_pairing_step(bridge_phone):
    api, fake, _, call_id = bridge_phone
    key = route(api, fake)
    assert route(api, fake) == key
    rid = api.store.enqueue(key, "q", "question", "What do you remember from this chat?")
    api.store.answer(rid, SESSION, "amber river")
    assert api.store.remote_result(key, rid)["owner_answer"] == "amber river"
    journal = api.store.get(call_id, SESSION)
    assert "bridge_capability" not in json.dumps(journal)
    with api.store.connection() as db:
        assert fake.posts[-1]["bridge_capability"] not in str(
            db.execute("SELECT * FROM phone_call_routes").fetchall()
        )


def test_route_is_created_only_after_call_approval(bridge_phone):
    api, _, _, _ = bridge_phone
    call_id = prepared(api)
    with pytest.raises(PhoneError):
        api.store.issue_route(call_id, SESSION)
    with pytest.raises(PhoneError):
        api.store.route("unknown-route")


def test_question_retry_and_session_bound_answer(bridge_phone):
    api, fake, _, _ = bridge_phone
    key = route(api, fake)
    rid = api.store.enqueue(key, "q", "question", "Which day?")
    assert api.store.enqueue(key, "q", "question", "Which day?") == rid
    with pytest.raises(PhoneError):
        api.store.enqueue(key, "q", "question", "Changed question")
    with pytest.raises(PhoneError):
        api.store.answer(rid, "other-session", "Monday")
    assert api.store.claim_turn(rid, SESSION)["id"] == rid
    assert api.store.claim_turn(rid, SESSION) is None
    api.store.answer(rid, SESSION, "Monday")
    assert api.store.remote_result(key, rid)["owner_answer"] == "Monday"


def test_call_end_and_stop_disconnect_the_transport(bridge_phone):
    api, fake, _, call_id = bridge_phone
    capability = fake.posts[-1]["bridge_capability"]
    key = route(api, fake)
    rid = api.store.enqueue(key, "q", "question", "Continue?")
    fake.completed = True
    api.status(SESSION, call_id)
    with pytest.raises(PhoneError):
        api.store.route(capability)
    with pytest.raises(PhoneError):
        api.store.remote_result(key, rid)


def test_restarting_transport_keeps_the_same_session(bridge_phone):
    api, fake, now, _ = bridge_phone
    key = route(api, fake)
    rid = api.store.enqueue(key, "q", "question", "Persist me")
    reopened = BridgeStore(api.store.path, clock=lambda: now[0])
    assert reopened.route(fake.posts[-1]["bridge_capability"]) == key
    assert reopened.inbox(SESSION)[0]["request_id"] == rid
    api.store.kill()
    api.store.kill(False)
    with pytest.raises(PhoneError):
        reopened.route(fake.posts[-1]["bridge_capability"])


def test_request_uses_full_call_window_without_separate_work_approval(bridge_phone):
    api, fake, now, _ = bridge_phone
    key = route(api, fake)
    ids = [api.store.enqueue(key, str(i), "question", "Next turn") for i in range(5)]
    now[0] += 91
    api.store.answer(ids[-1], SESSION, "Still connected")
    assert api.store.remote_result(key, ids[-1])["state"] == "answered"
    now[0] += 30
    with pytest.raises(PhoneError):
        api.store.remote_result(key, ids[-1])


@contextmanager
def running_bridge(store):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
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
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def test_http_turn_answer_and_reconnect_without_pairing(bridge_phone):
    api, fake, _, _ = bridge_phone
    capability = fake.posts[-1]["bridge_capability"]

    async def exercise(url):
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + TOKEN}) as http:
            payload = {
                "call_route": capability,
                "message": "Which test phrase?",
                "request_key": "q",
            }
            task = asyncio.create_task(http.post(url + "/phone/turn", json=payload))
            for _ in range(100):
                inbox = api.store.inbox(SESSION)
                if inbox:
                    api.store.answer(inbox[0]["request_id"], SESSION, "amber river")
                    break
                await asyncio.sleep(0.01)
            result = (await task).json()
            assert result["state"] == "answered" and result["owner_answer"] == "amber river"
            assert (await http.post(url + "/phone/turn", json=payload)).json() == result
            assert (
                await http.post(
                    url + "/phone/result",
                    json={"call_route": capability, "request_id": result["request_id"]},
                )
            ).json() == result
            assert len(api.store.inbox(SESSION)) == 1
            assert "pair" not in json.dumps(result) and capability not in json.dumps(result)
            response = await http.post(
                url + "/phone/result",
                json={"call_route": "different-call", "request_id": result["request_id"]},
            )
            assert response.status_code == 400

    with running_bridge(api.store) as url:
        asyncio.run(exercise(url))


def test_transport_rejects_wrong_credentials_browser_and_wrong_host(bridge_phone):
    api, _, _, _ = bridge_phone

    async def exercise(url):
        async with httpx2.AsyncClient() as http:
            assert (await http.post(url + "/phone/turn")).status_code == 401
            assert (
                await http.post(url + "/phone/turn", headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
            assert (
                await http.post(
                    url + "/phone/turn",
                    headers={
                        "Authorization": "Bearer " + TOKEN,
                        "Origin": "https://example.invalid",
                    },
                )
            ).status_code == 403
            assert (
                await http.post(
                    url + "/phone/turn",
                    headers={"Authorization": "Bearer " + TOKEN, "Host": "example.invalid"},
                )
            ).status_code == 400
            assert (
                await http.post(
                    url + "/phone/turn",
                    headers={"Authorization": "Bearer " + TOKEN},
                    content=b"x" * 32769,
                )
            ).status_code == 413

    with running_bridge(api.store) as url:
        asyncio.run(exercise(url))
