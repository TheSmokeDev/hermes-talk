"""Live capability catalog — what this Hermes session can ACTUALLY do.

Talk could always describe itself; it could never check. Asked "what can you
do right now?" the model either recited its system prompt or spent a whole
delegated agent turn finding out. This module is the third answer: a bounded,
read-only snapshot of the installed skills, the resolved toolsets and their
enabled/configured state, the gateway's own feature flags, and how much work
is in flight.

**Two tiers, in-process first.** The same doctrine
:meth:`talk_host.HostAdapter.agent_lane` and :mod:`talk_apiserver` already
established: if a Hermes agent is attached, ask it directly through the host's
own ``dispatch_tool``; otherwise ask the api_server over ``/v1/*``. A host
that does not know the in-process tool falls through rather than failing —
see :data:`talk_host.CAPABILITY_CATALOG_TOOL_NAME`.

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


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """One resolved catalog read. ``detail`` is written to be said out loud."""

    source: str
    skills: tuple[dict, ...]
    toolsets: tuple[dict, ...]
    capabilities: dict
    health: dict
    detail: str


#: Cached snapshot + when it was taken. Guarded for the same reason
#: ``talk_apiserver._STATUS_LOCK`` is: reads arrive from the event loop, from
#: run workers, and from the dashboard's thread pool.
_LOCK = threading.Lock()
_SNAPSHOT: CatalogSnapshot | None = None
_SNAPSHOT_AT: float = 0.0
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
    if not isinstance(payload, dict):
        return None
    capabilities = payload.get("capabilities")
    health = payload.get("health")
    return CatalogSnapshot(
        source=SOURCE_IN_PROCESS,
        skills=_entries(payload.get("skills")),
        toolsets=_entries(payload.get("toolsets")),
        capabilities=capabilities if isinstance(capabilities, dict) else {},
        health=_bounded_health(health if isinstance(health, dict) else {}),
        detail="the Hermes agent I'm attached to",
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
        capabilities=capabilities,
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


def _store(snapshot: CatalogSnapshot) -> None:
    global _SNAPSHOT, _SNAPSHOT_AT
    with _LOCK:
        _SNAPSHOT = snapshot
        _SNAPSHOT_AT = time.monotonic()


def _refresh_in_background() -> None:
    """Re-read off the hot path so a warm caller never waits on the network."""

    global _REFRESHING
    with _LOCK:
        if _REFRESHING:
            return
        _REFRESHING = True

    def worker() -> None:
        global _REFRESHING
        try:
            snapshot = _resolve_or_explain()
        finally:
            with _LOCK:
                _REFRESHING = False
        # Stored even when it failed, for the reason talk_apiserver stores a
        # failed probe: a cache that never fills re-reads on every single turn.
        _store(snapshot)

    threading.Thread(
        target=worker, name="talk-capabilities-resolve", daemon=True
    ).start()


def warm() -> CatalogSnapshot:
    """Read now, cache it, and return it. **BLOCKS.**

    The one function here that waits on the network. It belongs at a session
    start, on a thread where waiting is free — never in a tool handler, which
    runs on the loop carrying the audio. That handler wants :func:`status`.
    """

    snapshot = _resolve_or_explain()
    _store(snapshot)
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


def reset_for_tests() -> None:
    """Clear the cached snapshot between tests (never called in production)."""

    global _SNAPSHOT, _SNAPSHOT_AT, _REFRESHING
    with _LOCK:
        _SNAPSHOT = None
        _SNAPSHOT_AT = 0.0
        _REFRESHING = False


__all__ = [
    "CHECKING_DETAIL",
    "HEALTH_COUNTERS",
    "INERT_DETAIL",
    "SOURCE_API_SERVER",
    "SOURCE_IN_PROCESS",
    "SOURCE_NONE",
    "UNREACHABLE_DETAIL",
    "CatalogSnapshot",
    "reset_for_tests",
    "status",
    "warm",
]
