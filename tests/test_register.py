"""register(ctx) — against a stub host, loaded the way Hermes loads it.

The plugin is loaded here by file path with ``submodule_search_locations``,
which is exactly what ``hermes_cli/plugins.py`` does. That makes this test
cover the package half of the dual-import shim as well as the registration
surfaces themselves.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
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
        self.realtime_providers: list[object] = []
        self.realtime_receipt = True

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

    def register_realtime_voice_provider(self, provider):
        self._maybe_fail("realtime")
        self.realtime_providers.append(provider)
        return self.realtime_receipt

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
    if plugin.talk_core_realtime.core_provider_available():
        assert len(ctx.realtime_providers) == 1
        assert ctx.realtime_providers[0].name == "talk_openai_realtime"
    else:
        assert ctx.realtime_providers == []
    assert plugin.REGISTRATION_FAILURES == []
    expected_receipts = (
        {"registered"}
        if plugin.talk_core_realtime.core_provider_available()
        else {"registered", "unsupported-optional"}
    )
    assert set(plugin.talk_tools.REGISTRATION_RECEIPTS.values()) == expected_receipts


def test_realtime_registration_false_is_rejected_not_registered(plugin):
    if not plugin.talk_core_realtime.core_provider_available():
        pytest.skip("optional Hermes core API-v2 is absent")
    ctx = StubCtx()
    ctx.realtime_receipt = False

    plugin.register(ctx)

    assert plugin.REGISTRATION_RECEIPTS["realtime_voice_provider"] == "rejected"
    assert len(ctx.realtime_providers) == 1


def test_real_api_v2_context_accepts_the_talk_provider(plugin):
    if not plugin.talk_core_realtime.core_provider_available():
        pytest.skip("optional Hermes core API-v2 is absent")
    try:
        from agent import realtime_voice_registry
        from hermes_cli.plugins import PluginContext, PluginManifest
    except ImportError:
        pytest.skip("optional Hermes plugin API is absent")

    realtime_voice_registry._reset_for_tests()
    ctx = PluginContext(PluginManifest(name="hermes-talk-test"), object())
    try:
        plugin._attempt_boolean_registration(
            ctx,
            "register_realtime_voice_provider",
            "realtime voice provider",
            "realtime_voice_provider",
            plugin.talk_core_realtime.TalkOpenAIRealtimeProvider(),
        )
        registered = realtime_voice_registry.get_provider("talk_openai_realtime")
        assert plugin.REGISTRATION_RECEIPTS["realtime_voice_provider"] == "registered"
        assert registered is not None
        assert registered.api_version == 2
    finally:
        realtime_voice_registry._reset_for_tests()


def test_realtime_registration_exception_is_redacted_failure(plugin):
    if not plugin.talk_core_realtime.core_provider_available():
        pytest.skip("optional Hermes core API-v2 is absent")
    ctx = StubCtx(failing={"realtime"})

    plugin.register(ctx)

    assert plugin.REGISTRATION_RECEIPTS["realtime_voice_provider"] == "failed"
    assert any(
        "realtime voice provider: RuntimeError" in item for item in plugin.REGISTRATION_FAILURES
    )


def test_missing_realtime_host_surface_is_unsupported_optional(plugin):
    ctx = StubCtx()
    ctx.register_realtime_voice_provider = None

    plugin.register(ctx)

    assert plugin.REGISTRATION_RECEIPTS["realtime_voice_provider"] == "unsupported-optional"


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
    ctx = StubCtx(failing={"cli", "slash", "hook", "tts", "stt", "realtime"})

    plugin.register(ctx)

    # Core-absent standalone Talk records realtime as unsupported-optional;
    # a core-present host attempts it and records the eighth failure.
    expected_failures = 8 if plugin.talk_core_realtime.core_provider_available() else 7
    assert len(plugin.REGISTRATION_FAILURES) == expected_failures
    assert plugin.talk_host.get_ctx() is ctx
    expected_receipts = (
        {"failed"}
        if plugin.talk_core_realtime.core_provider_available()
        else {"failed", "unsupported-optional"}
    )
    assert set(plugin.talk_tools.REGISTRATION_RECEIPTS.values()) == expected_receipts


def test_registration_failure_receipts_redact_host_exception_text(plugin, monkeypatch):
    class SecretFailureCtx(StubCtx):
        def _maybe_fail(self, surface):
            raise RuntimeError("sk-host-secret must not reach status")

    monkeypatch.setattr(plugin.talk_providers, "providers_available", lambda: True)

    plugin.register(SecretFailureCtx())

    rendered = " ".join(plugin.REGISTRATION_FAILURES)
    assert "RuntimeError" in rendered
    assert "sk-host-secret" not in rendered


def test_providers_skipped_when_the_host_abcs_are_absent(plugin, monkeypatch):
    monkeypatch.setattr(plugin.talk_providers, "providers_available", lambda: False)
    ctx = StubCtx()

    plugin.register(ctx)

    assert ctx.tts_providers == []
    assert ctx.stt_providers == []
    assert plugin.REGISTRATION_FAILURES == []
    assert plugin.talk_tools.REGISTRATION_RECEIPTS["tts_provider"] == "unsupported-optional"
    assert (
        plugin.talk_tools.REGISTRATION_RECEIPTS["transcription_provider"] == "unsupported-optional"
    )


def test_attribute_error_inside_present_registration_method_is_failure(plugin, monkeypatch):
    class InternalAttributeErrorCtx(StubCtx):
        def register_cli_command(self, *args, **kwargs):
            raise AttributeError("internal implementation bug")

    monkeypatch.setattr(plugin.talk_providers, "providers_available", lambda: False)

    plugin.register(InternalAttributeErrorCtx())

    assert plugin.REGISTRATION_RECEIPTS["cli_command"] == "failed"
    assert any("cli command: AttributeError" in item for item in plugin.REGISTRATION_FAILURES)
    check = plugin.talk_cli.talk_doctor._plugin_check()
    assert check["status"] == "fail"
    assert check["details"]["required_issue_count"] == 1


def test_absent_optional_methods_warn_without_registration_failure(plugin, monkeypatch):
    class CoreOnlyCtx:
        def register_cli_command(self, **kwargs):
            pass

        def register_command(self, *args, **kwargs):
            pass

    monkeypatch.setattr(plugin.talk_providers, "providers_available", lambda: True)

    plugin.register(CoreOnlyCtx())

    assert plugin.REGISTRATION_FAILURES == []
    assert plugin.REGISTRATION_RECEIPTS["cli_command"] == "registered"
    assert plugin.REGISTRATION_RECEIPTS["slash_command"] == "registered"
    assert {
        state
        for receipt, state in plugin.REGISTRATION_RECEIPTS.items()
        if plugin.talk_tools.REGISTRATION_REQUIREMENTS[receipt] == "optional"
    } == {"unsupported-optional"}
    check = plugin.talk_cli.talk_doctor._plugin_check()
    assert check["status"] == "warn"
    assert check["details"]["required_issue_count"] == 0
    assert check["details"]["optional_issue_count"] == 6


def test_absent_required_method_is_a_required_failure_not_an_exception(plugin, monkeypatch):
    class MissingCliCtx:
        def register_command(self, *args, **kwargs):
            pass

        def register_hook(self, *args, **kwargs):
            pass

    monkeypatch.setattr(plugin.talk_providers, "providers_available", lambda: False)

    plugin.register(MissingCliCtx())

    assert plugin.REGISTRATION_RECEIPTS["cli_command"] == "unsupported-required"
    assert plugin.REGISTRATION_FAILURES == []
    check = plugin.talk_cli.talk_doctor._plugin_check()
    assert check["status"] == "fail"
    assert check["details"]["required_issue_count"] == 1


def test_session_end_hook_sweeps_transcripts_fail_open(plugin, monkeypatch, tmp_path):
    swept = []
    monkeypatch.setattr(plugin.talk_config, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(plugin.talk_transcript, "sweep_transcripts", swept.append)
    ctx = StubCtx()
    plugin.register(ctx)
    _, callback = ctx.hooks[0]

    assert callback(session_id="abc") is None
    assert swept == [tmp_path]


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


def test_core_absent_process_keeps_legacy_imports_and_reports_optional():
    script = textwrap.dedent(
        f"""
        import importlib.abc
        import importlib.util
        import sys
        import types
        from pathlib import Path

        class BlockCore(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "agent" or fullname.startswith("agent."):
                    raise ModuleNotFoundError(fullname)
                if fullname == "hermes_cli" or fullname.startswith("hermes_cli."):
                    raise ModuleNotFoundError(fullname)
                return None

        sys.meta_path.insert(0, BlockCore())
        root = Path({str(REPO_ROOT)!r})
        sys.path.insert(0, str(root))
        parent = types.ModuleType("hermes_plugins")
        parent.__path__ = []
        sys.modules["hermes_plugins"] = parent
        name = "hermes_plugins.hermes_talk_core_absent"
        spec = importlib.util.spec_from_file_location(
            name, root / "__init__.py", submodule_search_locations=[str(root)]
        )
        plugin = importlib.util.module_from_spec(spec)
        plugin.__package__ = name
        plugin.__path__ = [str(root)]
        sys.modules[name] = plugin
        spec.loader.exec_module(plugin)

        class OldHost:
            def register_cli_command(self, **kwargs): pass
            def register_command(self, *args, **kwargs): pass
            def register_hook(self, *args, **kwargs): pass
            def register_tts_provider(self, provider): pass
            def register_transcription_provider(self, provider): pass

        plugin.register(OldHost())
        assert plugin.talk_core_realtime.core_provider_available() is False
        assert plugin.talk_core_realtime.TalkOpenAIRealtimeProvider is None
        assert plugin.REGISTRATION_RECEIPTS["realtime_voice_provider"] == "unsupported-optional"
        assert plugin.talk_openai_realtime.OpenAIRealtimeSession
        assert plugin.talk_cli.cli_entry
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
