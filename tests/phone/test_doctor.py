import argparse
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from talk_phone import cli, plugin
from talk_phone import doctor as doctor_module
from talk_phone.client import DograhClient
from talk_phone.doctor import cli_report, inspect

from .test_client import dograh_http
from .test_plugin import Context


@pytest.mark.parametrize("entry,state", [
    ({}, "missing"), ({"settings": {"allow_gateway_injection": True}}, "missing"),
    ({"allow_gateway_injection": "true"}, "missing"),
    ({"allow_gateway_injection": True}, "granted"), (None, "unverified"),
])
def test_doctor_reports_only_the_owner_gate_from_the_active_profile(entry, state, monkeypatch):
    def read_config():
        if entry is None:
            raise OSError("private fixture details")
        return {"plugins": {"entries": {
            "hermes-talk": entry, "other": {"allow_gateway_injection": True},
        }}}

    monkeypatch.setitem(sys.modules, "hermes_cli.config", SimpleNamespace(
        load_config_readonly=read_config
    ))
    monkeypatch.setitem(
        sys.modules, "agent.secret_scope", SimpleNamespace(get_secret=lambda _: None)
    )
    report = doctor_module.doctor(Context())
    for projection in (report, cli_report(report)):
        assert projection["gateway_injection"] == state
        assert "private fixture details" not in str(projection)
        if state == "granted":
            assert "gateway_injection_help" not in projection
        else:
            assert "plugins.entries.hermes-talk.allow_gateway_injection: true" in (
                projection["gateway_injection_help"]
            )


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
    report = {"connection_ready": True, "call_attempted": False}
    monkeypatch.setattr(plugin, "doctor", lambda ctx: report)
    monkeypatch.setattr(
        plugin, "service", lambda ctx: pytest.fail("Doctor must not open the call journal")
    )
    ctx = Context()
    plugin.register(ctx)
    assert "skill:phone" in ctx.commands and "dograh" not in ctx.commands
    monkeypatch.setattr(doctor_module, "doctor", lambda ctx: report)
    assert cli.run(ctx, argparse.Namespace(phone_action="doctor")) == 0
    assert json.loads(capsys.readouterr().out) == cli_report(report)
    assert json.loads(plugin.command(ctx, "doctor")) == report


def test_cli_report_excludes_arbitrary_sensitive_provider_text():
    result = cli_report(
        {
            "connection_ready": True,
            "checks": [{"name": "api_key", "ok": False, "message": "PRIVATE-PROVIDER-VALUE"}],
            "note": "PRIVATE-PROVIDER-VALUE",
        }
    )
    assert "PRIVATE-PROVIDER-VALUE" not in json.dumps(result)
    assert result["connection_ready"] and not result["call_attempted"]


def test_doctor_preserves_unknown_call_and_approval(setup_phone):
    from .conftest import SESSION, approved

    api, _, _ = setup_phone
    call_id = approved(api)
    before = api.store.get(call_id, SESSION)
    with dograh_http(api.settings) as (settings, _):
        assert inspect(settings, lambda _: "key", DograhClient)["connection_ready"]
    assert api.store.get(call_id, SESSION) == before
