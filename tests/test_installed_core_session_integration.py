"""Cross-repository proof for installed Talk canonical Discord attachment.

This test is intentionally skipped in Talk-only environments.  Point
``HERMES_AGENT_REPO`` and ``PYTHONPATH`` at a Hermes Agent checkout exposing the
PRP-006A attachment seam to execute the real installed-plugin canary.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

TALK_REPO = Path(__file__).resolve().parent.parent
AGENT_REPO = Path(os.environ.get("HERMES_AGENT_REPO", "")).resolve()
_REQUIRED_AGENT_FILES = (
    "gateway/realtime_voice_invocation.py",
    "gateway/realtime_voice_messaging_host.py",
    "hermes_cli/plugins.py",
)
_AGENT_SEAM_AVAILABLE = bool(os.environ.get("HERMES_AGENT_REPO")) and all(
    (AGENT_REPO / relative).is_file() for relative in _REQUIRED_AGENT_FILES
)

pytestmark = pytest.mark.skipif(
    not _AGENT_SEAM_AVAILABLE,
    reason=(
        "requires HERMES_AGENT_REPO/PYTHONPATH pointing at an Agent checkout "
        "with the PRP-006A realtime attachment seam"
    ),
)


def _copy_installed_plugin(home: Path) -> Path:
    """Install the current Talk source as a normal user directory plugin."""

    destination = home / "plugins" / "hermes-talk"
    destination.mkdir(parents=True)
    shutil.copy2(TALK_REPO / "plugin.yaml", destination / "plugin.yaml")
    for source in TALK_REPO.glob("*.py"):
        shutil.copy2(source, destination / source.name)
    return destination


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for installed cross-repo state")
        await asyncio.sleep(0.01)


def test_installed_talk_core_join_and_busy_leave_use_one_canonical_discord_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Actual installed /talk owns capture only; Agent owns the one durable turn."""

    asyncio.run(_installed_talk_core_join_and_busy_leave(monkeypatch, tmp_path))


async def _installed_talk_core_join_and_busy_leave(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:

    # Fail rather than silently importing an unrelated globally installed Agent.
    agent_root = str(AGENT_REPO)
    assert Path(inspect.getfile(__import__("gateway"))).resolve().is_relative_to(AGENT_REPO)
    assert sys.path[0] == agent_root or agent_root in sys.path

    import gateway.realtime_voice_invocation as realtime_voice_invocation
    import gateway.run as gateway_run
    import hermes_cli.plugins as plugin_api
    import hermes_state
    import plugins.platforms.discord.adapter as discord_adapter_module
    import run_agent
    from agent.realtime_voice_registry import _reset_for_tests, get_provider
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.realtime_voice_controller import GatewayRealtimeVoiceController
    from gateway.realtime_voice_messaging_host import (
        _ACCEPTED_TASKS,
        _CLAIM_ATTR,
        _MARKER_KEY,
        GatewayRealtimeVoiceMessagingHost,
        RealtimeVoiceFinalizationReceipt,
    )
    from gateway.session import SessionStore, build_session_key
    from hermes_cli.plugins import PluginManager, get_plugin_command_registration
    from hermes_state import SessionDB
    from openai.resources.chat.completions import Completions
    from plugins.platforms.discord.adapter import DiscordAdapter

    home = tmp_path / "hermes-home"
    installed_plugin = _copy_installed_plugin(home)
    (home / "config.yaml").write_text(
        "model:\n  default: gpt-4o-mini\nplugins:\n  enabled:\n    - hermes-talk\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.delenv("DISCORD_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("DISCORD_ALLOWED_ROLES", raising=False)
    monkeypatch.delenv("DISCORD_ALLOWED_CHANNELS", raising=False)
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.setattr(plugin_api, "get_hermes_home", lambda: home)

    # Isolate the public discovery sweep, but do not bypass it: PluginManager
    # still parses plugin.yaml, file-path imports the installed package, creates
    # PluginContext, and invokes register(ctx).
    empty_bundled = tmp_path / "empty-bundled"
    empty_bundled.mkdir()
    monkeypatch.setattr(plugin_api, "get_bundled_plugins_dir", lambda: empty_bundled)
    for module_name in tuple(sys.modules):
        if module_name == "hermes_plugins.hermes_talk" or module_name.startswith(
            "hermes_plugins.hermes_talk."
        ):
            sys.modules.pop(module_name, None)
    monkeypatch.setattr(plugin_api, "_plugin_manager", PluginManager())
    manager = plugin_api.get_plugin_manager()
    manager.discover_and_load(force=True)

    registration = get_plugin_command_registration("talk")
    assert registration is not None
    assert registration["plugin"] == "hermes-talk"
    assert registration["invocation_context"] is True
    loaded = manager._plugins["hermes-talk"]
    assert loaded.error is None
    assert Path(inspect.getfile(loaded.module)).resolve() == installed_plugin / "__init__.py"
    talk_plugin = loaded.module
    assert Path(inspect.getfile(talk_plugin.talk_discord)).resolve() == (
        installed_plugin / "talk_discord.py"
    )
    assert hasattr(talk_plugin.talk_discord, "start_core_session"), sorted(
        name for name in vars(talk_plugin.talk_discord) if "core" in name or "session" in name
    )

    # One physical SessionDB is shared by the one production SessionStore and
    # runner's AsyncSessionDB facade.  GatewayRunner still executes its normal
    # constructor; only the DB factory is pinned to that exact authority.
    db = SessionDB(db_path=home / "state.db")
    real_session_db_type = SessionDB
    # GatewayRunner mutates this module global directly during construction.
    # Register its original value with monkeypatch so the following tests cannot
    # discover this test's runner after teardown through Talk's legacy bridge.
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", gateway_run._gateway_runner_ref)
    monkeypatch.setattr(hermes_state, "SessionDB", lambda: db)
    config = GatewayConfig(sessions_dir=home / "sessions")
    runner = gateway_run.GatewayRunner(config)
    monkeypatch.setattr(hermes_state, "SessionDB", real_session_db_type)
    assert type(runner) is gateway_run.GatewayRunner
    assert type(runner.session_store) is SessionStore
    assert runner.session_store._db is db
    assert runner._session_db._db is db

    # Gateway startup may load the operator's global legacy dotenv. Clear that
    # inherited policy so this adapter proves its own PlatformConfig allowlist.
    monkeypatch.delenv("DISCORD_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("DISCORD_ALLOWED_ROLES", raising=False)
    monkeypatch.delenv("DISCORD_ALLOWED_CHANNELS", raising=False)

    # The exact test interpreter intentionally has no discord.py transport.
    # Supply only its runtime channel classes; adapter auth/normalization/send
    # implementations remain production code.
    monkeypatch.setattr(
        discord_adapter_module,
        "discord",
        SimpleNamespace(
            DMChannel=type("DMChannel", (), {}),
            ForumChannel=type("ForumChannel", (), {}),
            Thread=type("Thread", (), {}),
        ),
    )

    user_id = 111111111111111111
    channel_id = 222222222222222222
    guild_id = 555555555555555555
    guild = SimpleNamespace(id=guild_id, name="Truthful Guild")
    channel = SimpleNamespace(
        id=channel_id,
        name="truthful-talk",
        guild=guild,
        parent=None,
        parent_id=None,
        topic=None,
    )

    class InteractionResponse:
        def __init__(self) -> None:
            self.defer_calls = 0

        async def defer(self, *, ephemeral: bool) -> None:
            assert ephemeral is True
            self.defer_calls += 1

        async def send_message(self, *_args, **_kwargs) -> None:
            raise AssertionError("the configured allowlist must authorize the slash")

    class Interaction:
        def __init__(self) -> None:
            self.user = SimpleNamespace(
                id=user_id,
                name="truthful-user",
                display_name="Truthful User",
                roles=(),
            )
            self.channel_id = channel_id
            self.channel = channel
            self.guild_id = guild_id
            self.guild = guild
            self.response = InteractionResponse()
            self.delete_calls = 0

        async def delete_original_response(self) -> None:
            self.delete_calls += 1

        async def edit_original_response(self, **_kwargs) -> None:
            raise AssertionError("these slash commands do not use a followup")

    adapter = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            typing_indicator=False,
            extra={"allow_from": [str(user_id)]},
        )
    )
    adapter._allowed_user_ids = adapter._get_allowed_users()
    adapter._allowed_role_ids = adapter._get_allowed_roles()
    assert adapter._allowed_user_ids == {str(user_id)}
    # GatewayRunner owns a second real authorization gate sourced from the
    # canonical Discord env bridge; align it with the adapter's config grant.
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", str(user_id))
    adapter.gateway_runner = runner
    adapter.set_message_handler(runner._handle_message)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    adapter.set_session_store(runner.session_store)
    adapter._voice_clients[guild_id] = object()
    adapter._voice_receivers[guild_id] = object()
    adapter._voice_text_channels[guild_id] = channel_id
    runner.adapters[Platform.DISCORD] = adapter

    deliveries: list[dict[str, object]] = []

    class PhysicalDiscordChannel:
        async def send(self, **kwargs):
            deliveries.append(dict(kwargs))
            return SimpleNamespace(id=900000000000000000 + len(deliveries))

    physical_channel = PhysicalDiscordChannel()

    class PhysicalDiscordClient:
        def get_channel(self, candidate: int):
            return physical_channel if candidate == channel_id else None

        async def fetch_channel(self, candidate: int):
            return self.get_channel(candidate)

    adapter._client = PhysicalDiscordClient()

    join_interaction = Interaction()
    normalized_join = adapter._build_slash_event(join_interaction, "/talk core join")
    source = normalized_join.source
    assert source.platform is Platform.DISCORD
    assert source.chat_id == str(channel_id)
    assert source.user_id == str(user_id)
    assert source.guild_id == str(guild_id)
    entry = runner.session_store.get_or_create_session(source)
    route = build_session_key(source)
    assert entry.session_key == route

    class ControlledWire:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            self.connect_calls: list[dict[str, object]] = []
            self.sent: list[tuple[object, ...]] = []
            self.close_calls = 0

        async def connect(self, **kwargs) -> None:
            self.connect_calls.append(dict(kwargs))

        async def send_json(self, messages) -> None:
            self.sent.append(tuple(messages))

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.incoming.get()

        async def close(self) -> None:
            self.close_calls += 1

    provider = get_provider("talk_openai_realtime")
    assert provider is not None
    assert type(provider) is talk_plugin.talk_core_realtime.TalkOpenAIRealtimeProvider
    assert (
        Path(inspect.getfile(type(provider))).resolve()
        == installed_plugin / "talk_core_realtime.py"
    )
    controlled_wire = ControlledWire()
    wire_factory_calls: list[dict[str, object]] = []

    def wire_factory(**kwargs):
        wire_factory_calls.append(dict(kwargs))
        if len(wire_factory_calls) > 1:
            raise AssertionError("the installed provider opened a second wire")
        return controlled_wire

    monkeypatch.setattr(
        provider,
        "_auth_resolver",
        lambda: talk_plugin.talk_auth.TalkAuth(
            token="test-platform-token",
            source=talk_plugin.talk_auth.SOURCE_CONFIGURED,
            detail="cross-repo test",
        ),
    )
    monkeypatch.setattr(provider, "_wire_factory", wire_factory)

    audio_stopped = threading.Event()

    class CaptureOnlyAudio:
        constructions = 0
        starts = 0
        stops = 0

        def __init__(self, actual_guild_id: int, *, capture_only: bool = False):
            type(self).constructions += 1
            assert actual_guild_id == guild_id
            assert capture_only is True

        def start(self) -> None:
            type(self).starts += 1

        def stop(self) -> None:
            type(self).stops += 1
            audio_stopped.set()

        def read_input_packet(self):
            audio_stopped.wait(0.01)
            return None

    monkeypatch.setattr(talk_plugin.talk_discord, "DiscordAudio", CaptureOnlyAudio)
    monkeypatch.setattr(
        talk_plugin.talk_cli,
        "run_talk_session",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy Talk executor must never be selected")
        ),
    )

    # The model/network boundary is the sole execution replacement.  The real
    # gateway handler, installed command, host, controller, provider pump,
    # SessionStore/SessionDB, turn lease, and adapter delivery all remain live.
    handler_started = threading.Event()
    release_handler = threading.Event()
    model_calls: list[dict[str, object]] = []

    def controlled_model_io(_completions, **api_kwargs):
        messages = api_kwargs.get("messages") or []
        is_canonical_turn = "installed Talk voice turn" in repr(messages)
        tools = api_kwargs.get("tools")
        if is_canonical_turn:
            model_calls.append(dict(api_kwargs))
            assert isinstance(tools, list) and tools
            tool_names = {
                schema["function"]["name"]
                for schema in tools
                if isinstance(schema, dict) and isinstance(schema.get("function"), dict)
            }
            assert "terminal" in tool_names
            handler_started.set()
            if not release_handler.wait(5):
                raise AssertionError("timed out waiting to release canonical model request")
        return SimpleNamespace(
            id="chatcmpl-truthful-talk",
            created=0,
            model="test-openai-compatible",
            choices=[
                SimpleNamespace(
                    index=0,
                    finish_reason="stop",
                    message=SimpleNamespace(
                        role="assistant",
                        content="canonical Talk response",
                        reasoning_content=None,
                        tool_calls=None,
                    ),
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=7,
                completion_tokens=3,
                total_tokens=10,
            ),
        )

    monkeypatch.setattr(Completions, "create", controlled_model_io)

    # Trap every forbidden duplicate authority after the one exact runtime is
    # assembled.  Host/controller are counted and permitted exactly once.
    constructor_calls = {"host": 0, "controller": 0, "agent": 0}
    real_host_init = GatewayRealtimeVoiceMessagingHost.__init__
    real_controller_init = GatewayRealtimeVoiceController.__init__
    real_agent_init = run_agent.AIAgent.__init__

    def one_host(self, *args, **kwargs):
        constructor_calls["host"] += 1
        if constructor_calls["host"] > 1:
            raise AssertionError("a second canonical messaging host was constructed")
        real_host_init(self, *args, **kwargs)

    def one_controller(self, *args, **kwargs):
        constructor_calls["controller"] += 1
        if constructor_calls["controller"] > 1:
            raise AssertionError("a second realtime controller was constructed")
        real_controller_init(self, *args, **kwargs)

    def one_agent(self, *args, **kwargs):
        constructor_calls["agent"] += 1
        if constructor_calls["agent"] > 1:
            raise AssertionError("a second canonical AIAgent was constructed")
        real_agent_init(self, *args, **kwargs)

    monkeypatch.setattr(GatewayRealtimeVoiceMessagingHost, "__init__", one_host)
    monkeypatch.setattr(GatewayRealtimeVoiceController, "__init__", one_controller)
    monkeypatch.setattr(run_agent.AIAgent, "__init__", one_agent)

    def duplicate(name: str):
        def reject(*_args, **_kwargs):
            raise AssertionError(f"a second {name} was constructed")

        return reject

    monkeypatch.setattr(gateway_run.GatewayRunner, "__init__", duplicate("GatewayRunner"))
    monkeypatch.setattr(SessionStore, "__init__", duplicate("SessionStore"))
    monkeypatch.setattr(real_session_db_type, "__init__", duplicate("SessionDB"))

    captured_events: list[object] = []

    def capture_dispatch(_hook_name, **kwargs):
        if "event" in kwargs:
            captured_events.append(kwargs["event"])
        return []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", capture_dispatch)

    try:
        # The real slash gate validates native snowflakes, applies the configured
        # allowlist, normalizes the source/event, and dispatches the installed handler.
        await adapter._run_simple_slash(join_interaction, "/talk core join")
        assert join_interaction.response.defer_calls == 1
        assert join_interaction.delete_calls == 1
        command_task = adapter._session_tasks.get(route)
        assert command_task is not None
        await asyncio.wait_for(command_task, timeout=5)
        core_task = talk_plugin.talk_discord._SESSION["task"]
        await _wait_until(lambda: len(controlled_wire.connect_calls) == 1 or core_task.done())
        if core_task.done():
            await core_task
        assert talk_plugin.talk_discord._SESSION["mode"] == "core"
        assert CaptureOnlyAudio.constructions == 1
        assert wire_factory_calls == [
            {
                "auth_token": "test-platform-token",
                "auth_source": talk_plugin.talk_auth.SOURCE_CONFIGURED,
            }
        ]
        wire_setup = controlled_wire.connect_calls[0]
        assert wire_setup["tools"] is None
        assert wire_setup["automatic_response"] is False
        assert constructor_calls == {"host": 1, "controller": 1, "agent": 0}

        await controlled_wire.incoming.put(
            {"type": "session.created", "session": {"id": "native-provider-session"}}
        )
        await controlled_wire.incoming.put(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "actual-talk-final-item",
                "transcript": "installed Talk voice turn",
            }
        )
        await asyncio.wait_for(asyncio.to_thread(handler_started.wait, 15), timeout=16)
        assert handler_started.is_set()
        assert runner._is_session_running(route)
        assert not release_handler.is_set()
        assert len(_ACCEPTED_TASKS) == 1
        accepted_task = next(iter(_ACCEPTED_TASKS))
        assert not accepted_task.done()

        rows_before_leave = db.get_messages(entry.session_id, include_inactive=True)
        pending_before = dict(adapter._pending_messages)
        model_count_before = len(model_calls)

        # This exact installed /talk handler must bypass generic busy routing.
        # It returns and closes capture while canonical work remains blocked.
        started = time.perf_counter()
        leave_interaction = Interaction()
        await asyncio.wait_for(
            adapter._run_simple_slash(leave_interaction, "/talk leave"), timeout=1
        )
        assert time.perf_counter() - started < 1
        assert leave_interaction.response.defer_calls == 1
        assert leave_interaction.delete_calls == 1
        await _wait_until(lambda: controlled_wire.close_calls >= 1, timeout=1)
        assert accepted_task.done() is False
        assert handler_started.is_set() and not release_handler.is_set()
        assert runner._is_session_running(route)
        assert db.get_messages(entry.session_id, include_inactive=True) == rows_before_leave
        assert adapter._pending_messages == pending_before == {}
        assert len(model_calls) == model_count_before == 1
        assert audio_stopped.is_set()

        release_handler.set()
        result = await asyncio.wait_for(accepted_task, timeout=5)
        assert result is True
        await _wait_until(lambda: not runner._is_session_running(route))

        rows = db.get_messages(entry.session_id, include_inactive=True)
        conversation_rows = [row for row in rows if row["role"] in {"user", "assistant"}]
        assert [(row["role"], row["content"]) for row in conversation_rows] == [
            ("user", "installed Talk voice turn"),
            ("assistant", "canonical Talk response"),
        ]
        user_row, assistant_row = conversation_rows
        assert user_row["id"] < assistant_row["id"]
        assert user_row["display_kind"] == "realtime_voice_turn"
        marker = user_row["display_metadata"][_MARKER_KEY]
        assert type(marker) is str and len(marker) == 32

        attached_event = next(event for event in captured_events if hasattr(event, _CLAIM_ATTR))
        claim = getattr(attached_event, _CLAIM_ATTR)
        receipt = claim.receipt
        assert type(receipt) is RealtimeVoiceFinalizationReceipt
        assert receipt.turn_marker == marker
        assert receipt.user_message_id == user_row["id"]
        assert receipt.assistant_message_id == assistant_row["id"]
        assert claim.host.validate_finalization(receipt)
        reread_user = next(
            row
            for row in db.get_messages(entry.session_id, include_inactive=True)
            if row["id"] == receipt.user_message_id
        )
        assert reread_user["display_metadata"][_MARKER_KEY] == marker

        canonical_deliveries = [
            item for item in deliveries if item.get("content") == "canonical Talk response"
        ]
        assert canonical_deliveries == [{"content": "canonical Talk response", "reference": None}]
        assert len(model_calls) == 1
        sent_messages = model_calls[0]["messages"]
        assert any(
            message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and message["content"].startswith("installed Talk voice turn")
            for message in sent_messages
        )
        assert len(controlled_wire.connect_calls) == 1
        assert controlled_wire.close_calls >= 1
        assert constructor_calls == {"host": 1, "controller": 1, "agent": 1}
        assert not _ACCEPTED_TASKS
        assert adapter._pending_messages == {}
        assert runner._turn_lease_tokens == {}
    finally:
        release_handler.set()
        task = talk_plugin.talk_discord._SESSION.get("task")
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        accepted = tuple(_ACCEPTED_TASKS)
        if accepted:
            await asyncio.gather(*accepted, return_exceptions=True)
        session_tasks = tuple(adapter._session_tasks.values())
        for session_task in session_tasks:
            if not session_task.done():
                session_task.cancel()
        if session_tasks:
            await asyncio.gather(*session_tasks, return_exceptions=True)
        adapter._session_tasks.clear()
        talk_plugin.talk_discord.reset_for_tests()
        for cached in tuple(getattr(runner, "_agent_cache", {}).values()):
            candidate = cached[0] if isinstance(cached, tuple) else cached
            if candidate is not None:
                runner._cleanup_agent_resources(candidate)
        getattr(runner, "_agent_cache", {}).clear()
        with realtime_voice_invocation._state_lock:
            realtime_voice_invocation._invocation_states.clear()
            realtime_voice_invocation._factory_records.clear()
            realtime_voice_invocation._consumed_factories.clear()
            realtime_voice_invocation._gateway_hosts.clear()
        manager._plugins.clear()
        manager._hooks.clear()
        manager._middleware.clear()
        manager._plugin_commands.clear()
        manager._discovered = False
        _reset_for_tests()
        for module_name in tuple(sys.modules):
            if module_name == "hermes_plugins.hermes_talk" or module_name.startswith(
                "hermes_plugins.hermes_talk."
            ):
                sys.modules.pop(module_name, None)
        db.close()
