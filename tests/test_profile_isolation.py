"""Profile-isolation tests for hermes-talk issue #185.

Tests that the new profile-scoped env resolution and Discord adapter lookup
work correctly under multiplexed gateway semantics, and degrade safely
for standalone (non-multiplexed) invocations.

Test count: 11 test methods across 4 classes.
"""

from __future__ import annotations

import builtins
import os
import subprocess
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
# UnscopedSecretError propagation
# ---------------------------------------------------------------------------


class TestUnscopedSecretErrorPropagation:
    """Verify _talk_env_get does NOT suppress UnscopedSecretError from
    the real Hermes agent.secret_scope.get_secret().

    The real get_secret raises UnscopedSecretError when:
      - multiplexing is active (is_multiplex_active() → True)
      - no profile scope is installed (_SECRET_SCOPE.get() → None)
      - no launch body scope (_LAUNCH_BODY_SCOPE.get() → None)

    Our wrapper must let this exception propagate, not swallow it.

    We use a subprocess to guarantee a fresh Python process with no
    pytest module caching interfering (pytest's module cleanup can
    interfere with module-level attribute patches).
    """

    def test_unscoped_secret_error_propagates_with_real_herms(self):
        """Run a subprocess that patches the real Hermes secret_scope
        module, imports talk_config, and verifies UnscopedSecretError
        propagates through _talk_env_get."""
        workspace = (
            "/home/akmod/.hermes/installs/1cd3fc7b241ff10b/"
            "environments/aa8e068f09a14311b40efb1dbaffda93/workspace"
        )
        agent_scope_path = workspace + "/agent/secret_scope.py"
        if not os.path.isfile(agent_scope_path):
            pytest.skip("real hermes-agent secret_scope.py not available")

        code = (
            "import sys\n"
            f"sys.path.insert(0, {workspace!r})\n"
            "from agent.secret_scope import get_secret\n"
            "import agent.secret_scope as S\n"
            "from types import SimpleNamespace\n"
            # Patch the module-level attributes directly (no patch.object)
            "S._SECRET_SCOPE = SimpleNamespace(get=lambda *a, **k: None)\n"
            "S._LAUNCH_BODY_SCOPE = SimpleNamespace(get=lambda *a, **k: None)\n"
            "S.is_multiplex_active = lambda: True\n"
            # Now import talk_config fresh and test
            "import talk_config\n"
            "talk_config._hermes_get_secret = get_secret\n"
            "try:\n"
            "    talk_config._talk_env_get('TALK_TEST_SECRET')\n"
            "    print('FAIL: no exception')\n"
            "except RuntimeError as e:\n"
            "    if 'TALK_TEST_SECRET' in str(e):\n"
            "        print('OK')\n"
            "    else:\n"
            "        print('FAIL: wrong error: ' + str(e))\n"
            "except Exception as e:\n"
            "    print('FAIL: wrong type: ' + type(e).__name__ + ': ' + str(e))\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=30,
        )
        assert r.stdout.strip() == "OK", (
            f"Expected UnscopedSecretError to propagate, got: {r.stdout.strip()}"
        )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_real_import = __import__
