import sqlite3
import sys
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
    settings.load_config = lambda: config
    saved = []
    settings.save_config = lambda value: saved.append(value)
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
    return root, saved


def test_import_preserves_history_and_revokes_old_approval(monkeypatch, tmp_path, setup_phone):
    api, _, _ = setup_phone
    config = {
        "plugins": {
            "enabled": ["hermes-dograh"],
            "disabled": [],
            "entries": {"hermes-dograh": {"settings": asdict(api.settings)}},
        }
    }
    root, saved = install_public_helpers(monkeypatch, tmp_path, config)
    old = BridgeStore(root / "hermes-dograh/calls.db", clock=lambda: 1000)
    old.prepare("owner-session", {"purpose": "synthetic history"})
    old.connect("old-transport")
    result = migration.import_dograh()
    assert result["call_placed"] is False and result["old_journal_preserved"]
    assert config["plugins"]["enabled"] == ["hermes-talk"]
    assert config["plugins"]["disabled"] == ["hermes-dograh"]
    assert saved and old.path.is_file()
    with sqlite3.connect(root / "hermes-talk/phone/calls.db") as copied:
        assert copied.execute("SELECT status FROM calls").fetchone()[0] == "cancelled"
        assert copied.execute("SELECT revoked FROM bridge_connections").fetchone()[0] == 1
    with sqlite3.connect(old.path) as original:
        assert original.execute("SELECT status FROM calls").fetchone()[0] == "prepared"
        assert original.execute("SELECT revoked FROM bridge_connections").fetchone()[0] == 0


def test_import_refuses_an_active_call_without_creating_target(monkeypatch, tmp_path):
    config = {"plugins": {"entries": {"hermes-dograh": {"settings": {}}}}}
    root, saved = install_public_helpers(monkeypatch, tmp_path, config)
    old = Store(root / "hermes-dograh/calls.db", clock=lambda: 1000)
    call_id = old.prepare("owner-session", {"purpose": "synthetic active"})
    old.finish(call_id, "owner-session", "started", run_id=42)
    with pytest.raises(PhoneError, match="End or reconcile"):
        migration.import_dograh()
    assert not saved and not Path(root / "hermes-talk/phone/calls.db").exists()
