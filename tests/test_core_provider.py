"""talk_core_provider — hermes-talk's lanes on the Hermes core contract.

These tests run against the REAL ``agent/realtime_voice_provider.py``, never a
hand-written stand-in. A stub written alongside the adapter would agree with
the adapter by construction and prove nothing: the whole point is that the
core's own validation, capability gating, and hook-override rules accept what
we build. The contract is located by (1) ``HERMES_TALK_CORE_CONTRACT``, (2)
``HERMES_AGENT_REPO`` — the convention the installed-integration test already
uses — or (3) an already-importable ``agent.realtime_voice_provider`` that has
the real shape; otherwise the contract-bound tests skip.

The tests below the fixture need no core at all and always run: redaction,
JSON-shape flattening, and the older-host registration path that every
released Hermes takes.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import talk_core_provider as base_provider  # noqa: E402
import talk_realtime as rt  # noqa: E402

#: Every symbol ``talk_core_provider`` imports, plus the API version. A
#: developer box can carry several in-flight drafts of this contract that
#: differ by a symbol or two, so a loose marker set silently binds the tests
#: to the wrong one -- which is how this check earned its length.
_REAL_CONTRACT_MARKERS = (
    "PCM16_24K",
    "InputAudioCommitted",
    "InputSpeechStarted",
    "InputSpeechStopped",
    "InputTranscript",
    "OutputAudio",
    "OutputTranscript",
    "RealtimeAudioFormat",
    "RealtimeSemanticEagerness",
    "RealtimeTurnDetection",
    "RealtimeTurnDetectionMode",
    "RealtimeCapability",
    "RealtimeToolResult",
    "RealtimeVoiceProvider",
    "RealtimeVoiceSession",
    "RealtimeVoiceSetup",
    "ResponseCompleted",
    "ResponseStarted",
    "SessionClosed",
    "SessionFailure",
    "SessionReady",
    "ToolCall",
    "ToolCallCancelled",
    "UnsupportedRealtimeCapability",
)


def _is_the_shipped_contract(module) -> bool:
    return getattr(module, "REALTIME_VOICE_PROVIDER_API_VERSION", None) == 2 and all(
        hasattr(module, name) for name in _REAL_CONTRACT_MARKERS
    )


def _load_real_contract():
    """Return the genuine contract module, or None when it is not present.

    An explicitly named checkout wins over whatever happens to be importable:
    ``HERMES_AGENT_REPO`` may already point at an unrelated draft, and on this
    developer box it does.
    """

    for env_var in ("HERMES_TALK_CORE_CONTRACT", "HERMES_AGENT_REPO"):
        root = (os.environ.get(env_var) or "").strip()
        if not root:
            continue
        candidate = Path(root) / "agent" / "realtime_voice_provider.py"
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location(
            "agent.realtime_voice_provider", candidate
        )
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 - a checkout we cannot load is not ours
            continue
        if _is_the_shipped_contract(module):
            return module

    try:
        installed = importlib.import_module("agent.realtime_voice_provider")
    except Exception:  # noqa: BLE001 - absent, or a different draft entirely
        return None
    return installed if _is_the_shipped_contract(installed) else None


@pytest.fixture
def core():
    """Import ``talk_core_provider`` bound to the real core contract."""

    contract = _load_real_contract()
    if contract is None:
        pytest.skip(
            "the shipped Hermes core realtime contract is not available; set "
            "HERMES_TALK_CORE_CONTRACT or HERMES_AGENT_REPO to a checkout"
        )

    saved = {
        name: sys.modules.get(name)
        for name in (
            "agent",
            "agent.realtime_voice_provider",
            "agent.realtime_voice_registry",
            "talk_core_provider",
        )
    }
    package = types.ModuleType("agent")
    # Point the package at the checkout the contract itself came from, so a
    # sibling module (the registry) resolves against THIS contract rather than
    # whichever draft happens to be importable.
    package.__path__ = [str(Path(contract.__file__).parent)]  # type: ignore[attr-defined]
    package.realtime_voice_provider = contract  # type: ignore[attr-defined]
    sys.modules["agent"] = package
    sys.modules["agent.realtime_voice_provider"] = contract
    sys.modules.pop("agent.realtime_voice_registry", None)
    sys.modules.pop("talk_core_provider", None)
    try:
        module = importlib.import_module("talk_core_provider")
        assert module.core_contract_available(), module._CORE_IMPORT_ERROR
        module.contract = contract  # type: ignore[attr-defined]
        yield module
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        importlib.import_module("talk_core_provider")


PLUGIN_LOGGER = "hermes_plugins.hermes_talk"


def _cached_plugin_modules(module_name: str) -> list[str]:
    """The plugin package and every submodule currently cached under it."""

    prefix = f"{module_name}."
    return [
        name
        for name in list(sys.modules)
        if name == module_name or name.startswith(prefix)
    ]


@pytest.fixture
def plugin():
    """Load ``__init__.py`` as a package, exactly the way Hermes loads it.

    Importing it flat would create a SECOND plugin identity whose ``from .
    import`` falls back to top-level modules, giving the session two
    ``talk_tools`` objects with two receipt dicts -- which quietly corrupts
    every other registration test that runs afterwards.
    """

    parent = "hermes_plugins"
    if parent not in sys.modules:
        namespace = types.ModuleType(parent)
        namespace.__path__ = []  # type: ignore[attr-defined]
        sys.modules[parent] = namespace

    module_name = f"{parent}.hermes_talk"
    # Same submodule purge test_register.load_plugin does, and for the same
    # reason: an installed hermes-talk under ~/.hermes or AppData otherwise
    # wins the `from . import ...` lookups.
    saved = {}
    for name in _cached_plugin_modules(module_name):
        saved[name] = sys.modules.pop(name)
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

    module.talk_tools.REGISTRATION_RECEIPTS.clear()
    module.talk_tools.REGISTRATION_FAILURES.clear()
    try:
        yield module
    finally:
        module.talk_tools.REGISTRATION_RECEIPTS.clear()
        module.talk_tools.REGISTRATION_FAILURES.clear()
        module.talk_host.bind_ctx(None)
        for name in _cached_plugin_modules(module_name):
            sys.modules.pop(name, None)
        sys.modules.update(saved)


class FakeTalkSession:
    """A hermes-talk RealtimeSession that records commands and replays events."""

    def __init__(self, events=(), *, connect_error: Exception | None = None):
        self.state = rt.SessionState.NEW
        self.sent: list[rt.RealtimeCommand] = []
        self.connected_with: rt.SessionSetup | None = None
        self.closes = 0
        self._events = list(events)
        self._connect_error = connect_error

    async def connect(self, setup: rt.SessionSetup) -> None:
        if self._connect_error is not None:
            self.state = rt.SessionState.FAILED
            raise self._connect_error
        self.connected_with = setup
        self.state = rt.SessionState.CONNECTED

    async def send(self, commands) -> None:
        self.sent.extend(commands)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def close(self) -> None:
        self.closes += 1
        self.state = rt.SessionState.CLOSED


def open_lane(core, provider_class, events=(), **kwargs):
    """Open one lane over a FakeTalkSession, bypassing auth and the network."""

    session = FakeTalkSession(events, **kwargs)
    provider = provider_class(
        auth_resolver=lambda: types.SimpleNamespace(token="t0ken", source="test"),
        session_factory=lambda auth: session,
    )
    setup = core.contract.RealtimeVoiceSetup(instructions="be brief")
    return provider, session, setup


def drain(core_session):
    async def run():
        return [event async for event in core_session.events()]

    return asyncio.run(run())


# -- no core required --------------------------------------------------------


def test_redaction_strips_every_credential_shape_from_provider_text():
    # The Gemini lane carries its API key in the socket URL, so transport text
    # built from the request is credential-bearing by construction.
    redacted = base_provider.redact(
        "connect wss://host/live?key=AIzaSyREALSECRET&alt=proto failed"
    )
    assert "AIzaSyREALSECRET" not in redacted
    assert "key=<redacted>" in redacted
    assert "alt=proto" in redacted

    assert "sk-live-abcdefg" not in base_provider.redact("Authorization sk-live-abcdefg")
    assert "xai-abcdefgh" not in base_provider.redact("token xai-abcdefgh rejected")
    bearer = base_provider.redact("header: Bearer ey.JHDR.sig")
    assert "ey.JHDR.sig" not in bearer
    assert base_provider.redact(None) == ""


def test_frozen_tool_parameters_survive_as_json_serializable_objects():
    import json
    from types import MappingProxyType

    frozen = MappingProxyType(
        {
            "type": "object",
            "properties": MappingProxyType({"q": MappingProxyType({"type": "string"})}),
            "required": ("q",),
        }
    )
    # json.dumps refuses a MappingProxyType outright, so every wire layer
    # downstream would fail on an otherwise valid tool schema.
    with pytest.raises(TypeError):
        json.dumps(frozen)
    plain = base_provider._plain(frozen)
    assert json.loads(json.dumps(plain)) == {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }


def test_advisory_offsets_degrade_to_unknown_instead_of_failing():
    assert base_provider._optional_ms(120) == 120
    assert base_provider._optional_ms(120.9) == 120
    for junk in (None, -1, True, False, "120", object()):
        assert base_provider._optional_ms(junk) is None


def test_a_host_without_the_hook_registers_nothing_warns_nothing(plugin, caplog):
    """Every released Hermes takes this path; it must be silent and harmless."""

    class OldHostCtx:
        """A host from before the realtime contract existed."""

    with caplog.at_level(logging.DEBUG, logger=PLUGIN_LOGGER):
        plugin._register_core_realtime_providers(OldHostCtx())

    assert (
        plugin.talk_tools.REGISTRATION_RECEIPTS["core_realtime_providers"]
        == "unsupported-optional"
    )
    assert plugin.talk_tools.REGISTRATION_FAILURES == []
    assert [r.levelno for r in caplog.records] == [logging.DEBUG]


def test_provider_names_are_namespaced_away_from_cores_bundled_openai():
    # The registry keys on the lowercased name and a re-registration silently
    # replaces the incumbent, so a bare "openai" would evict core's own backend.
    assert base_provider.PROVIDER_NAMES == (
        "hermes-talk/openai",
        "hermes-talk/grok",
        "hermes-talk/gemini",
    )
    assert all(name.startswith("hermes-talk/") for name in base_provider.PROVIDER_NAMES)
    assert len(set(base_provider.PROVIDER_NAMES)) == 3


def test_without_the_contract_the_module_is_importable_and_yields_no_providers():
    if base_provider.core_contract_available():
        pytest.skip("this box has the contract; the null path is covered elsewhere")
    assert base_provider.build_providers() == ()
    assert base_provider.TalkOpenAICoreProvider is None
    diagnostic = base_provider.core_contract_diagnostic()
    assert diagnostic["contract_available"] is False
    assert diagnostic["provider_names"] == list(base_provider.PROVIDER_NAMES)


# -- registration ------------------------------------------------------------


def test_all_three_lanes_register_on_a_host_that_has_the_hook(
    core, plugin, monkeypatch
):
    monkeypatch.setattr(plugin, "talk_core_provider", core)
    registered = []

    class NewHostCtx:
        def register_realtime_voice_provider(self, provider):
            registered.append(provider)
            return object()  # the real hook returns a registration handle

    plugin._register_core_realtime_providers(NewHostCtx())

    assert [p.name for p in registered] == list(core.PROVIDER_NAMES)
    assert all(p.api_version == 2 for p in registered)
    assert (
        plugin.talk_tools.REGISTRATION_RECEIPTS["core_realtime_providers"]
        == "registered"
    )
    assert plugin.talk_tools.REGISTRATION_FAILURES == []


@pytest.mark.parametrize("refusal", [None, False])
def test_a_refused_registration_is_rejected_not_reported_as_registered(
    core, plugin, monkeypatch, refusal
):
    monkeypatch.setattr(plugin, "talk_core_provider", core)

    class RefusingCtx:
        def register_realtime_voice_provider(self, provider):
            return refusal

    plugin._register_core_realtime_providers(RefusingCtx())

    assert (
        plugin.talk_tools.REGISTRATION_RECEIPTS["core_realtime_providers"] == "rejected"
    )


def test_a_raising_host_is_a_redacted_failure_not_a_crash(core, plugin, monkeypatch):
    monkeypatch.setattr(plugin, "talk_core_provider", core)

    class ExplodingCtx:
        def register_realtime_voice_provider(self, provider):
            raise RuntimeError("sk-host-secret must not reach the receipt")

    plugin._register_core_realtime_providers(ExplodingCtx())

    assert plugin.talk_tools.REGISTRATION_RECEIPTS["core_realtime_providers"] == "failed"
    rendered = " ".join(plugin.talk_tools.REGISTRATION_FAILURES)
    assert "sk-host-secret" not in rendered
    assert "RuntimeError" in rendered


def test_the_real_registry_accepts_every_lane(core):
    """Drive core's own registry, not a stub of it."""

    # The registry imports hermes_constants from its own checkout root.
    checkout_root = str(Path(core.contract.__file__).resolve().parent.parent)
    added = checkout_root not in sys.path
    if added:
        sys.path.insert(0, checkout_root)
    try:
        from agent import realtime_voice_registry
    except ImportError:
        pytest.skip("core registry module is not importable on this box")
    finally:
        if added:
            sys.path.remove(checkout_root)
    if (
        realtime_voice_registry.RealtimeVoiceProvider
        is not core.contract.RealtimeVoiceProvider
    ):
        pytest.skip("the importable registry belongs to a different contract build")

    realtime_voice_registry._reset_for_tests()
    try:
        for provider in core.build_providers():
            assert realtime_voice_registry.register_provider(provider) is True
        for name in core.PROVIDER_NAMES:
            assert realtime_voice_registry.get_provider(name) is not None
        # Case-insensitive lookup is the registry's contract, and nothing we
        # registered may sit in core's bundled "openai" slot.
        assert realtime_voice_registry.get_provider("HERMES-TALK/OPENAI") is not None
        assert realtime_voice_registry.get_provider("openai") is None
    finally:
        realtime_voice_registry._reset_for_tests()


# -- capabilities ------------------------------------------------------------


def test_capabilities_are_declared_not_faked(core):
    cap = core.contract.RealtimeCapability

    for provider_class in (core.TalkOpenAICoreProvider, core.TalkGrokCoreProvider):
        capabilities = provider_class.capabilities
        assert cap.TOOL_CALLING in capabilities
        assert cap.EXPLICIT_RESPONSE in capabilities
        assert cap.RESPONSE_CANCELLATION in capabilities
        assert cap.OUTPUT_TRUNCATION in capabilities
        assert cap.DYNAMIC_CONTEXT in capabilities
        # hermes-talk's neutral command set has no commit, so commit_audio()
        # could only ever be faked.
        assert cap.INPUT_COMMIT_EVENTS not in capabilities

    gemini = core.TalkGeminiCoreProvider.capabilities
    assert cap.TOOL_CALLING in gemini
    assert cap.TOOL_CALL_CANCELLATION in gemini
    # Gemini Live has no client cancel, no truncate, no item delete, and no
    # per-response metadata. The host must drop playback locally instead.
    assert cap.RESPONSE_CANCELLATION not in gemini
    assert cap.OUTPUT_TRUNCATION not in gemini
    assert cap.DYNAMIC_CONTEXT not in gemini
    assert cap.EXPLICIT_RESPONSE not in gemini


def test_an_unadvertised_operation_raises_instead_of_doing_nothing(core):
    provider, session, setup = open_lane(core, core.TalkGeminiCoreProvider)
    unsupported = core.contract.UnsupportedRealtimeCapability

    async def run():
        opened = await provider.open_session(setup)
        for call in (
            opened.cancel_response(),
            opened.truncate_output("item-1", 120),
            opened.add_context("ctx-1", "note"),
            opened.remove_context("ctx-1"),
            opened.create_response(),
        ):
            with pytest.raises(unsupported):
                await call
        await opened.close()

    asyncio.run(run())
    # A refusal must never reach the wire as a silent no-op command.
    assert session.sent == []


def test_gemini_declares_the_audio_format_its_session_actually_expects(core):
    """send_audio feeds hermes-talk, which owns the 24k -> 16k downsample."""

    provider, _session, setup = open_lane(core, core.TalkGeminiCoreProvider)

    async def run():
        opened = await provider.open_session(setup)
        try:
            return opened.input_audio_format, opened.output_audio_format
        finally:
            await opened.close()

    input_audio, output_audio = asyncio.run(run())
    assert input_audio.sample_rate_hz == 24_000
    assert output_audio.sample_rate_hz == 24_000
    assert input_audio.channels == 1


# -- event translation (provider -> core) ------------------------------------


def test_every_talk_event_reaches_the_host_as_its_core_counterpart(core):
    c = core.contract
    provider, _session, setup = open_lane(
        core,
        core.TalkOpenAICoreProvider,
        events=[
            rt.SessionReady(session_id="sess-1"),
            rt.SpeechStarted(input_id="item-1", offset_ms=40),
            rt.SpeechStopped(input_id="item-1", offset_ms=900),
            rt.InputAudioCommitted(input_id="item-1"),
            rt.Transcript(
                role=rt.TranscriptRole.USER,
                text="what is the plan",
                final=True,
                provenance=rt.TranscriptProvenance.INPUT_AUDIO,
            ),
            rt.ResponseStarted(response_id="resp-1", metadata={"turn": "1"}),
            rt.OutputAudio(data=b"\x01\x02", item_id="out-1", response_id="resp-1"),
            rt.Transcript(
                role=rt.TranscriptRole.ASSISTANT,
                text="here it is",
                final=False,
                provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
                response_id="resp-1",
            ),
            rt.FunctionCall(
                call_id="fc-1",
                name="search_memory",
                arguments='{"q":"plan"}',
                response_id="resp-1",
                item_id="out-2",
            ),
            rt.ResponseFinished(response_id="resp-1"),
            rt.SessionTerminated(state=rt.SessionState.CLOSED, detail="bye"),
        ],
    )

    async def run():
        opened = await provider.open_session(setup)
        return [event async for event in opened.events()]

    events = asyncio.run(run())

    assert [type(event) for event in events] == [
        c.SessionReady,
        c.InputSpeechStarted,
        c.InputSpeechStopped,
        c.InputAudioCommitted,
        c.InputTranscript,
        c.ResponseStarted,
        c.OutputAudio,
        c.OutputTranscript,
        c.ToolCall,
        c.ResponseCompleted,
        c.SessionClosed,
    ]
    assert events[0].session_id == "sess-1"
    assert (events[1].item_id, events[1].audio_start_ms) == ("item-1", 40)
    assert events[2].audio_end_ms == 900
    # The operator's own speech belongs to no response.
    assert events[4].text == "what is the plan" and events[4].final is True
    assert events[5].metadata == {"turn": "1"}
    assert events[6].data == b"\x01\x02" and events[6].response_id == "resp-1"
    assert events[7].final is False and events[7].response_id == "resp-1"
    assert events[8].call_id == "fc-1" and events[8].arguments == '{"q":"plan"}'
    assert events[10].reason == "bye"


def test_a_role_decides_which_transcript_side_the_host_sees(core):
    c = core.contract

    assert isinstance(
        core.translate_event(
            rt.Transcript(
                role=rt.TranscriptRole.USER,
                text="mine",
                final=True,
                provenance=rt.TranscriptProvenance.INPUT_AUDIO,
            )
        ),
        c.InputTranscript,
    )
    assert isinstance(
        core.translate_event(
            rt.Transcript(
                role=rt.TranscriptRole.ASSISTANT,
                text="theirs",
                final=True,
                provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
            )
        ),
        c.OutputTranscript,
    )


def test_a_failed_session_becomes_a_terminal_failure_and_ends_the_stream(core):
    c = core.contract
    provider, _session, setup = open_lane(
        core,
        core.TalkGrokCoreProvider,
        events=[
            rt.ProviderFailure(detail="one bad frame", terminal=False),
            rt.SessionTerminated(state=rt.SessionState.FAILED, detail="socket died"),
            rt.SessionReady(session_id="never-delivered"),
        ],
    )

    async def run():
        opened = await provider.open_session(setup)
        return [event async for event in opened.events()]

    events = asyncio.run(run())

    # A non-terminal complaint must not end the call; the terminal one must.
    assert [type(event) for event in events] == [c.SessionFailure, c.SessionFailure]
    assert events[0].terminal is False and events[0].message == "one bad frame"
    assert events[1].terminal is True and events[1].message == "socket died"


def test_a_stream_that_just_stops_still_closes_the_host_side(core):
    provider, _session, setup = open_lane(core, core.TalkOpenAICoreProvider, events=[])

    async def run():
        opened = await provider.open_session(setup)
        return [event async for event in opened.events()]

    events = asyncio.run(run())
    assert [type(event) for event in events] == [core.contract.SessionClosed]
    assert events[0].reason == "end of stream"


def test_a_malformed_provider_value_is_non_terminal_and_redacted(core):
    """One bad frame must never end a conversation."""

    c = core.contract
    rogue = rt.SpeechStarted(input_id="item-1")
    # Bypass the frozen dataclass to forge a value the core contract refuses.
    object.__setattr__(rogue, "input_id", "  key=AIzaLEAK  ")

    provider, _session, setup = open_lane(
        core,
        core.TalkOpenAICoreProvider,
        events=[rogue, rt.SessionReady(session_id="sess-2")],
    )

    async def run():
        opened = await provider.open_session(setup)
        return [event async for event in opened.events()]

    events = asyncio.run(run())
    # The complaint does not end the call: the next good event is delivered,
    # and the stream closes normally when the provider runs dry.
    assert [type(event) for event in events] == [
        c.SessionFailure,
        c.SessionReady,
        c.SessionClosed,
    ]
    assert events[0].terminal is False
    assert events[0].code == "protocol"
    assert "AIzaLEAK" not in events[0].message
    assert events[1].session_id == "sess-2"


def test_a_transport_explosion_is_a_terminal_failure_carrying_no_secret(core):
    class ExplodingSession(FakeTalkSession):
        async def __anext__(self):
            raise RuntimeError("wss://host/live?key=AIzaSyLEAKED refused")

    session = ExplodingSession()
    provider = core.TalkGeminiCoreProvider(
        auth_resolver=lambda: types.SimpleNamespace(token="t", source="test"),
        session_factory=lambda auth: session,
    )
    setup = core.contract.RealtimeVoiceSetup(instructions="hi")

    async def run():
        opened = await provider.open_session(setup)
        return [event async for event in opened.events()]

    events = asyncio.run(run())
    assert [type(event) for event in events] == [core.contract.SessionFailure]
    assert events[0].terminal is True
    assert "AIzaSyLEAKED" not in events[0].message
    assert "RuntimeError" in events[0].message


# -- cancellation and interruption -------------------------------------------


def test_gemini_tool_call_cancellation_reaches_the_host_as_a_typed_event(core):
    """The whole point of the neutral ToolCallsCancelled addition."""

    provider, _session, setup = open_lane(
        core,
        core.TalkGeminiCoreProvider,
        events=[rt.ToolCallsCancelled(call_ids=("fc-1", "fc-2"))],
    )

    async def run():
        opened = await provider.open_session(setup)
        return [event async for event in opened.events()]

    events = asyncio.run(run())
    assert isinstance(events[0], core.contract.ToolCallCancelled)
    assert events[0].call_ids == ("fc-1", "fc-2")


def test_gemini_live_actually_emits_the_cancellation_off_its_own_wire():
    """End to end from the Live frame, not from a hand-made event."""

    import talk_gemini_realtime as gemini

    session = gemini.GeminiRealtimeSession(auth_token="k", auth_source="test")
    events = session._decode({"toolCallCancellation": {"ids": ["fc-9", "", 7, None]}})

    assert [type(event) for event in events] == [rt.ToolCallsCancelled]
    # Malformed ids are filtered, not reported.
    assert events[0].call_ids == ("fc-9",)
    # And the send path still refuses to answer a cancelled call.
    assert session._cancelled_call_ids == {"fc-9": False}


def test_barge_in_maps_to_the_hosts_interruption_signal(core):
    provider, _session, setup = open_lane(
        core,
        core.TalkGeminiCoreProvider,
        events=[rt.SpeechStarted(), rt.ResponseFinished(response_id=None)],
    )

    async def run():
        opened = await provider.open_session(setup)
        return [event async for event in opened.events()]

    events = asyncio.run(run())
    assert isinstance(events[0], core.contract.InputSpeechStarted)
    assert events[0].item_id is None and events[0].audio_start_ms is None
    assert isinstance(events[1], core.contract.ResponseCompleted)


def test_cancel_reaches_the_wire_on_a_lane_that_has_one(core):
    provider, session, setup = open_lane(core, core.TalkOpenAICoreProvider)

    async def run():
        opened = await provider.open_session(setup)
        await opened.cancel_response("resp-1")
        await opened.truncate_output("item-1", 640)
        await opened.close()

    asyncio.run(run())
    assert [type(command) for command in session.sent] == [
        rt.CancelResponse,
        rt.TruncateOutput,
    ]
    assert session.sent[1].item_id == "item-1"
    assert session.sent[1].audio_end_ms == 640


# -- command translation (core -> provider) ----------------------------------


def test_audio_tools_context_and_responses_map_to_talk_commands(core):
    provider, session, setup = open_lane(core, core.TalkOpenAICoreProvider)
    result = core.contract.RealtimeToolResult

    async def run():
        opened = await provider.open_session(setup)
        await opened.send_audio(b"\x00\x01")
        await opened.send_audio(b"")  # empty chunks are not traffic
        await opened.submit_tool_results(
            [result(call_id="fc-1", output="ok"), result(call_id="fc-2", output="also")],
            continue_response=True,
        )
        await opened.create_response(metadata={"turn": "2"})
        await opened.add_context("ctx-1", "remember this")
        await opened.remove_context("ctx-1")
        await opened.close()

    asyncio.run(run())

    assert [type(command) for command in session.sent] == [
        rt.AppendInputAudio,
        rt.SubmitToolResult,
        rt.SubmitToolResult,
        rt.StartResponse,  # continue_response asks for exactly one reply
        rt.StartResponse,
        rt.AddContext,
        rt.RemoveContext,
    ]
    assert session.sent[0].data == b"\x00\x01"
    assert (session.sent[1].call_id, session.sent[1].output) == ("fc-1", "ok")
    assert session.sent[4].metadata == {"turn": "2"}
    assert session.sent[5].text == "remember this"


def test_tool_results_without_continuation_do_not_ask_for_a_reply(core):
    provider, session, setup = open_lane(core, core.TalkGrokCoreProvider)
    result = core.contract.RealtimeToolResult

    async def run():
        opened = await provider.open_session(setup)
        await opened.submit_tool_results(
            [result(call_id="fc-1", output="ok")], continue_response=False
        )
        await opened.close()

    asyncio.run(run())
    assert [type(command) for command in session.sent] == [rt.SubmitToolResult]


def test_the_setup_the_provider_gets_is_the_hosts_setup(core):
    c = core.contract
    tool = c.RealtimeTool(
        name="search_memory",
        description="look things up",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    session = FakeTalkSession()
    provider = core.TalkGrokCoreProvider(
        auth_resolver=lambda: types.SimpleNamespace(token="t", source="test"),
        session_factory=lambda auth: session,
    )
    setup = c.RealtimeVoiceSetup(
        model="grok-voice-latest",
        voice="rex",
        instructions="be brief",
        tools=(tool,),
        automatic_response=False,
    )

    async def run():
        opened = await provider.open_session(setup)
        await opened.close()

    asyncio.run(run())

    got = session.connected_with
    assert got.model == "grok-voice-latest"
    assert got.voice == "rex"
    assert got.instructions == "be brief"
    assert got.automatic_response is False
    assert [t.name for t in got.tools] == ["search_memory"]
    # The core freezes parameters; the wire needs plain JSON-able objects.
    import json

    assert json.loads(json.dumps(dict(got.tools[0].parameters)))["properties"] == {
        "q": {"type": "string"}
    }


def test_provider_turn_detection_capability_matrix_is_exact(core):
    mode = core.contract.RealtimeTurnDetectionMode

    assert core.TalkOpenAICoreProvider.supported_turn_detection_modes == frozenset(mode)
    assert core.TalkGrokCoreProvider.supported_turn_detection_modes == frozenset(
        {mode.PROVIDER_NATIVE, mode.SERVER_VAD}
    )
    assert core.TalkGeminiCoreProvider.supported_turn_detection_modes == frozenset(
        {mode.PROVIDER_NATIVE}
    )


@pytest.mark.parametrize(
    ("core_mode_name", "talk_mode", "eagerness_name"),
    [
        ("PROVIDER_NATIVE", rt.RealtimeTurnDetectionMode.PROVIDER_NATIVE, None),
        ("SERVER_VAD", rt.RealtimeTurnDetectionMode.SERVER_VAD, None),
        (
            "SEMANTIC_VAD",
            rt.RealtimeTurnDetectionMode.SEMANTIC_VAD,
            "HIGH",
        ),
    ],
)
def test_openai_turn_detection_bridge_is_exhaustive(
    core, core_mode_name, talk_mode, eagerness_name
):
    c = core.contract
    session = FakeTalkSession()
    provider = core.TalkOpenAICoreProvider(
        auth_resolver=lambda: types.SimpleNamespace(token="t", source="test"),
        session_factory=lambda auth: session,
    )
    eagerness = (
        None if eagerness_name is None else getattr(c.RealtimeSemanticEagerness, eagerness_name)
    )
    setup = c.RealtimeVoiceSetup(
        instructions="hi",
        turn_detection=c.RealtimeTurnDetection(
            mode=getattr(c.RealtimeTurnDetectionMode, core_mode_name),
            semantic_eagerness=eagerness,
        ),
    )

    async def run():
        opened = await provider.open_session(setup)
        await opened.close()

    asyncio.run(run())
    assert session.connected_with.turn_detection.mode is talk_mode
    expected_eagerness = (
        None if eagerness_name is None else getattr(rt.RealtimeSemanticEagerness, eagerness_name)
    )
    assert session.connected_with.turn_detection.semantic_eagerness is expected_eagerness


@pytest.mark.parametrize(
    ("provider_name", "mode_name"),
    [
        ("TalkGrokCoreProvider", "SEMANTIC_VAD"),
        ("TalkGeminiCoreProvider", "SERVER_VAD"),
        ("TalkGeminiCoreProvider", "SEMANTIC_VAD"),
    ],
)
def test_unsupported_turn_detection_is_refused_before_auth_or_session_factory(
    core, provider_name, mode_name
):
    calls = []
    provider = getattr(core, provider_name)(
        auth_resolver=lambda: calls.append("auth"),
        session_factory=lambda auth: calls.append("session"),
    )
    c = core.contract
    setup = c.RealtimeVoiceSetup(
        instructions="hi",
        turn_detection=c.RealtimeTurnDetection(
            mode=getattr(c.RealtimeTurnDetectionMode, mode_name)
        ),
    )

    with pytest.raises(ValueError, match="unsupported turn detection mode"):
        asyncio.run(provider.open_session(setup))
    assert calls == []


def test_an_unset_model_or_voice_falls_back_to_the_lanes_default(core):
    import talk_config

    session = FakeTalkSession()
    provider = core.TalkGeminiCoreProvider(
        auth_resolver=lambda: types.SimpleNamespace(token="t", source="test"),
        session_factory=lambda auth: session,
    )

    async def run():
        opened = await provider.open_session(
            core.contract.RealtimeVoiceSetup(instructions="hi")
        )
        await opened.close()

    asyncio.run(run())
    assert session.connected_with.model == talk_config.talk_gemini_model()
    assert session.connected_with.voice == talk_config.talk_gemini_voice()


def test_a_foreign_audio_format_is_refused_before_auth_or_a_socket(core):
    calls = []
    provider = core.TalkOpenAICoreProvider(
        auth_resolver=lambda: calls.append("auth"),
        session_factory=lambda auth: calls.append("session"),
    )
    setup = core.contract.RealtimeVoiceSetup(
        instructions="hi",
        input_audio=core.contract.RealtimeAudioFormat(sample_rate_hz=8_000),
    )

    with pytest.raises(ValueError, match="input audio must be"):
        asyncio.run(provider.open_session(setup))
    assert calls == []


# -- lifecycle ---------------------------------------------------------------


def test_close_releases_the_underlying_session_exactly_once(core):
    provider, session, setup = open_lane(core, core.TalkOpenAICoreProvider)

    async def run():
        opened = await provider.open_session(setup)
        await opened.close()
        await opened.close()  # repeated closes are successful no-ops
        return opened

    opened = asyncio.run(run())
    assert session.closes == 1
    assert opened.closed is True


def test_a_failed_connect_does_not_leak_the_session(core):
    session = FakeTalkSession(connect_error=rt.RealtimeSessionError("handshake 401"))
    provider = core.TalkGrokCoreProvider(
        auth_resolver=lambda: types.SimpleNamespace(token="t", source="test"),
        session_factory=lambda auth: session,
    )
    setup = core.contract.RealtimeVoiceSetup(instructions="hi")

    with pytest.raises(rt.RealtimeSessionError):
        asyncio.run(provider.open_session(setup))
    assert session.closes == 1


def test_the_session_is_usable_as_an_async_context_manager(core):
    provider, session, setup = open_lane(core, core.TalkOpenAICoreProvider)

    async def run():
        async with await provider.open_session(setup) as opened:
            await opened.send_audio(b"\x00\x01")

    asyncio.run(run())
    assert session.closes == 1


# -- availability is offline and read-only -----------------------------------


@pytest.mark.parametrize(
    "provider_name", ["TalkOpenAICoreProvider", "TalkGrokCoreProvider", "TalkGeminiCoreProvider"]
)
def test_availability_never_touches_the_network_or_writes_an_auth_store(
    core, monkeypatch, provider_name, tmp_path
):
    """hermes-talk#82 was rejected upstream for exactly this.

    ``talk_auth.resolve_auth`` refreshes an expiring token over the network and
    atomically rewrites ``auth.json``; a readiness probe must never reach it.
    """

    import talk_auth
    import talk_config
    import talk_grok_auth

    def forbidden(*args, **kwargs):
        raise AssertionError("is_available() performed a forbidden side effect")

    # Every write-capable resolver and every transport entry point.
    monkeypatch.setattr(talk_auth, "resolve_auth", forbidden)
    monkeypatch.setattr(talk_grok_auth, "resolve_grok_auth", forbidden)
    monkeypatch.setattr(talk_auth, "_write_auth_json", forbidden, raising=False)
    monkeypatch.setattr(talk_auth, "_post_token_form", forbidden, raising=False)
    monkeypatch.setattr(talk_auth, "_refresh_codex_credential", forbidden, raising=False)
    import httpx

    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(os, "replace", forbidden)
    monkeypatch.setattr(talk_config, "get_hermes_home", lambda: tmp_path)

    provider = getattr(core, provider_name)()
    assert isinstance(provider.is_available(), bool)


def test_availability_reports_configured_without_resolving_a_token(core, monkeypatch):
    import talk_auth
    import talk_grok_auth

    monkeypatch.setattr(talk_auth, "auth_diagnostic", lambda: {"configured": True})
    monkeypatch.setattr(
        talk_grok_auth, "grok_auth_diagnostic", lambda: {"configured": True}
    )
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-value")
    monkeypatch.delenv("TALK_GEMINI_API_KEY", raising=False)

    assert core.TalkOpenAICoreProvider().is_available() is True
    assert core.TalkGrokCoreProvider().is_available() is True
    assert core.TalkGeminiCoreProvider().is_available() is True

    monkeypatch.setattr(talk_auth, "auth_diagnostic", lambda: {"configured": False})
    monkeypatch.setattr(
        talk_grok_auth, "grok_auth_diagnostic", lambda: {"configured": False}
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    assert core.TalkOpenAICoreProvider().is_available() is False
    assert core.TalkGrokCoreProvider().is_available() is False
    assert core.TalkGeminiCoreProvider().is_available() is False


def test_a_broken_probe_reports_unavailable_instead_of_raising(core, monkeypatch):
    import talk_auth

    def explode():
        raise RuntimeError("auth store is corrupt")

    monkeypatch.setattr(talk_auth, "auth_diagnostic", explode)
    assert core.TalkOpenAICoreProvider().is_available() is False


def test_the_setup_schema_names_an_env_var_and_never_a_value(core):
    for provider in core.build_providers():
        schema = provider.get_setup_schema()
        assert schema["name"] == provider.display_name
        assert schema["env_vars"][0]["key"].endswith("_API_KEY")
        assert provider.default_model()
        assert provider.default_voice()
        assert provider.list_voices()


def _synthetic_old_head_contract():
    """Minimal #101808-shaped core: API v2 with every base name, no turn-detection names.

    Binds the exact head the maintainer reviewed against: the three semantic
    turn-detection symbols are absent, so the adapter must degrade to native
    instead of dropping the whole core lane.
    """

    module = types.ModuleType("agent.realtime_voice_provider")
    module.REALTIME_VOICE_PROVIDER_API_VERSION = 2
    module.PCM16_24K = object()
    capability_members = (
        "TOOL_CALLING",
        "INPUT_TRANSCRIPTION",
        "OUTPUT_TRANSCRIPTION",
        "EXPLICIT_RESPONSE",
        "RESPONSE_CANCELLATION",
        "OUTPUT_TRUNCATION",
        "DYNAMIC_CONTEXT",
        "TOOL_CALL_CANCELLATION",
    )
    module.RealtimeCapability = type(
        "RealtimeCapability", (), {name: object() for name in capability_members}
    )
    for name in (
        "InputAudioCommitted",
        "InputSpeechStarted",
        "InputSpeechStopped",
        "InputTranscript",
        "OutputAudio",
        "OutputTranscript",
        "RealtimeAudioFormat",
        "RealtimeToolResult",
        "RealtimeVoiceEvent",
        "RealtimeVoiceProvider",
        "RealtimeVoiceSession",
        "RealtimeVoiceSetup",
        "ResponseCompleted",
        "ResponseStarted",
        "SessionClosed",
        "SessionFailure",
        "SessionReady",
        "ToolCall",
        "ToolCallCancelled",
    ):
        setattr(module, name, type(name, (), {}))
    assert not hasattr(module, "RealtimeTurnDetectionMode")
    return module


def test_old_head_contract_keeps_core_lane_with_native_only_turn_detection():
    """The #101808 head must not take the whole core lane down with it."""

    contract = _synthetic_old_head_contract()
    saved = {
        name: sys.modules.get(name)
        for name in ("agent", "agent.realtime_voice_provider", "talk_core_provider")
    }
    package = types.ModuleType("agent")
    package.__path__ = []
    package.realtime_voice_provider = contract
    sys.modules["agent"] = package
    sys.modules["agent.realtime_voice_provider"] = contract
    sys.modules.pop("talk_core_provider", None)
    try:
        module = importlib.import_module("talk_core_provider")
        assert module.core_contract_available()
        assert not module.turn_detection_available()
        providers = module.build_providers()
        assert len(providers) == 3
        assert all(p.supported_turn_detection_modes == frozenset() for p in providers)
        native = providers[0]._talk_turn_detection(None)
        assert native == rt.RealtimeTurnDetection()
        assert native.mode == rt.RealtimeTurnDetectionMode.PROVIDER_NATIVE
        class _UnsupportedMode:
            value = "semantic_vad"

        semantic = types.SimpleNamespace(mode=_UnsupportedMode(), semantic_eagerness=None)
        with pytest.raises(ValueError, match="does not support turn detection mode"):
            providers[0]._talk_turn_detection(semantic)
        diagnostic = module.core_contract_diagnostic()
        assert diagnostic["contract_available"] is True
        assert diagnostic["turn_detection_available"] is False
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        importlib.import_module("talk_core_provider")
