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
import time
from pathlib import Path

try:
    from . import (
        talk_audio,
        talk_auth,
        talk_config,
        talk_host,
        talk_identity,
        talk_runs,
        talk_steer,
    )
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_audio
    import talk_auth
    import talk_config
    import talk_host
    import talk_identity
    import talk_runs
    import talk_steer

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

_TOOL_CHECK_WORK: dict = {
    "type": "function",
    "name": "check_work",
    "description": (
        "Check on background work you started. Call with no arguments when "
        "asked how things are going, or with a run number to check one job. "
        "Reports what is still running and what has landed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "integer",
                "description": "A specific run number. Omit for everything recent.",
            },
        },
        "additionalProperties": False,
    },
}

_TOOL_LIST_AGENTS: dict = {
    "type": "function",
    "name": "list_agents",
    "description": (
        "List running and recent background work, each entry tagged with "
        "what it supports: 'can steer' (a live subagent id), 'stop only' (a "
        "run number), or unreachable. ALWAYS call this first when the user "
        "refers to work by description ('the audit', 'that research one') — "
        "resolve the id here, never from memory of earlier speech."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

_TOOL_STEER_AGENT: dict = {
    "type": "function",
    "name": "steer_agent",
    "description": (
        "Queue a redirection note into background work that is ALREADY "
        "RUNNING, without stopping it — 'focus on pricing instead', 'skip "
        "the tests'. The note is QUEUED, not delivered: if the agent takes "
        "another step it sees the note then, and delivery is confirmed "
        "separately. Never "
        "say the agent already has it. Only subagent ids (like "
        "sa-0-a1b2c3d4, from list_agents) can be steered — run numbers "
        "cannot; offer stop_work for those. This never cancels work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": (
                    "The subagent id from list_agents. Not a run number."
                ),
            },
            "text": {
                "type": "string",
                "description": (
                    "The redirection, written to the agent doing the work. It "
                    "never sees this conversation, so make it stand alone."
                ),
            },
        },
        "required": ["agent_id", "text"],
        "additionalProperties": False,
    },
}

_TOOL_STOP_WORK: dict = {
    "type": "function",
    "name": "stop_work",
    "description": (
        "Stop background work on any lane: a subagent id or a run number "
        "(both from list_agents). Stopping drops any queued-but-unread "
        "steering note. Use only when the user clearly wants the work "
        "cancelled, not redirected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "A run number or a subagent id from list_agents.",
            },
            "reason": {
                "type": "string",
                "description": "Optional short reason, for the record.",
            },
        },
        "required": ["target"],
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

    return copy.deepcopy(
        [
            _TOOL_SEARCH_MEMORY,
            _TOOL_DELEGATE_TASK,
            _TOOL_CHECK_WORK,
            _TOOL_LIST_AGENTS,
            _TOOL_STEER_AGENT,
            _TOOL_STOP_WORK,
            _TOOL_TALK_STATUS,
        ]
    )


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


def _describe_age(run: dict) -> str:
    """How long a run has been going, in words a voice can say."""

    started = run.get("ts")
    if not isinstance(started, (int, float)):
        return ""
    seconds = max(0, int(time.time() - started))
    if seconds < 60:
        return f" {seconds}s"
    if seconds < 3_600:
        return f" {seconds // 60}m"
    return f" {seconds // 3600}h"


def _describe_run(run: dict) -> str:
    line = f"run {run.get('runId')} ({run.get('kind')}) {run.get('status')}"
    if run.get("status") == "running":
        line += _describe_age(run)
    if run.get("status") == "lost":
        line += " (started before this session — I can't see how it ended)"
    return line


def _handle_check_work(arguments: dict) -> str:
    run_id = arguments.get("run_id")
    if run_id is not None:
        try:
            wanted = int(run_id)
        except (TypeError, ValueError):
            return "check_work needs a run number."
        run = talk_runs.get_run(wanted)
        if run is None:
            return f"I don't have a run number {wanted} in this session."
        body = run.get("output") or "still working"
        return f"{_describe_run(run)}, {run.get('label')}: {body}"

    # include_history so a run from a PREVIOUS session surfaces as `lost`
    # rather than vanishing — this process cannot see a detached child it
    # never spawned, and saying nothing would read as "nothing is running".
    runs = talk_runs.list_runs(limit=10, include_history=True)
    lines = "; ".join(_describe_run(run) for run in runs) if runs else ""
    # Steer receipts ride along: "did my note land?" is a check_work
    # question, and the ledger is the only place the answer lives. First
    # degrade notes whose child left the registry — "queued" with nobody
    # left to drain it is exactly the overclaim the ledger exists to stop.
    talk_host.degrade_gone_children()
    notes = talk_steer.notes_summary()
    if lines and notes:
        return f"{lines}. {notes}"
    if notes:
        return notes
    if lines:
        return lines
    return "Nothing is running and nothing recent has finished."


def _handle_list_agents(arguments: dict) -> str:
    return talk_host.host().list_agents()


def _handle_steer_agent(arguments: dict) -> str:
    agent_id = str(arguments.get("agent_id") or "").strip()
    if not agent_id:
        return "steer_agent needs the subagent id — call list_agents first."
    text = str(arguments.get("text") or "").strip()
    if not text:
        return "steer_agent needs the note itself."
    return talk_host.host().steer_agent(agent_id, text)


def _handle_stop_work(arguments: dict) -> str:
    target = str(arguments.get("target") or "").strip()
    if not target:
        return "stop_work needs to know which job to stop."
    reason = str(arguments.get("reason") or "").strip() or None
    return talk_host.host().stop_work(target, reason)


def _identity_summary() -> dict[str, int]:
    """Resolved identity sections as ``{NAME: char_count}``. Never content.

    Counts are POST-cap, so the number is what actually rides the prompt
    rather than what the host happened to hand over.
    """

    try:
        sections = talk_host.host().identity_sections()
    except Exception as exc:  # noqa: BLE001 — status must survive a bad host
        _log.debug("identity summary unavailable: %s: %s", type(exc).__name__, exc)
        return {}
    return {
        name: len(talk_identity.cap_section(name, body)) for name, body in sections.items()
    }


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
        # Which tier a real-agent request would actually take: an in-process
        # agent loop, a real agent over the api_server, or neither. The bool
        # above answers a narrower question and cannot stand in for this one.
        "agent_lane": talk_host.host().agent_lane(),
        "audio_available": talk_audio.audio_available(),
        # Which identity sections resolved and how big they are — NEVER the
        # content. This is spoken aloud and lands in transcripts; the whole
        # point of the sections is that they hold things about the operator
        # that should not be read back out on request.
        "identity": _identity_summary(),
        # Which credential lane a session would use — never the token itself.
        "auth": talk_auth.auth_status(),
    }
    if REGISTRATION_FAILURES:
        status["registration_failures"] = list(REGISTRATION_FAILURES)
    return json.dumps(status)


_HANDLERS = {
    "search_memory": _handle_search_memory,
    "delegate_task": _handle_delegate_task,
    "check_work": _handle_check_work,
    "list_agents": _handle_list_agents,
    "steer_agent": _handle_steer_agent,
    "stop_work": _handle_stop_work,
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
