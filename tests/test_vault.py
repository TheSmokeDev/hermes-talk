"""Vault recall — the lookup a voice session can actually make.

Hermetic like the identity suite: ``plugins.memory`` is injected as a fake, so
the real resolution code runs without hermes-agent installed and without ever
touching the operator's vault.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

# Flat module import: the suite runs with `tests/` on sys.path, matching the
# flat-module layout the plugin itself uses.
from test_identity_injection import StubProvider, _Ctx, _install_provider

import talk_host
import talk_vault


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    talk_host.bind_ctx(None)
    monkeypatch.setitem(sys.modules, "plugins", None)
    monkeypatch.setitem(sys.modules, "plugins.memory", None)
    talk_vault.reset()
    yield
    talk_vault.reset()
    talk_host.bind_ctx(None)


# --- availability -------------------------------------------------------------


def test_no_hermes_means_no_vault():
    assert talk_vault.available() is False
    assert talk_vault.document_count() == 0
    # NOT "" — that string means "nothing is written down about that", which
    # we do not know. We know we could not look.
    with pytest.raises(talk_vault.VaultSearchError):
        talk_vault.search("anything")


def test_an_unavailable_provider_is_never_initialized(monkeypatch):
    provider = _install_provider(monkeypatch, StubProvider(available=False))

    assert talk_vault.available() is False
    assert "initialize" not in provider.calls


def test_a_provider_that_raises_is_simply_absent(monkeypatch):
    _install_provider(monkeypatch, StubProvider(raises=True))

    assert talk_vault.available() is False


def test_no_configured_provider_is_absent(monkeypatch):
    _install_provider(monkeypatch, StubProvider("hits"), active="")

    assert talk_vault.available() is False


# --- the cache ----------------------------------------------------------------


def test_the_provider_is_resolved_once_not_per_lookup(monkeypatch):
    """A rebuild is a full vault walk (~0.3s measured) on the loop carrying
    the microphone, so a per-lookup rebuild would be audible."""

    provider = _install_provider(monkeypatch, StubProvider("hits"))

    for _ in range(5):
        talk_vault.search("anything")

    assert provider.calls.count("initialize") == 1


def test_a_failed_load_is_not_retried_per_lookup(monkeypatch):
    """The failure path includes the filesystem walk too — retrying it on
    every lookup is the same stall as rebuilding on every lookup."""

    calls: list[str] = []

    def counting_active():
        calls.append("probe")
        return ""

    memory = types.ModuleType("plugins.memory")
    memory._get_active_memory_provider = counting_active
    memory.load_memory_provider = lambda name: None
    plugins_pkg = types.ModuleType("plugins")
    plugins_pkg.memory = memory
    monkeypatch.setitem(sys.modules, "plugins", plugins_pkg)
    monkeypatch.setitem(sys.modules, "plugins.memory", memory)

    for _ in range(5):
        talk_vault.available()

    assert len(calls) == 1


def test_concurrent_first_lookups_keep_exactly_one_provider(monkeypatch):
    """Two tool calls can land together (a retry, or the dashboard and the
    gateway in one process). Exactly one provider is KEPT, and every loser is
    closed rather than leaked.

    Note what this deliberately does NOT promise: that only one is BUILT.
    Resolution runs outside the lock so that a provider's ``initialize`` —
    arbitrary third-party code — can never deadlock the call by reaching back
    into this module. A cold-cache race can therefore build more than one; the
    contract is that at most one survives.
    """

    made: list[StubProvider] = []

    def factory(_name):
        made.append(StubProvider("hits"))
        return made[-1]

    memory = types.ModuleType("plugins.memory")
    memory._get_active_memory_provider = lambda: "stub"
    memory.load_memory_provider = factory
    plugins_pkg = types.ModuleType("plugins")
    plugins_pkg.memory = memory
    monkeypatch.setitem(sys.modules, "plugins", plugins_pkg)
    monkeypatch.setitem(sys.modules, "plugins.memory", memory)

    start = threading.Barrier(4)

    def worker():
        start.wait(5)
        talk_vault.available()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    kept = talk_vault.provider()
    assert kept in made
    for extra in made:
        if extra is not kept:
            assert "shutdown" in extra.calls, "a losing provider was leaked"
    assert "shutdown" not in kept.calls


# --- ownership ----------------------------------------------------------------


def test_a_provider_we_loaded_is_shut_down_on_reset(monkeypatch):
    provider = _install_provider(monkeypatch, StubProvider("hits"))
    talk_vault.provider()

    talk_vault.reset()

    assert provider.calls[-1] == "shutdown"


def test_a_borrowed_provider_survives_reset(monkeypatch):
    """The sharpest edge in the borrow optimization: this instance belongs to
    a running agent, and shutting it down would take that agent's memory with
    it — a plugin teardown silently breaking the host."""

    live = StubProvider("hits")
    live.initialize("agent-session")
    live.calls.clear()
    talk_host.bind_ctx(_Ctx(provider=live))
    assert talk_vault.provider() is live

    talk_vault.reset()

    assert "shutdown" not in live.calls


def test_a_shutdown_that_raises_still_clears_the_cache(monkeypatch):
    provider = _install_provider(monkeypatch, StubProvider("hits"))
    talk_vault.provider()
    provider._raises = True

    talk_vault.reset()

    # The next call resolves fresh rather than serving a provider we already
    # decided to drop.
    provider._raises = False
    assert talk_vault.provider() is provider
    assert provider.calls.count("initialize") == 2


# --- search -------------------------------------------------------------------


def test_search_returns_what_the_provider_found(monkeypatch):
    _install_provider(monkeypatch, StubProvider("## Recall\nthe offer ladder is locked"))

    assert "the offer ladder is locked" in talk_vault.search("offer ladder")


def test_search_is_bounded(monkeypatch):
    _install_provider(monkeypatch, StubProvider("x" * 99_999))

    assert len(talk_vault.search("anything")) == talk_vault.MAX_VAULT_CHARS


def test_an_empty_query_never_reaches_the_provider(monkeypatch):
    provider = _install_provider(monkeypatch, StubProvider("hits"))

    assert talk_vault.search("   ") == ""
    assert "prefetch" not in provider.calls


def test_nothing_found_is_empty_not_an_error(monkeypatch):
    _install_provider(monkeypatch, StubProvider(""))

    assert talk_vault.search("kites") == ""


def test_a_failing_lookup_raises_rather_than_reading_as_empty(monkeypatch):
    """"Nothing written down" and "the lookup broke" must stay
    distinguishable all the way up — collapsing them would have the session
    confidently tell the operator they never wrote something down."""

    class Broken(StubProvider):
        def prefetch(self, query, *, session_id=""):
            raise OSError("index file vanished")

    _install_provider(monkeypatch, Broken("hits"))

    with pytest.raises(talk_vault.VaultSearchError, match="OSError"):
        talk_vault.search("anything")


def test_the_lookup_never_claims_a_primary_session(monkeypatch):
    provider = _install_provider(monkeypatch, StubProvider("hits"))

    talk_vault.search("anything")

    assert provider.init_kwargs["agent_context"] != "primary"
    assert provider.init_kwargs["session_id"] == talk_vault.VAULT_SESSION_ID


def test_document_count_survives_a_broken_status(monkeypatch):
    class NoStatus(StubProvider):
        def initialize(self, session_id, **kwargs):
            super().initialize(session_id, **kwargs)
            self._index = types.SimpleNamespace(status=lambda: 1 / 0)

    _install_provider(monkeypatch, NoStatus("hits"))

    assert talk_vault.document_count() == 0
    assert talk_vault.available() is True


# --- warm before the call ------------------------------------------------------


def test_the_index_is_built_during_session_setup_not_mid_call(monkeypatch):
    """The first resolve costs a full vault walk (~0.9s measured live). On the
    loop carrying the microphone that is audible dead air, so it MUST be paid
    while the session is being minted, not on the operator's first question.

    Both mint-time paths warm it: identity_sections builds the pointer, and
    default_talk_tools asks whether to advertise the tool.
    """

    provider = _install_provider(monkeypatch, StubProvider("hits"))

    talk_host.host().identity_sections()

    assert "initialize" in provider.calls  # warm already, before any tool call
    provider.calls.clear()
    talk_vault.search("anything")
    assert "initialize" not in provider.calls  # the lookup pays only the search


def test_an_include_filter_cannot_defer_the_warm_into_the_call(monkeypatch):
    """TALK_IDENTITY_INCLUDE drops the SECTION, and the filter runs after the
    sections are built — so excluding MEMORY must not push the index build
    into the live call instead."""

    provider = _install_provider(monkeypatch, StubProvider("hits"))
    monkeypatch.setenv("TALK_IDENTITY_INCLUDE", "PERSONA")

    talk_host.host().identity_sections()

    assert "initialize" in provider.calls


# --- ownership on the failure paths -------------------------------------------


def test_a_provider_that_fails_is_available_is_closed_not_leaked(monkeypatch):
    """It was CONSTRUCTED, so it is ours. Abandoning it without closing leaks
    it past every reset(), because the cache only remembers what it kept."""

    provider = _install_provider(monkeypatch, StubProvider(available=False))

    assert talk_vault.available() is False
    assert "shutdown" in provider.calls


def test_a_provider_that_throws_in_initialize_is_closed_not_leaked(monkeypatch):
    class BadInit(StubProvider):
        def initialize(self, session_id, **kwargs):
            self.calls.append("initialize")
            raise RuntimeError("initialize exploded")

    provider = _install_provider(monkeypatch, BadInit())

    assert talk_vault.available() is False
    assert "shutdown" in provider.calls


def test_a_provider_with_no_index_is_closed_not_leaked(monkeypatch):
    class NoIndex(StubProvider):
        def initialize(self, session_id, **kwargs):
            self.calls.append("initialize")

    provider = _install_provider(monkeypatch, NoIndex())

    assert talk_vault.available() is False
    assert "shutdown" in provider.calls


def test_resolution_does_not_hold_the_lock(monkeypatch):
    """`initialize` is arbitrary third-party code. Under a non-reentrant lock,
    a provider that reached back into this module would DEADLOCK the voice
    call rather than degrade — so resolution runs outside it."""

    seen = []

    class Reentrant(StubProvider):
        def initialize(self, session_id, **kwargs):
            # The hazard, exercised directly: a provider reaching back into
            # this module from inside its own initialize. Guarded to fire once
            # so the test measures the LOCK, not stub recursion.
            if not seen:
                seen.append("reentered")
                talk_vault.provider()
            super().initialize(session_id, **kwargs)

    _install_provider(monkeypatch, Reentrant("hits"))

    # The assertion is that this RETURNS AT ALL. Under a held non-reentrant
    # lock the re-entrant call blocks forever and the voice call goes silent
    # with no error — the worst shape of failure this plugin has.
    assert talk_vault.available() is True
    assert seen == ["reentered"]


def test_teardown_does_not_hold_the_lock(monkeypatch):
    """Round-2 finding: moving the LOAD outside the lock is not enough.
    ``shutdown()`` is third-party code too, so releasing a losing racer's
    provider under the lock re-opens the same deadlock the split closed.
    """

    observed: list[bool] = []

    class Watching(StubProvider):
        def shutdown(self):
            observed.append(talk_vault._LOCK.locked())
            super().shutdown()

    made: list[Watching] = []
    arrived = threading.Barrier(2)

    def factory(_name):
        item = Watching("hits")
        made.append(item)
        arrived.wait(timeout=5)  # force both racers past the cold check
        return item

    memory = types.ModuleType("plugins.memory")
    memory._get_active_memory_provider = lambda: "stub"
    memory.load_memory_provider = factory
    plugins_pkg = types.ModuleType("plugins")
    plugins_pkg.memory = memory
    monkeypatch.setitem(sys.modules, "plugins", plugins_pkg)
    monkeypatch.setitem(sys.modules, "plugins.memory", memory)

    threads = [threading.Thread(target=talk_vault.provider) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert not any(t.is_alive() for t in threads)
    assert len(made) == 2  # the race really happened
    assert observed == [False], "the loser was torn down under the lock"
