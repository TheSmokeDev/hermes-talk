"""GPT-Live configuration and explicit subscription/API credential selection."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

try:
    from . import talk_auth, talk_config
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_auth
    import talk_config

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
    mode = values.get("auth_mode", "subscription")
    if mode in {"subscription", "api"}:
        for field_name in ("model", "voice"):
            key = f"TALK_LIVE_{mode.upper()}_{field_name.upper()}"
            if key in source:
                value = str(source[key]).strip().lower()
                if not value:
                    raise LiveConfigError(f"{key} is set but empty")
                values[field_name] = value
    return LiveConfig(**values)


SOURCE_CODEX_APP_SERVER = "codex-app-server"


def _codex_cli_auth(source: Mapping[str, str]) -> talk_auth.TalkAuth:
    """The subscription lane: the Codex CLI's own login, driven through its app-server.

    Talk holds no token on this lane. The CLI's presence is checked here; its
    ChatGPT sign-in is confirmed by the app-server when the call starts.
    """

    try:
        from .talk_live_codex import INSTALL_HINT, codex_binary
    except ImportError:  # pragma: no cover - flat-module fallback
        from talk_live_codex import INSTALL_HINT, codex_binary
    if not codex_binary(source):
        raise talk_auth.TalkAuthError(f"{INSTALL_HINT}. API credentials were not used.")
    return talk_auth.TalkAuth(
        token="",
        source=SOURCE_CODEX_APP_SERVER,
        detail="Codex CLI login through codex app-server (ChatGPT subscription)",
    )


def validate_live_auth(auth: talk_auth.TalkAuth, config: LiveConfig) -> None:
    if config.auth_mode == "subscription":
        if auth.source != SOURCE_CODEX_APP_SERVER or auth.token:
            raise talk_auth.TalkAuthError(
                "GPT-Live subscription runs only through the Codex CLI app-server; "
                "API credentials were not used."
            )
        return
    if (
        not auth.token
        or auth.token != auth.token.strip()
        or any(character in auth.token for character in "\r\n")
    ):
        raise talk_auth.TalkAuthError("GPT-Live credential is empty or invalid")
    if auth.source not in {talk_auth.SOURCE_CONFIGURED, talk_auth.SOURCE_ENV}:
        raise talk_auth.TalkAuthError("GPT-Live API mode requires an explicitly configured API key")


def resolve_live_auth(
    *,
    env: Mapping[str, str] | None = None,
    config: LiveConfig | None = None,
) -> talk_auth.TalkAuth:
    source = talk_config.SECRET_ENV if env is None else env
    config = config or resolve_live_config(source)
    if config.auth_mode == "subscription":
        auth = _codex_cli_auth(source)
        validate_live_auth(auth, config)
        return auth
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
    "SOURCE_CODEX_APP_SERVER",
    "SUBSCRIPTION_MODELS",
    "SUBSCRIPTION_VOICES",
    "LiveConfig",
    "LiveConfigError",
    "resolve_live_auth",
    "resolve_live_config",
    "validate_live_auth",
]
