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

# ``talk_doctor`` imports this module back (for its registration receipts),
# so this pair is a cycle. It resolves because NEITHER module touches the
# other at import time — every cross-reference is inside a function body. Keep
# it that way: a module-level ``talk_doctor.SECRET_PATTERNS`` here would break
# the import on whichever module loads second.
try:
    from . import (
        talk_approvals,
        talk_audio,
        talk_auth,
        talk_capabilities,
        talk_config,
        talk_core_realtime,
        talk_doctor,
        talk_host,
        talk_identity,
        talk_pause,
        talk_runs,
        talk_steer,
        talk_vault,
    )
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_approvals
    import talk_audio
    import talk_auth
    import talk_capabilities
    import talk_config
    import talk_core_realtime
    import talk_doctor
    import talk_host
    import talk_identity
    import talk_pause
    import talk_runs
    import talk_steer
    import talk_vault

_log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 4_000

#: Populated by ``register(ctx)`` when a surface fails to register. Reported by
#: ``talk_status`` so a half-registered plugin says so out loud instead of
#: looking healthy.
REGISTRATION_FAILURES: list[str] = []
REGISTRATION_RECEIPTS: dict[str, str] = {}
REGISTRATION_REQUIREMENTS: dict[str, str] = {
    "cli_command": "required",
    "slash_command": "required",
    "session_end_hook": "optional",
    "subagent_start_hook": "optional",
    "subagent_stop_hook": "optional",
    "post_tool_call_hook": "optional",
    "pre_approval_request_hook": "optional",
    "tts_provider": "optional",
    "transcription_provider": "optional",
    "realtime_voice_provider": "optional",
    "core_realtime_providers": "optional",
}

_TOOL_SEARCH_MEMORY: dict = {
    "type": "function",
    "name": "search_memory",
    "description": (
        "Look up what was said or decided in past Hermes sessions, and who or "
        "what a name refers to. Use this whenever you are asked about earlier "
        "work, prior decisions, people, repos, or anything you would only "
        "know from a previous conversation. Returns matching excerpts to "
        "summarize aloud. An answer that begins 'from remembered context' is "
        "a remembered profile fact, not a verbatim quote — say so when you "
        "pass it on. If what comes back could match more than one thing, ask "
        "which one before acting on it rather than taking the closest match."
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

_TOOL_SEARCH_VAULT: dict = {
    "type": "function",
    "name": "search_vault",
    "description": (
        "Look something up in the operator's long-term notes — the durable "
        "vault of projects, decisions, people and standing rules. Use this "
        "for what is WRITTEN DOWN, as opposed to search_memory, which is "
        "what was SAID in past sessions. Returns matching excerpts to "
        "summarize aloud."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look for, in plain words.",
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
        "WORK_STARTED receipt — say it is running and move on. If the task "
        "touches something other work might also touch — a repository "
        "checkout, a deployment target — name it in resource_keys so two jobs "
        "never collide; a refusal names the run in the way, so offer to wait "
        "for it, stop it, or retry without that key."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "The complete, self-contained brief. No references back to this conversation."
                ),
            },
            "background": {
                "type": "boolean",
                "description": "Run without blocking the call (default true).",
            },
            "execution_mode": {
                "type": "string",
                "enum": ["exclusive", "parallel_read_only"],
                "description": (
                    "How this task may share its resource_keys with other running "
                    "work. 'exclusive' (the default): nothing else touching the "
                    "same key runs at the same time. 'parallel_read_only': the "
                    "task only reads, so it may overlap other read-only work on "
                    "the same key — honored only when the operator has chosen to "
                    "trust that declaration. Use exclusive unless the task is "
                    "certainly read-only."
                ),
            },
            "resource_keys": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 8,
                "description": (
                    "Stable names for what the task touches: an absolute "
                    "repository path, a deployment target, a service name. Two "
                    "tasks that share a key never run together unless both are "
                    "parallel_read_only. Omit when the task touches nothing shared."
                ),
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
        "asked how things are going; every finished run_id returned must then "
        "be passed back in a specific call to retrieve that run's bounded output."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "integer",
                "description": "A specific run number whose bounded output should be returned.",
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
                "description": ("The subagent id from list_agents. Not a run number."),
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

_TOOL_REDIRECT_AGENT: dict = {
    "type": "function",
    "name": "redirect_agent",
    "description": (
        "Interrupt background work's CURRENT step and re-aim it right now — "
        "stronger than steer_agent, for corrections that can't wait ('stop, "
        "wrong repo', 'abandon that approach'). The agent keeps everything "
        "it already finished; only its in-flight thinking is dropped and "
        "retried with the correction. If it's mid-tool the correction lands "
        "when the tool finishes. Only subagent ids (from list_agents) can "
        "be redirected — run numbers cannot. This never cancels the work; "
        "use stop_work for that."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": ("The subagent id from list_agents. Not a run number."),
            },
            "text": {
                "type": "string",
                "description": (
                    "The correction, written to the agent doing the work. It "
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


_TOOL_RESOLVE_APPROVAL: dict = {
    "type": "function",
    "name": "resolve_approval",
    "description": (
        "Answer a pending approval request from background work you delegated. "
        "Call this the moment the operator answers an approval question, with "
        "the run number from the question and their choice. 'once' allows the "
        "action this one time, 'session' allows it for the rest of that run, "
        "'deny' refuses it. There is no 'always' by voice — if the operator "
        "asks for always, offer session instead. If their answer is unclear, "
        "ask once; if still unclear, deny. An unanswered question, or the "
        "operator interrupting it, denies automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "integer",
                "description": "The run number from the approval question.",
            },
            "choice": {
                "type": "string",
                "enum": ["once", "session", "deny"],
                "description": "The operator's answer.",
            },
        },
        "required": ["run_id", "choice"],
        "additionalProperties": False,
    },
}


_TOOL_TALK_CAPABILITIES: dict = {
    "type": "function",
    "name": "talk_capabilities",
    "description": (
        "Report what this Hermes session can ACTUALLY do right now: installed "
        "skills, resolved toolsets with whether each one is enabled and "
        "configured, the gateway's feature flags, and how much work is in "
        "flight. Use when asked what you can do, which tools or skills are "
        "available, or why something seems missing. A toolset listed here "
        "with enabled or configured false is NOT usable — say so rather than "
        "offering it. If the source is 'unavailable', say you could not read "
        "the catalog; never answer from memory instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


_TOOL_PAUSE_VOICE_INPUT: dict = {
    "type": "function",
    "name": "pause_voice_input",
    "description": (
        "Pause listening — mute your microphone WITHOUT ending the call. Use "
        "when the operator says to stop listening, mute the mic, or hold on "
        "while they talk to someone else. Playback, background work and its "
        "announcements all continue; only their speech stops reaching you. "
        "Once paused you cannot hear a spoken resume: the operator resumes "
        "from their own control, which the tool result names — repeat it as "
        "you confirm the pause. Pass paused=false to resume when a non-spoken "
        "path asks you to."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "paused": {
                "type": "boolean",
                "description": "true (default) pauses the microphone; false resumes it.",
            },
        },
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


def default_talk_tools(*, pausable: bool = False) -> list[dict]:
    """The tool set advertised to a new Talk session (fresh copies per call).

    The base set is unconditional. ``search_vault`` is CONDITIONAL: it is
    advertised only when a memory provider is actually loadable in this
    process, because advertising a lookup that cannot be served is the same
    defect as the provider block this plugin stopped passing through.
    ``pause_voice_input`` is conditional the same way, on ``pausable``: the
    session passes True only when this process pumps the microphone AND the
    operator has a guaranteed way to resume it (a keyboard the session owns,
    or ``/talk resume``). Default False — the dashboard tab's microphone
    lives in the browser, and a terminal whose stdin is not a tty has no key
    to press — so a pause tool is never offered where the only way back
    would be Ctrl+C.
    """

    tools = [
        _TOOL_SEARCH_MEMORY,
        _TOOL_DELEGATE_TASK,
        _TOOL_CHECK_WORK,
        _TOOL_LIST_AGENTS,
        _TOOL_STEER_AGENT,
        _TOOL_REDIRECT_AGENT,
        _TOOL_STOP_WORK,
        _TOOL_RESOLVE_APPROVAL,
        _TOOL_TALK_STATUS,
        _TOOL_TALK_CAPABILITIES,
    ]
    if pausable:
        tools.append(_TOOL_PAUSE_VOICE_INPUT)
    try:
        if talk_vault.available():
            tools.insert(1, _TOOL_SEARCH_VAULT)
    except Exception as exc:  # noqa: BLE001 — a missing tool, never a dead session
        _log.debug("vault availability unknown: %s: %s", type(exc).__name__, exc)
    return copy.deepcopy(tools)


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


def _handle_search_vault(arguments: dict) -> str:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "search_vault needs something to look for."
    try:
        found = talk_vault.search(query)
    except talk_vault.VaultSearchError as exc:
        return f"the vault lookup failed: {exc}"
    if not found:
        # Distinct sentence from the failure above, deliberately: "nothing
        # written down" and "the lookup broke" must never sound the same.
        return f"nothing in the notes about {query}."
    # Vault notes are untrusted text; the leading provenance marker is
    # reserved for search_memory's Honcho tier and must not be forgeable
    # from note content.
    return talk_host.strip_reserved_marker(found)


def _handle_delegate_task(arguments: dict) -> str:
    task = str(arguments.get("task") or "").strip()
    if not task:
        return "delegate_task needs a task to hand off."
    background = arguments.get("background")
    # The admission declaration (hermes-talk#101) is validated HERE, before
    # any backend is consulted: a malformed declaration must not fall through
    # to a lane that would then run the task unfenced.
    mode = arguments.get("execution_mode")
    if mode is not None:
        mode = str(mode).strip().lower() or None
        if mode is not None and mode not in talk_runs.EXECUTION_MODES:
            return "delegate_task's execution_mode must be 'exclusive' or 'parallel_read_only'."
    try:
        keys = talk_runs.normalize_resource_keys(arguments.get("resource_keys"))
    except ValueError as exc:
        return f"delegate_task could not use those resource_keys: {exc}."
    return talk_host.host().run_agent(
        task, background is not False, execution_mode=mode, resource_keys=keys
    )


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
        # What a live run holds (hermes-talk#101), so "why was that refused?"
        # has an answer the model can read out.
        admission = run.get("admission") if isinstance(run.get("admission"), dict) else {}
        held = [key for key in admission.get("keys") or () if isinstance(key, str)]
        if held:
            line += " holding " + ", ".join(f"'{key}'" for key in held)
    if run.get("status") == "lost":
        line += " (started before this session — I can't see how it ended)"
    # A stop verb's detached confirmation lands in meta (hermes-talk#2) —
    # this is where "ask me in a moment for the receipt" pays off.
    meta = run.get("meta") if isinstance(run.get("meta"), dict) else {}
    if meta.get("stop_result"):
        line += f" — stop receipt: {meta['stop_result']}"
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
    finished = [
        int(run["runId"])
        for run in runs
        if run.get("status") in talk_runs.TERMINAL_STATUSES and isinstance(run.get("runId"), int)
    ]
    if lines and finished:
        retrieval = "; ".join(
            f"call check_work with run_id {run_id} for that run's bounded output"
            for run_id in finished
        )
        lines = f"{lines}. {retrieval}."
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


def _handle_redirect_agent(arguments: dict) -> str:
    agent_id = str(arguments.get("agent_id") or "").strip()
    if not agent_id:
        return "redirect_agent needs the subagent id — call list_agents first."
    text = str(arguments.get("text") or "").strip()
    if not text:
        return "redirect_agent needs the correction itself."
    return talk_host.host().redirect_agent(agent_id, text)


def _handle_stop_work(arguments: dict) -> str:
    target = str(arguments.get("target") or "").strip()
    if not target:
        return "stop_work needs to know which job to stop."
    reason = str(arguments.get("reason") or "").strip() or None
    return talk_host.host().stop_work(target, reason)


def _handle_resolve_approval(arguments: dict) -> str:
    try:
        run_id = int(arguments.get("run_id"))
    except (TypeError, ValueError):
        return "resolve_approval needs the run number from the approval question."
    return talk_approvals.resolve(run_id, arguments.get("choice"))


#: What the model reads back after a pause flip. Spoken, so each one says
#: what is TRUE now and, for a pause, how the operator gets back — a paused
#: microphone cannot carry the word "resume". The PAUSED receipt names the
#: control THIS session registered (``{resume}``), never a key or a command
#: from another room.
PAUSE_RECEIPTS: dict[str, str] = {
    talk_pause.PAUSED: (
        "Microphone paused — you are no longer hearing the operator. Playback, "
        "background work and its announcements continue. Tell them how to "
        "resume: {resume}."
    ),
    talk_pause.ALREADY_PAUSED: "The microphone was already paused.",
    talk_pause.RESUMED: "Microphone resumed — you are hearing the operator again.",
    talk_pause.ALREADY_LISTENING: "The microphone was not paused; you are already listening.",
    talk_pause.NO_SESSION: (
        "There is no live voice session attached to this process, so there is "
        "no microphone here to pause — in the dashboard tab the browser owns "
        "the microphone, so use its own mute control."
    ),
    talk_pause.NO_RESUME_PATH: (
        "The microphone was not paused: this session has no control the "
        "operator could resume it with, and a pause nobody can undo would end "
        "the call in all but name. They can still hang up with Ctrl+C."
    ),
    talk_pause.UNSUPPORTED: "This session's audio device cannot pause its input.",
}

_FALSE_WORDS = frozenset({"false", "no", "0", "off", "resume"})


def _handle_pause_voice_input(arguments: dict) -> str:
    raw = arguments.get("paused")
    if isinstance(raw, str):
        paused = raw.strip().lower() not in _FALSE_WORDS
    else:
        paused = True if raw is None else bool(raw)
    outcome = talk_pause.set_paused(paused, source=talk_pause.SOURCE_TOOL)
    receipt = PAUSE_RECEIPTS[outcome]
    if outcome == talk_pause.PAUSED:
        # The gate above guarantees a control was registered; the fallback
        # only covers a detach racing this read.
        receipt = receipt.format(resume=talk_pause.resume_control() or "their own control")
    return receipt


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
    return {name: len(talk_identity.cap_section(name, body)) for name, body in sections.items()}


def _handle_talk_status(arguments: dict) -> str:
    try:
        voice = talk_config.talk_voice()
    except talk_config.TalkConfigError as exc:
        voice = f"unusable ({exc})"
    core_realtime = talk_core_realtime.core_provider_diagnostic()
    core_realtime.update(
        {
            "contract": "api-v2-input-only",
            "registration": REGISTRATION_RECEIPTS.get(
                "realtime_voice_provider", "unsupported-optional"
            ),
        }
    )
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
        # The old duplex lane still owns provider tools/output. The optional
        # core lane is deliberately input-only and never executes them.
        "legacy_lane": "legacy-provider-executor",
        "legacy_session": {
            "scope": "limited provider-owned session",
            "full_parity_command": "/talk core join",
        },
        "transcript": {
            "current_call": "temporary local capture",
            "after_close": "handed off for durable-memory review",
            "archive": "not live searchable or user-facing",
            "core_persistence": "separate canonical session path",
        },
        "core_realtime": core_realtime,
    }
    if REGISTRATION_FAILURES:
        status["registration_failures"] = list(REGISTRATION_FAILURES)
    return json.dumps(status)


#: How many catalog entries survive the fallback rendering below. Reached only
#: after the full payload already failed to fit, so the choice is not "40 or
#: everything", it is "40 named entries or a torn JSON document".
MAX_CATALOG_ENTRIES = 40


def _catalog_name(entry: dict) -> str:
    """The speakable name of one skill or toolset, whatever the host called it."""

    for key in ("name", "id", "slug"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unnamed"


#: The boolean flags that decide whether a catalog entry is usable. They
#: survive compaction for skills and toolsets ALIKE: a disabled skill that
#: compacts to a bare name would read as plainly available, which is exactly
#: the "missing/disabled tools are not advertised" promise broken.
USABILITY_FLAGS = ("enabled", "configured", "installed", "disabled")


def _compact_entry(entry: dict) -> dict:
    """A skill or toolset as its name plus the flags that decide usability."""

    compact: dict = {"name": _catalog_name(entry)}
    for key in USABILITY_FLAGS:
        if isinstance(entry.get(key), bool):
            compact[key] = entry[key]
    return compact


def _render_catalog(payload: dict) -> str:
    """Redact and serialize one capabilities payload for spoken output."""

    return json.dumps(talk_doctor.redact_value(payload))


def _handle_talk_capabilities(arguments: dict) -> str:
    snapshot = talk_capabilities.status()
    payload = {
        "source": snapshot.source,
        "detail": snapshot.detail,
        "skills": list(snapshot.skills),
        "toolsets": list(snapshot.toolsets),
        "capabilities": snapshot.capabilities,
        "health": snapshot.health,
    }
    rendered = _render_catalog(payload)
    if len(rendered) <= MAX_OUTPUT_CHARS:
        return rendered
    # A real install's full catalog does not fit the spoken-output budget, and
    # execute_talk_tool bounds by TAIL TRUNCATION — which would hand the model
    # a JSON document cut off mid-object. Re-render skills/toolsets as names
    # plus the flags that decide usability: less detail, still honest about
    # what it dropped. `health` is already bounded by HEALTH_COUNTERS. If
    # `capabilities` alone is still too large after that, drop it too rather
    # than let tail truncation tear it mid-object.
    skills = list(snapshot.skills)
    toolsets = list(snapshot.toolsets)
    payload["skills"] = [_compact_entry(entry) for entry in skills[:MAX_CATALOG_ENTRIES]]
    payload["toolsets"] = [
        _compact_entry(entry) for entry in toolsets[:MAX_CATALOG_ENTRIES]
    ]
    payload["skills_omitted"] = max(0, len(skills) - MAX_CATALOG_ENTRIES)
    payload["toolsets_omitted"] = max(0, len(toolsets) - MAX_CATALOG_ENTRIES)
    payload["detail"] = (
        f"{snapshot.detail} — names only, the full catalog is too long to read out"
    )
    rendered = _render_catalog(payload)
    if len(rendered) > MAX_OUTPUT_CHARS:
        payload["capabilities"] = {}
        payload["capabilities_omitted"] = True
        payload["detail"] += ", capabilities omitted"
        rendered = _render_catalog(payload)
    if len(rendered) > MAX_OUTPUT_CHARS:
        # Even the deepest compaction tier can lose to upstream-minted absurdly
        # long names. Degrade to a minimal, fixed-shape summary rather than
        # ever handing execute_talk_tool a document its tail truncation would
        # tear mid-object.
        rendered = _render_catalog(
            {
                "source": snapshot.source,
                "skills_count": len(skills),
                "toolsets_count": len(toolsets),
                "detail": (
                    "the catalog is too large to read out, even as names — "
                    "counts only"
                ),
            }
        )
    return rendered


_HANDLERS = {
    "search_memory": _handle_search_memory,
    "search_vault": _handle_search_vault,
    "delegate_task": _handle_delegate_task,
    "check_work": _handle_check_work,
    "list_agents": _handle_list_agents,
    "steer_agent": _handle_steer_agent,
    "redirect_agent": _handle_redirect_agent,
    "stop_work": _handle_stop_work,
    "resolve_approval": _handle_resolve_approval,
    "talk_status": _handle_talk_status,
    "talk_capabilities": _handle_talk_capabilities,
    "pause_voice_input": _handle_pause_voice_input,
}


__all__ = [
    "MAX_CATALOG_ENTRIES",
    "MAX_OUTPUT_CHARS",
    "PAUSE_RECEIPTS",
    "REGISTRATION_FAILURES",
    "REGISTRATION_RECEIPTS",
    "REGISTRATION_REQUIREMENTS",
    "TalkToolError",
    "default_talk_tools",
    "execute_talk_tool",
    "plugin_version",
]
