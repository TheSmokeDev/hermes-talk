"""Tool surface — the speakable-error contract and off-host degradation."""

from __future__ import annotations

import json

import pytest

import talk_host
import talk_tools


@pytest.fixture(autouse=True)
def unbound_ctx():
    """Every test starts detached from Hermes unless it says otherwise."""

    talk_host.bind_ctx(None)
    yield
    talk_host.bind_ctx(None)


def test_default_tools_are_fresh_copies():
    first = talk_tools.default_talk_tools()
    first[0]["name"] = "mutated"
    assert [tool["name"] for tool in talk_tools.default_talk_tools()] == [
        "search_memory",
        "delegate_task",
        "talk_status",
    ]


def test_every_advertised_tool_has_a_handler():
    for tool in talk_tools.default_talk_tools():
        assert tool["name"] in talk_tools._HANDLERS
        assert tool["type"] == "function"
        assert tool["parameters"]["type"] == "object"


def test_unknown_tool_raises():
    # A name the model was never given is a client bug, not a call failure —
    # it is the one case that escapes as an exception.
    with pytest.raises(talk_tools.TalkToolError, match="launch_missiles"):
        talk_tools.execute_talk_tool("launch_missiles", {})


def test_handler_failure_returns_speakable_text(monkeypatch):
    def boom(_arguments):
        raise RuntimeError("disk on fire")

    monkeypatch.setitem(talk_tools._HANDLERS, "talk_status", boom)

    result = talk_tools.execute_talk_tool("talk_status", {})

    assert result.startswith("talk_status failed: RuntimeError: disk on fire")


def test_output_is_bounded(monkeypatch):
    monkeypatch.setitem(talk_tools._HANDLERS, "talk_status", lambda _a: "x" * 99_999)
    assert len(talk_tools.execute_talk_tool("talk_status", {})) == talk_tools.MAX_OUTPUT_CHARS


def test_empty_output_still_says_something(monkeypatch):
    monkeypatch.setitem(talk_tools._HANDLERS, "talk_status", lambda _a: "")
    assert talk_tools.execute_talk_tool("talk_status", {}) == "(no output)"


def test_talk_status_reports_state(monkeypatch):
    monkeypatch.delenv("TALK_VOICE", raising=False)
    monkeypatch.setenv("TALK_MODEL", "gpt-realtime-2.1")
    talk_tools.REGISTRATION_FAILURES.clear()

    status = json.loads(talk_tools.execute_talk_tool("talk_status", {}))

    assert status["model"] == "gpt-realtime-2.1"
    assert status["voice"] == "cedar"
    assert status["attached_to_hermes"] is False
    assert isinstance(status["audio_available"], bool)
    assert "registration_failures" not in status


def test_talk_status_surfaces_registration_failures():
    talk_tools.REGISTRATION_FAILURES.append("tts provider: ValueError: nope")
    try:
        status = json.loads(talk_tools.execute_talk_tool("talk_status", {}))
        assert status["registration_failures"] == ["tts provider: ValueError: nope"]
    finally:
        talk_tools.REGISTRATION_FAILURES.clear()


def test_talk_status_survives_an_unusable_voice(monkeypatch):
    monkeypatch.setenv("TALK_VOICE", "not-a-voice")
    status = json.loads(talk_tools.execute_talk_tool("talk_status", {}))
    assert status["voice"].startswith("unusable")


def test_search_memory_degrades_without_a_host():
    result = talk_tools.execute_talk_tool("search_memory", {"query": "the deploy"})
    assert "memory isn't available" in result
    assert "Traceback" not in result


def test_delegate_task_degrades_without_a_host():
    result = talk_tools.execute_talk_tool("delegate_task", {"task": "ship it"})
    assert "can't hand off work" in result


def test_search_memory_needs_a_query():
    assert "needs something to look for" in talk_tools.execute_talk_tool("search_memory", {})


def test_delegate_task_needs_a_task():
    assert "needs a task" in talk_tools.execute_talk_tool("delegate_task", {"task": "  "})


class _StubCtx:
    """Records dispatch_tool calls the way the Hermes plugin context would."""

    def __init__(self, result="{}"):
        self.calls: list[tuple[str, dict]] = []
        self.result = result

    def dispatch_tool(self, tool_name, args, **kwargs):
        self.calls.append((tool_name, args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_search_memory_relays_through_the_host_tool():
    ctx = _StubCtx(json.dumps({"success": True, "result": "you shipped it Tuesday"}))
    talk_host.bind_ctx(ctx)

    result = talk_tools.execute_talk_tool("search_memory", {"query": "deploy", "limit": 99})

    assert ctx.calls == [(talk_host.MEMORY_TOOL_NAME, {"query": "deploy", "limit": 8})]
    assert result == "you shipped it Tuesday"


def test_delegate_task_returns_a_work_started_receipt():
    ctx = _StubCtx(json.dumps({"success": True, "result": "subagent 4 started"}))
    talk_host.bind_ctx(ctx)

    result = talk_tools.execute_talk_tool("delegate_task", {"task": "rebuild the index"})

    assert ctx.calls == [(talk_host.DELEGATE_TOOL_NAME, {"goal": "rebuild the index"})]
    assert result.startswith("WORK_STARTED")
    assert "subagent 4 started" in result


def test_host_dispatch_failure_is_spoken_not_raised():
    talk_host.bind_ctx(_StubCtx(RuntimeError("registry offline")))

    result = talk_tools.execute_talk_tool("search_memory", {"query": "anything"})

    assert "memory lookup failed" in result
    assert "registry offline" in result


def test_host_error_envelope_is_flattened():
    talk_host.bind_ctx(_StubCtx(json.dumps({"success": False, "error": "no session db"})))

    result = talk_tools.execute_talk_tool("search_memory", {"query": "anything"})

    assert result == "that failed: no session db"


def test_non_json_host_result_passes_through_bounded():
    talk_host.bind_ctx(_StubCtx("y" * 99_999))

    result = talk_tools.execute_talk_tool("search_memory", {"query": "anything"})

    assert len(result) == talk_host.MAX_TOOL_OUTPUT_CHARS
