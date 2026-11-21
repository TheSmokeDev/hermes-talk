"""Profile-isolation tests for hermes-talk issue #185.

Tests that the new profile-scoped env resolution and Discord adapter lookup
work correctly under multiplexed gateway semantics, and degrade safely
for standalone (non-multiplexed) invocations.

No Hermes install is needed: a fake ``agent.secret_scope`` and a fake
``gateway.session_context`` model the host's context-local profile scope
(``get_secret`` returns the bound profile's value or the default, and fails
closed when multiplexing is on and no scope is bound).
"""

from __future__ import annotations

import builtins
import contextvars
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import talk_config
import talk_discord

# ---------------------------------------------------------------------------
# A fake multiplexed host: one process, one context-local profile scope
# ---------------------------------------------------------------------------

_SECRET_SCOPE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "test_secret_scope", default=None
)
_SESSION_PROFILE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "test_session_profile", default=None
)


class UnscopedSecretError(RuntimeError):
    """Stands in for Hermes's fail-closed error (also a RuntimeError there)."""


def _multiplexed_get_secret(name: str, default: str | None = None) -> str | None:
    """Hermes ``get_secret`` semantics for a routed profile under multiplexing."""

    scope = _SECRET_SCOPE.get()
    if scope is None:
        raise UnscopedSecretError(name)
    return scope.get(name, default)


def _as_profile(profile: str, secrets: dict, fn, *args):
    """Run ``fn`` in a fresh context bound to one profile, like a gateway turn."""

    def turn():
        _SECRET_SCOPE.set(secrets)
        _SESSION_PROFILE.set(profile)
        return fn(*args)

    return contextvars.copy_context().run(turn)


@pytest.fixture
def multiplexed(monkeypatch):
    """Install the fake host and seed the process env with values no routed
    profile may ever see."""

    monkeypatch.setattr(talk_config, "_hermes_get_secret", _multiplexed_get_secret)
    session_context = types.ModuleType("gateway.session_context")
    session_context.get_session_env = lambda name, default="": (
        (_SESSION_PROFILE.get() or default) if name == "HERMES_SESSION_PROFILE" else default
    )
    gateway = types.ModuleType("gateway")
    gateway.session_context = session_context
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    monkeypatch.setenv("TALK_VOICE", "ash")
    monkeypatch.setenv("TALK_PROVIDER", "grok")
    monkeypatch.setenv("OPENAI_API_KEY", "process-key")
    monkeypatch.setenv("TALK_REPLY_LANGUAGE", "German")
    monkeypatch.setenv("TALK_API_SERVER_PROBE_TIMEOUT_S", "9")
    return gateway


# ---------------------------------------------------------------------------
# talk_config._talk_env_get  (profile-scoped env resolution)
# ---------------------------------------------------------------------------


class TestTalkEnvGet:
    """Verify _talk_env_get delegates to Hermes get_secret when available."""

    def test_without_hermes_falls_back_to_os_environ(self, monkeypatch):
        """When agent.secret_scope is not importable, falls through to os.environ."""
        monkeypatch.setattr(talk_config, "_hermes_get_secret", None)
        monkeypatch.setenv("TALK_TEST_MARKER", "hello-from-env")
        monkeypatch.delenv("TALK_UNSET_KEY", raising=False)
        assert talk_config._talk_env_get("TALK_TEST_MARKER") == "hello-from-env"
        assert talk_config._talk_env_get("TALK_UNSET_KEY", "default") == "default"
        assert talk_config._talk_env_get("TALK_UNSET_KEY") is None

    def test_delegates_to_hermes_get_secret_with_the_default(self, monkeypatch):
        calls = []

        def get_secret(name, default=None):
            calls.append((name, default))
            return "scoped"

        monkeypatch.setattr(talk_config, "_hermes_get_secret", get_secret)
        monkeypatch.setenv("TALK_TEST_MARKER", "hello-from-env")
        assert talk_config._talk_env_get("TALK_TEST_MARKER", "fallback") == "scoped"
        assert calls == [("TALK_TEST_MARKER", "fallback")]

    def test_shared_credential_fallback(self, monkeypatch):
        """resolve_openai_key falls back from TALK_OPENAI_API_KEY to OPENAI_API_KEY,
        both profile-scoped."""
        monkeypatch.setattr(talk_config, "_hermes_get_secret", _multiplexed_get_secret)
        monkeypatch.setenv("OPENAI_API_KEY", "process-key")
        assert _as_profile("work", {"OPENAI_API_KEY": "shared-a"},
                           talk_config.resolve_openai_key) == "shared-a"
        with pytest.raises(talk_config.TalkConfigError, match="no OpenAI key"):
            _as_profile("work", {}, talk_config.resolve_openai_key)


# ---------------------------------------------------------------------------
# talk_discord Discord adapter resolution
# ---------------------------------------------------------------------------


class _DiscordKey:
    """Hashable mock for an enum value used as dict key."""
    __slots__ = ("name", "value")
    def __init__(self, value: str, name: str = "") -> None:
        self.value = value
        self.name = name
    def __hash__(self) -> int:
        return hash(self.value)
    def __eq__(self, other: object) -> bool:
        if isinstance(other, _DiscordKey):
            return self.value == other.value
        return False


class _FakePlatform:
    DISCORD = _DiscordKey("DISCORD", "DISCORD")


class _FakeGatewayConfig:
    Platform = _FakePlatform


class TestDiscordAdapterResolution:
    """Verify resolve_voice_bridge uses the invoking profile's adapter map.

    Mirrors the rule from authz_mixin.py:_adapters_for_profile:
      - None / "default" → primary adapters (runner.adapters)
      - Named profile in _profile_adapters → that profile's map (fail closed)
      - Named profile == _primary_profile_name → primary adapters
      - Named profile NOT in _profile_adapters and != primary → fail closed ({})
      - No _profile_adapters (standalone/legacy) → primary adapters
    """

    def _make_adapter_map(self, discord_adapter):
        """Create a minimal adapter map keyed by DISCORD key."""
        return {_DiscordKey("DISCORD", "DISCORD"): discord_adapter}

    def _make_runner(
        self,
        adapters: dict | None = None,
        profile_adapters: dict[str, dict] | None = None,
        primary_name: str = "default",
    ) -> SimpleNamespace:
        """Build a minimal mock runner with adapter maps."""
        if adapters is None:
            adapters = {}
        if profile_adapters is None:
            profile_adapters = {}
        runner = SimpleNamespace(
            adapters=adapters,
            _profile_adapters=profile_adapters,
        )
        if primary_name is not None:
            runner._primary_profile_name = primary_name
        return runner

    def _import_mock(self, name, *args, **kwargs):
        """Replace __import__ for the adapter-search paths."""
        fake_modules = {
            "gateway.config": _FakeGatewayConfig,
            "models": _FakeGatewayConfig,
            "gateway.platform_registry": _FakeGatewayConfig,
        }
        if name in fake_modules:
            return fake_modules[name]
        return _real_import(name, *args, **kwargs)

    def test_standalone_falls_back_to_default_adapters(self):
        """When no profile is known (profile_name=None), uses runner.adapters."""
        from talk_discord import _resolve_discord_adapter_for_profile

        discord_adapter = object()
        adapters = self._make_adapter_map(discord_adapter)
        runner = self._make_runner(adapters=adapters, profile_adapters={})

        with patch.object(builtins, "__import__", side_effect=self._import_mock):
            result = _resolve_discord_adapter_for_profile(runner, None)
            assert result is discord_adapter

    def test_secondary_profile_uses_profile_map(self):
        """When profile_name is in _profile_adapters, uses that profile's map."""
        from talk_discord import _resolve_discord_adapter_for_profile

        adapters = self._make_adapter_map(object())
        profile_map = self._make_adapter_map(object())
        profile_adapters = {"WorkProfile": profile_map}
        runner = self._make_runner(adapters=adapters, profile_adapters=profile_adapters)
        profile_discord = profile_map[_DiscordKey("DISCORD", "DISCORD")]
        default_discord = adapters[_DiscordKey("DISCORD", "DISCORD")]

        with patch.object(builtins, "__import__", side_effect=self._import_mock):
            result = _resolve_discord_adapter_for_profile(runner, "WorkProfile")
            assert result is profile_discord
            assert result is not default_discord

    def test_explicit_primary_profile_uses_runner_adapters(self):
        """When profile_name matches _primary_profile_name but is not in
        _profile_adapters, uses runner.adapters (primary profile by name)."""
        from talk_discord import _resolve_discord_adapter_for_profile

        discord_adapter = object()
        adapters = self._make_adapter_map(discord_adapter)
        # Runner has _profile_adapters but "LaunchProfile" is NOT a key in it.
        # It is the primary by _primary_profile_name.
        profile_adapters = {"WorkProfile": self._make_adapter_map(object())}
        runner = self._make_runner(
            adapters=adapters,
            profile_adapters=profile_adapters,
            primary_name="LaunchProfile",
        )

        with patch.object(builtins, "__import__", side_effect=self._import_mock):
            result = _resolve_discord_adapter_for_profile(runner, "LaunchProfile")
            assert result is discord_adapter
            # NOT a WorkProfile bot
            work_bot = profile_adapters["WorkProfile"][_DiscordKey("DISCORD", "DISCORD")]
            assert result is not work_bot

    def test_unknown_secondary_profile_fails_closed(self):
        """When profile_name is not in _profile_adapters and != _primary_profile_name,
        returns None (no adapter map found) rather than borrowing the default bot.

        This mirrors authz_mixin.py:_adapters_for_profile which returns {}
        for unknown secondary profiles.
        """
        from talk_discord import _resolve_discord_adapter_for_profile

        default_discord = object()
        adapters = self._make_adapter_map(default_discord)
        # Multiplexed: primary is "default", "GhostProfile" is not in _profile_adapters
        profile_adapters = {"WorkProfile": self._make_adapter_map(object())}
        runner = self._make_runner(
            adapters=adapters,
            profile_adapters=profile_adapters,
            primary_name="default",
        )

        with patch.object(builtins, "__import__", side_effect=self._import_mock):
            result = _resolve_discord_adapter_for_profile(runner, "GhostProfile")
            # Should return None (mapped to empty dict → no Discord adapter found)
            # The generic "adapter isn't loaded on this gateway" from
            # resolve_voice_bridge covers this — never borrows default bot.
            assert result is None

    def test_secondary_profile_map_no_discord_raises(self):
        """When _profile_adapters[profile] exists but has no Discord bot,
        raises rather than falling back to another profile."""
        from talk_discord import _resolve_discord_adapter_for_profile

        adapters = self._make_adapter_map(object())
        slack_key = _DiscordKey("SLACK", "SLACK")
        profile_map = {slack_key: object()}
        profile_adapters = {"WorkProfile": profile_map}
        runner = self._make_runner(adapters=adapters, profile_adapters=profile_adapters)

        with patch.object(builtins, "__import__", side_effect=self._import_mock):
            with pytest.raises(Exception) as exc_info:
                _resolve_discord_adapter_for_profile(runner, "WorkProfile")
            assert "Discord adapter is not loaded" in str(exc_info.value)
            assert "WorkProfile" in str(exc_info.value)

    def test_multiple_profiles_each_have_own_bot(self):
        """Different profiles have different Discord bots."""
        from talk_discord import _resolve_discord_adapter_for_profile

        adapters = self._make_adapter_map(object())
        profile_adapters = {
            "WorkProfile": self._make_adapter_map(object()),
            "HomeProfile": self._make_adapter_map(object()),
        }
        runner = self._make_runner(adapters=adapters, profile_adapters=profile_adapters)
        work_bot = profile_adapters["WorkProfile"][_DiscordKey("DISCORD", "DISCORD")]
        home_bot = profile_adapters["HomeProfile"][_DiscordKey("DISCORD", "DISCORD")]
        default_bot = adapters[_DiscordKey("DISCORD", "DISCORD")]

        with patch.object(builtins, "__import__", side_effect=self._import_mock):
            assert _resolve_discord_adapter_for_profile(runner, "WorkProfile") is work_bot
            assert _resolve_discord_adapter_for_profile(runner, "HomeProfile") is home_bot
            assert _resolve_discord_adapter_for_profile(runner, None) is default_bot
            assert _resolve_discord_adapter_for_profile(runner, "default") is default_bot



# ---------------------------------------------------------------------------
# _resolve_current_profile  (session context resolution)
# ---------------------------------------------------------------------------


class TestResolveCurrentProfile:
    """Verify _resolve_current_profile reads HERMES_SESSION_PROFILE correctly."""

    def test_standalone_returns_none(self, monkeypatch):
        """When gateway.session_context is not importable, returns None."""
        monkeypatch.setitem(sys.modules, "gateway", None)
        monkeypatch.setitem(sys.modules, "gateway.session_context", None)
        assert talk_discord._resolve_current_profile() is None

    def test_multiplex_returns_profile_name(self, multiplexed):
        """When gateway.session_context is available, reads from the session env."""
        assert _as_profile("work", {}, talk_discord._resolve_current_profile) == "work"
        assert _as_profile("home", {}, talk_discord._resolve_current_profile) == "home"
        # No turn bound in this context: the legacy runner.adapters path.
        assert talk_discord._resolve_current_profile() is None


# ---------------------------------------------------------------------------
# UnscopedSecretError propagation
# ---------------------------------------------------------------------------


class TestUnscopedSecretErrorPropagation:
    """Verify _talk_env_get does NOT suppress UnscopedSecretError from
    Hermes's agent.secret_scope.get_secret().

    The real get_secret raises UnscopedSecretError when multiplexing is active
    and no profile scope is installed. Our wrapper must let this exception
    propagate, not swallow it or fall back to the process environment.
    """

    def test_unscoped_secret_error_propagates(self, multiplexed):
        with pytest.raises(UnscopedSecretError, match="TALK_TEST_SECRET"):
            talk_config._talk_env_get("TALK_TEST_SECRET")
        with pytest.raises(UnscopedSecretError, match="TALK_OPENAI_API_KEY"):
            talk_config.resolve_openai_key()

    def test_module_load_binds_the_hosts_get_secret(self, monkeypatch):
        """With agent.secret_scope importable, a fresh talk_config binds it."""
        import importlib.util

        agent = types.ModuleType("agent")
        secret_scope = types.ModuleType("agent.secret_scope")
        secret_scope.get_secret = _multiplexed_get_secret
        agent.secret_scope = secret_scope
        monkeypatch.setitem(sys.modules, "agent", agent)
        monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)
        # A separate module object: the shared talk_config stays untouched.
        spec = importlib.util.spec_from_file_location(
            "talk_config_scope_probe", talk_config.__file__
        )
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        assert fresh._hermes_get_secret is _multiplexed_get_secret


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_real_import = __import__
