"""Identity injection — what a voice session already knows before you speak.

Fully hermetic: the Hermes modules this reads through (``agent.prompt_builder``
and ``plugins.memory``) are INJECTED as fakes into ``sys.modules``, so the
suite exercises the real resolution code without hermes-agent installed and
without ever touching the operator's SOUL.md or memory store. The autouse
fixture also blocks the real modules, so a box that HAS hermes-agent on the
path still runs the same test.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

import talk_host
import talk_identity
import talk_tools

_BLOCKED = ("agent", "agent.prompt_builder", "plugins", "plugins.memory")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    talk_host.bind_ctx(None)
    monkeypatch.delenv("TALK_IDENTITY_INCLUDE", raising=False)
    # None in sys.modules makes `import x` raise ImportError — the default
    # state for every test here is "no Hermes at all".
    for name in _BLOCKED:
        monkeypatch.setitem(sys.modules, name, None)
    yield
    talk_host.bind_ctx(None)


def _install_persona(monkeypatch, soul: str | None):
    """Inject a fake ``agent.prompt_builder.load_soul_md``."""

    prompt_builder = types.ModuleType("agent.prompt_builder")
    prompt_builder.load_soul_md = lambda context_length=None: soul
    agent_pkg = types.ModuleType("agent")
    agent_pkg.prompt_builder = prompt_builder
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", prompt_builder)


class StubProvider:
    """A MemoryProvider shaped like the real ABC, with a call log."""

    def __init__(self, block="", *, available=True, raises=False):
        self._block = block
        self._available = available
        self._raises = raises
        self.calls: list[str] = []
        self.init_kwargs: dict = {}

    @property
    def name(self):
        return "stub"

    def is_available(self):
        self.calls.append("is_available")
        if self._raises:
            raise RuntimeError("provider is broken")
        return self._available

    def initialize(self, session_id, **kwargs):
        self.calls.append("initialize")
        self.init_kwargs = {"session_id": session_id, **kwargs}
        if self._raises:
            raise RuntimeError("provider is broken")

    def system_prompt_block(self):
        self.calls.append("system_prompt_block")
        if self._raises:
            raise RuntimeError("provider is broken")
        return self._block

    def shutdown(self):
        self.calls.append("shutdown")
        if self._raises:
            raise RuntimeError("provider is broken")


def _install_provider(monkeypatch, provider, *, active="stub"):
    """Inject a fake ``plugins.memory`` exposing the two accessors we use."""

    memory = types.ModuleType("plugins.memory")
    memory._get_active_memory_provider = lambda: active
    memory.load_memory_provider = lambda name: provider
    plugins_pkg = types.ModuleType("plugins")
    plugins_pkg.memory = memory
    monkeypatch.setitem(sys.modules, "plugins", plugins_pkg)
    monkeypatch.setitem(sys.modules, "plugins.memory", memory)
    return provider


class _Ctx:
    """A plugin context whose private chain leads to a live memory manager."""

    def __init__(self, block="", *, raises=False):
        manager = types.SimpleNamespace()
        if raises:

            def boom():
                raise RuntimeError("manager exploded")

            manager.build_system_prompt = boom
        else:
            manager.build_system_prompt = lambda: block
        agent = types.SimpleNamespace(_memory_manager=manager)
        self._manager = types.SimpleNamespace(_cli_ref=types.SimpleNamespace(agent=agent))


# --- nothing available -------------------------------------------------------


def test_no_hermes_at_all_yields_no_sections():
    assert talk_host.host().identity_sections() == {}


def test_no_active_provider_yields_no_memory(monkeypatch):
    _install_provider(monkeypatch, StubProvider("ignored"), active="")

    assert talk_host.host().identity_sections() == {}


def test_unavailable_provider_is_never_initialized(monkeypatch):
    provider = _install_provider(monkeypatch, StubProvider("ignored", available=False))

    assert talk_host.host().identity_sections() == {}
    assert "initialize" not in provider.calls


def test_provider_that_raises_on_everything_costs_only_its_section(monkeypatch):
    _install_persona(monkeypatch, "I am the operator's soul.")
    _install_provider(monkeypatch, StubProvider(raises=True))

    sections = talk_host.host().identity_sections()

    # The session still starts, and the section that DID resolve survives.
    assert "MEMORY" not in sections
    assert sections["PERSONA"] == "I am the operator's soul."


def test_empty_block_is_not_a_section(monkeypatch):
    _install_provider(monkeypatch, StubProvider("   \n  "))

    assert talk_host.host().identity_sections() == {}


# --- the provider path -------------------------------------------------------


def test_provider_block_becomes_the_memory_section(monkeypatch):
    provider = _install_provider(monkeypatch, StubProvider("# Memory\nIndexed: 412 docs."))

    sections = talk_host.host().identity_sections()

    assert sections == {"MEMORY": "# Memory\nIndexed: 412 docs."}
    # initialize before the block: a real provider returns "" until it has one.
    assert provider.calls == [
        "is_available",
        "initialize",
        "system_prompt_block",
        "shutdown",
    ]


def test_a_provider_we_initialize_is_always_shut_down(monkeypatch):
    class HalfBroken(StubProvider):
        def system_prompt_block(self):
            self.calls.append("system_prompt_block")
            raise RuntimeError("block failed")

    provider = _install_provider(monkeypatch, HalfBroken())

    assert talk_host.host().identity_sections() == {}
    # We own this instance; failing to read it does not excuse leaking it.
    assert provider.calls[-1] == "shutdown"


def test_probe_never_claims_to_be_a_primary_session(monkeypatch):
    provider = _install_provider(monkeypatch, StubProvider("block"))

    talk_host.host().identity_sections()

    # Non-primary context => the ABC's contract is that providers skip writes.
    assert provider.init_kwargs["agent_context"] != "primary"
    assert provider.init_kwargs["session_id"] == talk_host.PROBE_SESSION_ID
    assert "hermes_home" in provider.init_kwargs


# --- the live-agent path -----------------------------------------------------


def test_the_live_agents_block_is_preferred_over_loading_a_provider(monkeypatch):
    provider = _install_provider(monkeypatch, StubProvider("from a fresh provider"))
    talk_host.bind_ctx(_Ctx("from the live agent"))

    sections = talk_host.host().identity_sections()

    assert sections == {"MEMORY": "from the live agent"}
    # Already initialized in-process — nothing was loaded or torn down.
    assert provider.calls == []


def test_a_ctx_without_an_agent_falls_through_to_the_provider(monkeypatch):
    _install_provider(monkeypatch, StubProvider("from a fresh provider"))
    talk_host.bind_ctx(types.SimpleNamespace())  # no _manager at all

    assert talk_host.host().identity_sections() == {"MEMORY": "from a fresh provider"}


def test_a_raising_agent_manager_falls_through_to_the_provider(monkeypatch):
    _install_provider(monkeypatch, StubProvider("from a fresh provider"))
    talk_host.bind_ctx(_Ctx(raises=True))

    assert talk_host.host().identity_sections() == {"MEMORY": "from a fresh provider"}


# --- persona -----------------------------------------------------------------


def test_persona_comes_from_the_hermes_loader(monkeypatch):
    _install_persona(monkeypatch, "  You are talking to an operator who ships.  ")

    sections = talk_host.host().identity_sections()

    assert sections == {"PERSONA": "You are talking to an operator who ships."}


def test_absent_soul_is_simply_no_persona(monkeypatch):
    _install_persona(monkeypatch, None)

    assert talk_host.host().identity_sections() == {}


# --- TALK_IDENTITY_INCLUDE ---------------------------------------------------


def _both(monkeypatch):
    _install_persona(monkeypatch, "soul text")
    _install_provider(monkeypatch, StubProvider("memory text"))


def test_default_carries_every_resolved_section(monkeypatch):
    _both(monkeypatch)

    assert set(talk_host.host().identity_sections()) == {"PERSONA", "MEMORY"}


def test_include_filters_and_is_case_insensitive(monkeypatch):
    _both(monkeypatch)
    monkeypatch.setenv("TALK_IDENTITY_INCLUDE", " memory ")

    # The trap: the list REPLACES the default. Asking for memory silently
    # stops the persona travelling.
    assert set(talk_host.host().identity_sections()) == {"MEMORY"}


def test_unknown_names_are_dropped_not_fatal(monkeypatch):
    _both(monkeypatch)
    monkeypatch.setenv("TALK_IDENTITY_INCLUDE", "PERSONA,TELEPATHY")

    assert set(talk_host.host().identity_sections()) == {"PERSONA"}


def test_only_unknown_names_yields_nothing_rather_than_raising(monkeypatch):
    _both(monkeypatch)
    monkeypatch.setenv("TALK_IDENTITY_INCLUDE", "TELEPATHY")

    assert talk_host.host().identity_sections() == {}


def test_a_blank_value_means_the_default(monkeypatch):
    _both(monkeypatch)
    monkeypatch.setenv("TALK_IDENTITY_INCLUDE", "   ")

    assert set(talk_host.host().identity_sections()) == {"PERSONA", "MEMORY"}


# --- caps --------------------------------------------------------------------


@pytest.mark.parametrize("name", ["PERSONA", "MEMORY"])
def test_cap_is_exact_at_the_boundary(name):
    cap = talk_identity.IDENTITY_CAPS[name]

    assert len(talk_identity.cap_section(name, "x" * (cap - 1))) == cap - 1
    assert len(talk_identity.cap_section(name, "x" * cap)) == cap
    assert len(talk_identity.cap_section(name, "x" * (cap + 1))) == cap


def test_an_oversized_provider_block_is_capped_in_the_prompt(monkeypatch):
    _install_provider(monkeypatch, StubProvider("m" * 99_999))

    instructions = talk_identity.build_instructions(talk_host.host().identity_sections())

    # Measure the rendered body only — the preamble has its own "m"s.
    body = instructions.split(talk_identity.IDENTITY_HEADERS["MEMORY"] + ":\n\n", 1)[1]
    assert len(body) == talk_identity.IDENTITY_CAPS["MEMORY"]


# --- render parity -----------------------------------------------------------


def test_build_instructions_renders_header_and_body_for_each_section():
    sections = {"PERSONA": "soul text", "MEMORY": "memory text"}

    instructions = talk_identity.build_instructions(sections)

    assert instructions.startswith(talk_identity.VOICE_PREAMBLE)
    for name, body in sections.items():
        assert talk_identity.IDENTITY_HEADERS[name] in instructions
        assert body in instructions


def test_no_sections_still_yields_the_bare_preamble():
    assert talk_identity.build_instructions(None) == talk_identity.VOICE_PREAMBLE
    assert talk_identity.build_instructions({}) == talk_identity.VOICE_PREAMBLE


# --- talk_status -------------------------------------------------------------


def test_status_reports_counts_and_never_the_content(monkeypatch):
    secret = "the operator's private soul, which must not be spoken back"
    block = "indexed 412 documents"
    _install_persona(monkeypatch, secret)
    _install_provider(monkeypatch, StubProvider(block))
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: None)

    payload = talk_tools.execute_talk_tool("talk_status", {})
    status = json.loads(payload)

    assert status["identity"] == {"PERSONA": len(secret), "MEMORY": len(block)}
    # The whole point of the sections is that they hold things about the
    # operator; status is spoken aloud and lands in transcripts.
    assert secret not in payload
    assert block not in payload


def test_status_counts_are_post_cap(monkeypatch):
    _install_provider(monkeypatch, StubProvider("m" * 99_999))
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: None)

    status = json.loads(talk_tools.execute_talk_tool("talk_status", {}))

    assert status["identity"]["MEMORY"] == talk_identity.IDENTITY_CAPS["MEMORY"]


def test_status_survives_a_host_that_raises(monkeypatch):
    def boom():
        raise RuntimeError("host exploded")

    monkeypatch.setattr(talk_host.host(), "identity_sections", boom)
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: None)

    status = json.loads(talk_tools.execute_talk_tool("talk_status", {}))

    assert status["identity"] == {}
