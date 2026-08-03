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

The detached backend spawns ``hermes [--profile <name>] -z <task>``. The
profile comes from ``TALK_AGENT_PROFILE`` or, unset, from auto-detection in
:mod:`talk_config` — on an install whose model config lives only in a profile,
a bare ``hermes -z`` cannot resolve a model and the child dies immediately.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:
    from . import talk_auth, talk_config, talk_runs
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_auth
    import talk_config
    import talk_runs

_log = logging.getLogger(__name__)

#: Hermes's ``memory`` tool is a WRITE surface (add/replace/remove against the
#: durable memory file); saved memory itself is injected into every turn rather
#: than queried. ``session_search`` is the FTS5 READ surface, so that is what a
#: spoken "what do you remember about X" relays into.
MEMORY_TOOL_NAME = "session_search"
DELEGATE_TOOL_NAME = "delegate_task"

MAX_TOOL_OUTPUT_CHARS = 2_000

#: Errors that mean "this environment has no agent loop to delegate INTO" —
#: the only two the run_agent chain is allowed to fall through on. Hermes's
#: own text is ``tool_error("delegate_task requires a parent agent context.")``
#: (tools/delegate_tool.py:2365); a `hermes talk` CLI session has no
#: ``_cli_ref``, so ``dispatch_tool`` cannot attach a parent agent and every
#: delegation lands here. Any OTHER error — spawning paused, depth exceeded, a
#: real failure — is the host's decision and must NOT be routed around.
AGENT_LOOP_ABSENT_MARKERS = (
    "requires a parent agent context",
    f"unknown tool: {DELEGATE_TOOL_NAME}",
)

HERMES_BINARY = "hermes"

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


def _agent_loop_absent(raw: Any) -> bool:
    """True when a dispatch result says the AGENT LOOP is missing, not the task.

    Read off the registry's universal error envelope (``{"error": ...}``), and
    matched against named markers rather than "any error": falling back on a
    generic failure would route around an operator's paused delegation or a
    depth limit, which is the host's call, not this plugin's.
    """

    text = raw if isinstance(raw, str) else json.dumps(raw, default=str)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    error = parsed.get("error")
    if not isinstance(error, str):
        return False
    lowered = error.lower()
    return any(marker in lowered for marker in AGENT_LOOP_ABSENT_MARKERS)


def hermes_binary() -> str | None:
    """Absolute path to the ``hermes`` executable, or ``None``.

    Resolved through ``shutil.which`` rather than passed bare: on Windows the
    installed entry point is an npm ``hermes.cmd`` shim, and CreateProcess
    does no PATHEXT resolution — a bare ``"hermes"`` argv would simply fail
    to launch.
    """

    return shutil.which(HERMES_BINARY)


def agent_argv(binary: str, task: str, profile: str | None) -> list[str]:
    """The one-shot argv. ``--profile`` is global, so it precedes ``-z``.

    Without the flag on an install whose model config lives only in a profile,
    the child dies with ``Invalid length for parameter modelId, value: 0`` —
    no model resolved. See :func:`talk_config.detect_agent_profile`.
    """

    if profile:
        return [binary, "--profile", profile, "-z", task]
    return [binary, "-z", task]


def _detached_agent_worker(task: str, binary: str) -> Any:
    """Build the worker that runs one headless Hermes one-shot to completion."""

    def worker(run_id: int) -> str:
        # Resolved at spawn time, not at start_run time, and recorded on the
        # run so check_work can say which agent actually ran.
        profile = talk_config.agent_profile()
        talk_runs.annotate_run(run_id, profile=profile)
        # No `env=`: the child inherits this process's environment verbatim,
        # so HERMES_HOME (and the rest of the operator's config) resolves to
        # exactly what the voice session itself is using.
        completed = subprocess.run(
            agent_argv(binary, task, profile),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=talk_config.agent_timeout_s(),
            check=False,
        )
        stdout = (completed.stdout or "").strip()
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip() or stdout or "no output"
            message = f"the agent exited {completed.returncode}: {detail}"[
                -talk_runs.HISTORY_OUTPUT_CAP :
            ]
            # Mark it failed HERE rather than raising: an exception would put
            # a type name in front of a message meant to be spoken. Returning
            # afterwards is safe — terminal transitions are first-writer-wins,
            # so the registry's own done-transition is a no-op.
            talk_runs.finish_run(run_id, "failed", message)
            return message
        return (stdout or "the agent finished without printing anything")[
            : talk_runs.HISTORY_OUTPUT_CAP
        ]

    return worker


#: Session id handed to a provider we initialize ourselves. Marked so a
#: provider that scopes storage by session cannot mistake a read-only probe
#: for a real conversation.
PROBE_SESSION_ID = "hermes-talk-identity-probe"


def _resolve_persona() -> str:
    """The operator's SOUL.md, via Hermes's own loader.

    ``agent.prompt_builder.load_soul_md`` needs no agent instance, so this
    works in a standalone ``hermes talk`` too. Deliberately NO raw-file
    fallback: that loader also runs Hermes's injection scan over the content,
    and reading the file directly would silently drop that check to gain a
    section. No Hermes, no persona.
    """

    try:
        from agent.prompt_builder import load_soul_md

        return (load_soul_md() or "").strip()
    except Exception as exc:  # noqa: BLE001 — a missing section, never an outage
        _log.debug("persona section unavailable: %s: %s", type(exc).__name__, exc)
        return ""


def _memory_block_from_agent() -> str:
    """The LIVE agent's already-assembled memory block, when there is one.

    Preferred over loading a provider ourselves: this instance is already
    initialized, so reading it costs nothing and has no lifecycle side
    effects. The traversal mirrors ``PluginContext.dispatch_tool``'s own
    route to the parent agent (plugins.py:604-608) and is guarded at every
    hop — these are framework internals and may simply not be there.
    """

    ctx = get_ctx()
    if ctx is None:
        return ""
    try:
        cli = getattr(getattr(ctx, "_manager", None), "_cli_ref", None)
        manager = getattr(getattr(cli, "agent", None), "_memory_manager", None)
        if manager is None:
            return ""
        return (manager.build_system_prompt() or "").strip()
    except Exception as exc:  # noqa: BLE001 — a missing section, never an outage
        _log.debug("agent memory block unavailable: %s: %s", type(exc).__name__, exc)
        return ""


def _memory_block_from_provider() -> str:
    """Load the configured memory provider ourselves and ask it for its block.

    The standalone path, where no agent exists to borrow one from. The
    lifecycle is deliberate: ``system_prompt_block()`` is empty before
    ``initialize()`` for a real provider (hermes-homie-memory returns "" until
    its index exists), so initializing is required — and every provider we
    initialize we also shut down, because we own this instance. It is safe to
    do so: ``load_memory_provider`` builds a FRESH instance per call, so a
    running agent's own provider is untouched.
    """

    try:
        from plugins.memory import _get_active_memory_provider, load_memory_provider
    except Exception as exc:  # noqa: BLE001 — no Hermes, no provider section
        _log.debug("memory provider plugins unavailable: %s: %s", type(exc).__name__, exc)
        return ""

    provider = None
    try:
        active = _get_active_memory_provider()
        if not active:
            return ""
        provider = load_memory_provider(active)
        if provider is None or not provider.is_available():
            return ""
        provider.initialize(
            PROBE_SESSION_ID,
            platform="cli",
            hermes_home=str(talk_config.get_hermes_home()),
            # Non-primary: the ABC's contract is that providers skip writes
            # outside a primary agent context, and a voice prompt probe must
            # never write to the operator's memory store.
            agent_context="flush",
        )
        return (provider.system_prompt_block() or "").strip()
    except Exception as exc:  # noqa: BLE001 — a missing section, never an outage
        _log.debug("memory provider block unavailable: %s: %s", type(exc).__name__, exc)
        return ""
    finally:
        if provider is not None:
            try:
                provider.shutdown()
            except Exception as exc:  # noqa: BLE001 — teardown is best-effort
                _log.debug("provider shutdown failed: %s: %s", type(exc).__name__, exc)


def _resolve_memory_block() -> str:
    """The memory section: the live agent's block, else the configured one."""

    return _memory_block_from_agent() or _memory_block_from_provider()


class HostAdapter:
    """Binds hermes-talk to Hermes, degrading to speakable text off-host."""

    def identity_sections(self) -> dict[str, str]:
        """What the host knows, as ordered named sections for the prompt.

        This is what makes a voice session start already knowing the
        operator instead of having to ask. Every section is optional and
        independently defensive: a broken memory provider costs the MEMORY
        section, never the call. Returns ``{}`` when nothing resolves.

        Resolution is call-time, so a provider enabled after this module was
        imported is seen by the very next session.
        """

        sections: dict[str, str] = {}
        persona = _resolve_persona()
        if persona:
            sections["PERSONA"] = persona
        memory = _resolve_memory_block()
        if memory:
            sections["MEMORY"] = memory

        include = talk_config.identity_include()
        if include is None:
            return sections
        return {name: body for name, body in sections.items() if name in include}

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
        """Hand a self-contained task to a background Hermes agent.

        Three backends, tried in order, and every fall-through is ANNOUNCED in
        the returned text — a voice surface that quietly downgrades is worse
        than one that refuses:

        1. **The host's own agent loop** (``dispatch_tool``) when a plugin
           context is bound and Hermes has a parent agent to delegate into.
           Its result re-enters the conversation through Hermes itself.
        2. **A detached headless Hermes** (``hermes -z``) on a registry run
           thread. This is what makes ``delegate_task`` real in a standalone
           ``hermes talk``, where there IS no agent loop. Returns the
           WORK_STARTED sentinel; the session watcher speaks the result.
        3. Neither available — speakable refusal naming what is missing.

        ``background`` is accepted for the caller's mental model but never
        forwarded: Hermes documents the tool's own flag as deprecated and
        ignored, and both real backends are asynchronous regardless.
        """

        ctx = get_ctx()
        if ctx is not None:
            try:
                raw = ctx.dispatch_tool(DELEGATE_TOOL_NAME, {"goal": prompt})
            except Exception as exc:  # noqa: BLE001 — the model speaks the failure
                return f"I couldn't start that work: {type(exc).__name__}: {exc}"
            if not _agent_loop_absent(raw):
                return f"WORK_STARTED — {_speakable(raw)}"

        return self._run_detached_agent(prompt)

    def _run_detached_agent(self, prompt: str) -> str:
        """Tier 2/3: run the task as a detached ``hermes -z`` one-shot."""

        binary = hermes_binary()
        if binary is None:
            return (
                "I can't hand off work right now — there's no Hermes agent "
                "attached to this call and no `hermes` command on the PATH to "
                "run one."
            )
        label = prompt.strip()[:60]
        try:
            run_id = talk_runs.start_run(
                "agent", label, _detached_agent_worker(prompt, binary)
            )
        except Exception as exc:  # noqa: BLE001 — the model speaks the failure
            return f"I couldn't start that work: {type(exc).__name__}: {exc}"
        return (
            f"{talk_runs.started_sentinel(run_id, 'agent', label)} — running as a "
            "detached Hermes agent; I'll tell you when it lands."
        )


_HOST = HostAdapter()


def host() -> HostAdapter:
    """The shared adapter. Stateless — the ctx lives at module scope."""

    return _HOST


__all__ = [
    "AGENT_LOOP_ABSENT_MARKERS",
    "DELEGATE_TOOL_NAME",
    "HERMES_BINARY",
    "MAX_TOOL_OUTPUT_CHARS",
    "MEMORY_TOOL_NAME",
    "PROBE_SESSION_ID",
    "HostAdapter",
    "agent_argv",
    "bind_ctx",
    "get_ctx",
    "hermes_binary",
    "host",
]
