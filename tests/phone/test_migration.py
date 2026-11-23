import sqlite3
import sys
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import ModuleType

import pytest

from talk_phone import migration
from talk_phone.bridge_store import BridgeStore
from talk_phone.policy import PhoneError
from talk_phone.store import Store


def install_public_helpers(monkeypatch, tmp_path, config):
    root = tmp_path / "plugins"
    settings = ModuleType("hermes_cli.config")
    settings.load_config_readonly = lambda: deepcopy(config)
    saved = []
    secrets = ModuleType("agent.secret_scope")
    secrets.get_secret = lambda key: "synthetic-key"
    storage = ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda plugin: root / plugin
    monkeypatch.setitem(sys.modules, "hermes_cli.config", settings)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secrets)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    monkeypatch.setattr(migration, "data_path", lambda: root / "hermes-talk/phone/calls.db")

    class VerifiedClient:
        def __init__(self, *args):
            pass

        def preflight(self):
            return {}

        def check_caller(self):
            return {}

    monkeypatch.setattr(migration, "DograhClient", VerifiedClient)
    class Context:
        def get_config(self, key, default=None):
            return config["plugins"]["entries"].get("hermes-talk", {}).get("settings", {}).get(
                key, default
            )

        def set_config(self, key, value):
            assert key.startswith("phone_")
            saved.append(key)
            config["plugins"]["entries"].setdefault("hermes-talk", {}).setdefault(
                "settings", {}
            )[key] = value

    return root, saved, Context()


def test_import_preserves_history_and_revokes_old_approval(monkeypatch, tmp_path, setup_phone):
    api, _, _ = setup_phone
    config = {
        "plugins": {
            "enabled": ["unrelated", "hermes-talk"],
            "disabled": ["hermes-dograh"],
            "entries": {"hermes-dograh": {"settings": asdict(api.settings)}},
        }
    }
    root, saved, ctx = install_public_helpers(monkeypatch, tmp_path, config)
    old = BridgeStore(root / "hermes-dograh/calls.db", clock=lambda: 1000)
    old.prepare("owner-session", {"purpose": "synthetic history"})
    with old.connection(write=True) as db:
        db.execute(
            "INSERT INTO bridge_connections(connection,code,created,expires) "
            "VALUES('legacy-transport','legacy-code',1000,1120)"
        )
    result = migration.import_dograh(ctx)
    assert result["call_placed"] is False and result["old_journal_preserved"]
    assert config["plugins"]["enabled"] == ["unrelated", "hermes-talk"]
    assert config["plugins"]["disabled"] == ["hermes-dograh"]
    assert config["plugins"]["entries"]["hermes-dograh"]["settings"] == asdict(api.settings)
    assert saved[-1] == "phone_enabled" and ctx.get_config("phone_enabled") is True
    assert result["owner_commands"] == [
        "hermes plugins disable hermes-dograh", "hermes plugins enable hermes-talk"
    ]
    with sqlite3.connect(root / "hermes-talk/phone/calls.db") as copied:
        assert copied.execute("SELECT status FROM calls").fetchone()[0] == "cancelled"
        assert copied.execute("SELECT revoked FROM bridge_connections").fetchone()[0] == 1
    with sqlite3.connect(old.path) as original:
        assert original.execute("SELECT status FROM calls").fetchone()[0] == "prepared"
        assert original.execute("SELECT revoked FROM bridge_connections").fetchone()[0] == 0


def test_import_refuses_an_active_call_without_creating_target(monkeypatch, tmp_path):
    config = {"plugins": {"entries": {"hermes-dograh": {"settings": {}}}}}
    root, saved, ctx = install_public_helpers(monkeypatch, tmp_path, config)
    old = Store(root / "hermes-dograh/calls.db", clock=lambda: 1000)
    call_id = old.prepare("owner-session", {"purpose": "synthetic active"})
    old.finish(call_id, "owner-session", "started", run_id=42)
    with pytest.raises(PhoneError, match="End or reconcile"):
        migration.import_dograh(ctx)
    assert not saved and not Path(root / "hermes-talk/phone/calls.db").exists()


def test_owner_switches_plugins_before_import(monkeypatch, tmp_path):
    config = {"plugins": {"enabled": ["hermes-dograh"], "disabled": [],
                         "entries": {"hermes-dograh": {"settings": {}}}}}
    before = deepcopy(config)
    _, saved, ctx = install_public_helpers(monkeypatch, tmp_path, config)
    with pytest.raises(PhoneError) as exc:
        migration.import_dograh(ctx)
    for command in ("hermes plugins disable hermes-dograh", "hermes plugins enable hermes-talk"):
        assert command in str(exc.value)
    assert not saved and config == before


def test_settings_failure_does_not_publish_partial_import(monkeypatch, tmp_path):
    config = {"plugins": {"entries": {"hermes-dograh": {"settings": {}}}}}
    root, _, ctx = install_public_helpers(monkeypatch, tmp_path, config)
    Store(root / "hermes-dograh/calls.db").prepare("session", {"purpose": "test"})

    def fail(key, value):
        raise OSError("write failed")

    monkeypatch.setattr(ctx, "set_config", fail)
    with pytest.raises((OSError, PhoneError)):
        migration.import_dograh(ctx)
    assert not (root / "hermes-talk/phone/calls.db").exists()


def test_final_enable_failure_leaves_imported_journal_and_owner_recovery(monkeypatch, tmp_path):
    config = {"plugins": {"entries": {"hermes-dograh": {"settings": {}}}}}
    root, _, ctx = install_public_helpers(monkeypatch, tmp_path, config)
    Store(root / "hermes-dograh/calls.db").prepare("session", {"purpose": "test"})
    save = ctx.set_config

    def fail_enable(key, value):
        if key == "phone_enabled":
            raise OSError("write failed")
        save(key, value)

    monkeypatch.setattr(ctx, "set_config", fail_enable)
    with pytest.raises(PhoneError, match="hermes plugins config hermes-talk"):
        migration.import_dograh(ctx)
    with sqlite3.connect(root / "hermes-talk/phone/calls.db") as copied:
        assert copied.execute("SELECT status FROM calls").fetchone()[0] == "cancelled"
    assert not ctx.get_config("phone_enabled")
