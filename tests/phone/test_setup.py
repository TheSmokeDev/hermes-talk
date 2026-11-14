from dataclasses import replace

import pytest

from talk_phone.policy import DISCLOSURE, PhoneError, Settings
from talk_phone.setup import collect, phone_workflow

from .conftest import NUMBER, OWNER


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
