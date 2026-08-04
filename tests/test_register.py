"""register(ctx) — against a stub host, loaded the way Hermes loads it.

The plugin is loaded here by file path with ``submodule_search_locations``,
which is exactly what ``hermes_cli/plugins.py`` does. That makes this test
cover the package half of the dual-import shim as well as the registration
surfaces themselves.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_plugin():
    """Load ``__init__.py`` as ``hermes_plugins.hermes_talk``, as Hermes does."""

    parent = "hermes_plugins"
    if parent not in sys.modules:
        namespace = types.ModuleType(parent)
        namespace.__path__ = []  # type: ignore[attr-defined]
        sys.modules[parent] = namespace

    module_name = f"{parent}.hermes_talk"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(REPO_ROOT)]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class StubCtx:
    """Records every registration the plugin attempts."""

    def __init__(self, *, failing: set[str] | None = None):
        self.failing = failing or set()
        self.cli_commands: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.hooks: list[tuple[str, object]] = []
        self.tts_providers: list[object] = []
        self.stt_providers: list[object] = []

    def _maybe_fail(self, surface: str) -> None:
        if surface in self.failing:
            raise RuntimeError(f"{surface} unsupported on this host")

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self._maybe_fail("cli")
        self.cli_commands[name] = {
            "help": help,
            "setup_fn": setup_fn,
            "handler_fn": handler_fn,
            "description": description,
        }

    def register_command(self, name, handler, description="", args_hint=""):
        self._maybe_fail("slash")
        self.commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }

    def register_hook(self, hook_name, callback):
        self._maybe_fail("hook")
        self.hooks.append((hook_name, callback))

    def register_tts_provider(self, provider):
        self._maybe_fail("tts")
        self.tts_providers.append(provider)

    def register_transcription_provider(self, provider):
        self._maybe_fail("stt")
        self.stt_providers.append(provider)

    def dispatch_tool(self, tool_name, args, **kwargs):  # pragma: no cover - unused here
        return "{}"


@pytest.fixture
def plugin():
    module = load_plugin()
    module.REGISTRATION_FAILURES.clear()
    yield module
    module.REGISTRATION_FAILURES.clear()
    module.talk_host.bind_ctx(None)


def test_register_wires_every_surface(plugin, monkeypatch):
    monkeypatch.setattr(plugin.talk_providers, "providers_available", lambda: True)
    ctx = StubCtx()

    plugin.register(ctx)

    assert "talk" in ctx.cli_commands
    assert ctx.cli_commands["talk"]["setup_fn"] is plugin.talk_cli.setup_cli
    assert ctx.cli_commands["talk"]["handler_fn"] is plugin.talk_cli.cli_entry
    assert "talk" in ctx.commands
    assert callable(ctx.commands["talk"]["handler"])
    assert [name for name, _ in ctx.hooks] == [
        "on_session_end",
        "subagent_start",
        "subagent_stop",
    ]
    assert len(ctx.tts_providers) == 1
    assert len(ctx.stt_providers) == 1
    assert plugin.REGISTRATION_FAILURES == []


def test_register_binds_the_context(plugin):
    ctx = StubCtx()
    plugin.register(ctx)
    assert plugin.talk_host.get_ctx() is ctx


def test_a_failing_provider_does_not_abort_registration(plugin, monkeypatch):
    monkeypatch.setattr(plugin.talk_providers, "providers_available", lambda: True)
    ctx = StubCtx(failing={"tts"})

    plugin.register(ctx)

    # The three core surfaces still land, and the failure is on the record
    # rather than swallowed — talk_status reads this list.
    assert "talk" in ctx.cli_commands
    assert "talk" in ctx.commands
    assert ctx.hooks
    assert len(ctx.stt_providers) == 1
    assert len(plugin.REGISTRATION_FAILURES) == 1
    assert "tts provider" in plugin.REGISTRATION_FAILURES[0]


def test_every_surface_failing_is_recorded_separately(plugin, monkeypatch):
    monkeypatch.setattr(plugin.talk_providers, "providers_available", lambda: True)
    ctx = StubCtx(failing={"cli", "slash", "hook", "tts", "stt"})

    plugin.register(ctx)

    # cli + slash + three hooks + tts + stt, each recorded on its own line.
    assert len(plugin.REGISTRATION_FAILURES) == 7
    assert plugin.talk_host.get_ctx() is ctx


def test_providers_skipped_when_the_host_abcs_are_absent(plugin, monkeypatch):
    monkeypatch.setattr(plugin.talk_providers, "providers_available", lambda: False)
    ctx = StubCtx()

    plugin.register(ctx)

    assert ctx.tts_providers == []
    assert ctx.stt_providers == []
    assert plugin.REGISTRATION_FAILURES == []


def test_session_end_hook_is_a_noop(plugin):
    ctx = StubCtx()
    plugin.register(ctx)
    _, callback = ctx.hooks[0]
    assert callback(session_id="abc") is None


def test_slash_command_takes_the_discord_room_inside_an_event_loop(plugin):
    # Inside the gateway the call runs in the host's Discord voice channel,
    # not a terminal nobody is looking at. With no gateway present it must
    # refuse in one sentence rather than raise.
    import asyncio

    ctx = StubCtx()
    plugin.register(ctx)
    handler = ctx.commands["talk"]["handler"]

    async def call_from_a_loop():
        return handler("")

    reply = asyncio.run(call_from_a_loop())
    assert "gateway" in reply.lower()
    assert "traceback" not in reply.lower()


def test_slash_command_subcommands_are_gateway_only(plugin):
    # In a terminal, join/leave/status name a room that isn't there.
    ctx = StubCtx()
    plugin.register(ctx)
    handler = ctx.commands["talk"]["handler"]

    reply = handler("status")
    assert "terminal" in reply.lower()


def test_slash_command_runs_the_session_outside_a_loop(plugin, monkeypatch):
    ctx = StubCtx()
    plugin.register(ctx)
    monkeypatch.setattr(plugin.talk_cli, "cli_entry", lambda *a, **k: 0)

    assert ctx.commands["talk"]["handler"]("") == "Voice session ended."
