"""Tool surface — the speakable-error contract and off-host degradation."""

from __future__ import annotations

import json
import threading
import time

import pytest

import talk_host
import talk_runs
import talk_tools


def _wait_terminal(run_id: int, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = talk_runs.get_run(run_id)
        if run and run["status"] in talk_runs.TERMINAL_STATUSES:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")


@pytest.fixture(autouse=True)
def unbound_ctx(monkeypatch):
    """Every test starts detached from Hermes unless it says otherwise.

    ``hermes_binary`` is neutralized too: this box has a real ``hermes`` on
    PATH, and a tool test must never spawn one. Backend-chain coverage lives
    in test_delegation.py, where the subprocess is replaced explicitly.
    """

    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: None)
    yield
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()


def test_default_tools_are_fresh_copies():
    first = talk_tools.default_talk_tools()
    first[0]["name"] = "mutated"
    assert [tool["name"] for tool in talk_tools.default_talk_tools()] == [
        "search_memory",
        "delegate_task",
        "check_work",
        "list_agents",
        "steer_agent",
        "redirect_agent",
        "stop_work",
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


def test_delegate_task_degrades_with_no_agent_loop_and_no_binary():
    result = talk_tools.execute_talk_tool("delegate_task", {"task": "ship it"})
    assert "can't hand off work" in result
    assert "WORK_STARTED" not in result


def test_search_memory_needs_a_query():
    assert "needs something to look for" in talk_tools.execute_talk_tool("search_memory", {})


def test_delegate_task_needs_a_task():
    assert "needs a task" in talk_tools.execute_talk_tool("delegate_task", {"task": "  "})


def test_check_work_on_an_empty_registry():
    assert "Nothing is running" in talk_tools.execute_talk_tool("check_work", {})


def test_check_work_lists_a_running_run():
    gate = threading.Event()
    run_id = talk_runs.start_run("agent", "audit the site", lambda _rid: gate.wait(3) or "ok")

    result = talk_tools.execute_talk_tool("check_work", {})

    assert f"run {run_id} (agent) running" in result
    gate.set()


def test_check_work_lists_a_finished_run():
    run_id = talk_runs.start_run("agent", "audit", lambda _rid: "the index is rebuilt")
    _wait_terminal(run_id)

    assert f"run {run_id} (agent) done" in talk_tools.execute_talk_tool("check_work", {})


def test_check_work_by_id_speaks_the_output():
    run_id = talk_runs.start_run("agent", "audit", lambda _rid: "the index is rebuilt")
    _wait_terminal(run_id)

    result = talk_tools.execute_talk_tool("check_work", {"run_id": run_id})

    assert "the index is rebuilt" in result
    assert "audit" in result


def test_check_work_by_unknown_id():
    assert "don't have a run number" in talk_tools.execute_talk_tool("check_work", {"run_id": 4242})


def test_check_work_rejects_a_non_numeric_id():
    assert "needs a run number" in talk_tools.execute_talk_tool("check_work", {"run_id": "soon"})


def test_check_work_reports_a_previous_session_as_lost(monkeypatch, tmp_path):
    """A detached run this process never spawned must not read as 'nothing'."""

    history = tmp_path / "talk-runs.jsonl"
    history.write_text(
        json.dumps(
            {
                "runId": 3,
                "kind": "agent",
                "label": "left running",
                "status": "running",
                "ts": 1.0,
                "updated": 1.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(talk_runs, "_history_path", lambda: history)
    monkeypatch.setattr(talk_runs, "_history_enabled", lambda: True)

    result = talk_tools.execute_talk_tool("check_work", {})

    assert "run 3 (agent) lost" in result
    assert "can't see how it ended" in result


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
