"""Read-only connection diagnostics. No call journal, approvals, dialing, or provisioning."""

from __future__ import annotations

from . import __version__
from .client import DograhClient
from .policy import PhoneError, Settings, validate_number


def inspect(settings: Settings, get_secret, client_factory=DograhClient):
    checks = []

    def check(name, fn):
        try:
            fn()
        except PhoneError as exc:
            checks.append({"name": name, "ok": False, "message": str(exc)})
        except Exception:  # noqa: BLE001 - external boundary; keep host/provider details private
            checks.append({"name": name, "ok": False, "message": "Check failed; details withheld."})
        else:
            checks.append({"name": name, "ok": True})
        return checks[-1]["ok"]

    def need(value, message):
        if not value:
            raise PhoneError(message)

    configured = check("connection_settings", settings.connection_settings)
    key = None

    def api_key():
        nonlocal key
        key = get_secret("DOGRAH_API_KEY")
        need(
            isinstance(key, str) and key.strip(),
            "Set the Dograh API key in Hermes secret settings.",
        )

    has_key = check("api_key", api_key)
    remote_ok = False
    if configured and has_key:
        client = None

        def workflow():
            nonlocal client
            client = client_factory(settings, key)
            client.preflight()

        workflow_ok = check("published_workflow", workflow)
        caller_ok = check("carrier_and_caller", lambda: client.check_caller()) if client else False
        remote_ok = workflow_ok and caller_ok

    check(
        "owner_allowlist",
        lambda: need(settings.owner_user_ids, "Run /phone identity and allowlist that owner ID."),
    )

    def destinations():
        need(settings.allowed_numbers, "No owned test destinations are allowlisted.")
        for number in settings.allowed_numbers:
            validate_number(number, settings.allowed_numbers)

    check("destination_allowlist", destinations)
    check(
        "dialing_enabled",
        lambda: need(settings.dialing_enabled, "Dialing is disabled in plugin settings."),
    )
    if settings.bridge_enabled:

        def bridge_key():
            value = get_secret("DOGRAH_BRIDGE_TOKEN")
            need(
                isinstance(value, str) and len(value) >= 32,
                "Set a bridge token of at least 32 characters.",
            )

        check("bridge_token", bridge_key)

    return {
        "version": __version__,
        "connection_ready": remote_ok,
        "dialing_configuration_ready": remote_ok
        and all(c["ok"] for c in checks if c["name"] != "bridge_token"),
        "checks": checks,
        "approval_required": True,
        "call_attempted": False,
        "note": "Checks read configuration and Dograh metadata only. They do not "
        "test carrier credentials, audio, callbacks, bridge connectivity, "
        "the local kill switch, or an active call. A fresh owner approval "
        "is still required.",
    }


def doctor(ctx):
    from agent.secret_scope import get_secret

    from .context import enabled, phone_context

    if not enabled(ctx):
        return {
            "phone_enabled": False,
            "connection_ready": False,
            "call_attempted": False,
            "next_step": "Enable Phone mode in Hermes Talk plugin settings, or run hermes "
            "talk phone setup.",
        }
    return inspect(Settings.read(phone_context(ctx).get_config), get_secret)


def cli_report(report):
    """Keep CLI diagnostics to known statuses; never log arbitrary provider messages."""
    names = (
        "connection_settings",
        "api_key",
        "published_workflow",
        "carrier_and_caller",
        "owner_allowlist",
        "destination_allowlist",
        "dialing_enabled",
        "bridge_token",
    )
    rows = report.get("checks", [])
    checks = [
        {
            "name": name,
            "ok": any(
                row.get("name") == name and row.get("ok") is True
                for row in rows
                if isinstance(row, dict)
            ),
        }
        for name in names
    ]
    return {
        "version": __version__,
        "connection_ready": report.get("connection_ready") is True,
        "dialing_configuration_ready": report.get("dialing_configuration_ready") is True,
        "checks": checks,
        "call_attempted": False,
        "help": "Check failed settings in Hermes Settings > Plugins > hermes-talk.",
    }
