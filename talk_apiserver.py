"""Tier-2 agent lane — a real Hermes agent reached over the api_server platform.

Hermes's api_server gateway platform (``gateway/platforms/api_server.py``)
exposes a server-side ``AIAgent`` to any process on the box. That is what lets
a voice session running OUTSIDE an agent — the dashboard tab, a standalone
``hermes talk`` — still reach real tools instead of announcing a fallback.

**Why /v1/runs and not a synchronous chat endpoint.** ``POST /v1/runs``
(api_server.py:4159) answers ``202 {"run_id": …}`` the instant the run is
queued, and the result is collected later from ``GET /v1/runs/{id}``
(:4470, ``output`` on success / ``error`` on failure). The synchronous
alternatives hold an HTTP connection open for the entire agent loop:
``/v1/chat/completions`` is one call but blocks until the agent finishes, and
``POST /api/sessions/{id}/chat`` (:1867) blocks AND 404s unless a session was
created first — two round trips plus a persistent session row in the
operator's SessionDB for what is one lookup. A handle you can walk away from
is worth more here than a response you must wait for.

**Why nothing in this module blocks a voice turn.** The relay executes tools
SYNCHRONOUSLY on the event loop (``talk_relay.handle_event`` is called inline
from ``talk_cli``'s receive loop), so a tool that waits is a microphone that
stops. An agent run takes seconds to minutes. Therefore the run lane is
handed to a :mod:`talk_runs` worker thread and the caller gets the
``WORK_STARTED`` receipt immediately — the same machinery, and the same
already-built watchers on both surfaces, that background delegation uses. The
answer is SPOKEN when it lands. A bounded "wait a few seconds first" hybrid
was considered and rejected: any wait at all is dead air on a live call,
because the wait happens on the loop that carries the audio.

**Why :func:`status` never blocks either.** The obvious shortcut is to let the
availability probe wait once, on the theory that a closed loopback port
refuses instantly. That is not true everywhere: on a box whose firewall DROPS
instead of RSTs, connecting to a closed ``127.0.0.1`` port burns the entire
timeout (measured here — 8642, 9999, and 45999 all took the full budget while
an open port answered in 15ms). So :func:`status` answers from cache and
schedules a background refresh, and the ONE function that waits for the
network says so in its name: :func:`warm`, which belongs at session start,
never in a turn.

Every failure returns speakable text or a False verdict. Nothing here raises
at a tool handler.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass

import httpx

try:
    from . import talk_config
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_config

_log = logging.getLogger(__name__)

CAPABILITIES_PATH = "/v1/capabilities"
RUNS_PATH = "/v1/runs"

#: api_server run statuses that will never change again (api_server.py:4377-4404).
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})

#: Why the lane is unusable, in the words the model should say.
REASON_OK = "ok"
REASON_ABSENT = "absent"
REASON_UNAUTHORIZED = "unauthorized"
REASON_ERROR = "error"

MAX_OUTPUT_CHARS = 4_000


class TalkApiServerError(Exception):
    """The api_server was reachable but the run could not be completed."""


@dataclass(frozen=True, slots=True)
class ApiServerStatus:
    """One probe verdict. ``detail`` is written to be said out loud."""

    available: bool
    reason: str
    detail: str


#: Cached verdict + when it was taken. Guarded because tool calls arrive from
#: the event loop, run workers, and (in the dashboard) a thread pool.
_STATUS_LOCK = threading.Lock()
_STATUS: ApiServerStatus | None = None
_STATUS_AT: float = 0.0
_REFRESHING = False


INERT_DETAIL = "the Hermes api server lane is switched off in this process"
CHECKING_DETAIL = "I haven't finished checking whether the Hermes api server is up"


def _lane_enabled() -> bool:
    """Inert under pytest unless a test explicitly opts in.

    Same guard, and the same reason, as ``talk_runs._history_enabled``: suites
    exercise the host chain transitively without ever meaning to touch this
    lane, and an always-on probe would have every one of them dial whatever is
    listening on port 8642 of the machine running CI. Tests that DO mean to
    exercise the lane monkeypatch this to ``lambda: True``.
    """

    return "PYTEST_CURRENT_TEST" not in os.environ


def _auth_headers() -> dict:
    key = talk_config.api_server_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def probe() -> ApiServerStatus:
    """Ask the api_server whether it is there and whether we may use it.

    ``/v1/capabilities`` is authenticated (api_server.py:1455), which is the
    point: a 401 means the server is UP and our key is wrong, and that is a
    different sentence than "not running" — an operator who hears "not
    reachable" when the real problem is a mismatched key goes looking in the
    wrong place.
    """

    url = talk_config.api_server_url() + CAPABILITIES_PATH
    try:
        response = httpx.get(
            url,
            headers=_auth_headers(),
            timeout=talk_config.api_server_probe_timeout_s(),
        )
    except httpx.HTTPError as exc:
        return ApiServerStatus(
            available=False,
            reason=REASON_ABSENT,
            detail=(
                "the Hermes api server isn't reachable "
                f"({type(exc).__name__}) — set API_SERVER_ENABLED=true and "
                "restart the gateway to turn it on"
            ),
        )
    if response.status_code == 401:
        return ApiServerStatus(
            available=False,
            reason=REASON_UNAUTHORIZED,
            detail=(
                "the Hermes api server is running but rejected my key — set "
                "TALK_API_SERVER_KEY (or API_SERVER_KEY) to match the gateway"
            ),
        )
    if response.status_code != 200:
        return ApiServerStatus(
            available=False,
            reason=REASON_ERROR,
            detail=(
                f"the Hermes api server answered {response.status_code} when I "
                "asked what it supports"
            ),
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    features = payload.get("features") if isinstance(payload, dict) else None
    if isinstance(features, dict) and features.get("run_submission") is False:
        return ApiServerStatus(
            available=False,
            reason=REASON_ERROR,
            detail="the Hermes api server is running but does not accept runs",
        )
    return ApiServerStatus(available=True, reason=REASON_OK, detail="Hermes api server")


def _refresh_in_background() -> None:
    """Re-probe off the hot path so a warm caller never waits on the network."""

    global _REFRESHING
    with _STATUS_LOCK:
        if _REFRESHING:
            return
        _REFRESHING = True

    def worker() -> None:
        global _REFRESHING
        try:
            verdict = probe()
        except Exception as exc:  # noqa: BLE001 — a probe must never escape
            _log.warning("api server probe failed: %s: %s", type(exc).__name__, exc)
            verdict = ApiServerStatus(
                available=False,
                reason=REASON_ERROR,
                detail=f"I couldn't check the Hermes api server ({type(exc).__name__})",
            )
        finally:
            with _STATUS_LOCK:
                _REFRESHING = False
        # Stored even on failure: a cold cache that never fills would re-probe
        # on every single turn, which is the stampede this cache exists to stop.
        _store(verdict)

    threading.Thread(target=worker, name="talk-apiserver-probe", daemon=True).start()


def _store(verdict: ApiServerStatus) -> None:
    global _STATUS, _STATUS_AT
    with _STATUS_LOCK:
        _STATUS = verdict
        _STATUS_AT = time.monotonic()


def warm() -> ApiServerStatus:
    """Probe now, cache the verdict, and return it. **BLOCKS.**

    The one function here that waits for the network on the caller's thread.
    Call it where waiting is free — a session start, a dashboard status route
    already on a worker thread — so that :func:`status` is warm before the
    first spoken question arrives. Never call it from a tool handler.
    """

    if not _lane_enabled():
        return ApiServerStatus(available=False, reason=REASON_ABSENT, detail=INERT_DETAIL)
    try:
        verdict = probe()
    except Exception as exc:  # noqa: BLE001 — availability is never fatal
        _log.warning("api server probe failed: %s: %s", type(exc).__name__, exc)
        verdict = ApiServerStatus(
            available=False,
            reason=REASON_ERROR,
            detail=f"I couldn't check the Hermes api server ({type(exc).__name__})",
        )
    _store(verdict)
    return verdict


def warm_in_background() -> None:
    """Kick :func:`warm` off the caller's thread. Fire and forget."""

    _refresh_in_background()


def status() -> ApiServerStatus:
    """The current verdict. NEVER waits for the network.

    A cold cache schedules a probe and answers "still checking" — unavailable,
    because a lane we cannot vouch for must not be claimed. A stale cache
    answers with the last verdict and refreshes behind it. Either way the
    caller returns in microseconds, which is the only thing that matters on a
    thread that is also carrying audio.
    """

    if not _lane_enabled():
        return ApiServerStatus(available=False, reason=REASON_ABSENT, detail=INERT_DETAIL)
    with _STATUS_LOCK:
        cached, taken_at = _STATUS, _STATUS_AT
    if cached is None:
        _refresh_in_background()
        return ApiServerStatus(
            available=False, reason=REASON_ABSENT, detail=CHECKING_DETAIL
        )
    if time.monotonic() - taken_at >= talk_config.api_server_probe_ttl_s():
        _refresh_in_background()
    return cached


def is_available() -> bool:
    """True when a run submitted right now would be accepted."""

    return status().available


def start_run(prompt: str, *, session_id: str | None = None) -> str:
    """POST /v1/runs. Returns the run id; raises on anything else."""

    body: dict = {"input": prompt}
    if session_id:
        body["session_id"] = session_id
    try:
        response = httpx.post(
            talk_config.api_server_url() + RUNS_PATH,
            json=body,
            headers=_auth_headers(),
            timeout=talk_config.api_server_probe_timeout_s() * 4,
        )
    except httpx.HTTPError as exc:
        raise TalkApiServerError(
            f"I couldn't reach the Hermes api server ({type(exc).__name__})"
        ) from exc
    # 202 is the documented success (api_server.py:4464-4468); accept any 2xx
    # so a future 200 does not read as a failure.
    if response.status_code // 100 != 2:
        raise TalkApiServerError(
            f"the Hermes api server refused the run ({response.status_code}): "
            f"{response.text[:200] or 'no detail'}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise TalkApiServerError("the Hermes api server returned a non-JSON run") from exc
    run_id = payload.get("run_id") if isinstance(payload, dict) else None
    if not isinstance(run_id, str) or not run_id:
        raise TalkApiServerError("the Hermes api server returned a run with no id")
    return run_id


def get_run(run_id: str) -> dict:
    """GET /v1/runs/{id}. Returns the status object; raises on anything else."""

    try:
        response = httpx.get(
            f"{talk_config.api_server_url()}{RUNS_PATH}/{run_id}",
            headers=_auth_headers(),
            timeout=talk_config.api_server_probe_timeout_s() * 4,
        )
    except httpx.HTTPError as exc:
        raise TalkApiServerError(
            f"I lost contact with the Hermes api server ({type(exc).__name__})"
        ) from exc
    if response.status_code != 200:
        raise TalkApiServerError(
            f"the Hermes api server answered {response.status_code} for that run"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise TalkApiServerError("the Hermes api server returned a non-JSON status") from exc
    if not isinstance(payload, dict):
        raise TalkApiServerError("the Hermes api server returned an invalid status")
    return payload


def run_to_completion(
    prompt: str,
    *,
    session_id: str | None = None,
    on_start=None,
) -> str:
    """Run one agent turn and return its answer as speakable text.

    BLOCKS until the run terminates or the budget expires, so this belongs on
    a :mod:`talk_runs` worker thread and nowhere else. The deadline is the
    same ``TALK_AGENT_TIMEOUT_S`` that bounds a detached spawn — one budget
    for "how long may work run", whichever lane carries it.
    """

    run_id = start_run(prompt, session_id=session_id)
    if on_start is not None:
        with contextlib.suppress(Exception):  # a bookkeeping hook is never fatal
            on_start(run_id)
    poll = talk_config.api_server_poll_s()
    deadline = time.monotonic() + talk_config.agent_timeout_s()
    while time.monotonic() < deadline:
        time.sleep(poll)
        run = get_run(run_id)
        state = str(run.get("status") or "")
        if state not in TERMINAL_RUN_STATUSES:
            continue
        if state == "completed":
            output = run.get("output")
            text = output if isinstance(output, str) else json.dumps(output, default=str)
            return (text.strip() or "the agent finished without saying anything")[
                :MAX_OUTPUT_CHARS
            ]
        error = run.get("error")
        detail = str(error) if error else "no reason given"
        return f"the agent run {state}: {detail}"[:MAX_OUTPUT_CHARS]
    raise TalkApiServerError(
        "the agent run is still going after its whole time budget — it may "
        "still finish, but I stopped waiting"
    )


def stop_run(run_id: str) -> None:
    """POST /v1/runs/{id}/stop — hard-interrupt a running api_server agent.

    The lane's ONE lifecycle verb (api_server.py routes: create/get/events/
    approval/stop). Raises :class:`TalkApiServerError` with speakable text on
    any failure; returns None on any 2xx.
    """

    try:
        response = httpx.post(
            f"{talk_config.api_server_url()}{RUNS_PATH}/{run_id}/stop",
            headers=_auth_headers(),
            timeout=talk_config.api_server_probe_timeout_s() * 4,
        )
    except httpx.HTTPError as exc:
        raise TalkApiServerError(
            f"I couldn't reach the Hermes api server ({type(exc).__name__})"
        ) from exc
    if response.status_code // 100 != 2:
        raise TalkApiServerError(
            f"the Hermes api server refused the stop ({response.status_code})"
        )


def reset_for_tests() -> None:
    """Clear the cached verdict between tests (never called in production)."""

    global _STATUS, _STATUS_AT, _REFRESHING
    with _STATUS_LOCK:
        _STATUS = None
        _STATUS_AT = 0.0
        _REFRESHING = False


__all__ = [
    "CAPABILITIES_PATH",
    "CHECKING_DETAIL",
    "INERT_DETAIL",
    "MAX_OUTPUT_CHARS",
    "REASON_ABSENT",
    "REASON_ERROR",
    "REASON_OK",
    "REASON_UNAUTHORIZED",
    "RUNS_PATH",
    "TERMINAL_RUN_STATUSES",
    "ApiServerStatus",
    "TalkApiServerError",
    "get_run",
    "is_available",
    "probe",
    "reset_for_tests",
    "run_to_completion",
    "start_run",
    "status",
    "stop_run",
    "warm",
    "warm_in_background",
]
