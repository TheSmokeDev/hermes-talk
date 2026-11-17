import json
from types import SimpleNamespace

import yaml

from talk_phone import plugin

from .conftest import OWNER, SESSION, prepared


class Context:
    def __init__(self):
        self.tools, self.hooks, self.commands = {}, {}, {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, **kwargs):
        self.commands[name] = kwargs

    def register_cli_command(self, **kwargs):
        self.commands[kwargs["name"]] = kwargs

    def register_skill(self, name, path, **kwargs):
        assert path.is_file()
        self.commands[f"skill:{name}"] = kwargs

    def get_config(self, name, default=None):
        return True if name == "phone_enabled" else default


def test_manifest_matches_inert_registration():
    from pathlib import Path

    ctx = Context()
    plugin.register(ctx)
    manifest = yaml.safe_load(
        (Path(__file__).parents[2] / "plugin.yaml").read_text(encoding="utf-8")
    )
    assert set(ctx.tools) == set(manifest["provides_tools"])
    assert set(ctx.hooks) <= set(manifest["provides_hooks"])
    assert "phone_approve" not in ctx.tools


def test_command_requires_transport_identity_and_same_session(setup_phone, monkeypatch):
    monkeypatch.setattr(plugin, "enabled", lambda ctx: True)
    monkeypatch.setattr(plugin, "require_enabled", lambda ctx: None)
    api, _, _ = setup_phone
    monkeypatch.setattr(plugin, "service", lambda ctx: api)
    call_id = prepared(api)
    raw = f"approve {call_id}"
    assert "error" in json.loads(plugin.command(None, raw, session_id=SESSION, platform="local"))
    source = SimpleNamespace(user_id="synthetic-owner")
    assert "error" in json.loads(
        plugin.command(None, raw, session_id="other", platform="local", source=source)
    )
    result = json.loads(
        plugin.command(None, raw, session_id=SESSION, platform="local", source=source)
    )
    assert result["status"] == "approved"
    assert api.store.get(call_id, SESSION)["approved_by"] == OWNER


def test_model_gate_blocks_unapproved_call(setup_phone, monkeypatch):
    api, _, _ = setup_phone
    monkeypatch.setattr(plugin, "service", lambda ctx: api)
    monkeypatch.setattr(plugin, "session_id", lambda kwargs: SESSION)
    ctx = Context()
    plugin.register(ctx)
    gate = ctx.hooks["pre_tool_call"]
    call_id = prepared(api)
    assert gate("phone_call", {"call_id": call_id})["action"] == "block"
    api.approve(SESSION, call_id, OWNER)
    assert gate("phone_call", {"call_id": call_id}) is None
    api.store.kill()
    assert gate("phone_call", {"call_id": call_id})["action"] == "block"
