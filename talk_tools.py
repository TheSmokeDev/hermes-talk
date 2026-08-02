"""Talk tool surface — Realtime function-tool schemas and the executor.

The Realtime session advertises these tools; when the model emits a function
call the relay lands here and speaks whatever text comes back. The contract
that makes a live call survivable:

- An UNKNOWN tool name raises :class:`TalkToolError` — that is a client bug
  and the caller decides what to do about it.
- A KNOWN tool that fails RETURNS the failure as text. The model says what
  broke instead of the session dying on a stack trace.

Outputs are bounded plain text: the model summarizes them aloud, so nothing
here should be formatted for a screen.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

try:
    from . import talk_audio, talk_config, talk_host
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_audio
    import talk_config
    import talk_host

_log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 4_000

#: Populated by ``register(ctx)`` when a surface fails to register. Reported by
#: ``talk_status`` so a half-registered plugin says so out loud instead of
#: looking healthy.
REGISTRATION_FAILURES: list[str] = []

_TOOL_SEARCH_MEMORY: dict = {
    "type": "function",
    "name": "search_memory",
    "description": (
        "Look up what was said or decided in past Hermes sessions. Use this "
        "whenever you are asked about earlier work, prior decisions, people, "
        "or anything you would only know from a previous conversation. "
        "Returns matching excerpts to summarize aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look for, in plain words.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 8,
                "description": "How many matches to bring back (default 5).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

_TOOL_DELEGATE_TASK: dict = {
    "type": "function",
    "name": "delegate_task",
    "description": (
        "Hand a real task to a background Hermes agent and keep talking. The "
        "agent starts fresh and never sees this call, so write the whole task "
        "out: what to do, where it lives, and what done looks like. Returns a "
        "WORK_STARTED receipt — say it is running and move on."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "The complete, self-contained brief. No references back "
                    "to this conversation."
                ),
            },
            "background": {
                "type": "boolean",
                "description": "Run without blocking the call (default true).",
            },
        },
        "required": ["task"],
        "additionalProperties": False,
    },
}

_TOOL_TALK_STATUS: dict = {
    "type": "function",
    "name": "talk_status",
    "description": (
        "Report this voice plugin's own state: version, model, voice, whether "
        "it is attached to a Hermes agent, and whether audio is working. Use "
        "when asked what you are running on or why something is unavailable."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


class TalkToolError(Exception):
    """Unknown tool name or otherwise malformed tool call."""


def plugin_version() -> str:
    """The shipped plugin version, whichever way this plugin was loaded."""

    try:
        from importlib.metadata import version

        return version("hermes-talk")
    except Exception:  # noqa: BLE001 - file-path load has no installed metadata
        pass
    try:
        manifest = Path(__file__).resolve().parent / "plugin.yaml"
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def default_talk_tools() -> list[dict]:
    """The tool set advertised to a new Talk session (fresh copies per call)."""

    return copy.deepcopy([_TOOL_SEARCH_MEMORY, _TOOL_DELEGATE_TASK, _TOOL_TALK_STATUS])


def execute_talk_tool(name: str, arguments: dict | None) -> str:
    """Dispatch one tool call and return plain text for the model to speak."""

    handler = _HANDLERS.get(name)
    if handler is None:
        raise TalkToolError(f"unknown talk tool: {name!r}")
    try:
        output = handler(arguments or {})
    except Exception as exc:  # noqa: BLE001 — the model speaks the failure
        _log.warning("talk tool %s failed: %s: %s", name, type(exc).__name__, exc)
        return f"{name} failed: {type(exc).__name__}: {exc}"
    return (output or "(no output)")[:MAX_OUTPUT_CHARS]


# -- handlers -----------------------------------------------------------------


def _handle_search_memory(arguments: dict) -> str:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "search_memory needs something to look for."
    try:
        limit = int(arguments.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    return talk_host.host().search_memory(query, max(1, min(limit, 8)))


def _handle_delegate_task(arguments: dict) -> str:
    task = str(arguments.get("task") or "").strip()
    if not task:
        return "delegate_task needs a task to hand off."
    background = arguments.get("background")
    return talk_host.host().run_agent(task, background is not False)


def _handle_talk_status(arguments: dict) -> str:
    try:
        voice = talk_config.talk_voice()
    except talk_config.TalkConfigError as exc:
        voice = f"unusable ({exc})"
    status = {
        "version": plugin_version(),
        "model": talk_config.talk_model(),
        "voice": voice,
        "attached_to_hermes": talk_host.get_ctx() is not None,
        "audio_available": talk_audio.audio_available(),
    }
    if REGISTRATION_FAILURES:
        status["registration_failures"] = list(REGISTRATION_FAILURES)
    return json.dumps(status)


_HANDLERS = {
    "search_memory": _handle_search_memory,
    "delegate_task": _handle_delegate_task,
    "talk_status": _handle_talk_status,
}


__all__ = [
    "MAX_OUTPUT_CHARS",
    "REGISTRATION_FAILURES",
    "TalkToolError",
    "default_talk_tools",
    "execute_talk_tool",
    "plugin_version",
]
