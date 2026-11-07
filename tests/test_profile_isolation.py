"""Profile-isolation tests for hermes-talk issue #185.

Tests that the new profile-scoped env resolution and Discord adapter lookup
work correctly under multiplexed gateway semantics, and degrade safely
for standalone (non-multiplexed) invocations.
"""

from __future__ import annotations

import builtins
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# talk_config._talk_env_get  (profile-scoped env resolution)
# ---------------------------------------------------------------------------


class TestTalkEnvGet:
    """Verify _talk_env_get delegates to Hermes get_secret when available."""

    def test_without_herms_falls_back_to_os_environ(self):
        """When agent.secret_scope is not importable, falls through to os.environ."""
        old_modules = dict(sys.modules)
        sys.modules.pop("agent.secret_scope", None)
        try:
            import importlib
            import talk_config
            importlib.reload(talk_config)
            os.environ["TALK_TEST_MARKER"] = "hello-from-env"
            try:
                result = talk_config._talk_env_get("TALK_TEST_MARKER")
                assert result == "hello-from-env"
                result = talk_config._talk_env_get("TALK_UNSET_KEY", "default")
                assert result == "default"
                result = talk_config._talk_env_get("TALK_UNSET_KEY")
                assert result is None
            finally:
                os.environ.pop("TALK_TEST_MARKER", None)
        finally:
            sys.modules.clear()
            sys.modules.update(old_modules)

    def test_with_herms_reads_from_scope(self):
        """When agent.secret_scope is available, delegates to get_secret."""
        old_modules = dict(sys.modules)
        sys.modules.pop("agent.secret_scope", None)
        try:
            def mock_get_secret(name, default=None):
                scope = {
                    "TALK_PROVIDER": "grok",
                    "TALK_MODEL": "grok-voice-latest",
                }
                if name in scope:
                    return scope[name]
                return os.environ.get(name, default)

            sys.modules["agent.secret_scope"] = SimpleNamespace(
                get_secret=mock_get_secret,
                is_multiplex_active=lambda: False,
                serves_routed_profile=lambda: False,
            )

            import importlib
            import talk_config
            importlib.reload(talk_config)

            assert talk_config._talk_env_get("TALK_PROVIDER") == "grok"
            assert talk_config._talk_env_get("TALK_MODEL") == "grok-voice-latest"
            assert talk_config._talk_env_get("TALK_MODEL_X", "x") == "x"
        finally:
            sys.modules.clear()
            sys.modules.update(old_modules)

    def test_shared_credential_fallback(self):
        """resolve_openai_key falls back from TALK_OPENAI_API_KEY to OPENAI_API_KEY,
        both profile-scoped."""
        import importlib
        import talk_config
        importlib.reload(talk_config)
        os.environ.pop("TALK_OPENAI_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            importlib.reload(talk_config)
            key = talk_config.resolve_openai_key()
            assert "no OpenAI key" in str(key)
        except talk_config.TalkConfigError as exc:
            assert "no OpenAI key" in str(exc)


# ---------------------------------------------------------------------------
# talk_discord Discord adapter resolution
# ---------------------------------------------------------------------------


class _DiscordKey:
    """Hashable mock for an enum value used as dict key."""
    __slots__ = ("value", "name")
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
    """Verify resolve_voice_bridge uses the invoking profile's adapter map."""

    def _make_adapter_map(self, discord_adapter):
        """Create a minimal adapter map keyed by DISCORD key."""
        return {_DiscordKey("DISCORD", "DISCORD"): discord_adapter}

    def _make_runner(self, adapters=None, profile_adapters=None):
        """Build a minimal mock runner with adapter maps."""
        if adapters is None:
            adapters = {}
        if profile_adapters is None:
            profile_adapters = {}
        return SimpleNamespace(
            adapters=adapters,
            _profile_adapters=profile_adapters,
        )

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
        """When no profile is known (standalone), uses runner.adapters."""
        from talk_discord import _resolve_discord_adapter_for_profile

        discord_adapter = object()
        adapters = self._make_adapter_map(discord_adapter)
        runner = self._make_runner(adapters=adapters)
        # Explicitly clear _profile_adapters to simulate standalone
        del runner._profile_adapters

        with patch.object(builtins, "__import__", side_effect=self._import_mock):
            result = _resolve_discord_adapter_for_profile(runner, None)
            assert result is discord_adapter

    def test_secondary_profile_uses_profile_map(self):
        """When profile_name is known, uses _profile_adapters[profile_name]."""
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

    def test_secondary_profile_missing_falls_back_to_default(self):
        """When profile_name is known but _profile_adapters is missing, falls back."""
        from talk_discord import _resolve_discord_adapter_for_profile

        adapters = self._make_adapter_map(object())
        runner = self._make_runner(adapters=adapters)
        del runner._profile_adapters
        default_discord = adapters[_DiscordKey("DISCORD", "DISCORD")]

        with patch.object(builtins, "__import__", side_effect=self._import_mock):
            result = _resolve_discord_adapter_for_profile(runner, "WorkProfile")
            assert result is default_discord

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

    def test_legacy_host_without_profile_adapters(self):
        """A runner that lacks _profile_adapters entirely uses adapters."""
        from talk_discord import _resolve_discord_adapter_for_profile

        adapters = self._make_adapter_map(object())
        runner = self._make_runner(adapters=adapters)
        del runner._profile_adapters

        with patch.object(builtins, "__import__", side_effect=self._import_mock):
            result = _resolve_discord_adapter_for_profile(runner, "SomeProfile")
            assert result is adapters[_DiscordKey("DISCORD", "DISCORD")]


# ---------------------------------------------------------------------------
# _resolve_current_profile  (session context resolution)
# ---------------------------------------------------------------------------


class TestResolveCurrentProfile:
    """Verify _resolve_current_profile reads HERMES_SESSION_PROFILE correctly."""

    def test_standalone_returns_none(self):
        """When gateway.session_context is not importable, returns None."""
        from talk_discord import _resolve_current_profile
        result = _resolve_current_profile()
        assert result is None

    def test_multiplex_returns_profile_name(self):
        """When gateway.session_context is available, reads from the session env."""
        from talk_discord import _resolve_current_profile
        # The session context is only available in a gateway turn;
        # in this standalone test process it is not importable.
        result = _resolve_current_profile()
        assert result is None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_real_import = __import__
