"""GPT-Live configuration and explicit subscription/API credential selection."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

try:
    from . import talk_auth
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_auth

SUBSCRIPTION_MODELS = ("gpt-live-1-codex", "gpt-live-1-boulder-alpha")
API_MODELS = ("gpt-live-1",)
# OpenClaw 76378ddb, extensions/openai/realtime-quicksilver.ts (MIT).
SUBSCRIPTION_VOICES = (
    "arbor",
    "breeze",
    "cove",
    "ember",
    "juniper",
    "maple",
    "sol",
    "spruce",
    "vale",
)
# OpenAI Live BuiltInVoice schema, checked 2026-09-12.
API_VOICES = (
    "alloy",
    "ash",
    "ballad",
    "beacon",
    "bossa",
    "cedar",
    "cinder",
    "coral",
    "delta",
    "echo",
    "gleam",
    "marin",
    "meridian",
    "quartz",
    "ripple",
    "sage",
    "shimmer",
    "stone",
    "tempo",
    "verse",
    "vesper",
    "willow",
)


class LiveConfigError(ValueError):
    """An explicit Live setting is invalid."""


@dataclass(frozen=True, slots=True)
class LiveConfig:
    auth_mode: str = "subscription"
    model: str = ""
    voice: str = ""

    def __post_init__(self) -> None:
        if self.auth_mode not in {"subscription", "api"}:
            raise LiveConfigError("TALK_LIVE_AUTH must be subscription or api")
        subscription = self.auth_mode == "subscription"
        models = SUBSCRIPTION_MODELS if subscription else API_MODELS
        voices = SUBSCRIPTION_VOICES if subscription else API_VOICES
        model = self.model or models[0]
        voice = self.voice or ("cove" if subscription else "marin")
        if model not in models:
            raise LiveConfigError(f"TALK_LIVE_MODEL is unsupported for {self.auth_mode} auth")
        if voice not in voices:
            raise LiveConfigError(f"TALK_LIVE_VOICE is unsupported for {self.auth_mode} auth")
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "voice", voice)


def resolve_live_config(env: Mapping[str, str] | None = None) -> LiveConfig:
    source = os.environ if env is None else env
    values = {}
    for key, field_name in (
        ("TALK_LIVE_AUTH", "auth_mode"),
        ("TALK_LIVE_MODEL", "model"),
        ("TALK_LIVE_VOICE", "voice"),
    ):
        if key in source:
            value = str(source[key]).strip().lower()
            if not value:
                raise LiveConfigError(f"{key} is set but empty")
            values[field_name] = value
    return LiveConfig(**values)


def validate_live_auth(auth: talk_auth.TalkAuth, config: LiveConfig) -> None:
    if (
        not auth.token
        or auth.token != auth.token.strip()
        or any(character in auth.token for character in "\r\n")
    ):
        raise talk_auth.TalkAuthError("GPT-Live credential is empty or invalid")
    if config.auth_mode == "subscription":
        if auth.source != talk_auth.SOURCE_CODEX_OAUTH or not auth.account_id:
            raise talk_auth.TalkAuthError(
                "GPT-Live subscription requires a Codex OAuth login with an account ID; "
                "run `codex login`. API credentials were not used."
            )
        if auth.account_id != auth.account_id.strip() or any(
            character in auth.account_id for character in "\r\n"
        ):
            raise talk_auth.TalkAuthError("Codex OAuth account ID is invalid")
    elif auth.source not in {talk_auth.SOURCE_CONFIGURED, talk_auth.SOURCE_ENV}:
        raise talk_auth.TalkAuthError("GPT-Live API mode requires an explicitly configured API key")


def resolve_live_auth(
    *,
    env: Mapping[str, str] | None = None,
    codex_home: Path | None = None,
    config: LiveConfig | None = None,
) -> talk_auth.TalkAuth:
    source = os.environ if env is None else env
    config = config or resolve_live_config(source)
    if config.auth_mode == "subscription":
        selected_home = codex_home
        if selected_home is None and source.get("CODEX_HOME", "").strip():
            selected_home = Path(source["CODEX_HOME"].strip())
        try:
            auth = talk_auth._resolve_codex_oauth(selected_home)
        except talk_auth.TalkAuthError:
            raise talk_auth.TalkAuthError(
                "GPT-Live subscription authentication failed; run `codex login`. "
                "API credentials were not used."
            ) from None
        if auth is None:
            raise talk_auth.TalkAuthError(
                "GPT-Live subscription needs `codex login`; API credentials were not used."
            )
    else:
        auth = None
        for key, lane in (
            ("TALK_OPENAI_API_KEY", talk_auth.SOURCE_CONFIGURED),
            ("OPENAI_API_KEY", talk_auth.SOURCE_ENV),
        ):
            if key not in source:
                continue
            token = source[key].strip()
            if not token:
                raise talk_auth.TalkAuthError(f"{key} is set but empty; GPT-Live API auth stopped")
            auth = talk_auth.TalkAuth(token=token, source=lane, detail=key)
            break
        if auth is None:
            raise talk_auth.TalkAuthError(
                "GPT-Live API mode needs TALK_OPENAI_API_KEY or OPENAI_API_KEY; "
                "Codex OAuth was not used."
            )
    validate_live_auth(auth, config)
    return auth


__all__ = [
    "API_MODELS",
    "API_VOICES",
    "SUBSCRIPTION_MODELS",
    "SUBSCRIPTION_VOICES",
    "LiveConfig",
    "LiveConfigError",
    "resolve_live_auth",
    "resolve_live_config",
    "validate_live_auth",
]
