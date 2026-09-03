"""hermes-talk's realtime providers, published on the Hermes core contract.

Hermes core defines a provider-neutral speech-to-speech contract in
:mod:`agent.realtime_voice_provider` (API v2) and drives any registered
backend from ``hermes realtime --provider <name>``. hermes-talk already
speaks three realtime wires behind its OWN neutral contract
(:mod:`talk_realtime`), so this module is a translator, not a fourth
transport: it wraps each :class:`talk_realtime.RealtimeSession` in a core
:class:`RealtimeVoiceSession` and maps events and commands between the two
vocabularies.

Why this module sits next to ``talk_core_realtime``
---------------------------------------------------
``talk_core_realtime`` targets a SPECULATIVE draft of API v2 whose symbols
(``TranscriptProvenance``, ``RealtimeResponseRequest``, ``Interruption``, ...)
do not exist in the shipped contract, so its guarded import fails and it
registers nothing. It is left untouched: this module is additive, and the two
can never collide because they claim different provider names.

Rules this module keeps
-----------------------
- **Capabilities are declared, never faked.** A capability is advertised only
  when the underlying hermes-talk session actually puts that command on the
  wire. Gemini Live has no client-side cancel, no output truncate, and no
  conversation-item delete, so it advertises none of them and the host
  degrades explicitly (dropping playback locally) instead of being lied to.
- **Availability is offline and read-only.** ``is_available()`` never opens a
  socket, never refreshes a token, and never writes an auth store. Each lane
  uses its own read-only diagnostic; the write-capable resolver is confined to
  :meth:`open_session`, which is the connect path.
- **A credential only ever reaches its own provider's host**, and never a log,
  an exception message, or an event. Provider-authored text is redacted on the
  way into every core event, because the Gemini lane carries its key in the
  socket URL.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

try:
    from . import (
        talk_auth,
        talk_config,
        talk_gemini_realtime,
        talk_grok_auth,
        talk_grok_realtime,
        talk_openai_realtime,
    )
    from . import talk_realtime as rt
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_auth
    import talk_config
    import talk_gemini_realtime
    import talk_grok_auth
    import talk_grok_realtime
    import talk_openai_realtime
    import talk_realtime as rt

logger = logging.getLogger(__name__)

#: Registry names. Namespaced so they can never shadow core's bundled
#: ``openai`` backend: the registry keys on the lowercased name, and a
#: re-registration silently replaces whatever held that slot.
OPENAI_PROVIDER_NAME = "hermes-talk/openai"
GROK_PROVIDER_NAME = "hermes-talk/grok"
GEMINI_PROVIDER_NAME = "hermes-talk/gemini"

PROVIDER_NAMES = (OPENAI_PROVIDER_NAME, GROK_PROVIDER_NAME, GEMINI_PROVIDER_NAME)

_CORE_IMPORT_ERROR: BaseException | None = None
try:
    from agent.realtime_voice_provider import (
        PCM16_24K,
        REALTIME_VOICE_PROVIDER_API_VERSION,
        InputAudioCommitted,
        InputSpeechStarted,
        InputSpeechStopped,
        InputTranscript,
        OutputAudio,
        OutputTranscript,
        RealtimeAudioFormat,
        RealtimeCapability,
        RealtimeToolResult,
        RealtimeVoiceEvent,
        RealtimeVoiceProvider,
        RealtimeVoiceSession,
        RealtimeVoiceSetup,
        ResponseCompleted,
        ResponseStarted,
        SessionClosed,
        SessionFailure,
        SessionReady,
        ToolCall,
        ToolCallCancelled,
    )

    if REALTIME_VOICE_PROVIDER_API_VERSION != 2:
        raise ImportError(
            "Hermes realtime voice provider API is not version 2: "
            f"{REALTIME_VOICE_PROVIDER_API_VERSION}"
        )
except Exception as exc:  # noqa: BLE001 - an optional host surface must never
    # poison hermes-talk's own imports. Every released Hermes lands here.
    _CORE_IMPORT_ERROR = exc

_TURN_DETECTION_IMPORT_ERROR: BaseException | None = None
try:
    from agent.realtime_voice_provider import (
        RealtimeSemanticEagerness,
        RealtimeTurnDetection,
        RealtimeTurnDetectionMode,
    )
except Exception as exc:  # noqa: BLE001 - pre-semantic core heads lack these names
    _TURN_DETECTION_IMPORT_ERROR = exc
    RealtimeSemanticEagerness = None  # type: ignore[assignment]
    RealtimeTurnDetection = None  # type: ignore[assignment]
    RealtimeTurnDetectionMode = None  # type: ignore[assignment]


def turn_detection_available() -> bool:
    """True when the host contract carries the semantic turn-detection names.

    A pre-semantic core head (today's #101808) still gets the full core lane;
    only turn-detection controls degrade to provider-native. Never raises.
    """

    return _CORE_IMPORT_ERROR is None and _TURN_DETECTION_IMPORT_ERROR is None


def _contract_turn_modes(*names: str) -> frozenset:
    """Contract turn-detection modes by member name; empty on pre-semantic heads."""

    if not turn_detection_available():
        return frozenset()
    return frozenset(getattr(RealtimeTurnDetectionMode, name) for name in names)


def core_contract_available() -> bool:
    """True when this host exposes the exact core realtime contract we target."""

    return _CORE_IMPORT_ERROR is None


def core_contract_diagnostic() -> dict[str, Any]:
    """Read-only receipt for ``talk doctor`` / ``talk_status``. Never raises."""
    available = core_contract_available()
    return {
        "contract_available": available,
        "turn_detection_available": turn_detection_available(),
        "provider_names": list(PROVIDER_NAMES),
        "detail": "" if available else type(_CORE_IMPORT_ERROR).__name__,
    }


#: Credential shapes that must never reach a log, an exception, or an event.
#: The Gemini lane puts its API key in the socket URL query, so any transport
#: text built from the request is suspect by construction.
#: ``(pattern, replacement)``. The label is deliberately kept and only the
#: VALUE replaced: ``key=<redacted>`` tells a reader which credential was
#: elided, where a bare ``<redacted>`` hides the shape of the failure too.
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(key=)[^&\s\"']+"), r"\1<redacted>"),
    (re.compile(r"(?i)\b(bearer)\s+\S+"), r"\1 <redacted>"),
    (re.compile(r"\b(?:sk|xai)-[A-Za-z0-9_\-]{6,}"), "<redacted>"),
)


def redact(detail: Any) -> str:
    """Strip credential-shaped text out of provider-authored detail."""

    if not isinstance(detail, str):
        return ""
    for pattern, replacement in _SECRET_PATTERNS:
        detail = pattern.sub(replacement, detail)
    return detail


def _plain(value: Any) -> Any:
    """Turn frozen contract mappings/tuples back into JSON-serializable objects.

    The core freezes tool parameters into ``MappingProxyType`` and tuples;
    ``json.dumps`` refuses the former, so every wire layer downstream would
    fail on an otherwise valid tool schema.
    """

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    return value


def _optional_ms(value: Any) -> int | None:
    """Offsets are advisory: junk degrades to 'unknown', never to a failure."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value)


if _CORE_IMPORT_ERROR is None:

    def translate_event(event: Any) -> RealtimeVoiceEvent | None:
        """Map one hermes-talk event to a core event; ``None`` = nothing to say.

        Raises ``ValueError``/``TypeError`` when the provider handed us a value
        the core contract refuses. The caller turns that into a NON-terminal
        failure, because one bad frame must never end a conversation.
        """

        if isinstance(event, rt.SessionReady):
            return SessionReady(session_id=event.session_id)
        if isinstance(event, rt.SpeechStarted):
            return InputSpeechStarted(
                item_id=event.input_id, audio_start_ms=_optional_ms(event.offset_ms)
            )
        if isinstance(event, rt.SpeechStopped):
            return InputSpeechStopped(
                item_id=event.input_id, audio_end_ms=_optional_ms(event.offset_ms)
            )
        if isinstance(event, rt.InputAudioCommitted):
            return InputAudioCommitted(item_id=event.input_id)
        if isinstance(event, rt.ResponseStarted):
            return ResponseStarted(
                response_id=event.response_id, metadata=dict(event.metadata)
            )
        if isinstance(event, rt.OutputAudio):
            return OutputAudio(
                data=event.data, item_id=event.item_id, response_id=event.response_id
            )
        if isinstance(event, rt.Transcript):
            # The operator's own speech belongs to no response, which is why
            # the core's InputTranscript has no response_id to carry it.
            if event.role is rt.TranscriptRole.USER:
                return InputTranscript(text=event.text, final=event.final)
            return OutputTranscript(
                text=event.text, final=event.final, response_id=event.response_id
            )
        if isinstance(event, rt.FunctionCall):
            return ToolCall(
                call_id=event.call_id,
                name=event.name,
                arguments=event.arguments,
                response_id=event.response_id,
                item_id=event.item_id,
            )
        if isinstance(event, rt.ToolCallsCancelled):
            return ToolCallCancelled(call_ids=event.call_ids)
        if isinstance(event, rt.ResponseFinished):
            return ResponseCompleted(response_id=event.response_id)
        if isinstance(event, rt.ProviderFailure):
            return SessionFailure(
                code="provider_failure",
                message=redact(event.detail),
                terminal=event.terminal,
            )
        if isinstance(event, rt.SessionTerminated):
            if event.state is rt.SessionState.CLOSED:
                return SessionClosed(reason=redact(event.detail))
            return SessionFailure(
                code="session_failed",
                message=redact(event.detail) or "provider session failed",
                terminal=True,
            )
        return None

    class TalkCoreSession(RealtimeVoiceSession):
        """One hermes-talk realtime session behind the core session contract."""

        def __init__(
            self,
            session: Any,
            capabilities: frozenset,
            *,
            input_audio: RealtimeAudioFormat,
            output_audio: RealtimeAudioFormat,
        ) -> None:
            super().__init__(
                capabilities, input_audio=input_audio, output_audio=output_audio
            )
            self._session = session

        async def _send(self, *commands: Any) -> None:
            await self._session.send(commands)

        # -- required ---------------------------------------------------------

        async def send_audio(self, audio: bytes) -> None:
            if not audio:
                return
            await self._send(rt.AppendInputAudio(data=bytes(audio)))

        def _events(self) -> AsyncIterator[RealtimeVoiceEvent]:
            return self._stream()

        async def _stream(self) -> AsyncIterator[RealtimeVoiceEvent]:
            try:
                async for event in self._session:
                    try:
                        mapped = translate_event(event)
                    except (ValueError, TypeError) as exc:
                        yield SessionFailure(
                            code="protocol",
                            message=(
                                "provider sent a malformed "
                                f"{type(event).__name__}: {redact(str(exc))}"
                            ),
                            terminal=False,
                        )
                        continue
                    if mapped is not None:
                        yield mapped
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced as the terminal event
                logger.warning(
                    "hermes-talk realtime stream failed: %s", type(exc).__name__
                )
                yield SessionFailure(
                    code="transport",
                    message=f"{type(exc).__name__}: {redact(str(exc))}",
                )
                return
            yield SessionClosed(reason="end of stream")

        async def _close(self) -> None:
            await self._session.close()

        # -- optional operations ----------------------------------------------
        # Every hook is implemented; which ones are REACHABLE is decided by the
        # capability set each provider passes in. The base class gates the
        # public methods, so an unadvertised operation raises
        # UnsupportedRealtimeCapability instead of quietly doing nothing.

        async def _submit_tool_results(
            self, results: tuple[RealtimeToolResult, ...], continue_response: bool
        ) -> None:
            commands: list[Any] = [
                rt.SubmitToolResult(call_id=result.call_id, output=result.output)
                for result in results
            ]
            if continue_response:
                commands.append(rt.StartResponse())
            await self._send(*commands)

        async def _create_response(self, metadata: Mapping[str, str]) -> None:
            await self._send(rt.StartResponse(metadata=dict(metadata)))

        async def _cancel_response(self, response_id: str | None) -> None:
            # hermes-talk's cancel is session-global: the wire cancels the
            # in-flight response, which is the one the host means. The contract
            # allows an unnamed cancel where the wire carries no id.
            del response_id
            await self._send(rt.CancelResponse())

        async def _truncate_output(self, item_id: str, audio_end_ms: int) -> None:
            await self._send(
                rt.TruncateOutput(item_id=item_id, audio_end_ms=audio_end_ms)
            )

        async def _add_context(self, item_id: str, text: str) -> None:
            await self._send(rt.AddContext(item_id=item_id, text=text))

        async def _remove_context(self, item_id: str) -> None:
            await self._send(rt.RemoveContext(item_id=item_id))

    def _describe(audio: RealtimeAudioFormat) -> str:
        return f"{audio.mime_type} {audio.sample_rate_hz} Hz x{audio.channels}"

    class _TalkCoreProvider(RealtimeVoiceProvider):
        """Shared factory: resolve auth, build the talk session, wrap it."""

        #: Subclasses fill these in.
        provider_name = ""
        provider_display_name = ""
        provider_tag = ""
        env_key = ""
        env_url = ""
        input_audio = PCM16_24K
        output_audio = PCM16_24K

        supported_turn_detection_modes = _contract_turn_modes("PROVIDER_NATIVE")

        def __init__(
            self,
            *,
            auth_resolver: Callable[[], Any] | None = None,
            session_factory: Callable[[Any], Any] | None = None,
        ) -> None:
            # None sentinels, resolved at call time: a test can replace either
            # seam after construction without fighting a cached default.
            self._auth_resolver = auth_resolver
            self._session_factory = session_factory

        @property
        def name(self) -> str:
            return self.provider_name

        @property
        def display_name(self) -> str:
            return self.provider_display_name

        def _resolve_auth(self) -> Any:
            raise NotImplementedError

        def _build_session(self, auth: Any) -> Any:
            raise NotImplementedError

        def get_setup_schema(self) -> Mapping[str, Any]:
            return {
                "name": self.display_name,
                "badge": "paid",
                "tag": self.provider_tag,
                "env_vars": (
                    {"key": self.env_key, "prompt": self.env_key, "url": self.env_url},
                ),
            }

        def _talk_turn_detection(self, turn_detection: Any) -> rt.RealtimeTurnDetection:
            if turn_detection is None:
                # Pre-semantic host: the setup carries no turn_detection at all.
                return rt.RealtimeTurnDetection()
            if turn_detection.mode not in self.supported_turn_detection_modes:
                raise ValueError(
                    f"{self.display_name} does not support turn detection mode "
                    f"{turn_detection.mode.value}"
                )
            modes = {
                RealtimeTurnDetectionMode.PROVIDER_NATIVE: (
                    rt.RealtimeTurnDetectionMode.PROVIDER_NATIVE
                ),
                RealtimeTurnDetectionMode.SERVER_VAD: (rt.RealtimeTurnDetectionMode.SERVER_VAD),
                RealtimeTurnDetectionMode.SEMANTIC_VAD: (rt.RealtimeTurnDetectionMode.SEMANTIC_VAD),
            }
            eagerness = {
                None: None,
                RealtimeSemanticEagerness.AUTO: rt.RealtimeSemanticEagerness.AUTO,
                RealtimeSemanticEagerness.LOW: rt.RealtimeSemanticEagerness.LOW,
                RealtimeSemanticEagerness.MEDIUM: rt.RealtimeSemanticEagerness.MEDIUM,
                RealtimeSemanticEagerness.HIGH: rt.RealtimeSemanticEagerness.HIGH,
            }
            return rt.RealtimeTurnDetection(
                mode=modes[turn_detection.mode],
                semantic_eagerness=eagerness[turn_detection.semantic_eagerness],
            )

        def _talk_setup(self, setup: RealtimeVoiceSetup) -> Any:
            for label, requested, expected in (
                ("input", setup.input_audio, self.input_audio),
                ("output", setup.output_audio, self.output_audio),
            ):
                if requested is not None and requested != expected:
                    raise ValueError(
                        f"{self.display_name} {label} audio must be "
                        f"{_describe(expected)}, got {_describe(requested)}"
                    )
            return rt.SessionSetup(
                model=setup.model or self.default_model(),
                voice=setup.voice or self.default_voice(),
                instructions=setup.instructions,
                tools=tuple(
                    rt.ToolDefinition(
                        name=tool.name,
                        description=tool.description,
                        parameters=_plain(tool.parameters),
                    )
                    for tool in setup.tools
                ),
                turn_detection=self._talk_turn_detection(getattr(setup, "turn_detection", None)),
            )

        async def open_session(self, setup: RealtimeVoiceSetup) -> RealtimeVoiceSession:
            # Shape first: an unusable setup is refused before any credential is
            # resolved and before a socket is opened.
            self.validate_setup(setup)
            talk_setup = self._talk_setup(setup)
            resolver = self._auth_resolver or self._resolve_auth
            auth = resolver()
            factory = self._session_factory or self._build_session
            session = factory(auth)
            del auth
            core_session = TalkCoreSession(
                session,
                self.capabilities,
                input_audio=self.input_audio,
                output_audio=self.output_audio,
            )
            try:
                await session.connect(talk_setup)
            except BaseException:
                await core_session.close()
                raise
            return core_session

    # -- OpenAI ---------------------------------------------------------------

    OPENAI_CAPABILITIES = frozenset(
        {
            RealtimeCapability.TOOL_CALLING,
            RealtimeCapability.INPUT_TRANSCRIPTION,
            RealtimeCapability.OUTPUT_TRANSCRIPTION,
            RealtimeCapability.EXPLICIT_RESPONSE,
            RealtimeCapability.RESPONSE_CANCELLATION,
            RealtimeCapability.OUTPUT_TRUNCATION,
            RealtimeCapability.DYNAMIC_CONTEXT,
        }
    )

    class TalkOpenAICoreProvider(_TalkCoreProvider):
        """OpenAI Realtime through hermes-talk's ephemeral-client-secret mint.

        INPUT_COMMIT_EVENTS is deliberately absent: hermes-talk's neutral
        command set has no commit, so ``commit_audio()`` could only be faked.
        The wire's own ``input_audio_buffer.committed`` notifications are still
        translated and delivered.
        """

        capabilities = OPENAI_CAPABILITIES
        provider_name = OPENAI_PROVIDER_NAME
        provider_display_name = "OpenAI Realtime (hermes-talk)"
        provider_tag = "gpt-realtime speech-to-speech"
        env_key = "OPENAI_API_KEY"
        env_url = "https://platform.openai.com/api-keys"
        supported_turn_detection_modes = _contract_turn_modes(
            "PROVIDER_NATIVE", "SERVER_VAD", "SEMANTIC_VAD"
        )

        def default_model(self) -> str | None:
            return talk_config.talk_model() or talk_config.DEFAULT_TALK_MODEL

        def default_voice(self) -> str | None:
            return talk_config.talk_voice() or talk_config.DEFAULT_TALK_VOICE

        def list_models(self) -> tuple[Mapping[str, Any], ...]:
            return ({"id": talk_config.DEFAULT_TALK_MODEL, "display": "GPT Realtime"},)

        def list_voices(self) -> tuple[Mapping[str, Any], ...]:
            return tuple(
                {"id": voice, "display": voice.title()}
                for voice in talk_config.OPENAI_REALTIME_VOICES
            )

        def is_available(self) -> bool:
            """Offline: reads the auth store, never refreshes or rewrites it.

            ``talk_auth.auth_diagnostic`` is the deliberate read-only twin of
            ``resolve_auth``, which refreshes an expiring Codex token over the
            network and atomically rewrites ``auth.json`` -- forbidden on a
            readiness probe.
            """

            try:
                self.default_voice()
                model = self.default_model()
                auth = talk_auth.auth_diagnostic()
            except Exception:  # readiness never raises
                logger.debug("hermes-talk/openai readiness probe failed", exc_info=True)
                return False
            return bool(model and auth.get("configured"))

        def _resolve_auth(self) -> Any:
            return talk_auth.resolve_auth()

        def _build_session(self, auth: Any) -> Any:
            return talk_openai_realtime.OpenAIRealtimeSession(
                auth_token=auth.token, auth_source=auth.source
            )

    # -- xAI / Grok -----------------------------------------------------------

    GROK_CAPABILITIES = OPENAI_CAPABILITIES

    class TalkGrokCoreProvider(_TalkCoreProvider):
        """xAI Grok realtime. The resolved xAI token is the socket bearer.

        OUTPUT_TRUNCATION is advertised because ``conversation.item.truncate``
        genuinely goes on the wire. If the server answers that it is
        unimplemented, hermes-talk degrades that session to cancel-only and
        logs it once -- a provider-side reality the session already owns.
        """

        capabilities = GROK_CAPABILITIES
        provider_name = GROK_PROVIDER_NAME
        provider_display_name = "xAI Grok Realtime (hermes-talk)"
        provider_tag = "grok-voice speech-to-speech"
        env_key = "XAI_API_KEY"
        env_url = "https://console.x.ai"
        supported_turn_detection_modes = _contract_turn_modes(
            "PROVIDER_NATIVE", "SERVER_VAD"
        )

        def default_model(self) -> str | None:
            return talk_config.talk_grok_model() or talk_config.DEFAULT_GROK_MODEL

        def default_voice(self) -> str | None:
            return talk_config.talk_grok_voice() or talk_config.DEFAULT_GROK_VOICE

        def list_models(self) -> tuple[Mapping[str, Any], ...]:
            return ({"id": talk_config.DEFAULT_GROK_MODEL, "display": "Grok Voice"},)

        def list_voices(self) -> tuple[Mapping[str, Any], ...]:
            return tuple(
                {"id": voice, "display": voice.title()}
                for voice in talk_config.GROK_REALTIME_VOICES
            )

        def is_available(self) -> bool:
            """Offline: parses the host auth store directly.

            ``grok_auth_diagnostic`` never calls the host resolver, which may
            refresh and write.
            """

            try:
                self.default_voice()
                model = self.default_model()
                diagnostic = talk_grok_auth.grok_auth_diagnostic()
            except Exception:  # readiness never raises
                logger.debug("hermes-talk/grok readiness probe failed", exc_info=True)
                return False
            return bool(model and diagnostic.get("configured"))

        def _resolve_auth(self) -> Any:
            return talk_grok_auth.resolve_grok_auth()

        def _build_session(self, auth: Any) -> Any:
            return talk_grok_realtime.GrokRealtimeSession(
                auth_token=auth.token, auth_source=auth.source
            )

    # -- Gemini Live ----------------------------------------------------------

    #: Gemini Live has no client cancel, no output truncate, and no
    #: conversation-item delete, so RESPONSE_CANCELLATION, OUTPUT_TRUNCATION and
    #: DYNAMIC_CONTEXT are all absent -- the host drops playback locally on
    #: barge-in instead of being handed a truncation that never happened.
    #: EXPLICIT_RESPONSE is absent too: the wire carries no per-response
    #: metadata, so create_response(metadata=...) could only discard it.
    GEMINI_CAPABILITIES = frozenset(
        {
            RealtimeCapability.TOOL_CALLING,
            RealtimeCapability.INPUT_TRANSCRIPTION,
            RealtimeCapability.OUTPUT_TRANSCRIPTION,
            RealtimeCapability.TOOL_CALL_CANCELLATION,
        }
    )

    class TalkGeminiCoreProvider(_TalkCoreProvider):
        """Google Gemini Live.

        Audio note: this session's ``input_audio_format`` is 24 kHz, not the
        16 kHz Live itself wants, because ``send_audio`` feeds hermes-talk's
        session -- which owns the 24 kHz -> 16 kHz downsample
        (``Pcm24To16Resampler``). Declaring 16 kHz here would hand that
        resampler audio it would then convert a second time.
        """

        capabilities = GEMINI_CAPABILITIES
        provider_name = GEMINI_PROVIDER_NAME
        provider_display_name = "Gemini Live (hermes-talk)"
        provider_tag = "gemini live speech-to-speech"
        env_key = "GEMINI_API_KEY"
        env_url = "https://aistudio.google.com/apikey"

        def default_model(self) -> str | None:
            return talk_config.talk_gemini_model() or talk_config.DEFAULT_GEMINI_MODEL

        def default_voice(self) -> str | None:
            return talk_config.talk_gemini_voice() or talk_config.DEFAULT_GEMINI_VOICE

        def list_models(self) -> tuple[Mapping[str, Any], ...]:
            return ({"id": talk_config.DEFAULT_GEMINI_MODEL, "display": "Gemini Live"},)

        def list_voices(self) -> tuple[Mapping[str, Any], ...]:
            return tuple(
                {"id": voice, "display": voice}
                for voice in talk_config.GEMINI_LIVE_VOICES
            )

        def is_available(self) -> bool:
            """Offline: pure environment reads, no file and no network I/O.

            The resolved key is only ever tested for truth and dropped; it is
            never held, returned, or logged.
            """

            try:
                self.default_voice()
                model = self.default_model()
                configured = bool(talk_config.resolve_gemini_key())
            except Exception:  # readiness never raises
                logger.debug("hermes-talk/gemini readiness probe failed", exc_info=True)
                return False
            return bool(model and configured)

        def _resolve_auth(self) -> Any:
            return talk_auth.TalkAuth(
                token=talk_config.resolve_gemini_key(),
                source=talk_auth.SOURCE_ENV,
                detail="Gemini API key",
            )

        def _build_session(self, auth: Any) -> Any:
            return talk_gemini_realtime.GeminiRealtimeSession(
                auth_token=auth.token, auth_source=auth.source
            )

    def build_providers() -> tuple[RealtimeVoiceProvider, ...]:
        """Every hermes-talk lane, as core providers, in registration order."""

        return (
            TalkOpenAICoreProvider(),
            TalkGrokCoreProvider(),
            TalkGeminiCoreProvider(),
        )

else:  # pragma: no cover - exercised by the core-absent subprocess test
    # Null objects so ``import talk_core_provider`` is always safe and
    # ``register(ctx)`` can degrade without a try/except at the call site.
    translate_event = None  # type: ignore[assignment]
    TalkCoreSession = None  # type: ignore[assignment]
    TalkOpenAICoreProvider = None  # type: ignore[assignment]
    TalkGrokCoreProvider = None  # type: ignore[assignment]
    TalkGeminiCoreProvider = None  # type: ignore[assignment]
    OPENAI_CAPABILITIES = frozenset()
    GROK_CAPABILITIES = frozenset()
    GEMINI_CAPABILITIES = frozenset()

    def build_providers() -> tuple[Any, ...]:
        """No contract, no providers -- never an exception."""

        return ()


__all__ = [
    "GEMINI_CAPABILITIES",
    "GEMINI_PROVIDER_NAME",
    "GROK_CAPABILITIES",
    "GROK_PROVIDER_NAME",
    "OPENAI_CAPABILITIES",
    "OPENAI_PROVIDER_NAME",
    "PROVIDER_NAMES",
    "TalkCoreSession",
    "TalkGeminiCoreProvider",
    "TalkGrokCoreProvider",
    "TalkOpenAICoreProvider",
    "build_providers",
    "core_contract_diagnostic",
    "redact",
    "translate_event",
    "turn_detection_available",
]
