"""Direct tools and native background children use the same captured host conversation."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import talk_delegated
import talk_hybrid


def make_relay(execute):
    binding = talk_delegated.Binding(
        SimpleNamespace(execute_tools=execute),
        "route",
        "session",
        Path("/unused"),
        "42",
        "42",
        hybrid=True,
    )
    catalog = [
        {"name": name, "parameters": {"type": "object"}} for name in ("terminal", "delegate_task")
    ]
    return talk_hybrid.HybridRelay(binding, lambda *_: None, catalog)


def call(name, arguments, call_id="one"):
    return {"name": name, "arguments": json.dumps(arguments), "call_id": call_id}


def result(commands):
    return json.loads(commands[0].output)


def test_direct_tool_and_background_launch_use_distinct_stable_admissions():
    requests = []

    def execute(**request):
        requests.append(request)
        return {
            "ok": True,
            "status": "terminal",
            "outputs": [
                {"call_id": request["calls"][0]["call_id"], "output": "actual host result"},
            ],
        }

    voice = make_relay(execute)
    direct = call("terminal", {"command": "git branch --show-current"})
    first = result(asyncio.run(voice.handle_tool_call_async(direct)))
    assert first["outputs"][0]["output"] == "actual host result"
    asyncio.run(voice.handle_tool_call_async(direct))
    assert requests[0] == requests[1]
    asyncio.run(
        voice.handle_tool_call_async(call("delegate_work", {"task": "Run the tests"}, "two"))
    )
    assert requests[-1]["require_background"] is True
    assert requests[-1]["calls"][0]["name"] == "delegate_task"
    assert requests[-1]["calls"][0]["arguments"] == {
        "tasks": [{"goal": "Run the tests", "group": "voice-task"}],
    }
    assert requests[-1]["request_id"] != requests[0]["request_id"]
    assert all(
        row["session_key"] == "route" and row["expected_session_id"] == "session"
        for row in requests
    )


def test_native_child_control_is_not_replaced_with_parent_control():
    requests = []
    voice = make_relay(lambda **request: requests.append(request) or {"ok": True})
    asyncio.run(
        voice.handle_tool_call_async(
            call(
                "delegation_control",
                {
                    "action": "steer",
                    "subagent_id": "child-7",
                    "message": "Only the backend",
                },
            )
        )
    )
    assert requests[0]["calls"][0]["arguments"] == {
        "action": "steer",
        "subagent_id": "child-7",
        "message": "Only the backend",
    }
    assert requests[0]["require_background"] is False


def test_ambiguous_exact_tool_call_is_observed_with_its_original_identity():
    requests = []

    def execute(**request):
        requests.append(request)
        if len(requests) == 1:
            raise TimeoutError
        return {"ok": True, "status": "terminal", "outputs": [{"output": "done"}]}

    voice = make_relay(execute)
    unknown = result(
        asyncio.run(voice.handle_tool_call_async(call("terminal", {"command": "pwd"})))
    )
    assert unknown["status"] == "unknown"
    observed = asyncio.run(voice.status(unknown["request_id"]))
    assert observed["outputs"] == [{"output": "done"}]
    assert requests[0] == requests[1]


def test_raw_or_unsupported_delegation_cannot_fall_back_inline():
    requests = []
    voice = make_relay(lambda **request: requests.append(request) or {"ok": True})
    for event in (
        call("delegate_task", {"tasks": [{"goal": "work"}]}),
        call("delegation_control", {"action": "spawn", "task": "work"}),
    ):
        assert not result(asyncio.run(voice.handle_tool_call_async(event)))["ok"]
    assert requests == []
    names = [
        tool["name"]
        for tool in talk_hybrid.tools(
            [
                {"name": "terminal"},
                {"name": "delegate_task"},
            ]
        )
    ]
    assert "delegate_task" not in names
    assert {"delegate_work", "delegation_control", "terminal"} <= set(names)
