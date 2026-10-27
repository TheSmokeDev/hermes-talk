"""Self-hosted OpenAI-Realtime-compatible servers behind the neutral session contract.

A local server (huggingface/speech-to-speech, LocalAI, vLLM realtime) speaks
the OpenAI GA vocabulary, so this lane reuses the OpenAI adapter's encoder,
decoder and wire session rather than duplicating them. Only the edges differ:

- No ephemeral mint: there is no ``client_secrets`` endpoint, so the optional
  ``TALK_LOCAL_API_KEY`` is the socket bearer, and no key means no header.
- The socket URL is the operator's (``TALK_LOCAL_URL``), never OpenAI's.
- The session update drops OpenAI-hosted ids the server cannot resolve: the
  ``gpt-4o-transcribe`` transcription model and OpenAI noise reduction. The
  ``default`` voice is omitted so the server keeps its own speaker.

Probe ground truth (huggingface/speech-to-speech 1.1.0 on Windows, RTX 3090,
2026-10-26): flat function tools round-trip through
``response.function_call_arguments.done`` -> ``function_call_output`` ->
``response.create``; ``response.cancel`` plus ``conversation.item.truncate``
end the response with status ``cancelled`` and no error. The server holds one
session per pool slot and refuses extra sockets with close code 1008.
"""

from __future__ import annotations

from typing import Any

try:
    from . import talk_config, talk_openai_realtime, talk_wire
    from . import talk_realtime as rt
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_config
    import talk_openai_realtime
    import talk_realtime as rt
    import talk_wire


def build_session_update(setup: rt.SessionSetup) -> dict[str, Any]:
    """The OpenAI GA update minus the OpenAI-hosted ids a local server lacks."""

    update = talk_openai_realtime.build_session_update(setup)
    session = update["session"]
    audio = session.get("audio", {})
    audio_input = audio.get("input", {})
    audio_input.pop("transcription", None)
    audio_input.pop("noise_reduction", None)
    output = audio.get("output")
    server_default = (None, "", talk_config.DEFAULT_LOCAL_VOICE)
    if isinstance(output, dict) and output.get("voice") in server_default:
        output.pop("voice", None)
        if not output:
            audio.pop("output")
    return update


class LocalRealtimeSession(talk_openai_realtime.OpenAIRealtimeSession):
    """OpenAI Realtime wire against a self-hosted server URL, without a mint."""

    def __init__(
        self,
        *,
        auth_token: str,
        auth_source: str,
        ws_url: str,
        aiohttp_module=None,
    ) -> None:
        super().__init__(
            auth_token=auth_token,
            auth_source=auth_source,
            aiohttp_module=aiohttp_module,
            mint_session=self._descriptor,
            ws_url=ws_url,
        )
        self._ws_url = ws_url

    def _descriptor(self, setup: rt.SessionSetup) -> talk_wire.TalkSessionDescriptor:
        # The raw local key (possibly empty) stands in for the minted secret.
        return talk_wire.TalkSessionDescriptor(
            client_secret=self.auth_token,
            expires_at_ms=None,
            offer_url=self._ws_url,
            model=setup.model,
            voice=setup.voice,
        )

    def _session_update(self, setup: rt.SessionSetup) -> dict[str, Any]:
        return build_session_update(setup)


__all__ = ["LocalRealtimeSession", "build_session_update"]
