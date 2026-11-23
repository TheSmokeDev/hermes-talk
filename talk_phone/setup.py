"""Native Hermes setup: select an existing test carrier, without a separate UI."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlsplit

from .client import DograhClient
from .policy import DISCLOSURE, PhoneError, Settings, origin, validate_number

CONVERSATION_PROMPT = (
    "You are the voice interface to the owner's existing Hermes conversation. "
    "Goal: {{purpose}}. Owner instructions: {{owner_instructions}}. "
    "For every question or requested action, call hermes_chat with the caller's words. "
    "Hermes has the same memory, tools and permissions as the chat. "
    "Use a new request_key for each new input; reuse it only when retrying that input. "
    "If the response is pending, call hermes_result with its request_id until answered. "
    "Read owner_answer naturally to the caller. Never invent a result. "
    "The call is connected automatically; never ask for pairing codes or owner pairing. "
    "Do not ask for separate Phone approvals or say the owner must release a result. "
    "If Hermes reports an existing permission decision, relay it accurately. "
    "Be concise. When the caller says goodbye, use the finished transition."
)


def conversation_tool_definitions(public_url, credential_uuid):
    """Dograh renders call routing server-side; it is never a model parameter."""
    base = origin(public_url, service_http=True)
    definitions = []
    for name, endpoint, params, description in [
        (
            "hermes_chat",
            "turn",
            ["message", "request_key"],
            "Send the caller's question or instruction to their existing Hermes chat.",
        ),
        (
            "hermes_result",
            "result",
            ["request_id"],
            "Get the answer for a pending Hermes chat turn.",
        ),
    ]:
        definitions.append(
            {
                "name": name,
                "description": description,
                "category": "http_api",
                "definition": {
                    "type": "http_api",
                    "config": {
                        "method": "POST",
                        "url": base + "/phone/" + endpoint,
                        "credential_uuid": credential_uuid,
                        "timeout_ms": 25000,
                        "parameters": [
                            {"name": p, "type": "string", "description": p, "required": True}
                            for p in params
                        ],
                        "body_template": {
                            **{p: "{{" + p + "}}" for p in params},
                            "call_route": "{{initial_context.hermes_call_route}}",
                        },
                    },
                },
            }
        )
    return definitions


def configure_conversation(client, settings, credential_uuid, *, bridge_url=None):
    """Configure only the selected workflow; preserve unrelated nodes/settings."""
    versions = client.request(
        "GET", f"/api/v1/workflow/{settings.workflow_id}/versions?status=published&limit=1"
    )
    version = versions[0]
    graph = version["workflow_json"]
    agents = [n for n in graph["nodes"] if n["type"] == "agentNode"]
    if len(agents) != 1:
        raise PhoneError("The selected Phone workflow needs one conversation agent.")
    attached = set(agents[0]["data"].get("tool_uuids", []))
    tools = client.request("GET", "/api/v1/tools/")
    tool_ids = []
    for definition in conversation_tool_definitions(
        bridge_url or settings.bridge_public_url, credential_uuid
    ):
        matches = [
            t for t in tools if t.get("name") == definition["name"] and t["tool_uuid"] in attached
        ]
        if len(matches) > 1:
            raise PhoneError("Multiple Hermes Phone tools found; inspect the selected account.")
        if matches:
            current = matches[0]
            if current.get("definition") != definition["definition"]:
                client.request("PUT", "/api/v1/tools/" + current["tool_uuid"], definition)
            tool_ids.append(current["tool_uuid"])
        else:
            created = client.request("POST", "/api/v1/tools/", definition)
            tool_ids.append(created["tool_uuid"])
    if (
        agents[0]["data"].get("prompt") == CONVERSATION_PROMPT
        and agents[0]["data"].get("tool_uuids") == tool_ids
    ):
        return
    agents[0]["data"]["prompt"] = CONVERSATION_PROMPT
    agents[0]["data"]["tool_uuids"] = tool_ids
    client.request(
        "PUT",
        f"/api/v1/workflow/{settings.workflow_id}",
        {
            "workflow_definition": graph,
            "workflow_configurations": version["workflow_configurations"],
        },
    )
    client.request(
        "POST",
        f"/api/v1/workflow/{settings.workflow_id}/publish",
        {
            "version_name": "Hermes Talk same-session Phone",
            "change_description": "Continue the original Hermes chat automatically over the phone.",
        },
    )


def connect_conversation(client, settings, bridge_token, prompt):
    """Reuse the selected workflow's existing transport and stored credential."""
    version = client.request(
        "GET", f"/api/v1/workflow/{settings.workflow_id}/versions?status=published&limit=1"
    )[0]
    attached = {
        u
        for n in version["workflow_json"]["nodes"]
        for u in n.get("data", {}).get("tool_uuids", [])
    }
    tools = client.request("GET", "/api/v1/tools/")
    previous = [
        t["definition"]["config"]
        for t in tools
        if t["tool_uuid"] in attached
        and (t["definition"]["type"] == "mcp" or t["name"] == "hermes_chat")
    ]
    address = settings.bridge_public_url
    credential = None
    if len(previous) == 1:
        cfg = previous[0]
        parsed = urlsplit(cfg["url"])
        address = address or f"{parsed.scheme}://{parsed.netloc}"
        credential = cfg.get("credential_uuid")
    address = origin(
        address or prompt("Hermes Phone address reachable from Dograh"), service_http=True
    )
    if not credential:
        credential = client.request(
            "POST",
            "/api/v1/credentials/",
            {
                "name": f"Hermes Talk Phone {settings.workflow_id}",
                "credential_type": "bearer_token",
                "credential_data": {"token": bridge_token},
            },
        )["uuid"]
    result = replace(settings, bridge_enabled=True, bridge_public_url=address)
    configure_conversation(client, result, credential)
    return result


def phone_workflow():
    """A disclosure followed by the existing Hermes conversation."""
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
                "id": "hermes-conversation",
                "type": "agentNode",
                "position": {"x": 300, "y": 100},
                "data": {
                    "name": "Hermes conversation",
                    "prompt": CONVERSATION_PROMPT,
                    "allow_interrupt": True,
                    "add_global_prompt": False,
                    "tool_uuids": [],
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
                "id": "connect",
                "source": "hermes-start",
                "target": "hermes-conversation",
                "data": {"label": "Connected", "condition": "The caller responds to the greeting."},
            },
            {
                "id": "finish",
                "source": "hermes-conversation",
                "target": "hermes-end",
                "data": {
                    "label": "Finished",
                    "condition": "The caller says goodbye, declines, or asks to end.",
                },
            },
        ],
    }


def choose(rows, label, prompt, current=0):
    if not rows:
        raise PhoneError(
            f"No ready {label} found. Connect a test carrier and owned number in Dograh first."
        )
    select = getattr(prompt, "choose", None)
    if callable(select):
        return select(rows, label, current)
    print(f"\nChoose your dedicated test {label} by its account ID:")
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
    from hermes_cli.setup import prompt, prompt_choice, prompt_yes_no

    from .context import INJECTION_OPT_IN, phone_context, save_phone_settings

    ctx = phone_context(ctx)
    print(
        "Hermes Talk Phone setup. Use a dedicated Dograh account and an existing outbound number."
    )
    print("Phone settings are stored in Hermes Talk. Setup does not dial or purchase numbers.")
    print(INJECTION_OPT_IN)
    try:
        settings = Settings.read(ctx.get_config)
        key = get_secret("DOGRAH_API_KEY")
        bridge_token = get_secret("DOGRAH_BRIDGE_TOKEN")
        if not isinstance(key, str) or not key.strip() or (
            not isinstance(bridge_token, str) or len(bridge_token.strip()) < 32
        ):
            print(
                "Run hermes plugins config hermes-talk in this profile and fill the Phone "
                "Dograh API key and bridge bearer secret. Use a random bridge token of at "
                "least 32 characters, then rerun hermes talk phone setup."
            )
            return 1
        current_ready = False
        if settings.workflow_id:
            try:
                settings.connection_settings()
                client = DograhClient(settings, key)
                client.preflight()
                client.check_caller()
                current_ready = bool(settings.owner_user_ids and settings.allowed_numbers)
            except PhoneError:
                pass

        class HermesInputs:
            def __call__(self, question, **kwargs):
                return prompt(question, **kwargs)

            def choose(self, rows, label, current):
                labels = [
                    str(row["id"])
                    + ": "
                    + "".join(
                        ch
                        for ch in str(row.get("name") or row.get("address") or label)
                        if ch.isprintable()
                    )[:160]
                    for row in rows
                ]
                default = next((i for i, row in enumerate(rows) if row["id"] == current), 0)
                index = prompt_choice(
                    f"Choose your dedicated test {label}", labels, default=default
                )
                if not 0 <= index < len(rows):
                    raise PhoneError("Setup cancelled before selecting a caller.")
                return rows[index]

        if current_ready and prompt_yes_no(
            "Keep the existing verified connection and settings", default=True
        ):
            result = connect_conversation(client, settings, bridge_token, prompt)
        else:
            chosen = collect(settings, key, HermesInputs(), prompt_yes_no)
            result = connect_conversation(DograhClient(chosen, key), chosen, bridge_token, prompt)
        save_phone_settings(ctx, result)
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
