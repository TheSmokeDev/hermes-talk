"""Experimental Discord auto-join on ``/talk join`` (hermes-talk#224).

The gateway, adapter and session context are fakes; no Discord connection is
made. Off by default: ``/talk join`` must call ``start_session`` exactly as
before and never touch the host's join helpers.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from test_register import load_plugin

GUILD, CHAT, USER, VOICE = 101, 202, 303, 404


class _FakeAdapter:
    def __init__(self, *, in_voice=True, perms=(True, True), join_ok=True, connected=False):
        self._voice_clients: dict = {GUILD: object()} if connected else {}
        self._voice_receivers: dict = {}
        self.guild = types.SimpleNamespace(id=GUILD, me=object())
        self.voice_channel = types.SimpleNamespace(
            id=VOICE,
            guild=self.guild,
            permissions_for=lambda _me: types.SimpleNamespace(connect=perms[0], speak=perms[1]),
        )
        text = types.SimpleNamespace(id=CHAT, guild=self.guild)
        self._client = types.SimpleNamespace(get_channel=lambda cid: text if cid == CHAT else None)
        self.in_voice = in_voice
        self.join_ok = join_ok
        self.joins: list = []
        self.leaves: list = []

    async def get_user_voice_channel(self, guild_id, user_id):
        return self.voice_channel if self.in_voice and user_id == str(USER) else None

    async def join_voice_channel(self, channel):
        self.joins.append(channel)
        if self.join_ok:
            self._voice_clients[GUILD] = object()
        return self.join_ok

    async def leave_voice_channel(self, guild_id):
        self.leaves.append(guild_id)
        self._voice_clients.pop(guild_id, None)


def _install_host(monkeypatch, adapter, *, user=USER, platform="discord"):
    env = {
        "HERMES_SESSION_PLATFORM": platform,
        "HERMES_SESSION_USER_ID": str(user),
        "HERMES_SESSION_CHAT_ID": str(CHAT),
    }
    platform_enum = types.SimpleNamespace(DISCORD="discord")
    config = types.ModuleType("gateway.config")
    config.Platform = platform_enum
    run = types.ModuleType("gateway.run")
    runner = types.SimpleNamespace(adapters={platform_enum.DISCORD: adapter})
    run._gateway_runner_ref = lambda: runner
    session_context = types.ModuleType("gateway.session_context")
    session_context.get_session_env = lambda name, default="": env.get(name, default)
    package = types.ModuleType("gateway")
    package.run, package.config, package.session_context = run, config, session_context
    for name, module in {
        "gateway": package, "gateway.run": run, "gateway.config": config,
        "gateway.session_context": session_context,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(sys.modules, "models", None)


@pytest.fixture
def plugin(monkeypatch):
    module = load_plugin()
    for name in ("TALK_DISCORD_AUTO_JOIN", "TALK_TASK_TARGET", "TALK_VOICE_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", str(USER))
    monkeypatch.setattr(module.talk_config, "plugin_setting", lambda key: None)
    module.talk_discord.reset_for_tests()
    yield module
    module.talk_discord.reset_for_tests()


def _record_start(monkeypatch, plugin, *, start_task=True, receipt="Starting voice."):
    calls: list[dict] = []
    holder: dict = {}

    def start_session(**kwargs):
        calls.append(kwargs)
        if start_task:
            task = asyncio.get_running_loop().create_future()
            holder["task"] = task
            plugin.talk_discord._SESSION["task"] = task
        return receipt

    monkeypatch.setattr(plugin.talk_discord, "start_session", start_session)
    return calls, holder


async def _join(plugin):
    result = plugin._talk_command("join", invocation=types.SimpleNamespace())
    return await result if asyncio.iscoroutine(result) else result


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


def test_knob_defaults_off_and_validates(plugin, monkeypatch):
    config = plugin.talk_config
    assert config.discord_auto_join() is False
    monkeypatch.setattr(config, "plugin_setting", {"discord_auto_join": True}.get)
    assert config.discord_auto_join() is True
    monkeypatch.setenv("TALK_DISCORD_AUTO_JOIN", "off")  # env wins
    assert config.discord_auto_join() is False
    monkeypatch.setenv("TALK_DISCORD_AUTO_JOIN", "maybe")
    with pytest.raises(config.TalkConfigError, match="TALK_DISCORD_AUTO_JOIN"):
        config.discord_auto_join()


def test_off_by_default_join_is_unchanged(plugin, monkeypatch):
    adapter = _FakeAdapter()
    _install_host(monkeypatch, adapter)
    calls, _ = _record_start(monkeypatch, plugin)

    async def run():
        return plugin._talk_command("join", invocation=types.SimpleNamespace())

    assert asyncio.run(run()) == "Starting voice."
    assert calls == [{}]  # the exact pre-feature call: no guild, no join
    assert adapter.joins == []


def test_enabled_joins_the_callers_channel_then_starts(plugin, monkeypatch):
    monkeypatch.setenv("TALK_DISCORD_AUTO_JOIN", "1")
    adapter = _FakeAdapter()
    _install_host(monkeypatch, adapter)
    calls, holder = _record_start(monkeypatch, plugin)

    async def run():
        receipt = await _join(plugin)
        await _settle()
        assert adapter.leaves == []  # live session keeps the connection
        holder["task"].set_result(0)  # the session ends
        await _settle()
        return receipt

    assert asyncio.run(run()) == "Starting voice."
    assert adapter.joins == [adapter.voice_channel]
    assert calls == [{"guild_id": GUILD}]
    assert adapter.leaves == [GUILD]  # Talk closes only what it opened


def test_existing_connection_is_reused_and_never_left(plugin, monkeypatch):
    monkeypatch.setenv("TALK_DISCORD_AUTO_JOIN", "1")
    adapter = _FakeAdapter(connected=True)
    _install_host(monkeypatch, adapter)
    calls, holder = _record_start(monkeypatch, plugin)

    async def run():
        await _join(plugin)
        holder["task"].set_result(0)
        await _settle()

    asyncio.run(run())
    assert adapter.joins == [] and adapter.leaves == []
    assert calls == [{"guild_id": GUILD}]


def test_refused_start_does_not_strand_the_bot(plugin, monkeypatch):
    monkeypatch.setenv("TALK_DISCORD_AUTO_JOIN", "1")
    adapter = _FakeAdapter()
    _install_host(monkeypatch, adapter)
    _record_start(monkeypatch, plugin, start_task=False, receipt="I'm already live.")

    async def run():
        receipt = await _join(plugin)
        await _settle()
        return receipt

    assert asyncio.run(run()) == "I'm already live."
    assert adapter.leaves == [GUILD]


@pytest.mark.parametrize(
    ("adapter_kwargs", "host_kwargs", "match"),
    [
        ({"in_voice": False}, {}, "not in a voice channel"),
        ({"perms": (True, False)}, {}, "permission"),
        ({"join_ok": False}, {}, "couldn't join"),
        ({}, {"user": 999}, "Talk operators"),
        ({}, {"platform": "telegram"}, "Discord server"),
    ],
)
def test_refusals_never_start_a_session(plugin, monkeypatch, adapter_kwargs, host_kwargs, match):
    monkeypatch.setenv("TALK_DISCORD_AUTO_JOIN", "1")
    adapter = _FakeAdapter(**adapter_kwargs)
    _install_host(monkeypatch, adapter, **host_kwargs)
    calls, _ = _record_start(monkeypatch, plugin)

    receipt = asyncio.run(_join(plugin))
    assert match in receipt
    assert calls == []
    if adapter_kwargs.get("join_ok") is not False:
        assert adapter.joins == []


def test_empty_operator_list_refuses(plugin, monkeypatch):
    monkeypatch.setenv("TALK_DISCORD_AUTO_JOIN", "1")
    monkeypatch.delenv("TALK_DISCORD_OPERATOR_USER_IDS")
    adapter = _FakeAdapter()
    _install_host(monkeypatch, adapter)
    calls, _ = _record_start(monkeypatch, plugin)
    assert "Talk operators" in asyncio.run(_join(plugin))
    assert calls == [] and adapter.joins == []
