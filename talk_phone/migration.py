"""Explicit, same-profile migration; previous call approvals are never transferred."""

import sqlite3
from dataclasses import asdict

from .client import DograhClient
from .context import data_path
from .policy import PhoneError, Settings


def import_dograh():
    from agent.secret_scope import get_secret
    from hermes_cli.config import load_config, save_config
    from plugins.plugin_storage import plugin_data_dir

    config = load_config()
    plugins = config.setdefault("plugins", {})
    entries = plugins.setdefault("entries", {})
    raw = entries.get("hermes-dograh", {}).get("settings")
    if not isinstance(raw, dict):
        raise PhoneError("No separate Dograh configuration exists in this Hermes profile.")
    target = entries.setdefault("hermes-talk", {}).setdefault("settings", {})
    if target.get("phone_enabled"):
        raise PhoneError(
            "Phone mode is already configured; keep its existing settings or edit them in Hermes."
        )
    settings = Settings.read(lambda key, default=None: raw.get(key, default))
    client = DograhClient(settings, get_secret("DOGRAH_API_KEY"))
    client.preflight()
    client.check_caller()
    old = plugin_data_dir("hermes-dograh") / "calls.db"
    destination = data_path()
    if destination.exists():
        raise PhoneError(
            "A Talk phone journal already exists. Inspect it before migration;"
            " it was not overwritten."
        )
    if old.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Hold the source writer lock through the snapshot so no new dial can race it.
        with sqlite3.connect(old, isolation_level=None) as source:
            source.execute("BEGIN IMMEDIATE")
            active = source.execute(
                "SELECT 1 FROM calls WHERE status IN ('started','dialing','unknown') LIMIT 1"
            ).fetchone()
            if active:
                source.rollback()
                raise PhoneError("End or reconcile the existing Dograh call before migrating.")
            # A separate reader can snapshot the committed data while the writer lock is held.
            with sqlite3.connect(old) as reader, sqlite3.connect(destination) as copied:
                reader.backup(copied)
                copied.execute(
                    "UPDATE calls SET status='cancelled',approved_until=NULL WHERE "
                    "status IN ('prepared','approved')"
                )
                if copied.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='bridge_connections'"
                ).fetchone():
                    copied.execute("UPDATE bridge_connections SET revoked=1")
            source.rollback()
    values = {k: list(v) if isinstance(v, tuple) else v for k, v in asdict(settings).items()}
    target.update({"phone_" + k: v for k, v in values.items()})
    target["phone_enabled"] = True
    plugins["enabled"] = [p for p in plugins.get("enabled", []) if p != "hermes-dograh"]
    if "hermes-talk" not in plugins["enabled"]:
        plugins["enabled"].append("hermes-talk")
    plugins["disabled"] = [p for p in plugins.get("disabled", []) if p != "hermes-talk"]
    if "hermes-dograh" not in plugins["disabled"]:
        plugins["disabled"].append("hermes-dograh")
    save_config(config)
    return {
        "phone_enabled": True,
        "connection_verified": True,
        "previous_approvals_revoked": True,
        "old_journal_preserved": True,
        "separate_plugin_disabled": True,
        "call_placed": False,
    }
