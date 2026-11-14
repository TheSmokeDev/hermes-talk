import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from talk_phone.client import DISCLOSURE, DograhClient
from talk_phone.policy import PhoneError

from .conftest import SESSION, approved


@contextmanager
def dograh_http(settings):
    state = {
        "requests": [],
        "cap": 120,
        "greeting": DISCLOSURE,
        "greeting_type": "text",
        "greeting_interrupt": False,
        "redirect": False,
        "completed": True,
        "bound_call_id": "synthetic-call-id",
        "carrier_ready": True,
        "phone_active": True,
        "phone_carrier": 1,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["requests"].append(("GET", self.path, self.headers.get("X-API-Key")))
            if state["redirect"]:
                self.send_response(302)
                self.send_header("Location", "/credential-leak-target")
                self.end_headers()
                return
            if "/versions?" in self.path:
                value = [
                    {
                        "id": 9,
                        "status": "published",
                        "workflow_configurations": {"max_call_duration": state["cap"]},
                        "workflow_json": {
                            "nodes": [
                                {
                                    "type": "startCall",
                                    "data": {
                                        "prompt": "Follow the owner's test instructions.",
                                        "greeting": state["greeting"],
                                        "greeting_type": state["greeting_type"],
                                        "allow_greeting_interrupt": state["greeting_interrupt"],
                                        "add_global_prompt": False,
                                        "allow_interrupt": False,
                                    },
                                }
                            ]
                        },
                    }
                ]
            elif self.path.endswith("/telephony-configs"):
                value = {
                    "configurations": [
                        {
                            "id": 1,
                            "inactive": False,
                            "supports_outbound": True,
                            "is_ready_for_outbound": state["carrier_ready"],
                            "name": "private-provider-name",
                        }
                    ]
                }
            elif self.path.endswith("/phone-numbers"):
                value = {
                    "phone_numbers": [
                        {
                            "id": 1,
                            "telephony_configuration_id": state["phone_carrier"],
                            "is_active": state["phone_active"],
                            "address_type": "pstn",
                            "address": "private-phone-number",
                        }
                    ]
                }
            elif "/runs/" in self.path:
                value = {
                    "id": 42,
                    "workflow_id": 1,
                    "is_completed": state["completed"],
                    "initial_context": {"hermes_call_id": state["bound_call_id"]},
                    "transcript_public_url": "https://secret.invalid/transcript?token=private",
                    "recording_url": "sensitive",
                    "public_access_token": "never-return-me",
                    "cost_info": {"total": 0},
                    "gathered_context": {"result": "test done"},
                }
            else:
                value = {"workflow_uuid": settings.agent_uuid}
            self.respond(value)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(("POST", self.path, body))
            self.respond({"status": "success", "workflow_run_id": 42, "workflow_run_name": "test"})

        def respond(self, value):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield replace(settings, base_url=f"http://127.0.0.1:{server.server_port}"), state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_http_round_trip_to_local_fake(setup_phone):
    api, _, _ = setup_phone
    with dograh_http(api.settings) as (settings, state):
        api.settings = settings
        api.client_factory = lambda s: DograhClient(s, "synthetic-key")
        call_id = approved(api)
        assert api.call(SESSION, call_id)["run_id"] == 42
        result = api.status(SESSION, call_id)
        assert result["status"] == "completed"
        assert result["untrusted_call_data"]["has_transcript"]
        assert "private" not in json.dumps(result)
        assert "never-return-me" not in json.dumps(result)
        posts = [r for r in state["requests"] if r[0] == "POST"]
        assert len(posts) == 1
        assert posts[0][1] == f"/api/v1/public/agent/workflow/{settings.agent_uuid}"
        assert posts[0][2]["initial_context"]["hermes_call_id"] == call_id
        assert posts[0][2]["telephony_configuration_id"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("cap", None),
        ("cap", 600),
        ("greeting", "Hello, I'm a human."),
        ("greeting", None),
        ("greeting_type", "audio"),
        ("greeting_interrupt", True),
    ],
)
def test_remote_preflight_refuses_before_post(setup_phone, field, value):
    api, _, _ = setup_phone
    with dograh_http(api.settings) as (settings, state):
        api.settings = settings
        state[field] = value
        api.client_factory = lambda s: DograhClient(s, "synthetic-key")
        with pytest.raises(PhoneError):
            api.call(SESSION, approved(api))
        assert not any(r[0] == "POST" for r in state["requests"])


def test_redirect_never_receives_key(setup_phone):
    api, _, _ = setup_phone
    with dograh_http(api.settings) as (settings, state):
        state["redirect"] = True
        with pytest.raises(PhoneError, match="302"):
            DograhClient(settings, "synthetic-key").preflight()
        assert len(state["requests"]) == 1


def test_pairing_verifies_provider_run_and_local_call_binding(setup_phone):
    api, _, _ = setup_phone
    with dograh_http(api.settings) as (settings, state):
        client = DograhClient(settings, "synthetic-key")
        with pytest.raises(PhoneError):
            client.verify_bridge_run(42, "synthetic-call-id")  # Completed run.
        state["completed"] = False
        with pytest.raises(PhoneError):
            client.verify_bridge_run(42, "different-call")
        client.verify_bridge_run(42, "synthetic-call-id")
