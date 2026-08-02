"""Pure OpenAI Realtime wire layer — session payloads and ephemeral mints.

Ported from the proven Talk Mode slice (second-brain `talk_session.py`).
This module knows the OpenAI Realtime wire format and NOTHING about the
host: no Hermes imports, no identity assembly, no tool handlers. Callers
pass instructions, tools, and an auth token in; only the ephemeral client
secret ever travels onward to a client.

Invariants carried from the source system:
- Fail-closed on upstream errors — a mint failure is an exception with a
  bounded upstream excerpt, never a silent fallback.
- The raw API key is used exactly once (the Authorization header here) and
  never appears in any returned descriptor.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

try:
    from .talk_config import DEFAULT_TALK_MODEL, DEFAULT_TALK_VOICE
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    from talk_config import DEFAULT_TALK_MODEL, DEFAULT_TALK_VOICE

OPENAI_REALTIME_OFFER_URL = "https://api.openai.com/v1/realtime/calls"
OPENAI_REALTIME_WS_URL = "wss://api.openai.com/v1/realtime"
CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
INPUT_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
MINT_TIMEOUT_S = 30.0


class TalkWireError(Exception):
    """Base wire-layer error."""


class TalkUpstreamError(TalkWireError):
    """OpenAI Realtime returned an unusable response."""


@dataclass(frozen=True, slots=True)
class TalkSessionDescriptor:
    """Client-facing session metadata — ephemeral secret ONLY."""

    client_secret: str
    expires_at_ms: int | None
    offer_url: str
    model: str
    voice: str

    def to_wire(self) -> dict:
        return {
            "clientSecret": self.client_secret,
            "expiresAt": self.expires_at_ms,
            "offerUrl": self.offer_url,
            "model": self.model,
            "voice": self.voice,
        }


def build_session_payload(
    *,
    model: str = DEFAULT_TALK_MODEL,
    voice: str = DEFAULT_TALK_VOICE,
    instructions: str,
    tools: list[dict] | None = None,
) -> dict:
    """OpenAI Realtime session config — server VAD, barge-in enabled."""

    payload: dict = {
        "type": "realtime",
        "model": model,
        "instructions": instructions,
        "audio": {
            "input": {
                "noise_reduction": {"type": "near_field"},
                "turn_detection": {
                    "type": "server_vad",
                    "create_response": True,
                    "interrupt_response": True,
                },
                "transcription": {"model": INPUT_TRANSCRIPTION_MODEL},
            },
            "output": {"voice": voice},
        },
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def post_client_secret(auth_token: str, session: dict) -> dict:
    """POST the session to the client_secrets endpoint. Isolated for tests."""

    response = httpx.post(
        CLIENT_SECRETS_URL,
        json={"session": session},
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        },
        timeout=MINT_TIMEOUT_S,
    )
    if response.status_code != 200:
        raise TalkUpstreamError(
            f"OpenAI Realtime client secret failed ({response.status_code}): "
            f"{response.text[:300] or response.reason_phrase}"
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise TalkUpstreamError("OpenAI Realtime client secret returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise TalkUpstreamError("OpenAI Realtime client secret returned invalid payload")
    return payload


def parse_client_secret(payload: dict) -> tuple[str, int | None]:
    """Accept both flat ``{value, expires_at}`` and nested client_secret shapes."""

    value = payload.get("value")
    nested = payload.get("client_secret")
    if not isinstance(value, str) or not value:
        value = nested.get("value") if isinstance(nested, dict) else None
    if not isinstance(value, str) or not value:
        raise TalkUpstreamError("OpenAI Realtime client secret response did not include a value")
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, (int, float)) and isinstance(nested, dict):
        expires_at = nested.get("expires_at")
    expires_at_ms = int(expires_at * 1000) if isinstance(expires_at, (int, float)) else None
    return value, expires_at_ms


def mint_ephemeral_session(
    *,
    auth_token: str,
    model: str,
    voice: str,
    instructions: str,
    tools: list[dict] | None = None,
) -> TalkSessionDescriptor:
    """Mint an ephemeral Realtime client secret for one client session.

    The caller resolves auth and assembles instructions; this function owns
    only the wire exchange. The returned descriptor carries the ephemeral
    secret — never the auth token that minted it.
    """

    session = build_session_payload(
        model=model, voice=voice, instructions=instructions, tools=tools
    )
    payload = post_client_secret(auth_token, session)
    secret, expires_at_ms = parse_client_secret(payload)
    return TalkSessionDescriptor(
        client_secret=secret,
        expires_at_ms=expires_at_ms,
        offer_url=OPENAI_REALTIME_OFFER_URL,
        model=model,
        voice=voice,
    )


__all__ = [
    "CLIENT_SECRETS_URL",
    "INPUT_TRANSCRIPTION_MODEL",
    "OPENAI_REALTIME_OFFER_URL",
    "OPENAI_REALTIME_WS_URL",
    "TalkSessionDescriptor",
    "TalkUpstreamError",
    "TalkWireError",
    "build_session_payload",
    "mint_ephemeral_session",
    "parse_client_secret",
    "post_client_secret",
]
