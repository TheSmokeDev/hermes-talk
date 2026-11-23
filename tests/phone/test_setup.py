import sys
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from talk_phone.policy import DISCLOSURE, PhoneError, Settings
from talk_phone.setup import choose, collect, phone_workflow

from .conftest import NUMBER, OWNER


class PluginSettings:
    def __init__(self, settings):
        self.values = {"phone_" + key: list(value) if isinstance(value, tuple) else value
                       for key, value in asdict(settings).items()}
        self.writes = []

    def get_config(self, key, default=None):
        return self.values.get(key, default)

    def set_config(self, key, value):
        self.writes.append((key, value))
        self.values[key] = value


@pytest.fixture
def setup_secrets(monkeypatch):
    secrets = {"DOGRAH_API_KEY": "fixture-secret-api-key", "DOGRAH_BRIDGE_TOKEN": "fixture-" * 6}
    monkeypatch.setitem(sys.modules, "agent.secret_scope", SimpleNamespace(get_secret=secrets.get))
    monkeypatch.setitem(sys.modules, "hermes_cli.setup", SimpleNamespace(
        prompt=lambda *a, **kw: pytest.fail("Unexpected prompt"),
        prompt_choice=lambda *a, **kw: pytest.fail("Unexpected choice"),
        prompt_yes_no=lambda *a, **kw: True,
    ))
    return secrets


@pytest.mark.parametrize("name,value", [
    ("DOGRAH_API_KEY", None), ("DOGRAH_BRIDGE_TOKEN", None), ("DOGRAH_BRIDGE_TOKEN", "short"),
])
def test_missing_owner_secrets_stop_before_network_or_writes(
    setup_secrets, name, value, monkeypatch, capsys
):
    from talk_phone import setup

    setup_secrets[name] = value
    monkeypatch.setattr(setup, "DograhClient", lambda *a: pytest.fail("Unexpected Dograh access"))
    ctx = PluginSettings(Settings())
    assert setup.run(ctx) == 1 and ctx.writes == []
    output = capsys.readouterr().out
    assert "hermes plugins config hermes-talk" in output
    assert "plugins.entries.hermes-talk.allow_gateway_injection: true" in output.splitlines()
    assert "fixture-secret" not in output


@pytest.mark.parametrize("initial,keep", [(True, False), (False, True), (False, False)])
def test_setup_saves_only_namespaced_settings_in_both_paths(
    setup_secrets, setup_phone, initial, keep, monkeypatch, capsys
):
    from talk_phone import setup

    configured = setup_phone[0].settings
    ctx = PluginSettings(Settings() if initial else configured)
    collected = []

    def collect(settings, key, prompts, confirm):
        assert key == setup_secrets["DOGRAH_API_KEY"]
        collected.append(True)
        return configured

    def connect(client, settings, token, prompt):
        assert token == setup_secrets["DOGRAH_BRIDGE_TOKEN"]
        return replace(settings, bridge_enabled=True, bridge_public_url="https://test.invalid")

    monkeypatch.setattr(sys.modules["hermes_cli.setup"], "prompt_yes_no", lambda *a, **kw: keep)
    monkeypatch.setattr(setup, "DograhClient", Provider().factory)
    monkeypatch.setattr(setup, "collect", collect)
    monkeypatch.setattr(setup, "connect_conversation", connect)
    assert setup.run(ctx) == 0
    assert bool(collected) is (initial or not keep)
    assert ctx.writes[-1] == ("phone_enabled", True)
    assert ctx.values["phone_dialing_enabled"] is True
    assert ctx.values["phone_bridge_public_url"] == "https://test.invalid"
    assert all(key.startswith("phone_") for key, _ in ctx.writes)
    output = capsys.readouterr().out
    assert "plugins.entries.hermes-talk.allow_gateway_injection: true" in output
    assert not any(value in output or value in repr(ctx.writes) for value in setup_secrets.values())


def test_interrupted_reconfiguration_leaves_dialing_disabled(setup_phone):
    from talk_phone.context import save_phone_settings

    configured = setup_phone[0].settings

    class FailingSettings(PluginSettings):
        def set_config(self, key, value):
            if key == "phone_owner_user_ids":
                raise OSError("Failed fixture write")
            super().set_config(key, value)

    ctx = FailingSettings(configured)
    with pytest.raises(OSError):
        save_phone_settings(ctx, replace(configured, owner_user_ids=("local:new-owner",)))
    assert ctx.writes == [("phone_dialing_enabled", False)]


def test_fallback_selection_does_not_log_private_contact_metadata(capsys):
    rows = [{"id": 2, "name": "PRIVATE-NAME", "address": "PRIVATE-CONTACT"}]
    assert choose(rows, "caller", lambda *args, **kwargs: "2") == rows[0]
    output = capsys.readouterr().out
    assert "PRIVATE-NAME" not in output and "PRIVATE-CONTACT" not in output


class Provider:
    def __init__(self):
        self.requests = []

    def factory(self, settings, key):
        return self

    def request(self, method, path, body=None):
        self.requests.append((method, path, body))
        if path.endswith("/telephony-configs"):
            return {
                "configurations": [
                    {
                        "id": 2,
                        "name": "Test carrier",
                        "inactive": False,
                        "supports_outbound": True,
                        "is_ready_for_outbound": True,
                    },
                    {
                        "id": 3,
                        "inactive": False,
                        "supports_outbound": True,
                        "is_ready_for_outbound": False,
                    },
                ]
            }
        if path.endswith("/phone-numbers"):
            return {
                "phone_numbers": [
                    {
                        "id": 4,
                        "telephony_configuration_id": 2,
                        "is_active": True,
                        "address_type": "pstn",
                    }
                ]
            }
        if path.endswith("/create/definition"):
            return {"id": 5, "workflow_uuid": "00000000-0000-4000-8000-000000000005"}
        return {}

    def preflight(self):
        return {}

    def check_caller(self):
        return {}


def prompts(*answers):
    answers = iter(answers)
    return lambda *args, **kwargs: next(answers)


def test_setup_creates_only_explicit_test_agent_and_keeps_dialing_off():
    provider = Provider()
    confirmations = iter([True, False])
    settings = collect(
        Settings(),
        "synthetic-key",
        prompts("https://test.invalid", "2", "4", NUMBER, OWNER),
        lambda *a, **kw: next(confirmations),
        provider.factory,
    )
    assert settings.workflow_id == 5 and settings.allowed_numbers == (NUMBER,)
    assert settings.owner_user_ids == (OWNER,) and not settings.dialing_enabled
    writes = [(m, p) for m, p, _ in provider.requests if m != "GET"]
    assert writes == [
        ("POST", "/api/v1/workflow/create/definition"),
        ("PUT", "/api/v1/workflow/5"),
        ("POST", "/api/v1/workflow/5/publish"),
    ]
    graph = phone_workflow()
    assert graph["nodes"][0]["data"]["greeting"] == DISCLOSURE
    assert graph["nodes"][0]["data"]["allow_greeting_interrupt"] is False


def test_setup_rejects_unlisted_carrier_without_remote_writes():
    provider = Provider()
    with pytest.raises(PhoneError, match="listed carrier"):
        collect(
            Settings(),
            "synthetic",
            prompts("https://test.invalid", "3"),
            lambda *a, **kw: False,
            provider.factory,
        )
    assert all(m == "GET" for m, _, _ in provider.requests)


def test_setup_declined_creation_does_not_mutate_remote():
    provider = Provider()
    with pytest.raises(PhoneError, match="cancelled"):
        collect(
            Settings(),
            "synthetic",
            prompts("https://test.invalid", "2", "4"),
            lambda *a, **kw: False,
            provider.factory,
        )
    assert all(m == "GET" for m, _, _ in provider.requests)


def test_setup_reuses_agent_without_republishing_or_enabling_dialing():
    provider = Provider()
    settings = replace(Settings(), workflow_id=5, agent_uuid="00000000-0000-4000-8000-000000000005")
    result = collect(
        settings,
        "synthetic",
        prompts("https://test.invalid", "2", "4", NUMBER, OWNER),
        lambda *a, **kw: False,
        provider.factory,
    )
    assert not result.dialing_enabled
    assert all(m == "GET" for m, _, _ in provider.requests)


def test_conversation_tool_uses_server_context_and_existing_hermes_permissions():
    from talk_phone.setup import CONVERSATION_PROMPT, conversation_tool_definitions

    tools = conversation_tool_definitions("http://bridge-relay:8787", "synthetic-credential")
    assert [t["name"] for t in tools] == ["hermes_chat", "hermes_result"]
    for tool in tools:
        cfg = tool["definition"]["config"]
        assert cfg["credential_uuid"] == "synthetic-credential"
        assert cfg["body_template"]["call_route"] == "{{initial_context.hermes_call_route}}"
        assert "call_route" not in {p["name"] for p in cfg["parameters"]}
    assert "same memory, tools and permissions" in CONVERSATION_PROMPT
    assert "allowed_toolsets" not in str(tools)


def test_conversation_setup_preserves_other_workflows_and_carrier_configuration():
    from copy import deepcopy

    from talk_phone.setup import configure_conversation

    graph = phone_workflow()
    agent = next(n for n in graph["nodes"] if n["type"] == "agentNode")
    agent["data"]["prompt"] = "Legacy prompt"
    version = {"workflow_json": graph, "workflow_configurations": {"max_call_duration": 120}}
    writes = []

    class Client:
        def request(self, method, path, body=None):
            if method == "GET" and "versions?" in path:
                return [deepcopy(version)]
            if method == "GET":
                return [
                    {
                        "name": "hermes_chat",
                        "tool_uuid": "another-workflow-tool",
                        "definition": {
                            "type": "http_api",
                            "config": {"url": "https://other.invalid"},
                        },
                    }
                ]
            writes.append((method, path, deepcopy(body)))
            if path == "/api/v1/tools/":
                return {"tool_uuid": body["name"] + "-new"}
            return {}

    settings = replace(Settings(), workflow_id=1, bridge_public_url="https://phone.invalid")
    configure_conversation(Client(), settings, "synthetic-credential")
    assert not any("another-workflow-tool" in path for _, path, _ in writes)
    update = next(body for method, path, body in writes if method == "PUT")
    assert update["workflow_configurations"] == {"max_call_duration": 120}
    assert update["workflow_definition"]["nodes"][0] == graph["nodes"][0]
    assert all("telephony" not in path for _, path, _ in writes)
