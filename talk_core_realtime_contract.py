"""Hermes #95147 realtime voice provider adapter.

The transport remains plugin-owned. This adapter only maps Talk's existing
provider-neutral session onto Hermes's host-owned coordinator contract.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from agent.realtime_voice import (
    HeardAudioBoundary,
    RealtimeEvent,
    RealtimeEventType,
    RealtimeSession,
    RealtimeVoiceProvider,
)

try:
    from . import talk_auth, talk_config
    from . import talk_realtime as rt
    from .talk_grok_realtime import GrokRealtimeSession
    from .talk_openai_realtime import OpenAIRealtimeSession
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_auth
    import talk_config
    import talk_realtime as rt
    from talk_grok_realtime import GrokRealtimeSession
    from talk_openai_realtime import OpenAIRealtimeSession

OPENAI_PROVIDER_NAME = "talk_openai_realtime"
GROK_PROVIDER_NAME = "talk_grok_realtime"
PROVIDER_NAME = OPENAI_PROVIDER_NAME


def _tool_definition(value: Mapping[str, Any]) -> rt.ToolDefinition:
    function = value.get("function")
    source = function if isinstance(function, Mapping) else value
    name = source.get("name")
    description = source.get("description", "")
    parameters = source.get("parameters", {"type": "object", "properties": {}})
    if not isinstance(name, str) or not name.strip():
        raise ValueError("realtime tool definitions require a non-empty name")
    if not isinstance(description, str):
        raise TypeError("realtime tool descriptions must be strings")
    if not isinstance(parameters, Mapping):
        raise TypeError("realtime tool parameters must be an object")
    return rt.ToolDefinition(
        name=name,
        description=description,
        parameters=dict(parameters),
    )


class TalkRealtimeSession(RealtimeSession):
    """Translate one Talk session into the ordered Hermes event contract."""

    def __init__(self, session) -> None:
        self._session = session
        self._closed = False

    async def send_audio(self, pcm: bytes) -> None:
        await self._session.send((rt.AppendInputAudio(bytes(pcm)),))

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        async for event in self._session:
            mapped = self._map_event(event)
            if mapped is not None:
                yield mapped

    def _map_event(self, event: rt.RealtimeEvent) -> RealtimeEvent | None:
        if isinstance(event, rt.OutputAudio):
            return RealtimeEvent.audio(event.data, item_id=event.item_id)
        if isinstance(event, rt.Transcript):
            return RealtimeEvent.transcript(
                event.text,
                final=event.final,
                role=event.role.value,
            )
        if isinstance(event, rt.FunctionCall):
            try:
                arguments = json.loads(event.arguments)
            except json.JSONDecodeError as exc:
                return RealtimeEvent(
                    type=RealtimeEventType.ERROR,
                    text=f"invalid tool arguments: {exc.msg}",
                )
            if not isinstance(arguments, dict):
                return RealtimeEvent(
                    type=RealtimeEventType.ERROR,
                    text="invalid tool arguments: expected an object",
                )
            return RealtimeEvent.tool_call(event.call_id, event.name, arguments)
        if isinstance(event, rt.SpeechStarted):
            return RealtimeEvent(type=RealtimeEventType.TURN_STARTED, role="user")
        if isinstance(event, rt.SpeechStopped):
            return RealtimeEvent(type=RealtimeEventType.TURN_ENDED, role="user")
        if isinstance(event, rt.ResponseStarted):
            return RealtimeEvent(type=RealtimeEventType.TURN_STARTED, role="assistant")
        if isinstance(event, rt.ResponseFinished):
            return RealtimeEvent(type=RealtimeEventType.TURN_ENDED, role="assistant")
        if isinstance(event, rt.ProviderFailure):
            return RealtimeEvent(type=RealtimeEventType.ERROR, text=event.detail)
        if isinstance(event, rt.SessionTerminated) and event.state is rt.SessionState.FAILED:
            return RealtimeEvent(type=RealtimeEventType.ERROR, text=event.detail)
        return None

    async def submit_tool_result(self, call_id: str, output: str) -> None:
        await self._session.send(
            (
                rt.SubmitToolResult(call_id=call_id, output=output),
                rt.StartResponse(),
            )
        )

    async def add_context(self, item_id: str, text: str) -> None:
        await self._session.send(
            (
                rt.AddContext(
                    item_id=item_id,
                    text=text,
                    role=rt.ContextRole.SYSTEM,
                ),
            )
        )

    async def truncate_response(self, boundary: HeardAudioBoundary) -> None:
        await self._session.send(
            (
                rt.TruncateOutput(
                    item_id=boundary.item_id,
                    audio_end_ms=boundary.audio_end_ms,
                ),
            )
        )

    async def cancel_response(self) -> None:
        await self._session.send((rt.CancelResponse(),))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._session.close()


class _TalkRealtimeProvider(RealtimeVoiceProvider):
    """Shared #95147 mapping around one plugin-owned provider transport."""

    def __init__(
        self,
        *,
        name: str,
        display_name: str,
        auth_resolver: Callable[[], Any],
        session_factory: Callable[..., Any],
        model_resolver: Callable[[], str],
        voice_resolver: Callable[[], str],
    ) -> None:
        self._name = name
        self._display_name = display_name
        self._auth_resolver = auth_resolver
        self._session_factory = session_factory
        self._model_resolver = model_resolver
        self._voice_resolver = voice_resolver

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return self._display_name

    def is_available(self) -> bool:
        try:
            import aiohttp  # noqa: F401 - passive dependency probe

            return bool(self._model_resolver() and self._auth_resolver().token)
        except Exception:  # noqa: BLE001 - passive readiness must not escape
            return False

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "duplex",
            "tag": "plugin",
            "env_vars": [],
        }

    async def open_session(
        self,
        *,
        instructions: str,
        tools: list[dict[str, Any]],
        voice: str | None = None,
    ) -> RealtimeSession:
        auth = self._auth_resolver()
        session = self._session_factory(auth_token=auth.token, auth_source=auth.source)
        setup = rt.SessionSetup(
            model=self._model_resolver(),
            voice=voice or self._voice_resolver(),
            instructions=instructions,
            tools=tuple(_tool_definition(tool) for tool in tools),
            automatic_response=True,
        )
        try:
            await session.connect(setup)
        except BaseException:
            await session.close()
            raise
        return TalkRealtimeSession(session)


class TalkOpenAIRealtimeProvider(_TalkRealtimeProvider):
    """Plugin-owned OpenAI duplex transport for the Hermes #95147 seam."""

    def __init__(
        self,
        *,
        auth_resolver: Callable[[], Any] = talk_auth.resolve_auth,
        session_factory: Callable[..., Any] = OpenAIRealtimeSession,
    ) -> None:
        super().__init__(
            name=OPENAI_PROVIDER_NAME,
            display_name="Hermes Talk OpenAI Realtime",
            auth_resolver=auth_resolver,
            session_factory=session_factory,
            model_resolver=talk_config.talk_model,
            voice_resolver=talk_config.talk_voice,
        )


def _grok_auth() -> talk_auth.TalkAuth:
    scoped = bool((os.environ.get("TALK_XAI_API_KEY") or "").strip())
    return talk_auth.TalkAuth(
        token=talk_config.resolve_xai_key(),
        source=talk_auth.SOURCE_CONFIGURED if scoped else talk_auth.SOURCE_ENV,
        detail="TALK_XAI_API_KEY" if scoped else "XAI_API_KEY",
    )


class TalkGrokRealtimeProvider(_TalkRealtimeProvider):
    """Plugin-owned xAI Grok duplex transport for the Hermes #95147 seam."""

    def __init__(
        self,
        *,
        auth_resolver: Callable[[], Any] = _grok_auth,
        session_factory: Callable[..., Any] = GrokRealtimeSession,
    ) -> None:
        super().__init__(
            name=GROK_PROVIDER_NAME,
            display_name="Hermes Talk Grok Realtime",
            auth_resolver=auth_resolver,
            session_factory=session_factory,
            model_resolver=talk_config.talk_grok_model,
            voice_resolver=talk_config.talk_grok_voice,
        )


def configured_provider() -> RealtimeVoiceProvider:
    """Build the provider selected by TALK_PROVIDER for this invocation."""

    provider = talk_config.talk_provider()
    if provider == "openai":
        return TalkOpenAIRealtimeProvider()
    if provider == "grok":
        return TalkGrokRealtimeProvider()
    raise talk_config.TalkConfigError(
        f"Hermes #95147 terminal voice does not support provider {provider!r}"
    )


def configured_provider_name() -> str:
    """Return the #95147 registry key for the configured provider."""

    provider = talk_config.talk_provider()
    if provider == "openai":
        return OPENAI_PROVIDER_NAME
    if provider == "grok":
        return GROK_PROVIDER_NAME
    raise talk_config.TalkConfigError(
        f"Hermes #95147 terminal voice does not support provider {provider!r}"
    )


__all__ = [
    "GROK_PROVIDER_NAME",
    "OPENAI_PROVIDER_NAME",
    "PROVIDER_NAME",
    "TalkGrokRealtimeProvider",
    "TalkOpenAIRealtimeProvider",
    "TalkRealtimeSession",
    "configured_provider",
    "configured_provider_name",
]
