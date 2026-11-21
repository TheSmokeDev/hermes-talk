"""Run the Agent native integration fixture through the actual Talk consumer."""

import asyncio
import json
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

from hermes_cli.plugins import PluginContext

sys.path.insert(0, os.environ["HERMES_TALK_REPO"])
from talk_delegated import Binding
from talk_hybrid import HybridRelay

execute = PluginContext.execute_tools
relays = {}
receipts = {}


def through_talk(
    context, calls, *, session_key, expected_session_id, request_id, text, require_background=False
):
    """Only adapt the fixture's caller; the consumer still invokes the real Agent API."""
    key = (id(context), session_key, expected_session_id)
    if key not in relays:
        host = SimpleNamespace(
            execute_tools=lambda *args, **kwargs: execute(context, *args, **kwargs)
        )
        catalog = context.conversation_tools(
            session_key=session_key, expected_session_id=expected_session_id
        )
        assert catalog["ok"], catalog
        binding = Binding(
            host,
            session_key,
            expected_session_id,
            Path(os.environ["HERMES_HOME"]),
            "42",
            "700",
            hybrid=True,
        )
        relays[key] = HybridRelay(binding, lambda *_: None, catalog=catalog["tools"])
    voice = relays[key]
    assert len(calls) == 1
    call = calls[0]
    name, arguments = call["name"], call["arguments"]
    if name == "delegate_task" and "tasks" in arguments:
        name, arguments = "delegate_work", {"task": arguments["tasks"][0]["goal"]}
    elif name == "delegate_task":
        name = "delegation_control"
    event = {"name": name, "call_id": call["call_id"], "arguments": json.dumps(arguments)}
    response = asyncio.run(voice.handle_tool_call_async(event))
    result = json.loads(response[0].output)
    if result.get("admission_id"):
        target = (key, request_id)
        assert receipts.setdefault(target, result["admission_id"]) == result["admission_id"]
    return result


PluginContext.execute_tools = through_talk
runpy.run_path(
    str(Path(os.environ["HERMES_AGENT_REPO"]) / "tests/gateway/fixtures/plugin_exact_peer.py"),
    run_name="__main__",
)
