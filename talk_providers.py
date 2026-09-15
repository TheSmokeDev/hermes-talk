"""OpenAI TTS and transcription providers for Hermes's pluggable backends.

Bonus surface, not the point of this plugin: hermes-talk already resolves an
OpenAI Platform key, so it can also service Hermes's turn-based speech hooks.
The ABCs are imported defensively — the plugin must import and test green on a
machine with no hermes-agent installed, and :func:`providers_available` is what
``register(ctx)`` checks before offering them.

The provider names are deliberately NOT ``openai``: Hermes ships built-in
providers under that name for both TTS and STT, and built-ins always win —
registration of a shadowing name is rejected.
"""

from __future__ import annotations

from typing import Any

import httpx

try:
    from . import talk_config
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_config

try:
    from agent.transcription_provider import TranscriptionProvider as _STTBase
    from agent.tts_provider import TTSProvider as _TTSBase

    _ABCS_IMPORTED = True
except Exception:  # noqa: BLE001 - hermes-agent absent or partially installed
    _STTBase = object  # type: ignore[assignment,misc]
    _TTSBase = object  # type: ignore[assignment,misc]
    _ABCS_IMPORTED = False

PROVIDER_NAME = "talk_openai"
SPEECH_URL = "https://api.openai.com/v1/audio/speech"
TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_STT_MODEL = "gpt-4o-mini-transcribe"
REQUEST_TIMEOUT_S = 120.0


def providers_available() -> bool:
    """True when Hermes's provider ABCs were importable."""

    return _ABCS_IMPORTED


class OpenAITTSProvider(_TTSBase):  # type: ignore[misc,valid-type]
    """Speech synthesis through OpenAI's ``/v1/audio/speech``."""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "OpenAI (hermes-talk)"

    def is_available(self) -> bool:
        try:
            talk_config.resolve_openai_key()
        except talk_config.TalkConfigError:
            return False
        return True

    def list_voices(self) -> list[dict[str, Any]]:
        return [{"id": voice} for voice in talk_config.OPENAI_REALTIME_VOICES]

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {"id": DEFAULT_TTS_MODEL, "display": "GPT-4o mini TTS"},
            {"id": "tts-1", "display": "TTS-1"},
            {"id": "tts-1-hd", "display": "TTS-1 HD"},
        ]

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "OpenAI TTS (hermes-talk)",
            "badge": "paid",
            "tag": "Shares the Talk OpenAI key",
            "env_vars": [
                {
                    "key": "OPENAI_API_KEY",
                    "prompt": "OpenAI API key",
                    "url": "https://platform.openai.com/api-keys",
                }
            ],
        }

    @property
    def voice_compatible(self) -> bool:
        return True

    # Defined here rather than inherited: off-host the base class is ``object``.
    def default_model(self) -> str | None:
        return DEFAULT_TTS_MODEL

    def default_voice(self) -> str | None:
        return talk_config.DEFAULT_TALK_VOICE

    def synthesize(
        self,
        text: str,
        output_path: str,
        *,
        voice: str | None = None,
        model: str | None = None,
        speed: float | None = None,
        format: str = "mp3",
        **extra: Any,
    ) -> str:
        """Write synthesized audio to ``output_path``. Raises on failure."""

        payload: dict[str, Any] = {
            "model": model or DEFAULT_TTS_MODEL,
            "input": text,
            "voice": voice or self.default_voice(),
            "response_format": format,
        }
        if speed is not None:
            payload["speed"] = speed
        response = httpx.post(
            SPEECH_URL,
            json=payload,
            headers={"Authorization": f"Bearer {talk_config.resolve_openai_key()}"},
            timeout=REQUEST_TIMEOUT_S,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"OpenAI speech failed ({response.status_code}): {response.text[:300]}"
            )
        with open(output_path, "wb") as handle:
            handle.write(response.content)
        return output_path


class OpenAITranscriptionProvider(_STTBase):  # type: ignore[misc,valid-type]
    """Transcription through OpenAI's ``/v1/audio/transcriptions``."""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "OpenAI (hermes-talk)"

    def is_available(self) -> bool:
        try:
            talk_config.resolve_openai_key()
        except talk_config.TalkConfigError:
            return False
        return True

    def list_models(self) -> list[dict[str, Any]]:
        return [
            {"id": DEFAULT_STT_MODEL, "display": "GPT-4o mini transcribe"},
            {"id": "whisper-1", "display": "Whisper v1"},
        ]

    # Defined here rather than inherited: off-host the base class is ``object``.
    def default_model(self) -> str | None:
        return DEFAULT_STT_MODEL

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "OpenAI STT (hermes-talk)",
            "badge": "paid",
            "tag": "Shares the Talk OpenAI key",
            "env_vars": [
                {
                    "key": "OPENAI_API_KEY",
                    "prompt": "OpenAI API key",
                    "url": "https://platform.openai.com/api-keys",
                }
            ],
        }

    def transcribe(
        self,
        file_path: str,
        *,
        model: str | None = None,
        language: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Transcribe ``file_path``. Returns the envelope; never raises."""

        data: dict[str, Any] = {"model": model or DEFAULT_STT_MODEL}
        if language:
            data["language"] = language
        try:
            with open(file_path, "rb") as handle:
                response = httpx.post(
                    TRANSCRIPTIONS_URL,
                    data=data,
                    files={"file": (file_path, handle)},
                    headers={"Authorization": f"Bearer {talk_config.resolve_openai_key()}"},
                    timeout=REQUEST_TIMEOUT_S,
                )
            if response.status_code != 200:
                raise RuntimeError(
                    f"OpenAI transcription failed ({response.status_code}): "
                    f"{response.text[:300]}"
                )
            transcript = str(response.json().get("text") or "")
        except Exception as exc:  # noqa: BLE001 — the contract is an envelope
            return {
                "success": False,
                "transcript": "",
                "error": f"{type(exc).__name__}: {exc}",
                "provider": PROVIDER_NAME,
            }
        return {"success": True, "transcript": transcript, "provider": PROVIDER_NAME}


__all__ = [
    "DEFAULT_STT_MODEL",
    "DEFAULT_TTS_MODEL",
    "PROVIDER_NAME",
    "SPEECH_URL",
    "TRANSCRIPTIONS_URL",
    "OpenAITTSProvider",
    "OpenAITranscriptionProvider",
    "providers_available",
]
