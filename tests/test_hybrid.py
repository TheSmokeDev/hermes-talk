"""Voice corrections address an observed request and never replay ambiguous writes."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import talk_delegated
import talk_hybrid


def relay(control):
    binding = talk_delegated.Binding(
        SimpleNamespace(control_submission=control), "route", "session", Path("/unused"), "42", "42"
    )
    result = talk_hybrid.HybridRelay(binding, lambda *_: None)
    result.requests["known"] = "Run the backend tests"
    result.observed["known"] = {
        "ok": True,
        "status": "started",
        "authority_epoch": 5,
        "execution_generation": 7,
        "controls": ["steer", "interrupt"],
    }
    return result


def event(**args):
    return {
        "name": "control_task",
        "call_id": "control-one",
        "arguments": json.dumps(
            args or {"request_id": "known", "action": "steer", "text": "Backend only"}
        ),
    }


def output(commands):
    return json.loads(commands[0].output)


def test_steer_targets_the_observed_generation_without_claiming_application():
    calls = []

    def control(action, **identity):
        calls.append((action, identity))
        return {"ok": True, "control": {"status": "accepted", "delivery": "pending"}}

    voice = relay(control)
    result = output(asyncio.run(voice.handle_tool_call_async(event())))
    assert calls == [
        (
            "steer",
            {
                "session_key": "route",
                "expected_session_id": "session",
                "request_id": "known",
                "authority_epoch": 5,
                "execution_generation": 7,
                "text": "Backend only",
            },
        )
    ]
    assert result["control"] == {"status": "accepted", "delivery": "pending"}
    assert "known" not in voice.observed


def test_stale_control_refusal_is_not_retargeted_or_retried():
    calls = []

    def control(*args, **kwargs):
        calls.append(kwargs)
        return {"ok": False, "status": "refused", "error": "stale_execution"}

    voice = relay(control)
    for _ in range(2):
        result = output(asyncio.run(voice.handle_tool_call_async(event())))
        assert result["error"] == "stale_execution"
    assert len(calls) == 1


def test_unknown_control_outcome_is_not_replayed():
    calls = []

    def control(*args, **kwargs):
        calls.append(kwargs)
        raise TimeoutError

    voice = relay(control)
    for _ in range(2):
        result = output(asyncio.run(voice.handle_tool_call_async(event())))
        assert result["status"] == "unknown"
    assert len(calls) == 1


def test_unknown_child_or_unavailable_control_never_reaches_host():
    calls = []
    voice = relay(lambda *args, **kwargs: calls.append(kwargs))
    for args in (
        {"request_id": "child-id", "action": "interrupt"},
        {"request_id": "known", "action": "cancel"},
        {"request_id": "known", "action": "interrupt", "text": "unexpected"},
    ):
        result = output(asyncio.run(voice.handle_tool_call_async(event(**args))))
        assert not result["ok"]
    assert calls == []
