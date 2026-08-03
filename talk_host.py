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

Reaching a real agent is a three-tier chain, tried in order, and **every
fall-through is announced in the returned text**:

1. the bound plugin context's own agent loop (interactive ``/talk``)
2. a real Hermes agent over the api_server gateway platform
   (:mod:`talk_apiserver`) — the lane that makes the dashboard tab and a
   standalone ``hermes talk`` more than a fallback
3. what is left: a detached ``hermes -z`` for delegation, a spoken refusal
   naming what is missing for a memory lookup

Tiers 2 and 3 both return the ``WORK_STARTED`` receipt rather than an answer.
That is not a preference: the relay executes tools synchronously on the event
loop carrying the microphone, so a tool that waits for an agent is a call
that goes silent. :mod:`talk_runs` already owns "start it, speak it when it
lands", and both surfaces already watch for the receipt.

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
import time
from pathlib import Path
from typing import Any

try:
    from . import talk_apiserver, talk_auth, talk_config, talk_runs, talk_steer
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_apiserver
    import talk_auth
    import talk_config
    import talk_runs
    import talk_steer

_log = logging.getLogger(__name__)

#: Hermes's ``memory`` tool is a WRITE surface (add/replace/remove against the
#: durable memory file); saved memory itself is injected into every turn rather
#: than queried. ``session_search`` is the FTS5 READ surface, so that is what a
#: spoken "what do you remember about X" relays into.
MEMORY_TOOL_NAME = "session_search"
DELEGATE_TOOL_NAME = "delegate_task"

#: Hermes's child-scoped steering tool (hermes-agent#76805). Distinct from the
#: SESSION-scoped ``/steer`` that ships today: that one redirects the agent you
#: are talking to, this one redirects a named background child. Installs
#: without it fall back to :func:`_steer_via_registry`.
STEER_TOOL_NAME = "steer_subagent"

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

#: Which tier a real-agent request would take. ``LANE_NONE`` keeps the exact
#: wording the dashboard tile already shipped, so an install with no agent lane
#: reads the same as it did before the api_server lane existed.
LANE_ATTACHED = "attached"
LANE_API_SERVER = "api-server"
LANE_NONE = "out of process"

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
        # A bare {"error": ...} envelope (the registry's universal failure
        # shape) must read as failure even without a success key — otherwise
        # a refusal gets a success prefix bolted in front of it downstream.
        error = parsed.get("error")
        if parsed.get("success") is False or (
            parsed.get("success") is not True and isinstance(error, str) and error.strip()
        ):
            return f"that failed: {error or 'no reason given'}"[:limit]
        for key in ("result", "output", "transcript", "content", "message"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:limit]
    return json.dumps(parsed, default=str)[:limit]


def _agent_loop_absent(raw: Any, tool_name: str = DELEGATE_TOOL_NAME) -> bool:
    """True when a dispatch result says the AGENT LOOP is missing, not the task.

    Read off the registry's universal error envelope (``{"error": ...}``), and
    matched against named markers rather than "any error": falling back on a
    generic failure would route around an operator's paused delegation or a
    depth limit, which is the host's call, not this plugin's.

    ``tool_name`` adds that tool's own "unknown tool" marker, so a host with no
    ``session_search`` falls through to the api_server lane for the same reason
    a host with no ``delegate_task`` does. The default reproduces
    :data:`AGENT_LOOP_ABSENT_MARKERS` exactly.
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
    markers = (*AGENT_LOOP_ABSENT_MARKERS, f"unknown tool: {tool_name}")
    return any(marker in lowered for marker in markers)


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
        #
        # Popen (not subprocess.run) so the handle can be RETAINED — that
        # handle is the detached lane's only stop channel (stop_work →
        # talk_runs.terminate_process). run() would discard it.
        process = subprocess.Popen(
            agent_argv(binary, task, profile),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        talk_runs.register_process(run_id, process)
        try:
            out, err = process.communicate(timeout=talk_config.agent_timeout_s())
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
        finally:
            talk_runs.release_process(run_id)
        stdout = (out or "").strip()
        if process.returncode != 0:
            detail = (err or "").strip() or stdout or "no output"
            message = f"the agent exited {process.returncode}: {detail}"[
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


def _api_server_worker(task: str, *, session_id: str | None) -> Any:
    """Build the worker that runs one api_server agent run to completion.

    Runs on a :mod:`talk_runs` thread, which is the only place blocking is
    allowed — see :mod:`talk_apiserver`. Failures are marked failed with
    speakable text rather than raised, so the registry never puts an exception
    type in front of a sentence meant to be said out loud.
    """

    def worker(run_id: int) -> str:
        talk_runs.annotate_run(run_id, lane=LANE_API_SERVER)
        try:
            return talk_apiserver.run_to_completion(
                task,
                session_id=session_id,
                # The remote id is stop_work's only address for this run —
                # without it the lane is stop-capable in theory and
                # unstoppable in practice.
                on_start=lambda api_run_id: talk_runs.annotate_run(
                    run_id, api_run_id=api_run_id
                ),
            )[: talk_runs.HISTORY_OUTPUT_CAP]
        except talk_apiserver.TalkApiServerError as exc:
            message = str(exc)[-talk_runs.HISTORY_OUTPUT_CAP :]
            talk_runs.finish_run(run_id, "failed", message)
            return message

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

    def agent_lane(self) -> str:
        """Which tier a real-agent request would take RIGHT NOW.

        One of :data:`LANE_ATTACHED`, :data:`LANE_API_SERVER`,
        :data:`LANE_NONE`. Read by ``talk_status`` and by the dashboard's
        readiness tile, so the surface reports the lane it would actually use
        instead of a boolean that was true for a different reason.
        """

        if get_ctx() is not None:
            return LANE_ATTACHED
        try:
            if talk_apiserver.is_available():
                return LANE_API_SERVER
        except Exception as exc:  # noqa: BLE001 — a status read is never fatal
            _log.warning("api server lane check failed: %s: %s", type(exc).__name__, exc)
        return LANE_NONE

    def search_memory(self, query: str, limit: int = 5) -> str:
        """Search past Hermes sessions for what was said about ``query``.

        Three tiers, each fall-through said out loud (see the module docstring).
        Tier 2 answers with a receipt rather than the answer — an agent run is
        seconds of work and this call is on the loop carrying the microphone.
        """

        ctx = get_ctx()
        if ctx is not None:
            try:
                raw = ctx.dispatch_tool(
                    MEMORY_TOOL_NAME, {"query": query, "limit": limit}
                )
            except Exception as exc:  # noqa: BLE001 — the model speaks the failure
                return f"the memory lookup failed: {type(exc).__name__}: {exc}"
            if not _agent_loop_absent(raw, MEMORY_TOOL_NAME):
                return _speakable(raw)

        return self._search_memory_via_api_server(query, limit)

    def _search_memory_via_api_server(self, query: str, limit: int) -> str:
        """Tier 2/3 for a memory lookup."""

        verdict = talk_apiserver.status()
        if not verdict.available:
            # Detail LAST: it can itself be a two-clause sentence ("running but
            # rejected my key — set …"), and anything appended after that reads
            # as part of the remediation instead of as the refusal.
            return (
                "memory isn't available in this session — I can't look anything "
                f"up: I'm running outside a Hermes agent, and {verdict.detail}."
            )
        label = f"memory: {query.strip()[:50]}"
        prompt = (
            "Search this Hermes install's past sessions and saved memory for "
            f"anything about: {query.strip()}\n\n"
            f"Report the {max(1, min(limit, 8))} most relevant findings as plain "
            "spoken prose in a few sentences. No markdown, no bullet lists, no "
            "file paths. If you find nothing, say so plainly."
        )
        try:
            run_id = talk_runs.start_run(
                "agent", label, _api_server_worker(prompt, session_id=None)
            )
        except Exception as exc:  # noqa: BLE001 — the model speaks the failure
            return f"I couldn't start that lookup: {type(exc).__name__}: {exc}"
        return (
            f"{talk_runs.started_sentinel(run_id, 'agent', label)} — asking a "
            "Hermes agent through the api server; I'll tell you what it finds."
        )

    def run_agent(self, prompt: str, background: bool = True) -> str:
        """Hand a self-contained task to a background Hermes agent.

        Four backends, tried in order, and every fall-through is ANNOUNCED in
        the returned text — a voice surface that quietly downgrades is worse
        than one that refuses:

        1. **The host's own agent loop** (``dispatch_tool``) when a plugin
           context is bound and Hermes has a parent agent to delegate into.
           Its result re-enters the conversation through Hermes itself.
        2. **A real agent over the api_server** (``POST /v1/runs``) on a
           registry run thread. Preferred over a spawn: it reuses a warm,
           fully-tooled agent instead of paying a process start, and it is what
           makes delegation real in the dashboard tab.
        3. **A detached headless Hermes** (``hermes -z``) on a registry run
           thread — the lane that needs nothing enabled. Returns the
           WORK_STARTED sentinel; the session watcher speaks the result.
        4. None available — speakable refusal naming what is missing.

        ``background`` is accepted for the caller's mental model but never
        forwarded: Hermes documents the tool's own flag as deprecated and
        ignored, and every real backend is asynchronous regardless.
        """

        ctx = get_ctx()
        if ctx is not None:
            try:
                raw = ctx.dispatch_tool(DELEGATE_TOOL_NAME, {"goal": prompt})
            except Exception as exc:  # noqa: BLE001 — the model speaks the failure
                return f"I couldn't start that work: {type(exc).__name__}: {exc}"
            if not _agent_loop_absent(raw):
                spoken = _speakable(raw)
                if spoken.startswith("that failed"):
                    # A host refusal (paused delegation, depth limit) must
                    # never ride behind a WORK_STARTED prefix.
                    return f"I couldn't start that work — {spoken}"
                return f"WORK_STARTED — {spoken}"

        via_api_server = self._run_api_server_agent(prompt)
        if via_api_server is not None:
            return via_api_server
        return self._run_detached_agent(prompt)

    def _run_api_server_agent(self, prompt: str) -> str | None:
        """Tier 2: run the task on a real agent over the api_server.

        ``None`` means the lane is unavailable and the caller should fall
        through — a lane that cannot run must not consume the request.
        """

        if not talk_apiserver.is_available():
            return None
        label = prompt.strip()[:60]
        try:
            run_id = talk_runs.start_run(
                "agent", label, _api_server_worker(prompt, session_id=None)
            )
        except Exception as exc:  # noqa: BLE001 — the model speaks the failure
            return f"I couldn't start that work: {type(exc).__name__}: {exc}"
        return (
            f"{talk_runs.started_sentinel(run_id, 'agent', label)} — running on a "
            "Hermes agent through the api server; I'll tell you when it lands."
        )

    def _run_detached_agent(self, prompt: str) -> str:
        """Tier 3/4: run the task as a detached ``hermes -z`` one-shot."""

        binary = hermes_binary()
        if binary is None:
            return (
                "I can't hand off work right now — there's no Hermes agent "
                "attached to this call, the api server isn't reachable, and "
                "there's no `hermes` command on the PATH to run one."
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


    def steer_agent(self, agent_id: str, text: str) -> str:
        """Queue a redirection note into a RUNNING delegated child.

        The substrate contract this wording obeys: ``AIAgent.steer()`` is a
        QUEUE WRITE — ``True`` means queued, never delivered. Delivery has its
        own artifact (the drain INFO line, watched by :mod:`talk_steer`), so
        the call-time sentence claims queueing only and the ledger upgrades
        it to "landed" when the artifact fires.

        The ladder, most sanctioned first:

        1. ``delegate_tool.steer_subagent()`` when the host has it — the
           public module function from hermes-agent#76805 (`hasattr`-gated;
           merge day is a silent upgrade).
        2. The registry bridge (:func:`_steer_via_registry`) — one guarded
           read of the host's delegation registry, then the PUBLIC
           ``AIAgent.steer()``.

        Run NUMBERS are the api_server/detached lanes, which have no steer
        channel at all — those refuse with the one thing they CAN do (a real
        ``stop_work``).
        """

        text = (text or "").strip()
        if not text:
            return "I need the note itself before I can pass it along."

        # A bare number is a talk_runs id — the api_server and detached lanes.
        # Resolve it FIRST so those get their lane-specific refusal rather
        # than being tried as a subagent id and coming back "no such job".
        run = _registry_run(agent_id)
        if run is not None:
            return _unsteerable_run(run)

        module = _delegation_module()
        if module is None:
            return (
                "This Hermes build doesn't let me redirect running work — "
                "I can stop it instead."
            )

        steer = getattr(module, "steer_subagent", None)
        if callable(steer):
            # Arm the watcher BEFORE the queue write — a fast drain must
            # not beat the handler onto the logger.
            talk_steer.ensure_watcher()
            try:
                accepted = bool(steer(agent_id, text))
            except Exception as exc:  # noqa: BLE001 — the model speaks the failure
                return f"I couldn't get that through: {type(exc).__name__}: {exc}"
            if not accepted:
                # steer_subagent() False = unknown id, no live agent, OR a
                # steer failure the host swallowed — it cannot distinguish.
                # Say both possibilities instead of inventing one.
                return (
                    f"That didn't take — either {agent_id} already "
                    "finished, or the host refused the note. Want me to "
                    "list what's running?"
                )
            return _queued_reply(agent_id, text)

        # The host predates steer_subagent. Same registry, resolved here.
        return _steer_via_registry(agent_id, text)

    def list_agents(self) -> str:
        """Everything running or recent, tagged with what each can do.

        Discovery-first steering: the model resolves "the research one" HERE,
        against ids that exist right now, instead of reconstructing an id it
        heard once. Children come from the host's delegation registry
        (steerable); runs come from :mod:`talk_runs` (stop-only lanes).
        """

        lines: list[str] = []
        module = _delegation_module()
        if module is not None:
            try:
                children = module.list_active_subagents()
            except Exception as exc:  # noqa: BLE001 — a listing is never fatal
                _log.debug("list_active_subagents failed: %s", exc)
                children = []
            for child in children:
                sid = child.get("subagent_id")
                goal = str(child.get("goal") or "")[:60]
                age = ""
                started = child.get("started_at")
                if isinstance(started, (int, float)):
                    age = f", {max(0, int(time.time() - started))}s in"
                last_tool = child.get("last_tool")
                doing = f", running {last_tool}" if last_tool else ""
                lines.append(f"{sid} — {goal}{age}{doing} (can steer or stop)")
        for run in talk_runs.list_runs(limit=8, include_history=True):
            meta = run.get("meta") if isinstance(run.get("meta"), dict) else {}
            lane = meta.get("lane")
            status = run.get("status")
            if status == "running":
                tag = (
                    "stop only — api server run"
                    if lane == LANE_API_SERVER
                    else "stop only — detached run"
                )
            elif status == "lost":
                tag = "unreachable — started before this session"
            else:
                tag = status
            lines.append(f"run {run.get('runId')} — {run.get('label')} ({tag})")
        if not lines:
            return "Nothing is running and nothing recent has finished."
        return "; ".join(lines)

    def stop_work(self, target: str, reason: str | None = None) -> str:
        """Stop a running job on whichever lane it lives on.

        The one lifecycle verb every lane actually supports: children via the
        host's public ``interrupt_subagent()``, api_server runs via
        ``POST /v1/runs/{id}/stop``, detached one-shots via the retained
        process handle. Stopping a child DROPS any queued steer note by
        design (the host's ``clear_interrupt``), so matching receipts flip to
        superseded rather than lingering as "queued".
        """

        target = (target or "").strip()
        if not target:
            return "stop_work needs to know which job to stop."

        run = _registry_run(target)
        if run is not None:
            if run.get("status") in talk_runs.TERMINAL_STATUSES:
                return f"run {run.get('runId')} already finished."
            meta = run.get("meta") if isinstance(run.get("meta"), dict) else {}
            if meta.get("lane") == LANE_API_SERVER:
                api_run_id = meta.get("api_run_id")
                if not isinstance(api_run_id, str) or not api_run_id:
                    return (
                        "I can't stop that one — the api server never told me "
                        "its run id."
                    )
                try:
                    talk_apiserver.stop_run(api_run_id)
                except talk_apiserver.TalkApiServerError as exc:
                    return f"the stop didn't go through: {exc}"
                # 2xx = the server ACCEPTED the stop ("stopping"), not
                # that the agent is gone — say the request, not the outcome.
                return (
                    f"Sent the stop for run {run.get('runId')} — the "
                    "server is winding it down."
                )
            if talk_runs.terminate_process(int(run["runId"])):
                # terminate() is a signal, not a wait — winding down, not
                # proven gone.
                return f"Sent the stop for run {run.get('runId')} — it's winding down."
            return (
                "I couldn't stop that one — I don't hold a handle to its "
                "process anymore."
            )

        module = _delegation_module()
        interrupt = getattr(module, "interrupt_subagent", None) if module else None
        if not callable(interrupt):
            return "This Hermes build doesn't let me stop running work from here."
        try:
            stopped = bool(interrupt(target))
        except Exception as exc:  # noqa: BLE001 — the model speaks the failure
            return f"the stop didn't go through: {type(exc).__name__}: {exc}"
        if not stopped:
            return f"I don't see a running job called {target}."
        talk_steer.mark_superseded(target)
        # interrupt_subagent() REQUESTS an interrupt at the next boundary —
        # the child is stopping, not proven stopped.
        return (
            f"Asked {target} to stop — it winds down at its next step, "
            "and any note it hadn't read is dropped."
        )


_HOST = HostAdapter()


def _registry_run(target: str) -> dict | None:
    """The talk_runs record ``target`` names, or ``None`` if it names none.

    Only a bare integer can be a run id. A subagent id is an opaque string and
    must never be coerced into one.
    """

    try:
        run_id = int(str(target).strip())
    except (TypeError, ValueError):
        return None
    return talk_runs.get_run(run_id)


def _delegation_module() -> Any | None:
    """The host's ``tools.delegate_tool`` module, or ``None`` off-host.

    The ladder's single import seam: everything steer/stop touches on the
    attached lane resolves through this one guarded lookup, so a host without
    the module degrades to one refusal instead of scattered exceptions.
    """

    try:
        from tools import delegate_tool  # host-only import, lazy by design
    except Exception as exc:  # noqa: BLE001 — no Hermes tools in this process
        _log.debug("delegation module unavailable: %s: %s", type(exc).__name__, exc)
        return None
    return delegate_tool


def degrade_gone_children() -> None:
    """Flip queued notes to unconfirmed when their child left the registry.

    A note that stays 'queued' after its agent is gone is the exact
    overclaim the ledger exists to prevent — nobody is left to drain it.
    """

    module = _delegation_module()
    if module is None:
        return
    try:
        live = {c.get("subagent_id") for c in module.list_active_subagents()}
    except Exception:  # noqa: BLE001 — a bookkeeping sweep is never fatal
        return
    for sid in talk_steer.queued_subagent_ids():
        if sid not in live:
            talk_steer.mark_child_gone(sid)


def _queued_reply(subagent_id: str, text: str) -> str:
    """The call-time sentence for an ACCEPTED steer — claims queueing only.

    Ledgers the receipt and arms the drain watcher; "landed" is spoken later,
    by check_work, when (and only when) the drain artifact fires.
    """

    talk_steer.record_queued(subagent_id, text)
    watching = talk_steer.ensure_watcher()
    if watching:
        return (
            f"Passed it along to {subagent_id} — it's queued for their next "
            "step. I'll confirm when it lands."
        )
    return (
        f"Passed it along to {subagent_id} — it's queued for their next step. "
        "I can't watch for delivery on this build, so I won't know if it lands."
    )


def _unsteerable_run(run: dict) -> str:
    """Why this registry run cannot be steered, in words a voice can say.

    Every stop offered here is REAL: api_server runs stop via
    ``POST /v1/runs/{id}/stop``, detached runs via the retained process
    handle — both wired in :meth:`HostAdapter.stop_work`.
    """

    if run.get("status") in talk_runs.TERMINAL_STATUSES:
        return (
            f"run {run.get('runId')} already finished, so there's nothing left "
            "to redirect."
        )
    # Worker-observed facts land under ``meta`` (talk_runs.annotate_run), never
    # at the top level — reading ``run["lane"]`` silently mislabels every
    # api_server run as detached.
    meta = run.get("meta")
    lane = meta.get("lane") if isinstance(meta, dict) else None
    if lane == LANE_API_SERVER:
        return (
            f"Run {run.get('runId')} goes through the api server — I can't "
            "pass it notes, but I can try stopping it and restarting with "
            "your change. Want that?"
        )
    return (
        f"Run {run.get('runId')} is a detached one-shot — no way to reach it "
        "mid-run, but I can try stopping it and restarting with your change. "
        "Want that?"
    )


def _steer_via_registry(subagent_id: str, text: str) -> str:
    """Steer a live child by resolving Hermes's own subagent registry.

    The bridge for installs without ``steer_subagent``. Everything it touches
    is module state inside the SAME process, so this is a lookup rather than
    an RPC — one private dict read (``_active_subagents``), then the PUBLIC
    ``AIAgent.steer()``. Every step is guarded and a missing piece degrades
    to a spoken refusal instead of an exception.
    """

    delegate_tool = _delegation_module()
    if delegate_tool is None:
        return (
            "This Hermes build doesn't let me redirect running work — "
            "I can stop it instead."
        )

    registry = getattr(delegate_tool, "_active_subagents", None)
    if not isinstance(registry, dict):
        return (
            "I can't redirect running work on this Hermes version — its "
            "delegation registry isn't in the shape I know how to read."
        )

    lock = getattr(delegate_tool, "_active_subagents_lock", None)
    if lock is not None:
        with lock:
            record = registry.get(subagent_id)
            live = sorted(registry)
    else:  # pragma: no cover - every shipped Hermes has the lock
        record = registry.get(subagent_id)
        live = sorted(registry)

    if record is None:
        if not live:
            return "Nothing is running right now, so there's nothing to redirect."
        return (
            f"I don't have a running job called {subagent_id}. Running now: "
            f"{', '.join(live[:5])}."
        )

    agent = record.get("agent")
    if agent is None or not hasattr(agent, "steer"):
        return (
            f"I found {subagent_id} but can't reach it to redirect — it has no "
            "live agent behind it anymore."
        )

    try:
        accepted = bool(agent.steer(text))
    except Exception as exc:  # noqa: BLE001 — the model speaks the failure
        return f"I couldn't get that through to {subagent_id}: {type(exc).__name__}: {exc}"

    if not accepted:
        # AIAgent.steer() returns False ONLY for empty text (run_agent.py:
        # 3218-3219) — and empty text was rejected before the ladder. So a
        # False here is a contract change on the host side, not a state of
        # the child; say that instead of inventing a diagnosis.
        return (
            f"That didn't go through — {subagent_id} refused the note in a "
            "way this Hermes version shouldn't."
        )
    return _queued_reply(subagent_id, text)


def host() -> HostAdapter:
    """The shared adapter. Stateless — the ctx lives at module scope."""

    return _HOST


__all__ = [
    "AGENT_LOOP_ABSENT_MARKERS",
    "DELEGATE_TOOL_NAME",
    "HERMES_BINARY",
    "LANE_API_SERVER",
    "LANE_ATTACHED",
    "LANE_NONE",
    "MAX_TOOL_OUTPUT_CHARS",
    "MEMORY_TOOL_NAME",
    "PROBE_SESSION_ID",
    "STEER_TOOL_NAME",
    "HostAdapter",
    "agent_argv",
    "bind_ctx",
    "get_ctx",
    "hermes_binary",
    "host",
]
