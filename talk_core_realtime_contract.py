"""Hermes #95147 realtime voice provider adapter.

The transport remains plugin-owned. This adapter only maps Talk's existing
provider-neutral session onto Hermes's host-owned coordinator contract.
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
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
    from . import talk_auth, talk_config, talk_gemini_auth, talk_grok_auth
    from . import talk_realtime as rt
    from .talk_gemini_realtime import GeminiRealtimeSession
    from .talk_grok_realtime import GrokRealtimeSession
    from .talk_openai_realtime import OpenAIRealtimeSession
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_auth
    import talk_config
    import talk_gemini_auth
    import talk_grok_auth
    import talk_realtime as rt
    from talk_gemini_realtime import GeminiRealtimeSession
    from talk_grok_realtime import GrokRealtimeSession
    from talk_openai_realtime import OpenAIRealtimeSession

OPENAI_PROVIDER_NAME = "talk_openai_realtime"
GROK_PROVIDER_NAME = "talk_grok_realtime"
PROVIDER_NAME = OPENAI_PROVIDER_NAME
MAX_SETTLED_IDENTIFIERS = 256
GEMINI_PROVIDER_NAME = "talk_gemini_realtime"


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
        self._tool_lock = asyncio.Lock()
        self._pending_tool_calls: list[str] = []
        self._tool_call_responses: dict[str, str | None] = {}
        self._tool_outputs: dict[str, str] = {}
        self._completed_tool_calls: OrderedDict[str, None] = OrderedDict()
        self._response_finished = False
        self._response_in_flight = False
        self._active_response_id: str | None = None
        self._unnamed_response_cancelled = False
        self._settled_response_ids: OrderedDict[str, None] = OrderedDict()

    async def send_audio(self, pcm: bytes) -> None:
        await self._session.send((rt.AppendInputAudio(bytes(pcm)),))

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        async for event in self._session:
            mapped = self._map_event(event)
            if isinstance(event, rt.ResponseFinished):
                async with self._tool_lock:
                    await self._flush_tool_results()
            if mapped is not None:
                yield mapped

    def _map_event(self, event: rt.RealtimeEvent) -> RealtimeEvent | None:
        if isinstance(event, rt.SessionReady):
            return RealtimeEvent(
                type=RealtimeEventType.SESSION_READY,
                session_id=event.session_id,
            )
        if isinstance(event, rt.OutputAudio):
            if not self._belongs_to_active(event.response_id):
                return None
            return RealtimeEvent.audio(event.data, item_id=event.item_id)
        if isinstance(event, rt.Transcript):
            if (
                event.role is rt.TranscriptRole.ASSISTANT
                and not self._belongs_to_active(event.response_id)
            ):
                return None
            return RealtimeEvent.transcript(
                event.text,
                final=event.final,
                role=event.role.value,
            )
        if isinstance(event, rt.FunctionCall):
            if (
                event.call_id in self._completed_tool_calls
                or event.call_id in self._pending_tool_calls
                or not self._belongs_to_active(event.response_id)
            ):
                return None
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
            self._pending_tool_calls.append(event.call_id)
            self._tool_call_responses[event.call_id] = event.response_id
            return RealtimeEvent.tool_call(event.call_id, event.name, arguments)
        if isinstance(event, rt.SpeechStarted):
            return RealtimeEvent(type=RealtimeEventType.TURN_STARTED, role="user")
        if isinstance(event, rt.SpeechStopped):
            return RealtimeEvent(type=RealtimeEventType.TURN_ENDED, role="user")
        if isinstance(event, rt.ResponseStarted):
            if not self._start_response(event.response_id):
                return None
            return RealtimeEvent(type=RealtimeEventType.TURN_STARTED, role="assistant")
        if isinstance(event, rt.ResponseFinished):
            if not self._finish_response(event.response_id):
                return None
            self._response_finished = True
            return RealtimeEvent(type=RealtimeEventType.TURN_ENDED, role="assistant")
        if isinstance(event, rt.ProviderFailure):
            return RealtimeEvent(
                type=(
                    RealtimeEventType.ERROR
                    if event.terminal
                    else RealtimeEventType.WARNING
                ),
                text=event.detail,
            )
        if isinstance(event, rt.SessionTerminated):
            if event.state is rt.SessionState.FAILED:
                return RealtimeEvent(type=RealtimeEventType.ERROR, text=event.detail)
            if event.state is rt.SessionState.CLOSED and not self._closed:
                return RealtimeEvent(
                    type=RealtimeEventType.ERROR,
                    text=event.detail or "realtime voice provider closed unexpectedly",
                )
        return None

    async def submit_tool_result(self, call_id: str, output: str) -> None:
        async with self._tool_lock:
            if call_id in self._completed_tool_calls:
                return
            if call_id not in self._pending_tool_calls:
                raise ValueError(f"unknown realtime tool call {call_id!r}")
            self._tool_outputs[call_id] = output
            await self._flush_tool_results()

    async def _flush_tool_results(self) -> None:
        if (
            not self._response_finished
            or not self._pending_tool_calls
            or any(call_id not in self._tool_outputs for call_id in self._pending_tool_calls)
        ):
            return
        commands = (
            *(
                rt.SubmitToolResult(
                    call_id=call_id,
                    output=self._tool_outputs[call_id],
                )
                for call_id in self._pending_tool_calls
            ),
            rt.StartResponse(),
        )
        await self._session.send(commands)
        for call_id in self._pending_tool_calls:
            self._mark_tool_call_completed(call_id)
        self._pending_tool_calls.clear()
        self._tool_call_responses.clear()
        self._tool_outputs.clear()
        self._response_finished = False

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
        if self._cancel_active_response():
            await self._session.send((rt.CancelResponse(),))

    def _belongs_to_active(self, response_id: str | None) -> bool:
        if response_id is None:
            return not self._unnamed_response_cancelled
        if response_id in self._settled_response_ids:
            return False
        return self._active_response_id is None or response_id == self._active_response_id

    def _settle_response(self, response_id: str) -> None:
        if response_id in self._settled_response_ids:
            return
        self._settled_response_ids[response_id] = None
        if len(self._settled_response_ids) > MAX_SETTLED_IDENTIFIERS:
            self._settled_response_ids.popitem(last=False)

    def _start_response(self, response_id: str | None) -> bool:
        if response_id is not None and response_id in self._settled_response_ids:
            return False
        if (
            response_id is None
            and self._response_in_flight
            and self._active_response_id is not None
        ):
            return False
        self._unnamed_response_cancelled = False
        self._response_in_flight = True
        self._active_response_id = response_id
        self._response_finished = False
        return True

    def _cancel_active_response(self) -> bool:
        if not self._response_in_flight:
            return False
        if self._active_response_id is not None:
            if self._active_response_id in self._settled_response_ids:
                return False
            self._settle_response(self._active_response_id)
        elif self._unnamed_response_cancelled:
            return False
        else:
            self._unnamed_response_cancelled = True
        self._abandon_tool_calls(self._active_response_id)
        self._response_finished = False
        return True

    def _finish_response(self, response_id: str | None) -> bool:
        eligible = self._belongs_to_active(response_id)
        if response_id is not None:
            self._settle_response(response_id)
        if (
            response_id is not None
            and self._active_response_id is not None
            and response_id != self._active_response_id
        ):
            return False
        self._response_in_flight = False
        self._active_response_id = None
        self._unnamed_response_cancelled = False
        return eligible

    def _abandon_tool_calls(self, response_id: str | None) -> None:
        abandoned = [
            call_id
            for call_id in self._pending_tool_calls
            if self._tool_call_responses.get(call_id) == response_id
        ]
        for call_id in abandoned:
            self._pending_tool_calls.remove(call_id)
            self._tool_call_responses.pop(call_id, None)
            self._tool_outputs.pop(call_id, None)
            self._mark_tool_call_completed(call_id)

    def _mark_tool_call_completed(self, call_id: str) -> None:
        self._completed_tool_calls[call_id] = None
        self._completed_tool_calls.move_to_end(call_id)
        if len(self._completed_tool_calls) > MAX_SETTLED_IDENTIFIERS:
            self._completed_tool_calls.popitem(last=False)

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


class TalkGrokRealtimeProvider(_TalkRealtimeProvider):
    """Plugin-owned xAI Grok duplex transport for the Hermes #95147 seam."""

    def __init__(
        self,
        *,
        auth_resolver: Callable[[], Any] | None = None,
        session_factory: Callable[..., Any] = GrokRealtimeSession,
    ) -> None:
        super().__init__(
            name=GROK_PROVIDER_NAME,
            display_name="Hermes Talk Grok Realtime",
            auth_resolver=auth_resolver or talk_grok_auth.resolve_grok_auth,
            session_factory=session_factory,
            model_resolver=talk_config.talk_grok_model,
            voice_resolver=talk_config.talk_grok_voice,
        )


class TalkGeminiRealtimeProvider(_TalkRealtimeProvider):
    """Plugin-owned Gemini Live transport for the Hermes #95147 seam."""

    def __init__(
        self,
        *,
        auth_resolver: Callable[[], Any] = talk_gemini_auth.resolve_gemini_auth,
        session_factory: Callable[..., Any] = GeminiRealtimeSession,
    ) -> None:
        super().__init__(
            name=GEMINI_PROVIDER_NAME,
            display_name="Hermes Talk Gemini Live",
            auth_resolver=auth_resolver,
            session_factory=session_factory,
            model_resolver=talk_config.talk_gemini_model,
            voice_resolver=talk_config.talk_gemini_voice,
        )


def configured_provider() -> RealtimeVoiceProvider:
    """Build the provider selected by TALK_PROVIDER for this invocation."""

    provider = talk_config.talk_provider()
    if provider == "openai":
        return TalkOpenAIRealtimeProvider()
    if provider == "grok":
        return TalkGrokRealtimeProvider()
    if provider == "gemini":
        return TalkGeminiRealtimeProvider()
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
    if provider == "gemini":
        return GEMINI_PROVIDER_NAME
    raise talk_config.TalkConfigError(
        f"Hermes #95147 terminal voice does not support provider {provider!r}"
    )


__all__ = [
    "GEMINI_PROVIDER_NAME",
    "GROK_PROVIDER_NAME",
    "OPENAI_PROVIDER_NAME",
    "PROVIDER_NAME",
    "TalkGeminiRealtimeProvider",
    "TalkGrokRealtimeProvider",
    "TalkOpenAIRealtimeProvider",
    "TalkRealtimeSession",
    "configured_provider",
    "configured_provider_name",
]
