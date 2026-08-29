"""Live capability catalog — what this Hermes session can ACTUALLY do.

Talk could always describe itself; it could never check. Asked "what can you
do right now?" the model either recited its system prompt or spent a whole
delegated agent turn finding out. This module is the third answer: a bounded,
read-only snapshot of the installed skills, the resolved toolsets and their
enabled/configured state, the gateway's own feature flags, and how much work
is in flight.

**Two tiers, in-process first.** The same doctrine
:meth:`talk_host.HostAdapter.agent_lane` and :mod:`talk_apiserver` already
established: inside a Hermes process the catalog is enumerated directly from
the host's own registries (``tools.skills_tool``, ``hermes_cli.tools_config``,
``model_tools.get_tool_definitions`` — see
:func:`talk_host._catalog_from_host_modules`); otherwise the api_server
answers over ``/v1/*``. A process with no host registries falls through
rather than failing.

**Reading is all it does.** Nothing here executes, mints, or authorizes
anything, which is why ``talk_capabilities`` is classified in
:data:`talk_operator_auth.READ_ONLY_TALK_TOOLS`. A catalog entry saying a
toolset exists is not permission to use it, and the two questions are decided
in different modules on purpose.

**Redaction happens at the report boundary**, in
``talk_tools._handle_talk_capabilities``, matching :mod:`talk_doctor`'s own
convention — the collector returns what it read, and the thing that emits it
decides what may be said out loud. Upstream payloads are not this process's
text, so they are scrubbed before they can reach a transcript.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass

try:
    from . import talk_apiserver, talk_config, talk_host
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_apiserver
    import talk_config
    import talk_host

_log = logging.getLogger(__name__)

#: Where a resolved snapshot came from. Reported verbatim so an operator can
#: tell "the attached agent told me" from "the gateway told me".
SOURCE_IN_PROCESS = "in-process"
SOURCE_API_SERVER = "api-server"
SOURCE_NONE = "unavailable"

INERT_DETAIL = "the Hermes capability catalog lane is switched off in this process"
CHECKING_DETAIL = "I haven't finished reading the Hermes capability catalog"
UNREACHABLE_DETAIL = "I'm running outside a Hermes agent, and {reason}"

#: The only ``/health/detailed`` fields this feature surfaces. A whitelist and
#: not a blocklist: that endpoint is a health surface, not a catalog one, and
#: what a future gateway adds to it is not a decision this plugin gets to make
#: on an operator's behalf after the fact.
HEALTH_COUNTERS = ("active_runs", "active_delegations")

#: The only ``/v1/capabilities`` fields this feature surfaces — the same
#: discipline, for the same reason: a future gateway field must not silently
#: start riding a voice transcript just because it appeared in the document.
#: ``CAPABILITY_FIELDS`` are speakable strings; ``CAPABILITY_FEATURES`` are the
#: documented boolean feature flags (the chat/responses/runs/approval/session
#: surface the gateway advertises). Everything else — endpoints, auth config,
#: runtime prose, whatever ships next — is dropped, not spoken.
CAPABILITY_FIELDS = ("platform", "model")
CAPABILITY_FEATURES = (
    "chat_completions",
    "chat_completions_streaming",
    "responses_api",
    "responses_streaming",
    "run_submission",
    "run_status",
    "run_events_sse",
    "run_stop",
    "run_steer",
    "run_approval_response",
    "tool_progress_events",
    "approval_events",
    "session_resources",
    "session_chat",
    "session_chat_streaming",
    "session_fork",
    "session_model_lock",
    "model_options",
    "skills_api",
    "audio_api",
    "realtime_voice",
    "memory_write_api",
    "admin_config_rw",
    "jobs_admin",
)


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """One resolved catalog read. ``detail`` is written to be said out loud.

    ``tools`` is the LIVE resolved tool-name set — present only from the
    in-process tier, where ``get_tool_definitions`` applies the registry's
    availability gates. The REST tier cannot answer liveness and leaves it
    empty; empty therefore means "no live answer", never "nothing resolved",
    and consumers must not treat it as zero capability.
    """

    source: str
    skills: tuple[dict, ...]
    toolsets: tuple[dict, ...]
    capabilities: dict
    health: dict
    detail: str
    tools: tuple[str, ...] = ()


#: Cached snapshot + when it was taken. Guarded for the same reason
#: ``talk_apiserver._STATUS_LOCK`` is: reads arrive from the event loop, from
#: run workers, and from the dashboard's thread pool. ``_STORE_SEQ`` counts
#: stores: every read captures it before it starts, and :func:`_store` refuses
#: a snapshot whose read started before the currently-stored one landed —
#: last-writer-wins would otherwise let an old slow failure clobber a newer
#: healthy snapshot.
_LOCK = threading.Lock()
_SNAPSHOT: CatalogSnapshot | None = None
_SNAPSHOT_AT: float = 0.0
_STORE_SEQ = 0
_REFRESHING = False


def _rest_lane_enabled() -> bool:
    """Inert under pytest unless a test explicitly opts in.

    The same guard, and the same reason, as ``talk_apiserver._lane_enabled``,
    but its OWN switch: these four catalog reads bypass ``probe``, so a suite
    that pins ``probe`` to a fake verdict — which several do, to exercise the
    lane tile — would otherwise have this module dial port 8642 for real from
    the session-start warm. Tests that mean to exercise the REST tier
    monkeypatch this to ``lambda: True``.
    """

    return "PYTEST_CURRENT_TEST" not in os.environ


def _empty(detail: str) -> CatalogSnapshot:
    """A snapshot that answers honestly instead of claiming an empty install."""

    return CatalogSnapshot(
        source=SOURCE_NONE,
        skills=(),
        toolsets=(),
        capabilities={},
        health={},
        detail=detail,
    )


def _entries(value: object) -> tuple[dict, ...]:
    """Catalog entries from an untrusted payload field, non-dicts dropped."""

    if not isinstance(value, list):
        return ()
    return tuple(entry for entry in value if isinstance(entry, dict))


def _bounded_health(raw: dict) -> dict:
    """Only the counters in :data:`HEALTH_COUNTERS`, and only when integers."""

    return {
        key: raw[key]
        for key in HEALTH_COUNTERS
        if isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool)
    }


def _bounded_capabilities(raw: dict) -> dict:
    """Only the fields in :data:`CAPABILITY_FIELDS` / :data:`CAPABILITY_FEATURES`.

    The whole document is upstream text headed for a transcript, so it gets the
    :func:`_bounded_health` treatment: named keys with the right types survive,
    everything else is dropped rather than spoken.
    """

    bounded: dict = {
        key: raw[key] for key in CAPABILITY_FIELDS if isinstance(raw.get(key), str)
    }
    features = raw.get("features")
    if isinstance(features, dict):
        kept = {
            key: features[key]
            for key in CAPABILITY_FEATURES
            if isinstance(features.get(key), bool)
        }
        if kept:
            bounded["features"] = kept
    return bounded


def _looks_like_catalog(payload: dict) -> bool:
    """True only for a dict that is recognizably a capability catalog.

    The in-process tier builds this payload itself
    (:func:`talk_host._catalog_from_host_modules`), so this guard is pure
    defense in depth: a payload with none of the catalog keys must read as
    "no answer here" and fall through, never be stored as an empty catalog.
    """

    if "error" in payload:
        return False
    return (
        isinstance(payload.get("skills"), list)
        or isinstance(payload.get("toolsets"), list)
        or isinstance(payload.get("capabilities"), dict)
    )


def _from_in_process() -> CatalogSnapshot | None:
    """Tier 1. ``None`` means "no answer here", never "nothing installed"."""

    try:
        raw = talk_host.host().capability_catalog_probe()
    except Exception as exc:  # noqa: BLE001 — a probe must never escape
        _log.debug("in-process catalog read failed: %s: %s", type(exc).__name__, exc)
        return None
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not _looks_like_catalog(payload):
        return None
    capabilities = payload.get("capabilities")
    health = payload.get("health")
    tools = payload.get("tools")
    return CatalogSnapshot(
        source=SOURCE_IN_PROCESS,
        skills=_entries(payload.get("skills")),
        toolsets=_entries(payload.get("toolsets")),
        capabilities=_bounded_capabilities(
            capabilities if isinstance(capabilities, dict) else {}
        ),
        health=_bounded_health(health if isinstance(health, dict) else {}),
        detail="the Hermes agent I'm attached to",
        tools=(
            tuple(sorted({str(name) for name in tools if isinstance(name, str) and name}))
            if isinstance(tools, list)
            else ()
        ),
    )


def _from_api_server() -> CatalogSnapshot:
    """Tier 2. Four reads, and any one of them failing fails the whole catalog.

    Deliberately all-or-nothing: a half-read catalog that silently omits the
    toolsets would be spoken as though those toolsets did not exist, which is
    exactly the fabrication this feature exists to stop.
    """

    if not _rest_lane_enabled():
        return _empty(INERT_DETAIL)
    verdict = talk_apiserver.status()
    if not verdict.available:
        return _empty(UNREACHABLE_DETAIL.format(reason=verdict.detail))
    try:
        skills = talk_apiserver.list_skills()
        toolsets = talk_apiserver.list_toolsets()
        capabilities = talk_apiserver.capabilities_payload()
        health = talk_apiserver.health_detailed()
    except talk_apiserver.TalkApiServerError as exc:
        return _empty(str(exc))
    return CatalogSnapshot(
        source=SOURCE_API_SERVER,
        skills=tuple(skills),
        toolsets=tuple(toolsets),
        capabilities=_bounded_capabilities(capabilities),
        health=_bounded_health(health),
        detail="the Hermes api server",
    )


def _resolve() -> CatalogSnapshot:
    """Best available evidence. Touches no lock — see :func:`_refresh_in_background`."""

    snapshot = _from_in_process()
    if snapshot is not None:
        return snapshot
    return _from_api_server()


def _resolve_or_explain() -> CatalogSnapshot:
    """:func:`_resolve`, with any escaping failure turned into speakable text."""

    try:
        return _resolve()
    except Exception as exc:  # noqa: BLE001 — a catalog read is never fatal
        _log.warning("capability catalog read failed: %s: %s", type(exc).__name__, exc)
        return _empty(f"I couldn't read the capability catalog ({type(exc).__name__})")


def _read_seq() -> int:
    with _LOCK:
        return _STORE_SEQ


def _store(snapshot: CatalogSnapshot, started_seq: int) -> None:
    """Store one resolved snapshot — unless a newer one landed first.

    ``started_seq`` is :data:`_STORE_SEQ` as it was when this snapshot's READ
    began. If it no longer matches, some other read that started later has
    already stored, and what this thread is holding is older evidence wearing
    a newer arrival time — dropped, not stored.
    """

    global _SNAPSHOT, _SNAPSHOT_AT, _STORE_SEQ
    with _LOCK:
        if started_seq != _STORE_SEQ:
            return
        _STORE_SEQ += 1
        _SNAPSHOT = snapshot
        _SNAPSHOT_AT = time.monotonic()


def _refresh_in_background() -> None:
    """Re-read off the hot path so a warm caller never waits on the network."""

    global _REFRESHING
    with _LOCK:
        if _REFRESHING:
            return
        _REFRESHING = True
        started_seq = _STORE_SEQ

    def worker() -> None:
        global _REFRESHING
        try:
            snapshot = _resolve_or_explain()
        finally:
            with _LOCK:
                _REFRESHING = False
        # Stored even when it failed, for the reason talk_apiserver stores a
        # failed probe: a cache that never fills re-reads on every single turn.
        _store(snapshot, started_seq)

    threading.Thread(
        target=worker, name="talk-capabilities-resolve", daemon=True
    ).start()


def warm() -> CatalogSnapshot:
    """Read now, cache it, and return it. **BLOCKS.**

    The one function here that waits on the network. It belongs at a session
    start, on a thread where waiting is free — never in a tool handler, which
    runs on the loop carrying the audio. That handler wants :func:`status`.
    """

    started_seq = _read_seq()
    snapshot = _resolve_or_explain()
    _store(snapshot, started_seq)
    return snapshot


def status() -> CatalogSnapshot:
    """The current snapshot. NEVER waits for the network.

    A cold cache schedules a read and says it is still checking; a stale one
    answers with what it last saw and refreshes behind it. "Still checking" is
    reported as :data:`SOURCE_NONE` rather than as an empty catalog, because a
    catalog we cannot vouch for must not be spoken as a complete one.
    """

    with _LOCK:
        cached, taken_at = _SNAPSHOT, _SNAPSHOT_AT
    if cached is None:
        _refresh_in_background()
        return _empty(CHECKING_DETAIL)
    if time.monotonic() - taken_at >= talk_config.capability_catalog_ttl_s():
        _refresh_in_background()
    return cached


def warm_in_background() -> None:
    """Kick a catalog read off the caller's thread. Fire and forget.

    Session starts call this before instruction assembly so the cache is
    filling while auth and audio set up; a section built moments later reads
    whatever has landed, never waits.
    """

    _refresh_in_background()


# -- the resident-prompt section (capability bridge) -----------------------------

#: How many tool categories the prompt section may name. The prompt is
#: resident and re-billed on every turn — a budget, not a preference.
MAX_SECTION_CATEGORIES = 10

#: One catalog name, fit for the resident prompt. Catalog entries are
#: upstream text; a name with punctuation or whitespace tricks does not get
#: to ride a prompt nobody is reading on a screen. Identifiers only.
_SECTION_NAME_MAX_CHARS = 32
_SECTION_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_ ")


def _section_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > _SECTION_NAME_MAX_CHARS:
        return None
    if any(ch not in _SECTION_NAME_CHARS for ch in name):
        return None
    return name


def _usable(entry: dict) -> bool:
    """A catalog flag disqualifies only when present AND negative."""

    return not (
        entry.get("enabled") is False
        or entry.get("disabled") is True
        or entry.get("installed") is False
        or entry.get("configured") is False
    )


def instruction_section(snapshot: CatalogSnapshot | None = None) -> str | None:
    """The bounded capabilities block for the session prompt, or ``None``.

    ``None`` fails open to the plain preamble: the catalog is unreachable,
    or it holds nothing true to claim. When a snapshot IS available the
    section says what this install can do — a skill count and the usable
    tool categories — plus the two rules that keep the model honest: the
    delegation ceiling, and never inventing tool names.

    When the snapshot carries the live resolved tool set (the in-process
    tier), a category whose tools ALL failed the host's availability gates
    is not claimed: enabled-and-configured is a static config fact, not
    evidence the tool resolves right now.
    """

    snap = status() if snapshot is None else snapshot
    if snap.source == SOURCE_NONE:
        return None
    skill_count = sum(1 for entry in snap.skills if _usable(entry))
    live = set(snap.tools)
    categories: list[str] = []
    for entry in snap.toolsets:
        if not _usable(entry):
            continue
        name = _section_name(entry.get("name") or entry.get("label"))
        if name is None or name in categories:
            continue
        if live:
            tools = entry.get("tools")
            if isinstance(tools, list) and not any(
                isinstance(tool, str) and tool in live for tool in tools
            ):
                continue
        categories.append(name)
        if len(categories) >= MAX_SECTION_CATEGORIES:
            break
    if not skill_count and not categories:
        return None
    inventory = f"This Hermes install reports {skill_count} "
    inventory += "skill" if skill_count == 1 else "skills"
    inventory += " installed"
    if categories:
        inventory += (
            ", and these tool categories usable right now: " + ", ".join(categories)
        )
    inventory += "."
    return (
        inventory
        + " You can delegate anything Hermes can do: hand it to the delegate_task tool and "
        "the agent runs the full Hermes toolset — if the host needs approval for an action, "
        "you will be asked out loud before it fires. Never invent tool names: the only tools "
        "you can call directly are this session's advertised tools, and capability questions "
        "go to the talk_capabilities tool."
    )


def reset_for_tests() -> None:
    """Clear the cached snapshot between tests (never called in production).

    Bumps :data:`_STORE_SEQ` so a read still in flight from before the reset
    can no longer store into the cleaned cache.
    """

    global _SNAPSHOT, _SNAPSHOT_AT, _STORE_SEQ, _REFRESHING
    with _LOCK:
        _SNAPSHOT = None
        _SNAPSHOT_AT = 0.0
        _STORE_SEQ += 1
        _REFRESHING = False


__all__ = [
    "CAPABILITY_FEATURES",
    "CAPABILITY_FIELDS",
    "CHECKING_DETAIL",
    "HEALTH_COUNTERS",
    "INERT_DETAIL",
    "MAX_SECTION_CATEGORIES",
    "SOURCE_API_SERVER",
    "SOURCE_IN_PROCESS",
    "SOURCE_NONE",
    "UNREACHABLE_DETAIL",
    "CatalogSnapshot",
    "instruction_section",
    "reset_for_tests",
    "status",
    "warm",
    "warm_in_background",
]
