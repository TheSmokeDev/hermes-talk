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
from pathlib import Path
from typing import Any

try:
    from . import talk_apiserver, talk_auth, talk_config, talk_runs
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_apiserver
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
        if parsed.get("success") is False:
            return f"that failed: {parsed.get('error') or 'no reason given'}"[:limit]
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
            return talk_apiserver.run_to_completion(task, session_id=session_id)[
                : talk_runs.HISTORY_OUTPUT_CAP
            ]
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
                return f"WORK_STARTED — {_speakable(raw)}"

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


    def steer_run(self, target: str, text: str) -> str:
        """Redirect a RUNNING background job without stopping it.

        Only one of the three delegation lanes can carry a steer, so the other
        two name the reason instead of failing quietly:

        1. **Attached** — the child is live in this process. Preferred path is
           the host's own ``steer_subagent`` tool; on an install that predates
           it (hermes-agent#76805) we resolve the same registry ourselves,
           because ``AIAgent.steer()`` and ``_active_subagents`` have both
           shipped in ``main`` far longer than the tool that addresses a child
           by id.
        2. **api_server** — ``/v1/runs/{id}`` exposes ``stop`` and nothing
           else. A run there can be killed, never redirected.
        3. **detached ``hermes -z``** — a one-shot process with no inbound
           channel at all.

        Steering is not interrupting. The text arrives at the agent AFTER its
        next tool call, so a job already past its final tool call finishes
        without ever seeing it — which is why the reply says "passed it along"
        rather than promising the agent acted on it.
        """

        text = (text or "").strip()
        if not text:
            return "I need something to tell it before I can redirect it."

        # A bare number is a talk_runs id — the api_server and detached lanes.
        # Resolve it FIRST so those get their own lane-specific refusal rather
        # than being tried as a subagent id and coming back "no such job".
        run = _registry_run(target)
        if run is not None:
            return _unsteerable_run(run)

        ctx = get_ctx()
        if ctx is None:
            return (
                "I can't redirect that from here — I'm running outside a Hermes "
                "agent, so there's no live job in this process to reach."
            )

        try:
            raw = ctx.dispatch_tool(
                STEER_TOOL_NAME, {"subagent_id": target, "text": text}
            )
        except Exception as exc:  # noqa: BLE001 — the model speaks the failure
            return f"I couldn't get that through: {type(exc).__name__}: {exc}"
        if not _agent_loop_absent(raw, STEER_TOOL_NAME):
            return f"passed it along to {target}: {_speakable(raw)}"

        # The host has no steer_subagent tool. Same registry, resolved here.
        return _steer_via_registry(target, text)


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


def _unsteerable_run(run: dict) -> str:
    """Why this registry run cannot be steered, in words a voice can say."""

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
            f"I can't redirect run {run.get('runId')} — it's running on a Hermes "
            "agent through the api server, and that only lets me stop a run, "
            "not steer it. Want me to stop it and start over?"
        )
    return (
        f"I can't redirect run {run.get('runId')} — it's a detached one-shot "
        "Hermes process, so there's no way to reach it once it's going. Want me "
        "to stop it and start over?"
    )


def _steer_via_registry(subagent_id: str, text: str) -> str:
    """Steer a live child by resolving Hermes's own subagent registry.

    The bridge for installs without ``steer_subagent``. Everything it touches
    is module state inside the SAME process, so this is a lookup rather than
    an RPC — but it is private host internals, so every step is guarded and a
    missing piece degrades to a spoken refusal instead of an exception.
    """

    try:
        from tools import delegate_tool  # host-only import, lazy by design
    except Exception as exc:  # noqa: BLE001 — no Hermes tools importable here
        _log.debug("steer bridge unavailable: %s: %s", type(exc).__name__, exc)
        return (
            "I can't redirect running work on this Hermes version — it has no "
            "steer_subagent tool and I can't reach the delegation registry."
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
        return (
            f"{subagent_id} didn't take it — it's most likely past its last "
            "tool call, so it'll finish on the original brief."
        )
    # "Queued", not "done": delivery happens at the child's next tool-result
    # boundary, and a child with no boundary left never sees it. Promising
    # more than that is the failure mode the whole plugin is built against.
    return (
        f"Passed it along to {subagent_id} — it'll pick that up after its next "
        "step."
    )


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
