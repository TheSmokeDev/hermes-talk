"""Explicit, same-profile migration; previous call approvals are never transferred."""

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from .client import DograhClient
from .context import data_path, enabled, save_phone_settings
from .policy import PhoneError, Settings

OWNER_COMMANDS = ("hermes plugins disable hermes-dograh", "hermes plugins enable hermes-talk")


def _copy_history(source_path, staged):
    with (
        closing(sqlite3.connect(source_path)) as reader,
        closing(sqlite3.connect(staged)) as copied,
    ):
        reader.backup(copied)
        copied.execute(
            "UPDATE calls SET status='cancelled',approved_until=NULL WHERE "
            "status IN ('prepared','approved')"
        )
        if copied.execute(
            "SELECT 1 FROM sqlite_master WHERE name='bridge_connections'"
        ).fetchone():
            copied.execute("UPDATE bridge_connections SET revoked=1")
        copied.commit()


def import_dograh(ctx):
    from agent.secret_scope import get_secret
    from hermes_cli.config import load_config_readonly
    from plugins.plugin_storage import plugin_data_dir

    config = load_config_readonly()
    plugins = config.get("plugins", {})
    raw = plugins.get("entries", {}).get("hermes-dograh", {}).get("settings")
    if not isinstance(raw, dict):
        raise PhoneError("No separate Dograh configuration exists in this Hermes profile.")
    if enabled(ctx):
        raise PhoneError(
            "Phone mode is already configured; keep its existing settings or edit them in Hermes."
        )
    if "hermes-dograh" in (plugins.get("enabled") or []) and (
        "hermes-dograh" not in (plugins.get("disabled") or [])
    ):
        raise PhoneError(
            "Switch the active Phone plugin before importing: "
            + " then ".join(OWNER_COMMANDS)
            + ". Then rerun hermes talk phone setup --import-dograh."
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
        with closing(sqlite3.connect(old, isolation_level=None)) as source:
            source.execute("BEGIN IMMEDIATE")
            active = source.execute(
                "SELECT 1 FROM calls WHERE status IN ('started','dialing','unknown') LIMIT 1"
            ).fetchone()
            if active:
                source.rollback()
                raise PhoneError("End or reconcile the existing Dograh call before migrating.")
            with tempfile.TemporaryDirectory(dir=destination.parent) as stage:
                staged = Path(stage) / "calls.db"
                _copy_history(old, staged)
                save_phone_settings(ctx, settings, enable=False)
                try:
                    # Publish without replacing a journal created during the import.
                    if os.name == "nt":
                        staged.rename(destination)
                    else:
                        os.link(staged, destination)
                except FileExistsError as exc:
                    raise PhoneError("A Talk phone journal already exists.") from exc
            source.rollback()
    else:
        save_phone_settings(ctx, settings, enable=False)
    try:
        ctx.set_config("phone_enabled", True)
    except Exception as exc:
        raise PhoneError(
            "Phone settings were imported. Enable phone_enabled in "
            "'hermes plugins config hermes-talk' to finish."
        ) from exc
    return {
        "phone_enabled": True,
        "connection_verified": True,
        "previous_approvals_revoked": True,
        "old_journal_preserved": True,
        "owner_commands": list(OWNER_COMMANDS),
        "call_placed": False,
    }
