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

1. the bound plugin context's own agent loop (interactive ``/talk``). For a
   memory READ this is two tools, not one: ``session_search`` for what was
   actually said, then the Honcho memory plugin for what was remembered
   about the operator — the second answers with its provenance said out
   loud, because a remembered fact can be stale in a way a transcript
   line cannot
2. a real Hermes agent over the api_server gateway platform
   (:mod:`talk_apiserver`) — the lane that makes the dashboard tab and a
   standalone ``hermes talk`` more than a fallback
3. what is left: a detached ``hermes -z`` for delegation, a spoken refusal
   naming what is missing for a memory lookup

Tiers 2 and 3 both return the ``WORK_STARTED`` receipt rather than an answer.
That is not a preference: relay tool waits are bounded, so work that takes an
agent turn cannot return its answer through the original function call.
:mod:`talk_runs` already owns "start it, speak it when it lands", and both
surfaces already watch for the receipt.

The detached backend spawns ``hermes [--profile <name>] -z <task>``. The
profile comes from ``TALK_AGENT_PROFILE`` or, unset, from auto-detection in
:mod:`talk_config` — on an install whose model config lives only in a profile,
a bare ``hermes -z`` cannot resolve a model and the child dies immediately.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

try:
    from . import (
        talk_apiserver,
        talk_approvals,
        talk_auth,
        talk_config,
        talk_progress,
        talk_runs,
        talk_steer,
        talk_vault,
    )
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_apiserver
    import talk_approvals
    import talk_auth
    import talk_config
    import talk_progress
    import talk_runs
    import talk_steer
    import talk_vault

_log = logging.getLogger(__name__)

#: How long a stop verb politely waits for its confirmation before detaching
#: (hermes-talk#2). Long enough that the common fast path (an HTTP 2xx or a
#: child dying on SIGTERM) speaks the REAL result; short enough that a wedged
#: server can never dead-air the voice loop the way the old unbounded call
#: could (~6s worst case, measured).
STOP_CONFIRM_WAIT_S = 1.5

#: How long the DETACHED confirmation keeps waiting after the polite wait
#: gave up. Tests shrink this; production keeps it generous — the receipt
#: lands in the run's meta whenever it resolves.
STOP_LATE_CONFIRM_S = 30.0


def _spawn_daemon(fn, *args, name: str = "talk-stop") -> None:
    """Fire-and-forget worker. DAEMON by design: a stop confirmation (or a
    wedged memory dispatch) still in flight when the operator hangs up must
    never stall process exit (ThreadPoolExecutor threads are non-daemon and
    joined at shutdown — exactly the wrong contract for this)."""

    threading.Thread(target=fn, args=args, daemon=True, name=name).start()

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

def _catalog_from_host_modules() -> dict | None:
    """Enumerate the capability catalog from the host's own registries.

    ``None`` means "no answer here" — the host modules are absent or one of
    the reads failed — never "nothing is installed". The skills and toolsets
    reads are the SAME builders the api_server's catalog routes run
    (``gateway/platforms/api_server.py`` ``_handle_skills`` /
    ``_handle_toolsets``), so the in-process tier and the REST tier answer
    from one source of truth instead of drifting apart.

    The ``tools`` read is the LIVE answer: ``get_tool_definitions`` applies
    the registry's ``check_fn`` availability gates (a tool whose driver or
    keys are missing simply does not resolve), which the static
    enabled/configured toolset flags cannot see. It degrades to an empty
    list on its own failure rather than failing the catalog — the REST tier
    carries no such field, so nothing downstream may treat empty as
    "nothing resolved".

    All-or-nothing on skills/toolsets, matching :mod:`talk_capabilities`'s
    REST doctrine: a half-read catalog would be spoken as though the missing
    half did not exist.
    """

    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import (
            _get_effective_configurable_toolsets,
            _get_platform_tools,
            _toolset_has_keys,
            get_nous_subscription_features,
        )
        from tools.skills_tool import _find_all_skills, _sort_skills
        from toolsets import resolve_toolset
    except Exception as exc:  # noqa: BLE001 — no host registries in this process
        _log.debug("in-process catalog modules unavailable: %s: %s", type(exc).__name__, exc)
        return None
    try:
        skills = _sort_skills(_find_all_skills(skip_disabled=False))
        config = load_config()
        enabled_toolsets = _get_platform_tools(
            config, "api_server", include_default_mcp_servers=False
        )
        features = get_nous_subscription_features(config)
        toolsets: list[dict] = []
        for name, label, desc in _get_effective_configurable_toolsets():
            try:
                tools = sorted(set(resolve_toolset(name)))
            except Exception:  # noqa: BLE001 — the route degrades the same way
                tools = []
            toolsets.append(
                {
                    "name": name,
                    "label": label,
                    "description": desc,
                    "enabled": name in enabled_toolsets,
                    "configured": _toolset_has_keys(name, config, features=features),
                    "tools": tools,
                }
            )
    except Exception as exc:  # noqa: BLE001 — half a catalog is no catalog
        _log.debug("in-process catalog read failed: %s: %s", type(exc).__name__, exc)
        return None

    resolved: list[str] = []
    try:
        from model_tools import get_tool_definitions

        resolved = sorted(
            {
                str(definition["function"]["name"])
                for definition in get_tool_definitions(
                    quiet_mode=True, skip_tool_search_assembly=True
                )
                if isinstance(definition, dict)
                and isinstance(definition.get("function"), dict)
                and isinstance(definition["function"].get("name"), str)
            }
        )
    except Exception as exc:  # noqa: BLE001 — liveness is additive, not required
        _log.debug("in-process resolved-tool read failed: %s: %s", type(exc).__name__, exc)

    return {
        "skills": [entry for entry in skills if isinstance(entry, dict)],
        "toolsets": toolsets,
        "tools": resolved,
        # The in-process tier has no gateway feature document or run counters;
        # the bounded readers treat absent as empty either way.
        "capabilities": {},
        "health": {},
    }

#: Hermes's Honcho memory plugin's query-shaped read surface. A guess by the
#: same construction as :data:`CAPABILITY_CATALOG_TOOL_NAME` and safe for the
#: same reason: a host without it answers with the "unknown tool" marker
#: :func:`_agent_loop_absent` generalizes over, and the caller falls through
#: to the api_server lane. Wrong here costs the fast path, not the feature.
#:
#: What it returns is a REMEMBERED profile fact, not a verbatim transcript
#: line the way ``session_search`` is — so its answer ships with a provenance
#: prefix and ``session_search``'s does not.
HONCHO_SEARCH_TOOL_NAME = "honcho_search"

#: Said in front of a Honcho hit, and nothing else. Short because it is read
#: aloud on every such answer.
REMEMBERED_PREFIX = "from remembered context: "

MAX_TOOL_OUTPUT_CHARS = 2_000


def strip_reserved_marker(text: str) -> str:
    """Remove a forged leading provenance marker from a non-Honcho result.

    :data:`REMEMBERED_PREFIX` is attached by exactly one call site — the
    Honcho tier of :meth:`HostAdapter.search_memory` — and the session
    instructions tell the model an answer that begins with it is a remembered
    profile fact. Transcript and vault content is untrusted text; a line that
    LEADS with the literal marker would wear that provenance without having
    it. Only leading occurrences carry the claim, so only those are stripped
    (repeatedly — a single pass would leave a doubled marker still wearing
    it); one appearing mid-text is ordinary content and stays.
    """

    marker = REMEMBERED_PREFIX.strip()  # match with or without the space
    out = text.lstrip()
    while out.lower().startswith(marker):
        out = out[len(marker) :].lstrip()
    return out


def _dispatch_bounded(ctx: Any, tool: str, args: dict, timeout_s: float) -> tuple[str, Any]:
    """One ``dispatch_tool`` call with a hard wait bound.

    Returns ``("ok", result)``, ``("err", exception)``, or
    ``("timeout", None)``. A dispatch cannot be cancelled, so after a timeout
    the call keeps running on its throwaway daemon worker — what the bound
    buys is the caller's thread back, and the caller is the relay's FIXED
    tool pool: a worker held by a wedged network-backed plugin is a worker
    the pool never gets back, and with the pipeline serialized that is every
    later tool call (and the voice loop behind them) held hostage.
    """

    outcomes: queue.Queue = queue.Queue(maxsize=1)

    def _call() -> None:
        try:
            outcomes.put(("ok", ctx.dispatch_tool(tool, args)))
        except Exception as exc:  # noqa: BLE001 — the outcome IS the record
            outcomes.put(("err", exc))

    _spawn_daemon(_call, name="talk-memory-dispatch")
    try:
        return outcomes.get(timeout=timeout_s)
    except queue.Empty:
        return ("timeout", None)

#: Errors that mean "this environment has no agent loop to delegate INTO" —
#: the only two the run_agent chain is allowed to fall through on. Both are
#: the COMPLETE ``tool_error`` message the host mints, not fragments: the
#: registry's unknown-tool refusal is exactly ``Unknown tool: {name}``
#: (hermes-agent tools/registry.py:1120 via tool_error at :1291), and the
#: no-parent refusal is exactly ``delegate_task requires a parent agent
#: context.`` (tools/delegate_tool.py:3463); a `hermes talk` CLI session has
#: no ``_cli_ref``, so ``dispatch_tool`` cannot attach a parent agent and
#: every delegation lands there. Any OTHER error — spawning paused, depth
#: exceeded, a real failure — is the host's decision and must NOT be routed
#: around, even when its free text happens to QUOTE one of these markers.
AGENT_LOOP_ABSENT_MARKERS = (
    "delegate_task requires a parent agent context.",
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

    The match is the WHOLE error string (case-insensitive, outer whitespace
    ignored), never a substring: both marker shapes are complete
    ``tool_error`` messages the host mints verbatim (see
    :data:`AGENT_LOOP_ABSENT_MARKERS`), and a substring match misread a real
    refusal that merely QUOTED a marker in its free text as "no loop here".

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
    lowered = error.strip().lower()
    markers = (*AGENT_LOOP_ABSENT_MARKERS, f"unknown tool: {tool_name}")
    return any(lowered == marker for marker in markers)


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

    The scoping key is read HERE rather than passed in by each caller. Every
    api_server run this plugin starts belongs to the same operator scope, so
    a parameter would only add a way to forget it at a call site added later
    — and a run that silently lost its scope reads as working, right up until
    the operator notices the host remembers nothing across a ``/clear``. Read
    at dispatch time, so a knob set after import is honoured by the next run.
    """

    session_key = talk_config.session_key()

    def worker(run_id: int) -> str:
        talk_runs.annotate_run(run_id, lane=LANE_API_SERVER)

        def _on_start(api_run_id: str) -> None:
            # The remote id is stop_work's only address for this run —
            # without it the lane is stop-capable in theory and
            # unstoppable in practice. It is also the ONLY handle a
            # reconnect could resume tracking by, and in memory alone it
            # died with the process that is, by definition, the one that
            # is gone — so it rides the STRICT locked append
            # (durable=True: retried once, escalated to an error log if
            # it still cannot land), never the fail-open telemetry tee.
            talk_runs.annotate_run(run_id, durable=True, api_run_id=api_run_id)
            # The spoken approval bridge: the SSE sidecar that turns the
            # run's approval.request events into a spoken prompt. One daemon
            # thread per run, closed by the host when the run ends.
            talk_approvals.watch_run(run_id, api_run_id)

        try:
            return talk_apiserver.run_to_completion(
                task,
                session_id=session_id,
                session_key=session_key,
                on_start=_on_start,
                # Progress tap (hermes-talk#33): each poll's status payload is
                # THIS run's own — the per-run addressing is what makes the
                # projection incapable of cross-routing. The payload's
                # last_event maps to a bounded phase; its session id is the
                # correlator the same-process hook projection keys on.
                on_event=lambda payload: talk_progress.project_api_poll(
                    run_id, payload
                ),
            )[: talk_runs.HISTORY_OUTPUT_CAP]
        except talk_apiserver.TalkApiServerError as exc:
            message = str(exc)[-talk_runs.HISTORY_OUTPUT_CAP :]
            talk_runs.finish_run(run_id, "failed", message)
            return message

    return worker


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


#: Where Hermes keeps the two identity files the text agent already reads.
#: The voice session read them directly rather than through an agent, because
#: the gateway and the dashboard both run without one — the path that needs
#: this most is the one that has no ``_cli_ref``.
_IDENTITY_FILES = {
    "USER": ("memories/USER.md", "user_char_limit"),
    "MEMORY": ("memories/MEMORY.md", "memory_char_limit"),
    # Curated by the OPERATOR, not written by the model: who they are, which
    # repos and plugins they mean by name, what an alias maps to. Registered
    # here rather than read specially, so it inherits the same per-entry scan
    # and host budget as the two above — a hand-written file is still a file
    # anything with disk access can append to. ``working_char_limit`` need not
    # exist in a host's config.yaml: an absent key reads as 0 ("no host
    # opinion") and the plugin's own cap applies.
    "WORKING": ("memories/WORKING.md", "working_char_limit"),
}

#: How Hermes separates memory entries on disk (``tools/memory_tool.py:59``).
#: Scanning has to be PER ENTRY like the host's own sanitizer, not per file:
#: one poisoned entry must cost that entry, not the operator's whole profile.
IDENTITY_ENTRY_DELIMITER = "\n§\n"

#: The scope Hermes uses for these files. Deliberately BROADER than the
#: ``context`` scope it applies to SOUL.md — these are written by the model
#: from conversation content, so they carry the lower trust.
IDENTITY_SCAN_SCOPE = "strict"


def _sanitize_identity_entries(body: str, filename: str) -> str:
    """Replace threat-matching entries with a placeholder, per entry.

    Mirrors ``MemoryStore._sanitize_entries_for_snapshot``
    (``tools/memory_tool.py:207-240``). Reading these files directly gets the
    plugin off the agent-only path, but it must not get it off the SCAN — a
    memory entry the text prompt would block is strictly more dangerous in a
    voice prompt, where nobody is reading the screen.

    **Fails CLOSED, unlike every other resolver here.** If the scanner cannot
    be imported or throws, the section is dropped rather than passed through:
    everywhere else a failure costs a section, and here passing through IS the
    failure.
    """

    try:
        from tools.threat_patterns import scan_for_threats
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "identity scan unavailable (%s: %s) — dropping %s rather than "
            "injecting it unscanned",
            type(exc).__name__,
            exc,
            filename,
        )
        return ""

    kept: list[str] = []
    for entry in body.split(IDENTITY_ENTRY_DELIMITER):
        # Only genuinely EMPTY entries skip the scan. An entry that merely
        # LOOKS already-blocked does not: the marker is unauthenticated text
        # inside the very file this scan treats as untrusted, so exempting it
        # hands the bypass to exactly the attacker the scan exists to stop —
        # prefix a payload with "[BLOCKED:" and it rides into the prompt
        # verbatim. The placeholder emitted below contains no threat
        # patterns, so a real one survives a re-scan and needs no exemption.
        if not entry.strip():
            kept.append(entry)
            continue
        try:
            findings = scan_for_threats(entry, scope=IDENTITY_SCAN_SCOPE)
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "identity scan failed on %s (%s: %s) — dropping the entry",
                filename,
                type(exc).__name__,
                exc,
            )
            findings = ["scan_failed"]
        if findings:
            _log.warning(
                "identity entry from %s blocked: %s", filename, ", ".join(findings)
            )
            kept.append(
                f"[BLOCKED: {filename} entry contained threat pattern(s): "
                f"{', '.join(findings)}. Removed from the voice prompt.]"
            )
        else:
            kept.append(entry)
    return IDENTITY_ENTRY_DELIMITER.join(kept).strip()


def _identity_file(section: str) -> str:
    """One of Hermes's own identity files, capped by the host's own budget.

    Hermes puts ``MEMORY.md`` and ``USER.md`` on the agent's memory STORE and
    injects them into the text agent's system prompt; the memory MANAGER this
    module reads elsewhere holds external providers only. So a voice session
    could never see them, on any lane, no matter what was configured — the
    files were structurally out of reach rather than absent.

    Read from disk so this works with no agent and no plugin context: the
    lanes that need it most (gateway, dashboard) have neither.

    Reading raw does NOT mean reading unscanned. Hermes sanitizes these two
    per ENTRY at snapshot time (``MemoryStore._sanitize_entries_for_snapshot``,
    ``tools/memory_tool.py:207-240``) using the ``strict`` threat scope —
    which is BROADER than the ``context`` scope it applies to SOUL.md, because
    these files are written by the model from conversation content and then
    injected into every future turn. Bypassing that would let the voice prompt
    carry an entry the text prompt blocks, which is a strictly worse place for
    it to land: a live call has no one reading the screen. So the same scan
    runs here, per entry, with the same placeholder.

    One deliberate asymmetry remains: **the char limits are the host's WRITE
    budget**, not a read truncation. Hermes renders the whole file and uses
    these numbers to tell the model when to consolidate
    (``tools/memory_tool.py:663-676``). Reused here as a cap because a
    Realtime prompt is resident and re-charged every turn. On a file the host
    keeps within budget this trims nothing; it is the floor under one that has
    run over.
    """

    entry = _IDENTITY_FILES.get(section.upper())
    if entry is None:
        return ""
    relative, limit_key = entry
    try:
        path = talk_config.get_hermes_home() / relative
        body = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""  # absent is a missing section, never an error
    except Exception as exc:  # noqa: BLE001 — a section, never an outage
        _log.debug("identity file %s unavailable: %s: %s", relative, type(exc).__name__, exc)
        return ""
    if not body:
        return ""
    body = _sanitize_identity_entries(body, relative)
    if not body:
        return ""
    limit = talk_config.identity_char_limit(limit_key)
    return body[:limit] if limit else body


def _vault_pointer() -> str:
    """The one true sentence about vault recall, authored here.

    NOT the provider's own ``system_prompt_block``. That block exists to tell
    a TEXT agent which tools to call (``homie_memory_search``,
    ``homie_memory_context``, …) — tool names that are real in that registry
    and absent from a Realtime session's. Passing it through spent identity
    budget instructing the model to call things it does not have, and every
    provider's block has that shape by construction, so the passthrough was
    wrong as a class rather than for one provider.

    This says the same thing about a capability the session actually has, and
    only when it has it (:func:`talk_vault.available`).
    """

    try:
        if not talk_vault.available():
            return ""
        count = talk_vault.document_count()
    except Exception as exc:  # noqa: BLE001 — a missing pointer, never an outage
        _log.debug("vault pointer unavailable: %s: %s", type(exc).__name__, exc)
        return ""
    scope = f" ({count:,} documents indexed)" if count else ""
    return (
        f"Long-term written notes{scope} are searchable with the search_vault "
        "tool — use it for anything the operator would have written down "
        "rather than said."
    )


def _resolve_operator_pointer() -> str:
    """The one true sentence about looking up a spoken name, authored here.

    Same contract as :func:`_vault_pointer`: names a capability the session
    really has, only when it has one, and never inlines the content itself.

    Gated on a bound context rather than on a probe. There is no cheap
    ``available()`` for a remote tool the way :mod:`talk_vault` has one, and
    the alternative — a speculative ``dispatch_tool`` at every session mint —
    would pay a real round trip before the operator has said anything.
    :meth:`HostAdapter.capability_catalog_probe` may spend that call only
    because :mod:`talk_capabilities` caches its result behind a TTL; this
    sentence has no such cache, so ctx presence is the signal.

    The TOOL POINTER is all that lives behind this gate. The
    ask-before-acting rule that used to trail it depends on no tool and now
    rides :data:`talk_identity.ANTI_GUESS_RULE` in the preamble, which ships
    on every lane — this gate used to take the rule down with the pointer on
    exactly the ctx-less lanes (gateway, dashboard) where nobody is watching
    a silent wrong pick go by, and a pinned ``TALK_IDENTITY_INCLUDE`` or a
    failed scan would have dropped a section-borne rule the same way.
    """

    if get_ctx() is None:
        return ""
    return (
        "search_memory can also look up people, repos, and aliases that are "
        "not listed above."
    )


def _resolve_working_block() -> str:
    """The trailing half of the working section: what can be LOOKED UP.

    Ordered after the curated file in :meth:`HostAdapter.identity_sections`
    for the reason :func:`_resolve_memory_block` gives — the cap trims from
    the tail, and a known alias outranks the sentence about finding one.
    """

    return _resolve_operator_pointer()


def _resolve_memory_block() -> str:
    """The trailing half of the memory section: what can be LOOKED UP.

    Ordered after the durable files in :meth:`HostAdapter.identity_sections`
    on purpose — what the session already knows outranks what it could go
    fetch, and the cap trims from the tail.
    """

    return _vault_pointer()


class HostAdapter:
    """Binds hermes-talk to Hermes, degrading to speakable text off-host."""

    def diagnostic_identity_sections(self) -> dict[str, str]:
        """File-backed identity presence/size input for read-only diagnostics.

        Unlike :meth:`identity_sections`, this calls neither Hermes's persona
        loader (which provisions a default home/SOUL.md) nor a vault provider.
        It reads only already-existing files. The returned content stays
        in-process; doctor emits only post-cap character counts.
        """

        sections: dict[str, str] = {}
        home = talk_config.get_hermes_home()
        files = {
            "PERSONA": ("SOUL.md", None),
            "USER": ("memories/USER.md", "user_char_limit"),
            "MEMORY": ("memories/MEMORY.md", "memory_char_limit"),
            # File only, no pointer: diagnostics report what is ON DISK, and
            # the pointer is a property of the live session, not of the box.
            "WORKING": ("memories/WORKING.md", "working_char_limit"),
        }
        for name, (relative, limit_key) in files.items():
            try:
                body = (home / relative).read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
            except OSError:
                continue
            if not body:
                continue
            limit = talk_config.identity_char_limit(limit_key) if limit_key else 0
            sections[name] = body[:limit] if limit else body

        include = talk_config.identity_include()
        if include is None:
            return sections
        return {name: body for name, body in sections.items() if name in include}

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
        user = _identity_file("USER")
        if user:
            sections["USER"] = user
        # Real memory first, the provider's capability pointer after it: one
        # is what the session knows, the other is what it can go look up, and
        # the budget should go to the former.
        memory_parts = [
            part
            for part in (_identity_file("MEMORY"), _resolve_memory_block())
            if part
        ]
        if memory_parts:
            sections["MEMORY"] = "\n\n".join(memory_parts)
        # Same two-part shape, same reason for the order: the curated operator
        # file is what the session KNOWS, the pointer is what it can go find.
        working_parts = [
            part
            for part in (_identity_file("WORKING"), _resolve_working_block())
            if part
        ]
        if working_parts:
            sections["WORKING"] = "\n\n".join(working_parts)
        include = talk_config.identity_include()
        if include is None:
            return sections
        if "WORKING" not in include:
            # The upgrade trap: an include list pinned before WORKING existed
            # keeps working verbatim — and silently drops the operator's
            # curated context on every session from then on. The list
            # REPLACES the default by contract, so this cannot be repaired
            # here; it can only be said out loud, once per session mint.
            _log.warning(
                "TALK_IDENTITY_INCLUDE=%s does not name WORKING — the curated "
                "operator context (memories/WORKING.md) will not ride this "
                "session; add WORKING to the list if that is not intentional",
                ",".join(include),
            )
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
        CLI OAuth (the operator's ChatGPT subscription) by default, or required
        Codex OAuth when TALK_PREFER_CODEX_OAUTH=true. The other method that
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

    def capability_catalog_probe(self) -> str | None:
        """Read this host's own capability catalog in-process. NEVER raises.

        Returns the catalog JSON when this process holds Hermes's registries
        (see :func:`_catalog_from_host_modules`), or ``None`` when it does
        not or the read failed. ``None`` means "ask the api_server instead",
        never "this install has no capabilities" — an empty catalog and an
        unreadable one are different sentences and must not collapse into
        one.
        """

        payload = _catalog_from_host_modules()
        if payload is None:
            return None
        try:
            return json.dumps(payload, default=str)
        except (TypeError, ValueError):  # pragma: no cover - plain data by construction
            return None

    def search_memory(self, query: str, limit: int = 5) -> str:
        """Search past Hermes sessions for what was said about ``query``.

        Four tiers, each fall-through said out loud (see the module
        docstring). The last one answers with a receipt rather than the
        answer — an agent run is seconds of work and outlives the relay's
        bounded tool courtesy wait.

        The two in-process tiers answer DIFFERENT questions and say so.
        ``session_search`` returns what was actually said, verbatim, and is
        tried first. Honcho returns what was inferred and remembered about
        the operator, which can be stale in a way a transcript line cannot,
        so its answer carries :data:`REMEMBERED_PREFIX` and the transcript's
        does not. Collapsing the two would make a guess indistinguishable
        from a quote on a surface with nothing on screen to check.
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
                # Transcript content is untrusted text and the provenance
                # marker belongs to the Honcho tier alone — a line that
                # forges it as a leading prefix must not reach the model
                # wearing a recollection's provenance.
                return strip_reserved_marker(_speakable(raw))

            # Bounded, unlike session_search above: Honcho is a
            # network-backed plugin, session_search is a local FTS5 read. An
            # unbounded wait here wedges a fixed relay tool-pool worker for
            # the life of the process.
            kind, value = _dispatch_bounded(
                ctx,
                HONCHO_SEARCH_TOOL_NAME,
                {"query": query, "limit": limit},
                talk_config.memory_search_timeout_s(),
            )
            if kind == "timeout":
                # A Honcho that is here but not answering made no decision to
                # route around — same rule as a refusal below, minus a reason
                # to quote.
                return (
                    "the remembered-context lookup didn't answer in time — "
                    "ask me again in a moment."
                )
            if kind == "err":
                return f"the memory lookup failed: {type(value).__name__}: {value}"
            raw = value
            # Only "Honcho isn't here" falls through. A Honcho that IS here
            # and said no — reindexing, rate limited — made a decision that
            # belongs to it, exactly as for session_search above.
            if not _agent_loop_absent(raw, HONCHO_SEARCH_TOOL_NAME):
                spoken = _speakable(raw)
                if spoken.startswith("that failed"):
                    # A refusal is spoken, but it is not a recollection: the
                    # prefix marks the provenance of a FACT, and an error has
                    # none to mark.
                    return spoken
                return f"{REMEMBERED_PREFIX}{spoken}"

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
        except talk_runs.RoutingUnavailable as exc:
            # Refused BEFORE anything ran, so say that rather than implying a
            # started-then-broken lookup: nothing is in flight to check on.
            return f"I can't look that up yet — {exc}."
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
        except talk_runs.RoutingUnavailable as exc:
            return f"I can't start that yet — {exc}."
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
        except talk_runs.RoutingUnavailable as exc:
            return f"I can't start that yet — {exc}."
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

        The wire text carries a correlation token (``[tk-xxxxxxxx] note``,
        hermes-talk#1): the drain preview quotes the joined wire text, so the
        ledger matches on the token exactly instead of on free text — two
        live notes with identical wording can no longer land each other.

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

        token = talk_steer.new_token()
        wire_text = talk_steer.compose_wire_text(token, text)

        # Arm BOTH watchers BEFORE the queue write, whichever rung takes it —
        # a fast drain must not beat the handlers onto the loggers.
        talk_steer.ensure_watcher()
        talk_steer.ensure_pre_api_watcher()

        steer = getattr(module, "steer_subagent", None)
        if callable(steer):
            try:
                accepted = bool(steer(agent_id, wire_text))
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
            return _queued_reply(
                agent_id, wire_text, token=token, agent=_registry_agent(agent_id)
            )

        # The host predates steer_subagent. Same registry, resolved here.
        return _steer_via_registry(agent_id, wire_text, token=token)

    def redirect_agent(self, agent_id: str, text: str) -> str:
        """Interrupt a RUNNING child's current step and re-aim it now.

        Stronger than :meth:`steer_agent`: ``AIAgent.redirect()`` (public on
        the 0.20 host, run_agent.py:3257) aborts the in-flight model request
        and applies the correction on the retry — or hands it to Codex's
        native turn/steer — instead of waiting for the next tool boundary.
        Mid-tool it degrades to ``steer()`` inside the host, so the tool
        finishes at a safe boundary.

        ``True`` proves acceptance, but not which mechanism the host chose.
        The receipt therefore comes from the post-call state artifact: this
        token in ``_pending_redirect`` proves abort-and-retry, this token in
        ``_pending_steer`` proves the tool-boundary queue, and a successful
        Codex native call proves its own redirect. If those transient slots
        were consumed before inspection, the ledger deliberately degrades to
        ``queued``. The pre-call ``_executing_tools`` peek is wording-only and
        never selects the receipt state.

        ``False`` means no live turn — the correction falls back to the
        steer queue, spoken as exactly that.
        """

        text = (text or "").strip()
        if not text:
            return "I need the correction itself before I can redirect."

        run = _registry_run(agent_id)
        if run is not None:
            return _unsteerable_run(run)

        module = _delegation_module()
        if module is None:
            return (
                "This Hermes build doesn't let me redirect running work — "
                "I can stop it instead."
            )

        record, live = _registry_record(agent_id)
        if record is _NO_REGISTRY:
            return (
                "I can't redirect running work on this Hermes version — its "
                "delegation registry isn't in the shape I know how to read."
            )
        if record is None:
            if not live:
                return "Nothing is running right now, so there's nothing to redirect."
            return (
                f"I don't have a running job called {agent_id}. Running now: "
                f"{', '.join(live[:5])}."
            )
        agent = record.get("agent") if isinstance(record, dict) else None
        if agent is None:
            return (
                f"I found {agent_id} but can't reach it to redirect — it has "
                "no live agent behind it anymore."
            )
        if not callable(getattr(agent, "redirect", None)):
            # Pre-0.20 host: no hard redirect exists. The steer queue is the
            # honest fallback, and steer_agent's own sentence says queued.
            return self.steer_agent(agent_id, text)

        token = talk_steer.new_token()
        wire_text = talk_steer.compose_wire_text(token, text)

        # Advisory peek for WORDING only — the claim itself comes from the
        # return value either way.
        executing_tools = bool(getattr(agent, "_executing_tools", False))

        talk_steer.ensure_watcher()
        talk_steer.ensure_pre_api_watcher()

        try:
            accepted = bool(agent.redirect(wire_text))
        except Exception as exc:  # noqa: BLE001 — the model speaks the failure
            return (
                f"I couldn't get that through to {agent_id}: "
                f"{type(exc).__name__}: {exc}"
            )

        if not accepted:
            # No live turn to redirect (redirect() rejects between turns and
            # during teardown). Fall back to the steer queue so the
            # correction still reaches the next step.
            try:
                queued = bool(agent.steer(wire_text)) if hasattr(agent, "steer") else False
            except Exception:  # noqa: BLE001 — fallback must not raise past the verb
                queued = False
            if queued:
                talk_steer.record_queued(agent_id, wire_text, token=token, agent=agent)
                return (
                    f"{agent_id} wasn't mid-thought just then, so I queued the "
                    "correction as a note for its next step instead."
                )
            return (
                f"That didn't take — {agent_id} may have just finished. "
                "Want me to list what's running?"
            )

        # Classify the mechanism from a positive host artifact after the call,
        # never from the advisory pre-call peek.  A raced tool transition puts
        # this exact token in the steer queue; a hard redirect stashes it in
        # the redirect slot.  If either slot was consumed before we can see it,
        # degrade to queued rather than inventing the stronger claim.
        pending_steer = getattr(agent, "_pending_steer", None)
        pending_redirect = getattr(agent, "_pending_redirect", None)
        queued_artifact = isinstance(pending_steer, str) and wire_text in pending_steer
        redirected_artifact = (
            isinstance(pending_redirect, str) and wire_text in pending_redirect
        ) or getattr(agent, "api_mode", None) == "codex_app_server"

        if queued_artifact or not redirected_artifact:
            talk_steer.record_queued(agent_id, wire_text, token=token, agent=agent)
            if executing_tools or queued_artifact:
                return (
                    f"{agent_id} is mid-tool, so the correction is queued for "
                    "the moment the tool finishes — I'll confirm when it lands."
                )
            return (
                f"Redirect accepted by {agent_id}; I couldn't prove which path "
                "the host took, so I'm tracking it as queued and will only "
                "confirm if a delivery artifact appears."
            )

        talk_steer.record_redirected(agent_id, wire_text, token=token, agent=agent)
        return (
            f"Redirect accepted — {agent_id} takes the correction at its "
            "current step, or its very next one if a tool was mid-flight."
        )

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
            run_id = int(run["runId"])
            if meta.get("lane") == LANE_API_SERVER:
                api_run_id = meta.get("api_run_id")
                if not isinstance(api_run_id, str) or not api_run_id:
                    return (
                        "I can't stop that one — the api server never told me "
                        "its run id."
                    )
                # Off the voice loop (hermes-talk#2): the POST runs on a
                # daemon worker with a bounded courtesy wait. The old
                # synchronous call could dead-air the call for ~6s on a slow
                # server.
                outcomes: queue.Queue = queue.Queue(maxsize=1)

                def _post(_api_id: str = api_run_id) -> None:
                    try:
                        talk_apiserver.stop_run(_api_id)
                        outcomes.put(("ok", None))
                    except Exception as exc:  # noqa: BLE001 — the outcome IS the record
                        outcomes.put(("err", exc))

                _spawn_daemon(_post)
                try:
                    kind, err = outcomes.get(timeout=STOP_CONFIRM_WAIT_S)
                except queue.Empty:
                    # Durable BEFORE the promise (Codex v0.6.1 finding 1): a
                    # daemon receipt dies with the process, so the pending
                    # state is persisted synchronously — a later session's
                    # check_work still has SOMETHING truthful to say.
                    talk_runs.annotate_run(
                        run_id, tee=True, stop_result="stop sent, receipt pending"
                    )

                    def _receipt(_rid: int = run_id) -> None:
                        try:
                            late_kind, late_err = outcomes.get(timeout=STOP_LATE_CONFIRM_S)
                        except queue.Empty:
                            talk_runs.annotate_run(
                                _rid, tee=True, stop_result="no answer from the server"
                            )
                            return
                        talk_runs.annotate_run(
                            _rid,
                            tee=True,
                            stop_result=(
                                "accepted" if late_kind == "ok" else f"failed: {late_err}"
                            ),
                        )

                    _spawn_daemon(_receipt)
                    return (
                        f"Sending the stop for run {run.get('runId')} — the "
                        "server hasn't answered yet; ask me in a moment and "
                        "I'll have the receipt."
                    )
                if kind == "err":
                    return f"the stop didn't go through: {err}"
                # 2xx = the server ACCEPTED the stop ("stopping"), not
                # that the agent is gone — say the request, not the outcome.
                talk_runs.annotate_run(run_id, tee=True, stop_result="accepted")
                return (
                    f"Sent the stop for run {run.get('runId')} — the "
                    "server is winding it down."
                )
            # Capture the handle BEFORE terminating (Codex v0.6.1 finding 2):
            # the detached worker releases its registry entry the moment it
            # reaps the child, and a confirm that re-looks-up by run id would
            # then read a successful death as "never confirmed".
            proc = talk_runs.get_process(run_id)
            if talk_runs.terminate_process(run_id):
                # terminate() is a signal, not a wait — confirm within the
                # bounded budget, and keep confirming off-thread past it
                # (hermes-talk#2): the death receipt lands in the run's meta
                # either way, spoken by the next check_work.
                code = talk_runs.wait_process(run_id, STOP_CONFIRM_WAIT_S, process=proc)
                if code is not None:
                    talk_runs.annotate_run(run_id, tee=True, stop_result=f"exited {code}")
                    return f"Stopped run {run.get('runId')} — it's down."
                talk_runs.annotate_run(
                    run_id, tee=True, stop_result="stop sent, receipt pending"
                )

                def _confirm(_rid: int = run_id, _proc: object | None = proc) -> None:
                    late = talk_runs.wait_process(_rid, STOP_LATE_CONFIRM_S, process=_proc)
                    if late is not None:
                        talk_runs.annotate_run(_rid, tee=True, stop_result=f"exited {late}")
                        return
                    # No exit code — but the RUN may have finished anyway
                    # (worker reaped it through a path our handle can't see).
                    # Consult the record before speaking uncertainty.
                    current = talk_runs.get_run(_rid)
                    status = (current or {}).get("status")
                    talk_runs.annotate_run(
                        _rid,
                        tee=True,
                        stop_result=(
                            f"run finished as {status}"
                            if status in talk_runs.TERMINAL_STATUSES
                            else "stop signaled, never confirmed dead"
                        ),
                    )

                _spawn_daemon(_confirm)
                return (
                    f"Sent the stop for run {run.get('runId')} — it's winding "
                    "down; I'll have the death receipt next time you ask."
                )
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


def _queued_reply(
    subagent_id: str,
    wire_text: str,
    *,
    token: str | None = None,
    agent: object | None = None,
) -> str:
    """The call-time sentence for an ACCEPTED steer — claims queueing only.

    Ledgers the receipt (wire text, so every artifact quotes what was
    actually queued) and reports whether ANY delivery artifact is watchable;
    "landed" is spoken later, by check_work, when (and only when) a drain
    artifact fires.
    """

    talk_steer.record_queued(subagent_id, wire_text, token=token, agent=agent)
    watching = talk_steer.ensure_watcher() or talk_steer.ensure_pre_api_watcher()
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


#: Sentinel: the delegation registry itself was unreadable — a different
#: refusal than "this id names nothing".
_NO_REGISTRY = object()


def _registry_record(subagent_id: str) -> tuple[Any, list[str]]:
    """Resolve one live-child record from the host's delegation registry.

    Returns ``(record, live_ids)``. ``record`` is :data:`_NO_REGISTRY` when
    the registry is missing or not in a readable shape, ``None`` when the id
    names nothing. Everything it touches is module state inside the SAME
    process — one private dict read (``_active_subagents``) under the host's
    own lock.
    """

    delegate_tool = _delegation_module()
    if delegate_tool is None:
        return _NO_REGISTRY, []
    registry = getattr(delegate_tool, "_active_subagents", None)
    if not isinstance(registry, dict):
        return _NO_REGISTRY, []
    lock = getattr(delegate_tool, "_active_subagents_lock", None)
    if lock is not None:
        with lock:
            record = registry.get(subagent_id)
            live = sorted(registry)
    else:  # pragma: no cover - every shipped Hermes has the lock
        record = registry.get(subagent_id)
        live = sorted(registry)
    return record, live


def _registry_agent(subagent_id: str) -> Any | None:
    """Best-effort live AIAgent behind an id — receipt ATTRIBUTION only.

    Used on the public-fn rung, where ``steer_subagent()`` resolves the
    child itself and hands back only a bool. A failure here degrades the
    pre-API attribution to nothing (the receipt just can't land via that
    artifact) — it must never break the reply path.
    """

    try:
        record, _ = _registry_record(subagent_id)
    except Exception:  # noqa: BLE001 — attribution is optional, replies are not
        return None
    if record is _NO_REGISTRY or not isinstance(record, dict):
        return None
    return record.get("agent")


def _steer_via_registry(subagent_id: str, wire_text: str, *, token: str | None = None) -> str:
    """Steer a live child by resolving Hermes's own subagent registry.

    The bridge for installs without ``steer_subagent``. One guarded registry
    read (:func:`_registry_record`), then the PUBLIC ``AIAgent.steer()``.
    Every step is guarded and a missing piece degrades to a spoken refusal
    instead of an exception. ``wire_text`` already carries the correlation
    token — this rung never composes.
    """

    record, live = _registry_record(subagent_id)
    if record is _NO_REGISTRY:
        return (
            "I can't redirect running work on this Hermes version — its "
            "delegation registry isn't in the shape I know how to read."
        )

    if record is None:
        if not live:
            return "Nothing is running right now, so there's nothing to redirect."
        return (
            f"I don't have a running job called {subagent_id}. Running now: "
            f"{', '.join(live[:5])}."
        )

    agent = record.get("agent") if isinstance(record, dict) else None
    if agent is None or not hasattr(agent, "steer"):
        return (
            f"I found {subagent_id} but can't reach it to redirect — it has no "
            "live agent behind it anymore."
        )

    try:
        accepted = bool(agent.steer(wire_text))
    except Exception as exc:  # noqa: BLE001 — the model speaks the failure
        return f"I couldn't get that through to {subagent_id}: {type(exc).__name__}: {exc}"

    if not accepted:
        # AIAgent.steer() returns False ONLY for empty text (run_agent.py:
        # 3242-3243) — and empty text was rejected before the ladder. So a
        # False here is a contract change on the host side, not a state of
        # the child; say that instead of inventing a diagnosis.
        return (
            f"That didn't go through — {subagent_id} refused the note in a "
            "way this Hermes version shouldn't."
        )
    return _queued_reply(subagent_id, wire_text, token=token, agent=agent)


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
    "STEER_TOOL_NAME",
    "HostAdapter",
    "agent_argv",
    "bind_ctx",
    "get_ctx",
    "hermes_binary",
    "host",
    "strip_reserved_marker",
]
