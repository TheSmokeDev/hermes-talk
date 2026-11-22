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
import threading
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import talk_audio
import talk_call_journal
import talk_config
import talk_discord
import talk_doctor
import talk_host

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
# Two profiles, one gateway
# ---------------------------------------------------------------------------

_PROFILE_A = {
    "TALK_VOICE": "marin",
    "TALK_PROVIDER": "openai",
    "OPENAI_API_KEY": "key-a",
    "TALK_REPLY_LANGUAGE": "French",
    "TALK_API_SERVER_PROBE_TIMEOUT_S": "4",
    "TALK_ECHO_GATE_RATIO": "0.5",
    "TALK_CALL_JOURNAL": "1",
    "TALK_DISCORD_OPERATOR_USER_IDS": "111",
}
_PROFILE_B = {
    "TALK_VOICE": "verse",
    "OPENAI_API_KEY": "key-b",
    "TALK_DISCORD_OPERATOR_USER_IDS": "222",
}


def _settings():
    return {
        "voice": talk_config.talk_voice(),
        "provider": talk_config.talk_provider(),
        "openai_key": talk_config.resolve_openai_key(),
        "reply_language": talk_config.reply_language(),
        "probe_timeout": talk_config.api_server_probe_timeout_s(),
        "echo_ratio": talk_audio._env_float("TALK_ECHO_GATE_RATIO", 0.65),
        "journal": talk_call_journal.enabled(),
        "operators": talk_config.discord_operator_user_ids(),
    }


class TestTwoProfilesOneGateway:
    """Two profiles served by one process never see each other's settings,
    and neither sees the process environment's values."""

    def test_each_profile_reads_only_its_own_settings(self, multiplexed, monkeypatch):
        monkeypatch.setenv("TALK_ECHO_GATE_RATIO", "0.9")
        monkeypatch.setenv("TALK_CALL_JOURNAL", "0")
        monkeypatch.setenv("TALK_DISCORD_OPERATOR_USER_IDS", "999")
        assert _as_profile("a", _PROFILE_A, _settings) == {
            "voice": "marin",
            "provider": "openai",
            "openai_key": "key-a",
            "reply_language": "French",
            "probe_timeout": 4.0,
            "echo_ratio": 0.5,
            "journal": True,
            "operators": frozenset({111}),
        }
        # B leaves most knobs unset: it gets the shipped defaults, never A's
        # values and never the process environment's.
        assert _as_profile("b", _PROFILE_B, _settings) == {
            "voice": "verse",
            "provider": talk_config.DEFAULT_TALK_PROVIDER,
            "openai_key": "key-b",
            "reply_language": None,
            "probe_timeout": talk_config.DEFAULT_API_SERVER_PROBE_TIMEOUT_S,
            "echo_ratio": 0.65,
            "journal": False,
            "operators": frozenset({222}),
        }

    def test_doctor_reports_the_profiles_shared_key_not_the_process_one(
        self, multiplexed, monkeypatch,
    ):
        monkeypatch.setenv("GEMINI_API_KEY", "process-gemini")

        def gemini_auth():
            return talk_doctor._gemini_auth_check()

        with_key = _as_profile("a", {"GEMINI_API_KEY": "gemini-a"}, gemini_auth)
        without = _as_profile("b", {}, gemini_auth)
        assert with_key["details"]["source"] == "GEMINI_API_KEY"
        assert without["details"]["source"] is None
        assert without["details"]["keys"]["shared"] == "absent"

    def test_concurrent_turns_do_not_cross(self, multiplexed):
        barrier = threading.Barrier(2, timeout=5)
        seen: dict[str, tuple] = {}

        def turn(profile, secrets):
            def read():
                barrier.wait()  # both turns are bound before either reads
                return talk_config.talk_voice(), talk_config.resolve_openai_key()

            seen[profile] = _as_profile(profile, secrets, read)

        workers = [
            threading.Thread(target=turn, args=("a", _PROFILE_A)),
            threading.Thread(target=turn, args=("b", _PROFILE_B)),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
        assert seen == {"a": ("marin", "key-a"), "b": ("verse", "key-b")}

    def test_background_worker_keeps_the_dispatching_profile(self, multiplexed):
        """A worker dispatched in A's turn reads A's settings even after the
        dispatching context has moved on to B (#216 composed with #220)."""
        go = threading.Event()
        done = threading.Event()
        seen: dict = {}

        def worker():
            assert go.wait(5)
            try:
                seen["read"] = (talk_config.talk_voice(), talk_config.resolve_openai_key())
            except Exception as exc:  # noqa: BLE001 — recorded for the assertion
                seen["read"] = exc
            done.set()

        def dispatch_then_switch():
            talk_host._spawn_daemon(worker, name="talk-profile-probe")
            _SECRET_SCOPE.set(_PROFILE_B)
            _SESSION_PROFILE.set("b")
            go.set()
            assert done.wait(5)

        _as_profile("a", _PROFILE_A, dispatch_then_switch)
        assert seen["read"] == ("marin", "key-a")

    def test_a_worker_without_the_turns_context_fails_closed(self, multiplexed):
        """A plain thread loses the scope: the read refuses rather than falling
        back to the process environment's key."""
        seen: dict = {}

        def worker():
            try:
                seen["read"] = talk_config.resolve_openai_key()
            except Exception as exc:  # noqa: BLE001 — recorded for the assertion
                seen["read"] = exc

        def dispatch_plain():
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(5)

        _as_profile("a", _PROFILE_A, dispatch_plain)
        assert isinstance(seen["read"], UnscopedSecretError)

    def test_each_profile_speaks_through_its_own_discord_bot(self, multiplexed, monkeypatch):
        def adapter(guild):
            return SimpleNamespace(_voice_clients={guild: object()}, _voice_receivers={})

        default_bot, bot_a, bot_b = adapter(1), adapter(2), adapter(3)
        runner = SimpleNamespace(
            adapters={"discord": default_bot},
            _profile_adapters={"a": {"discord": bot_a}, "b": {"discord": bot_b}},
            _primary_profile_name="default",
        )
        run_module = types.ModuleType("gateway.run")
        run_module._gateway_runner_ref = lambda: runner
        multiplexed.run = run_module
        monkeypatch.setitem(sys.modules, "gateway.run", run_module)
        for name in ("gateway.config", "models", "gateway.platform_registry"):
            monkeypatch.setitem(sys.modules, name, None)

        assert _as_profile("a", _PROFILE_A, talk_discord.resolve_voice_bridge)["adapter"] is bot_a
        assert _as_profile("b", _PROFILE_B, talk_discord.resolve_voice_bridge)["adapter"] is bot_b
        assert talk_discord.resolve_voice_bridge()["adapter"] is default_bot

    def test_a_profile_without_a_bot_never_borrows_another(self, multiplexed, monkeypatch):
        bot_a = SimpleNamespace(_voice_clients={2: object()}, _voice_receivers={})
        runner = SimpleNamespace(
            adapters={"discord": SimpleNamespace(_voice_clients={1: object()},
                                                 _voice_receivers={})},
            _profile_adapters={"a": {"discord": bot_a}, "b": {}},
            _primary_profile_name="default",
        )
        run_module = types.ModuleType("gateway.run")
        run_module._gateway_runner_ref = lambda: runner
        multiplexed.run = run_module
        monkeypatch.setitem(sys.modules, "gateway.run", run_module)
        for name in ("gateway.config", "models", "gateway.platform_registry"):
            monkeypatch.setitem(sys.modules, name, None)

        with pytest.raises(talk_discord.TalkDiscordError, match="profile 'b'"):
            _as_profile("b", _PROFILE_B, talk_discord.resolve_voice_bridge)
        with pytest.raises(talk_discord.TalkDiscordError, match="isn't loaded"):
            _as_profile("ghost", {}, talk_discord.resolve_voice_bridge)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_real_import = __import__
