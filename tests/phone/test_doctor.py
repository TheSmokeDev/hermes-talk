import argparse
import json
from dataclasses import replace

import pytest

from talk_phone import plugin
from talk_phone.client import DograhClient
from talk_phone.doctor import inspect

from .test_client import dograh_http
from .test_plugin import Context


def test_connected_profile_can_keep_dialing_disabled(setup_phone):
    api, _, _ = setup_phone
    with dograh_http(api.settings) as (settings, state):
        settings = replace(settings, dialing_enabled=False, allowed_numbers=(), bridge_enabled=True)
        secrets = {"DOGRAH_API_KEY": "synthetic-key", "DOGRAH_BRIDGE_TOKEN": "x" * 32}
        result = inspect(settings, secrets.get)
        assert result["connection_ready"]
        assert not result["dialing_configuration_ready"]
        assert result["approval_required"] and not result["call_attempted"]
        assert not any(r[0] != "GET" for r in state["requests"])
        assert len(state["requests"]) == 4
        rendered = json.dumps(result)
        for private in (
            *secrets.values(),
            "private-phone-number",
            "private-provider-name",
            settings.base_url,
        ):
            assert private not in rendered


@pytest.mark.parametrize(
    "field,value", [("carrier_ready", False), ("phone_active", False), ("phone_carrier", 2)]
)
def test_carrier_or_caller_mismatch_fails_closed(setup_phone, field, value):
    api, _, _ = setup_phone
    with dograh_http(api.settings) as (settings, state):
        state[field] = value
        result = inspect(settings, lambda _: "synthetic-key")
        assert not result["connection_ready"]
        assert not result["dialing_configuration_ready"]
        assert all(r[0] == "GET" for r in state["requests"])


def test_missing_key_or_invalid_ids_skip_network(setup_phone):
    api, _, _ = setup_phone

    def no_client(*args):
        pytest.fail("Incomplete setup must not contact Dograh")

    assert not inspect(api.settings, lambda _: None, no_client)["connection_ready"]
    assert not inspect(replace(api.settings, workflow_id=0), lambda _: "key", no_client)[
        "connection_ready"
    ]


def test_invalid_allowlist_does_not_look_ready(setup_phone):
    api, _, _ = setup_phone
    with dograh_http(api.settings) as (settings, _):
        result = inspect(replace(settings, allowed_numbers=("911",)), lambda _: "key")
        assert result["connection_ready"]
        assert not result["dialing_configuration_ready"]


def test_bad_workflow_and_raw_exception_are_not_hidden_as_success(setup_phone):
    api, _, _ = setup_phone
    with dograh_http(api.settings) as (settings, state):
        state["greeting"] = "Different prompt"
        assert not inspect(settings, lambda _: "key")["connection_ready"]

    def failed_secret(_):
        raise RuntimeError("DO-NOT-RETURN-SECRET")

    result = inspect(api.settings, failed_secret)
    assert not result["connection_ready"]
    assert "DO-NOT-RETURN-SECRET" not in json.dumps(result)


def test_cli_and_slash_use_same_diagnostics_without_call_service(monkeypatch, capsys):
    monkeypatch.setattr(
        plugin, "doctor", lambda ctx: {"connection_ready": True, "call_attempted": False}
    )
    monkeypatch.setattr(
        plugin, "service", lambda ctx: pytest.fail("Doctor must not open the call journal")
    )
    ctx = Context()
    plugin.register(ctx)
    assert "skill:quickstart" in ctx.commands
    parser = argparse.ArgumentParser()
    cli = ctx.commands["dograh"]
    cli["setup_fn"](parser)
    assert cli["handler_fn"](parser.parse_args(["doctor"])) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(plugin.command(ctx, "doctor"))


def test_doctor_preserves_unknown_call_and_approval(setup_phone):
    from .conftest import SESSION, approved

    api, _, _ = setup_phone
    call_id = approved(api)
    before = api.store.get(call_id, SESSION)
    with dograh_http(api.settings) as (settings, _):
        assert inspect(settings, lambda _: "key", DograhClient)["connection_ready"]
    assert api.store.get(call_id, SESSION) == before
