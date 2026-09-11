"""Real-host integration, runnable from the canonical host test runner or an explicit checkout."""

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

HOST = os.environ.get("HERMES_TALK_WORKER_HOST")
if HOST:
    sys.path.insert(0, HOST)
else:
    try:
        import agent.task_worker_provider as host_contract
    except ImportError:
        pytest.skip("A compatible real Hermes worker host is required", allow_module_level=True)
    HOST = str(Path(host_contract.__file__).resolve().parents[1])
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402
from agent import task_worker_registry  # noqa: E402
from hermes_cli.plugins import PluginManager  # noqa: E402
from hermes_state_store_identity import get_store_id  # noqa: E402
from run_agent import AIAgent  # noqa: E402
from tests.gateway.test_dashboard_consumption import gateway  # noqa: E402

import talk_codex_worker  # noqa: E402
from talk_codex_wire import CodexAppServer  # noqa: E402
from talk_dashboard_tasks import DashboardOwnerContext, DashboardTasks  # noqa: E402
from talk_passive import HistoryTransport, digest  # noqa: E402

SCRIPT = Path(__file__).parent / "fixtures" / "codex_app_server.py"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        "complete",
        "approval_replay",
        "drop_turn",
        "ignore_interrupt",
        "ack_no_terminal",
        "lease_steer",
        "lease_approval",
    ],
)
async def test_dashboard_to_real_host_to_scripted_codex_preserves_ownership(
    tmp_path, monkeypatch, scenario
):
    task_worker_registry._reset_for_tests()
    monkeypatch.setattr(
        AIAgent, "run_conversation", lambda *a, **kw: pytest.fail("Hermes model ran")
    )
    monkeypatch.setattr("agent.model_metadata.fetch_model_metadata", lambda *a, **kw: {})
    wires = []
    timers = []
    if scenario.startswith("lease_"):
        from agent import periodic_scheduler

        original_schedule = periodic_scheduler.schedule

        def schedule(callback, interval, *args, **kwargs):
            timers.append(callback)
            return original_schedule(callback, interval, *args, **kwargs)

        monkeypatch.setattr(periodic_scheduler, "schedule", schedule)
    monkeypatch.setattr(talk_codex_worker.CodexWorker, "CANCEL_TIMEOUT_S", 0.25)
    wire_scenario = {"lease_steer": "hold", "lease_approval": "approval_replay"}.get(
        scenario, scenario
    )

    def wire(command, *, cwd):
        assert command[1:] == ("app-server", "--listen", "stdio://")
        process = CodexAppServer(
            (sys.executable, "-u", str(SCRIPT), str(tmp_path / "peer.json"), wire_scenario),
            cwd=cwd,
            timeout=8 if scenario.startswith("lease_") else 2,
            version_command=(sys.executable, str(SCRIPT), "--version"),
        )
        wires.append(process)
        return process

    monkeypatch.setattr(talk_codex_worker, "CodexAppServer", wire)
    async with gateway(tmp_path, monkeypatch) as (root, keys, stores, adapter, client):
        home = root / "profiles" / "alpha"
        plugin = home / "plugins" / "fixture-codex"
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text(
            "name: fixture-codex\nversion: 1.0.0\ndescription: fixture\n"
        )
        (plugin / "__init__.py").write_text(
            "from talk_codex_provider import build_provider\n"
            "def register(ctx):\n    ctx.register_task_worker_provider(build_provider(ctx))\n"
        )
        (home / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "plugins": {
                        "enabled": ["fixture-codex"],
                        "entries": {
                            "fixture-codex": {
                                "settings": {
                                    "codex_worker": {
                                        "enabled": True,
                                        "executable": sys.executable,
                                        "workspace": str(tmp_path),
                                        "model": "explicit-model",
                                    }
                                }
                            }
                        },
                    }
                }
            )
        )
        with adapter._profile_scope("alpha"):
            PluginManager().discover_and_load()
            assert task_worker_registry.configured_worker("hermes-talk-codex") is not None
        db = stores["alpha"]

        def parent(**kwargs):
            return AIAgent(
                api_key="fixture",
                provider="openrouter",
                model="fixture",
                base_url="https://openrouter.ai/api/v1",
                session_id=kwargs["session_id"],
                session_db=db,
                platform="api_server",
                enabled_toolsets=["file"],
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                tool_progress_callback=kwargs["tool_progress_callback"],
            )

        monkeypatch.setattr(adapter, "_create_agent", parent)
        principal = SimpleNamespace(state=SimpleNamespace(principal="actor"))

        def context(request, profile=None):
            return DashboardOwnerContext(
                request.state.principal, "verified_subject", "alpha", home, get_store_id(db)
            )

        def transport(owner):
            return HistoryTransport(
                str(client.make_url("/")).rstrip("/"),
                "alpha",
                keys["alpha"],
                named_profile=True,
                actor_scope=digest([owner.principal_id, owner.store_id]),
            )

        manager = DashboardTasks(context_resolver=context, transport_factory=transport)

        async def call(function, body):
            return await asyncio.to_thread(function, principal, body)

        bound = None
        try:
            with (
                patch("model_tools.get_tool_definitions", return_value=[]),
                patch("model_tools.check_toolset_requirements", return_value={}),
                patch("agent.process_bootstrap.OpenAI"),
            ):
                bound = await call(
                    manager.join,
                    {"session_id": "same-session", "profile": "alpha", "tab_id": "tab"},
                )
                capture = {"connection_id": bound.connection_id, "generation": bound.generation}

                async def tool(suffix, text, name, arguments):
                    event = await call(
                        manager.event,
                        {
                            **capture,
                            "kind": "input.final",
                            "input_id": "input-" + suffix,
                            "input_type": "typed",
                            "text": text,
                        },
                    )
                    await call(
                        manager.event,
                        {
                            **capture,
                            "kind": "response.started",
                            "interaction_id": event["interaction_id"],
                            "response_id": "response-" + suffix,
                        },
                    )
                    return await call(
                        manager.tool,
                        {
                            **capture,
                            "interaction_id": event["interaction_id"],
                            "response_id": "response-" + suffix,
                            "call_id": "call-" + suffix,
                            "name": name,
                            "arguments": arguments,
                        },
                    )

                original = "  Start a Codex worker for this task.  "
                created = await tool(
                    "start",
                    original,
                    "delegate_task",
                    {"task": "Derived Codex goal", "worker": "codex"},
                )
                local_run = created["action"]["run_id"]

                async def until(predicate):
                    while True:
                        state = await call(manager.state, capture)
                        if predicate(state):
                            return state
                        await asyncio.sleep(0.05)

                if scenario == "approval_replay":
                    state = await asyncio.wait_for(
                        until(lambda s: bool(s["jobs"][0]["approval"].get("approvals"))), 15
                    )
                    correction = "  Keep this exact\nCodex correction  "
                    steered = await tool("steer", correction, "steer_work", {"run_id": local_run})
                    assert steered["action"]["control"]["status"] == "queued", steered
                    await tool(
                        "approve",
                        "Approve that once",
                        "resolve_approval",
                        {"run_id": local_run, "choice": "once"},
                    )
                    await asyncio.wait_for(
                        until(lambda s: not s["jobs"][0]["approval"].get("approvals")), 10
                    )
                    await tool("stop", "Stop the worker", "stop_work", {"run_id": local_run})
                elif scenario in {"ignore_interrupt", "ack_no_terminal"}:
                    await asyncio.wait_for(
                        until(lambda s: s["jobs"][0]["steering"]["supported"]), 15
                    )
                    await tool("stop", "Stop the worker", "stop_work", {"run_id": local_run})
                elif scenario.startswith("lease_"):
                    from gateway.platforms.api_server_task_workers import current_worker

                    await asyncio.wait_for(
                        until(
                            lambda s: (
                                s["jobs"][0]["steering"]["supported"]
                                and (
                                    scenario != "lease_approval"
                                    or s["jobs"][0]["approval"].get("approvals")
                                )
                            )
                        ),
                        15,
                    )
                    remote = bound.stages.action(bound.token, local_run)["api_run_id"]
                    binding = current_worker(adapter._active_run_agents[remote], remote)
                    worker = binding.session.worker
                    original_authorize = worker.authorize
                    entered, release = threading.Event(), threading.Event()

                    def gate():
                        entered.set()
                        assert release.wait(12)
                        return original_authorize()

                    worker.authorize = gate
                    operation = asyncio.create_task(
                        tool(
                            "late",
                            "A queued operation",
                            "resolve_approval" if scenario == "lease_approval" else "steer_work",
                            {"run_id": local_run, "choice": "once"}
                            if scenario == "lease_approval"
                            else {"run_id": local_run},
                        )
                    )
                    try:
                        assert await asyncio.to_thread(entered.wait, 10)
                        child_id = binding.child_id
                        with db._read_ctx() as conn:
                            holder = conn.execute(
                                "SELECT holder FROM session_turn_leases WHERE conversation_id=?",
                                (child_id,),
                            ).fetchone()[0]
                        changed = db._execute_write(
                            lambda conn: (
                                conn.execute(
                                    "UPDATE session_turn_leases "
                                "SET holder='replacement-worker',expires_at=? "
                                    "WHERE conversation_id=? AND holder=?",
                                    (time.time() + 60, child_id, holder),
                                ).rowcount
                            )
                        )
                        assert changed == 1
                        assert not binding.session.request.still_authorized()
                        for callback in timers:
                            callback()
                        db._execute_write(
                            lambda conn: conn.execute(
                                "UPDATE session_turn_leases SET holder=?,expires_at=? "
                                "WHERE conversation_id=?",
                                (holder, time.time() + 60, child_id),
                            )
                        )
                        assert not binding.session.request.still_authorized()
                    finally:
                        release.set()
                    await asyncio.wait_for(operation, 12)
                await asyncio.wait_for(until(lambda s: s["jobs"][0]["result_available"]), 20)
                result = await call(manager.result, {**capture, "run_id": local_run})
                if scenario in {"ignore_interrupt", "ack_no_terminal"}:
                    assert (
                        result["status"] == "failed"
                        and result["error"] == "cancellation_unconfirmed"
                    )
                    assert result["output"] == "Partial work before stop"
                elif scenario.startswith("lease_"):
                    assert result["status"] == "failed"
                else:
                    assert result["status"] == (
                        "cancelled" if scenario == "approval_replay" else "completed"
                    ), result
                    assert "Second section [artifact](result.md)" in result["output"]
                    assert result["artifacts"][0]["changes"][0]["diff"] == "+full artifact"
                assert result["truncated"] is False
                rows = db.get_messages("same-session")
                assert sum(row["content"] == original for row in rows) == 1
                state = json.loads((tmp_path / "peer.json").read_text(encoding="utf-8"))
                methods = [row.get("method") for row in state["requests"]]
                assert methods.count("thread/start") == methods.count("turn/start") == 1
                if scenario.startswith("lease_"):
                    assert "turn/steer" not in methods
                    assert not state.get("approval_replies")
                if scenario == "approval_replay":
                    steer = next(
                        row for row in state["requests"] if row.get("method") == "turn/steer"
                    )
                    assert steer["params"]["input"][0]["text"] == correction
                    assert state["approval_replies"] == 1
                await call(manager.close, capture)
                bound = await call(
                    manager.join,
                    {"session_id": "same-session", "profile": "alpha", "tab_id": "tab"},
                )
                resumed = {"connection_id": bound.connection_id, "generation": bound.generation}
                assert (await call(manager.state, resumed))["announcements"] == []
                assert (await call(manager.result, {**resumed, "run_id": local_run}))[
                    "output"
                ] == result["output"]
        finally:
            if bound and not bound.closed:
                await call(
                    manager.close,
                    {"connection_id": bound.connection_id, "generation": bound.generation},
                )
            for process in wires:
                process.close()
            task_worker_registry._reset_for_tests()
