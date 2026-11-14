"""Native Hermes setup: select an existing test carrier, without a separate UI."""

from __future__ import annotations

from dataclasses import asdict, replace

from .client import DograhClient
from .policy import DISCLOSURE, PhoneError, Settings, origin, validate_number


def phone_workflow():
    """A basic phone conversation; optional MCP bridge setup remains separate."""
    return {
        "nodes": [
            {
                "id": "hermes-start",
                "type": "startCall",
                "position": {"x": 100, "y": 100},
                "data": {
                    "name": "Hermes phone assistant",
                    "greeting_type": "text",
                    "greeting": DISCLOSURE,
                    "allow_greeting_interrupt": False,
                    "allow_interrupt": True,
                    "add_global_prompt": False,
                    "prompt": (
                        "You are an AI assistant making an owner-authorized test call. "
                        "The fixed AI disclosure has already been spoken. "
                        "Goal: {{purpose}}. Owner instructions: {{owner_instructions}}. "
                        "Be brief and natural. Ask one question at a time. "
                        "Do not invent prior conversations, tools, facts, or completed work. "
                        "When the caller says goodbye, declines, or asks to end, "
                        "invoke the finished transition immediately."
                    ),
                },
            },
            {
                "id": "hermes-end",
                "type": "endCall",
                "position": {"x": 450, "y": 100},
                "data": {
                    "name": "Goodbye",
                    "add_global_prompt": False,
                    "prompt": "Say exactly: Thanks for helping test the assistant. Goodbye!",
                },
            },
        ],
        "edges": [
            {
                "id": "finish",
                "source": "hermes-start",
                "target": "hermes-end",
                "data": {
                    "label": "Finished",
                    "condition": "The caller says goodbye, declines, or asks to end.",
                },
            }
        ],
    }


def choose(rows, label, prompt, current=0):
    if not rows:
        raise PhoneError(
            f"No ready {label} found. Connect a test carrier and owned number in Dograh first."
        )
    print(f"\nChoose your dedicated test {label}:")
    for row in rows:
        print(f"  {row['id']}: {row.get('name') or row.get('address') or label}")
    default = str(current) if any(r["id"] == current for r in rows) else None
    value = prompt(f"{label} ID", default=default)
    selected = [r for r in rows if str(r["id"]) == value]
    if len(selected) != 1:
        raise PhoneError(f"Choose a listed {label} ID.")
    return selected[0]


def collect(settings, key, prompt, confirm, client_factory=DograhClient):
    """Collect explicit choices; never dial, buy a number, or change a carrier."""
    s = replace(settings, base_url=origin(prompt("Dograh address", default=settings.base_url)))
    client = client_factory(s, key)
    rows = client.request("GET", "/api/v1/organizations/telephony-configs").get(
        "configurations", []
    )
    ready = [
        r
        for r in rows
        if r.get("inactive") is False
        and r.get("supports_outbound") is True
        and r.get("is_ready_for_outbound") is True
    ]
    carrier = choose(ready, "carrier", prompt, s.telephony_configuration_id)
    rows = client.request(
        "GET", f"/api/v1/organizations/telephony-configs/{carrier['id']}/phone-numbers"
    )
    ready = [
        r
        for r in rows.get("phone_numbers", [])
        if r.get("is_active") is True
        and r.get("address_type") == "pstn"
        and r.get("telephony_configuration_id") == carrier["id"]
    ]
    phone = choose(ready, "caller number", prompt, s.from_phone_number_id)
    s = replace(s, telephony_configuration_id=carrier["id"], from_phone_number_id=phone["id"])
    if not s.workflow_id:
        if not confirm(
            "Create and publish a dedicated Hermes test agent? No call will be made", default=False
        ):
            raise PhoneError("Setup cancelled before creating an agent.")
        agent = client.request(
            "POST",
            "/api/v1/workflow/create/definition",
            {
                "name": "Hermes phone test",
                "workflow_definition": phone_workflow(),
            },
        )
        s = replace(s, workflow_id=agent["id"], agent_uuid=agent["workflow_uuid"])
        print(f"Created test agent {s.workflow_id}. Reuse that ID if setup is interrupted.")
        client.request(
            "PUT",
            f"/api/v1/workflow/{s.workflow_id}",
            {
                "workflow_definition": phone_workflow(),
                "workflow_configurations": {"max_call_duration": s.max_duration_seconds},
            },
        )
        client.request(
            "POST",
            f"/api/v1/workflow/{s.workflow_id}/publish",
            {
                "version_name": "Hermes phone setup",
                "change_description": "Fixed AI greeting and bounded owner-requested test calls.",
            },
        )
    # Verify the chosen connection before recording any dialing permission.
    client = client_factory(s, key)
    client.preflight()
    client.check_caller()
    default = s.allowed_numbers[0] if len(s.allowed_numbers) == 1 else None
    number = prompt(
        "Your owned test destination, full E.164 (for example +12025550123)", default=default
    )
    validate_number(number, (number,))
    print("In Hermes chat, /phone identity shows your authenticated owner ID.")
    default = s.owner_user_ids[0] if len(s.owner_user_ids) == 1 else None
    owner = prompt("Owner ID from /phone identity", default=default)
    if not owner or ":" not in owner or len(owner) > 256 or any(ord(c) < 33 for c in owner):
        raise PhoneError("Use the exact authenticated owner ID from /phone identity.")
    enabled = confirm(
        "Enable reviewed test calls? Carrier and model charges apply; each"
        " call still needs approval",
        default=False,
    )
    return replace(s, allowed_numbers=(number,), owner_user_ids=(owner,), dialing_enabled=enabled)


def run(ctx):
    from agent.secret_scope import get_secret
    from hermes_cli.config import load_config, save_config, save_env_value_secure
    from hermes_cli.setup import prompt, prompt_yes_no

    from .context import phone_context

    ctx = phone_context(ctx)
    print(
        "Hermes Talk Phone setup. Use a dedicated Dograh account and an existing outbound number."
    )
    print("Phone settings are stored in Hermes Talk. Setup does not dial or purchase numbers.")
    try:
        settings = Settings.read(ctx.get_config)
        key = get_secret("DOGRAH_API_KEY")
        current_ready = False
        if key and settings.workflow_id:
            try:
                settings.connection_settings()
                client = DograhClient(settings, key)
                client.preflight()
                client.check_caller()
                current_ready = bool(settings.owner_user_ids and settings.allowed_numbers)
            except PhoneError:
                pass
        if current_ready and prompt_yes_no(
            "Keep the existing verified connection and settings", default=True
        ):
            config = load_config()
            entry = (
                config.setdefault("plugins", {})
                .setdefault("entries", {})
                .setdefault("hermes-talk", {})
            )
            entry.setdefault("settings", {})["phone_enabled"] = True
            save_config(config)
            print(
                "Connection ready. Open Hermes and ask it to prepare a call, then "
                "review /phone approve."
            )
            return 0
        new_key = not key
        if new_key:
            key = prompt("Dograh API key (hidden)", password=True)
        result = collect(settings, key, prompt, prompt_yes_no)
        if new_key:
            save_env_value_secure("DOGRAH_API_KEY", key)
        config = load_config()
        entry = (
            config.setdefault("plugins", {}).setdefault("entries", {}).setdefault("hermes-talk", {})
        )
        values = {k: list(v) if isinstance(v, tuple) else v for k, v in asdict(result).items()}
        entry["settings"] = {
            **entry.get("settings", {}),
            **{"phone_" + k: v for k, v in values.items()},
            "phone_enabled": True,
        }
        save_config(config)
        print(
            "Saved in this Hermes profile. Ask Hermes to prepare a call, "
            "review the card, then /phone approve."
        )
        return 0
    except PhoneError as exc:
        print(f"Setup stopped: {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Setup cancelled.")
        return 1
    except Exception:  # noqa: BLE001 - external boundary; keep host/provider details private
        print(
            "Setup stopped. Provider details withheld; inspect the dedicated "
            "test account before retrying."
        )
        return 1
