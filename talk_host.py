"""HostAdapter — the five host interfaces hermes-talk needs from Hermes.

Everything the voice surface asks of the host comes through here: identity,
auth, state, memory reads, and work delegation. Two rules hold for every
method:

- **Never raise at the caller.** This adapter sits directly behind the
  Realtime tool surface. An ImportError escaping into the relay becomes dead
  air on a live call, so degradation is returned as text the model can SPEAK.
- **Resolve at call time.** The plugin context is bound once by
  ``register(ctx)``; every lookup reads it through the module so a test (or a
  later rebind) is seen immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from . import talk_auth, talk_config
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_auth
    import talk_config

#: Hermes's ``memory`` tool is a WRITE surface (add/replace/remove against the
#: durable memory file); saved memory itself is injected into every turn rather
#: than queried. ``session_search`` is the FTS5 READ surface, so that is what a
#: spoken "what do you remember about X" relays into.
MEMORY_TOOL_NAME = "session_search"
DELEGATE_TOOL_NAME = "delegate_task"

MAX_TOOL_OUTPUT_CHARS = 2_000

_CTX: Any | None = None


def bind_ctx(ctx: Any) -> None:
    """Bind the Hermes plugin context. Called once from ``register(ctx)``."""

    global _CTX
    _CTX = ctx


def get_ctx() -> Any | None:
    """The bound plugin context, or ``None`` outside a Hermes session."""

    return _CTX


def _speakable(raw: Any, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """Flatten a Hermes tool result (a JSON string) into bounded spoken text."""

    text = raw if isinstance(raw, str) else json.dumps(raw, default=str)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return text.strip()[:limit]
    if isinstance(parsed, dict):
        if parsed.get("success") is False:
            return f"that failed: {parsed.get('error') or 'no reason given'}"[:limit]
        for key in ("result", "output", "transcript", "content", "message"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:limit]
    return json.dumps(parsed, default=str)[:limit]


class HostAdapter:
    """Binds hermes-talk to Hermes, degrading to speakable text off-host."""

    def identity_sections(self) -> dict[str, str]:
        """Host identity context for the session prompt.

        v0.1 returns nothing and the voice preamble carries the whole
        contract. TODO(v0.2): read the active Hermes persona + system prompt
        off ``ctx`` and return them as ``PERSONA``/``USER`` sections.
        """

        return {}

    def resolve_openai_key(self) -> str:
        """A literal OpenAI API key. Fail-closed — raises without one.

        Key-lane only, for the REST TTS/STT providers: OAuth entitlement on
        the ``/v1/audio/*`` endpoints is unproven, so the providers refuse
        honestly rather than claim availability and 401. The voice session
        itself uses :meth:`resolve_auth`.
        """

        return talk_config.resolve_openai_key()

    def resolve_auth(self) -> talk_auth.TalkAuth:
        """The voice-session credential — key OR ChatGPT subscription.

        Fail-closed dual lane: TALK_OPENAI_API_KEY -> OPENAI_API_KEY -> Codex
        CLI OAuth (the operator's ChatGPT subscription). The other method that
        raises.
        """

        return talk_auth.resolve_auth()

    def state_dir(self) -> Path:
        """Durable state directory for run history and flush dedup."""

        return talk_config.state_dir()

    def search_memory(self, query: str, limit: int = 5) -> str:
        """Search past Hermes sessions for what was said about ``query``."""

        ctx = get_ctx()
        if ctx is None:
            return (
                "memory isn't available in this session — I'm running outside "
                "a Hermes agent, so I can't look anything up."
            )
        try:
            raw = ctx.dispatch_tool(MEMORY_TOOL_NAME, {"query": query, "limit": limit})
        except Exception as exc:  # noqa: BLE001 — the model speaks the failure
            return f"the memory lookup failed: {type(exc).__name__}: {exc}"
        return _speakable(raw)

    def run_agent(self, prompt: str, background: bool = True) -> str:
        """Hand a self-contained task to a Hermes subagent.

        ``background`` is accepted for the caller's mental model but is not
        forwarded: Hermes documents the tool's own ``background`` flag as
        deprecated and ignored, and backgrounds single-task delegations
        unconditionally.
        """

        ctx = get_ctx()
        if ctx is None:
            return (
                "I can't hand off work in this session — I'm running outside a "
                "Hermes agent, so there's no agent to delegate to."
            )
        try:
            raw = ctx.dispatch_tool(DELEGATE_TOOL_NAME, {"goal": prompt})
        except Exception as exc:  # noqa: BLE001 — the model speaks the failure
            return f"I couldn't start that work: {type(exc).__name__}: {exc}"
        return f"WORK_STARTED — {_speakable(raw)}"


_HOST = HostAdapter()


def host() -> HostAdapter:
    """The shared adapter. Stateless — the ctx lives at module scope."""

    return _HOST


__all__ = [
    "DELEGATE_TOOL_NAME",
    "MAX_TOOL_OUTPUT_CHARS",
    "MEMORY_TOOL_NAME",
    "HostAdapter",
    "bind_ctx",
    "get_ctx",
    "host",
]
