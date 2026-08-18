"""Async-run registry — voice never blocks.

Anything slower than a couple of seconds starts here, returns a spoken
receipt immediately, and is polled until it lands. Ported from a prior
proven Talk Mode implementation, trimmed to the two kinds this plugin
has: ``agent`` (a detached Hermes one-shot) and ``skill``.

The sentinel string is the contract between a tool handler, the Realtime
model, and the session watcher: a handler returns
``WORK_STARTED #<id> kind=<kind> (<label>)`` and the watcher starts polling
:func:`get_run`.

**Lifetime.** Runs live in memory and die with the process. A voice session
that ends does NOT stop the work — the detached child keeps going — but the
watcher that would have spoken the result dies with it. The JSONL history
tee is what survives: a row still marked ``running`` with no live registry
entry belonged to a dead process and is reported as ``lost``, never as
"still running", because this process cannot know either way.

**The ticket (hermes-talk#35).** A run is only accepted once an exact return
route exists for it. :func:`attach_owner` binds the live Talk connection;
:func:`start_run` refuses with :class:`RoutingUnavailable` when nothing is
bound, stamps every run with an immutable ticket (operator, profile, durable
Hermes session, Talk generation, request id), and persists that acceptance
record BEFORE the worker thread starts. Nothing can speak ``WORK_STARTED``
over a destination that was never minted.

The tee is fail-open at every seam EXCEPT the acceptance record, which is
fail-closed for exactly that reason. History IO must never break the
registry, and the file is compacted in place when it outgrows its cap.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

try:
    from . import talk_config
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_config

_log = logging.getLogger(__name__)

RUN_KINDS = ("agent", "skill")
TERMINAL_STATUSES = ("done", "failed")

# Eviction policy — terminal runs only; a running entry is never evicted.
_RUN_TTL_S = 24 * 60 * 60
_MAX_RUNS = 200

_RUNS: dict[int, dict] = {}
_RUN_LOCK = threading.Lock()
_RUN_SEQ = 0

# History tee knobs. Output is capped per record — a run's full text lives in
# the registry while it lives; history is a telemetry record, not a store.
_HISTORY_FILENAME = "talk-runs.jsonl"
_HISTORY_MAX_BYTES = 512_000
_HISTORY_COMPACT_KEEP = 300
HISTORY_OUTPUT_CAP = 2_000
_HISTORY_TAIL_LINES = 600
# Lock ordering: _HISTORY_LOCK is always taken WITHOUT _RUN_LOCK held (tees
# happen after the registry mutation completes).
_HISTORY_LOCK = threading.Lock()

#: Delivery states. ``pending`` is the mint-time value; ``delivered`` is set
#: exactly once, by whichever route actually hands the result to the operator.
DELIVERY_PENDING = "pending"
DELIVERED = "delivered"


class RoutingUnavailable(RuntimeError):
    """No durable destination is bound — refuse the work (hermes-talk#35).

    Distinct from a generic dispatch failure on purpose: the caller speaks a
    different sentence for "I never accepted this" than for "I accepted it and
    it broke", and only the former is safe to say before any work has run.
    """


# The ambient ticket for the currently attached Talk connection. Same shape and
# contract as talk_lifecycle's attach/detach: one connection at a time, last
# attach wins, fail closed while unbound. Module-level state is per PROCESS, so
# the CLI lane and the dashboard lane hold their own and cannot clobber
# each other's.
_OWNER_LOCK = threading.Lock()
_OWNER: dict | None = None


def attach_owner(
    *,
    talk_session_id: str,
    generation_id: str,
    hermes_session_id: str | None,
    operator: str,
    profile: str | None,
) -> None:
    """Bind the live Talk connection as the destination for new runs.

    ``talk_session_id`` is this connection's own identity, minted once at
    connect and stable for its lifetime. ``hermes_session_id`` is the richer,
    cross-restart-durable Hermes session id when a host context is attached
    (tier 1); it is absent for a standalone tier-2/3 session, which is not
    fatal — the Talk-minted id is still an exact destination for as long as
    the connection lives. Only a run carrying a ``hermesSessionId`` can be
    adopted after a restart, because only that id survives one.
    """

    global _OWNER
    with _OWNER_LOCK:
        _OWNER = {
            "talkSessionId": str(talk_session_id),
            "generationId": str(generation_id),
            "hermesSessionId": hermes_session_id or None,
            "operator": str(operator),
            "profile": profile or None,
        }


def detach_owner() -> None:
    """No connection is attached; new runs are refused until the next attach."""

    global _OWNER
    with _OWNER_LOCK:
        _OWNER = None


def current_owner() -> dict | None:
    """Snapshot the bound ticket owner, or ``None`` while unbound."""

    with _OWNER_LOCK:
        return dict(_OWNER) if _OWNER is not None else None


def started_sentinel(run_id: int, kind: str, label: str) -> str:
    """The receipt a tool handler returns so the session starts polling."""

    return f"WORK_STARTED #{run_id} kind={kind} ({label})"


def _history_path():
    """State dir resolved at call time (Rule 1) — tests repoint it."""

    return talk_config.state_dir() / _HISTORY_FILENAME


def _history_enabled() -> bool:
    """Inert under pytest unless a test explicitly opts in.

    Suites exercise this registry transitively WITHOUT repointing the state
    dir — an always-on tee would write test junk into the operator's real
    Hermes home from any of them. History tests monkeypatch this to
    ``lambda: True`` alongside a repointed ``HERMES_HOME``.
    """

    return "PYTEST_CURRENT_TEST" not in os.environ


def _append_history(record: dict) -> None:
    """Fail-open tee. History IO must never break the registry."""

    if not _history_enabled():
        return
    try:
        with _HISTORY_LOCK:
            path = _history_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            if path.stat().st_size > _HISTORY_MAX_BYTES:
                _compact_history_locked(path)
    except Exception as exc:  # noqa: BLE001 — telemetry, not truth
        _log.warning("talk run history append failed: %s", exc)


def _append_history_strict(record: dict) -> None:
    """Fail-CLOSED tee, for the acceptance record only (hermes-talk#35).

    Everything else about this file is telemetry and degrades quietly. The
    acceptance record is not: losing it means no durable destination was ever
    minted, so the caller must refuse the work rather than speak WORK_STARTED
    over nothing. Raises whatever the IO raised, deliberately unwrapped.

    A disabled tee is not a failure — it is a configuration where history is
    inert by design (see :func:`_history_enabled`), and the in-process
    registry remains a valid destination for the life of the connection.
    """

    if not _history_enabled():
        return
    with _HISTORY_LOCK:
        path = _history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        # Compaction is maintenance, not this record's durability contract —
        # the write above already landed. A compaction failure must never be
        # mistaken for "this record didn't make it."
        try:
            if path.stat().st_size > _HISTORY_MAX_BYTES:
                _compact_history_locked(path)
        except Exception as exc:  # noqa: BLE001 — compaction is best-effort
            _log.warning("talk run history compaction failed: %s", exc)


def _compact_history_locked(path) -> None:
    """Rewrite keeping the newest record per run, oldest runs dropped first.

    Caller holds ``_HISTORY_LOCK``. Atomic via tmp + ``os.replace``.
    ``errors="replace"`` so one torn byte cannot wedge compaction forever (a
    strict decode would raise BEFORE per-line handling, letting the file grow
    unbounded) — the torn line fails ``json.loads`` and is dropped, which is
    the designed degradation.
    """

    records: dict[int, dict] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
            records[int(rec["runId"])] = rec
        except Exception:  # noqa: BLE001 — a torn line is dropped, not fatal
            continue
    keep = sorted(records.keys(), reverse=True)[:_HISTORY_COMPACT_KEEP]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        "".join(json.dumps(records[rid], ensure_ascii=False) + "\n" for rid in sorted(keep)),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _history_seed_floor() -> int:
    """Highest persisted run id, distinguishing 'no history' from 'unreadable'.

    An absent file (or a disabled tee) seeds from zero. A file that EXISTS but
    cannot be read must NOT — seeding from zero would mint ids that collide
    with the persisted history the merge and compactor key on. Wall-clock
    seconds is a floor no plausible sequence has reached, so the restarted
    process stays collision-free at the cost of a big id.
    """

    if not _history_enabled():
        return 0
    try:
        with _HISTORY_LOCK:
            path = _history_path()
            if not path.exists():
                return 0
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        ids = []
        for line in lines:
            try:
                ids.append(int(json.loads(line)["runId"]))
            except Exception:  # noqa: BLE001 — a torn line costs itself only
                continue
        return max(ids, default=0)
    except Exception as exc:  # noqa: BLE001 — unreadable-but-present history
        _log.warning("talk run history unreadable at seed time: %s", exc)
        return int(time.time())


def _load_history() -> dict[int, dict]:
    """Newest record per run from the JSONL tail. Fail-open to empty."""

    if not _history_enabled():
        return {}
    try:
        with _HISTORY_LOCK:
            path = _history_path()
            if not path.exists():
                return {}
            # errors="replace": a torn byte must cost one line, not the file.
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        records: dict[int, dict] = {}
        for line in lines[-_HISTORY_TAIL_LINES:]:
            try:
                rec = json.loads(line)
                records[int(rec["runId"])] = rec
            except Exception:  # noqa: BLE001 — a torn line costs itself only
                continue
        return records
    except Exception as exc:  # noqa: BLE001 — history is telemetry, not truth
        _log.warning("talk run history read failed: %s", exc)
        return {}


def _evict_locked() -> None:
    """Drop stale terminal runs. Caller holds the lock."""

    if not _RUNS:
        return
    cutoff = time.time() - _RUN_TTL_S
    for run_id in [
        rid
        for rid, run in _RUNS.items()
        if run["status"] in TERMINAL_STATUSES and run["updated"] < cutoff
    ]:
        _RUNS.pop(run_id, None)
    if len(_RUNS) <= _MAX_RUNS:
        return
    terminal = sorted(
        (rid for rid, run in _RUNS.items() if run["status"] in TERMINAL_STATUSES),
        key=lambda rid: _RUNS[rid]["updated"],
    )
    for run_id in terminal[: len(_RUNS) - _MAX_RUNS]:
        _RUNS.pop(run_id, None)


def start_run(
    kind: str,
    label: str,
    worker: Callable[[int], str],
    *,
    meta: dict | None = None,
) -> int:
    """Register a run and spawn its daemon worker thread.

    ``worker(run_id)`` returns the final text to speak. Raising marks the run
    ``failed`` with the exception text — the registry always terminates.

    Raises :class:`RoutingUnavailable` — BEFORE any work starts — when no Talk
    connection is bound, or when the acceptance record cannot be persisted.
    Both mean the same thing: there is no exact place to send the result, so
    accepting the job would be a promise this process cannot keep.
    """

    if kind not in RUN_KINDS:
        raise ValueError(f"unknown run kind: {kind!r}")

    owner = current_owner()
    if owner is None:
        raise RoutingUnavailable(
            "no Talk connection is bound, so there's nowhere to deliver the result"
        )
    # The ticket is minted here and never mutated again: ownership is decided
    # at acceptance, so a later attach cannot retroactively claim this run.
    ticket = {**owner, "requestId": f"req-{uuid.uuid4().hex[:12]}"}

    global _RUN_SEQ
    # Seed the sequence past the persisted history once per process so run ids
    # stay monotonic across restarts — the history merge keys on runId, and a
    # restarted process must not mint colliding ids.
    if _RUN_SEQ == 0:
        floor = _history_seed_floor()
        with _RUN_LOCK:
            if _RUN_SEQ == 0:
                _RUN_SEQ = floor
    with _RUN_LOCK:
        _RUN_SEQ += 1
        run_id = _RUN_SEQ
        now = time.time()
        entry = {
            "kind": kind,
            "label": label,
            "status": "running",
            "output": "",
            "meta": dict(meta or {}),
            "ticket": dict(ticket),
            "delivery": DELIVERY_PENDING,
            "ts": now,
            "updated": now,
        }
    # Durability FIRST, then the registry, then the worker. The old order wrote
    # history last and fail-open, so a failed write still returned a run id and
    # the caller still spoke WORK_STARTED — a receipt for a run nothing could
    # ever route. A burned sequence number on refusal is the deliberate cost;
    # ids only have to be unique and monotonic, never gapless.
    try:
        _append_history_strict(
            {
                "runId": run_id,
                "kind": kind,
                "label": label,
                "status": "running",
                "output": "",
                "meta": dict(meta or {}),
                "ticket": dict(ticket),
                "delivery": DELIVERY_PENDING,
                "ts": now,
                "updated": now,
            }
        )
    except Exception as exc:
        # Re-raised, never swallowed: no durable route means no acceptance.
        _log.warning(
            "talk run acceptance write failed, refusing dispatch: %s: %s",
            type(exc).__name__, exc,
        )
        raise RoutingUnavailable(
            f"the run couldn't be recorded durably: {type(exc).__name__}: {exc}"
        ) from exc
    with _RUN_LOCK:
        _RUNS[run_id] = entry
        # Evict AFTER inserting so the cap holds for the registry as it now
        # stands; the entry just added is running, so it is never a candidate.
        _evict_locked()
    thread = threading.Thread(
        target=_run_worker,
        args=(run_id, worker),
        name=f"talk-run-{kind}-{run_id}",
        daemon=True,
    )
    thread.start()
    return run_id


def _run_worker(run_id: int, worker: Callable[[int], str]) -> None:
    try:
        output = worker(run_id)
        finish_run(run_id, "done", output)
    except Exception as exc:  # noqa: BLE001 — a run must always terminate
        _log.warning("talk run %s failed: %s: %s", run_id, type(exc).__name__, exc)
        finish_run(run_id, "failed", f"{type(exc).__name__}: {exc}")


def finish_run(run_id: int, status: str, output: str) -> bool:
    """Mark a run terminal. Unknown ids and double-finishes are no-ops.

    Terminal transitions are compare-and-set under ``_RUN_LOCK`` — FIRST
    WRITER WINS, so a later finish from any path can never overwrite the
    status or output. Returns True when THIS call performed the transition.
    """

    if status not in TERMINAL_STATUSES:
        raise ValueError(f"not a terminal status: {status!r}")
    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if run is None or run["status"] in TERMINAL_STATUSES:
            return False
        run["status"] = status
        run["output"] = output
        run["updated"] = time.time()
        tee = _terminal_tee_locked(run_id, run)
    _append_history(tee)
    return True


def _terminal_tee_locked(run_id: int, run: dict) -> dict:
    """The history record for a terminal transition. Caller holds the lock.

    Meta rides the tee: compaction keeps the NEWEST record per run, so a
    terminal record without meta would erase a durable stop receipt written
    moments earlier (Codex v0.6.1 finding 1). Ticket and delivery ride it for
    the identical reason — dropping them would erase the run's return route
    and let an already-spoken result be adopted again on the next reconnect.
    """

    return {
        "runId": run_id,
        "kind": run["kind"],
        "label": run["label"],
        "status": run["status"],
        "output": str(run["output"] or "")[:HISTORY_OUTPUT_CAP],
        "meta": dict(run["meta"]),
        "ticket": dict(run.get("ticket") or {}),
        "delivery": run.get("delivery") or DELIVERY_PENDING,
        "ts": run["ts"],
        "updated": run["updated"],
    }


def annotate_run(run_id: int, *, tee: bool = False, **fields: Any) -> None:
    """Merge worker-observed facts (pid, phase) into the entry.

    ``tee=True`` also persists a full snapshot to the JSONL history — for
    facts that must SURVIVE this process, like a stop receipt promised to
    the operator (Codex v0.6.1 finding 1: a daemon thread's receipt
    otherwise died with the session, and history reloads rebuilt
    ``meta={}``, leaving the promise unkeepable).
    """

    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            return
        run["meta"].update(fields)
        run["updated"] = time.time()
        record = _terminal_tee_locked(run_id, run) if tee else None
    if record is not None:
        _append_history(record)


def mark_delivered(run_id: int) -> bool:
    """Claim a run's result for delivery. True iff THIS call won the claim.

    Exact-once, compare-and-set, in the same first-writer-wins shape as
    :func:`finish_run`. Callers must claim BEFORE they speak: losing the claim
    means another route already owns this result, and speaking it anyway is
    the duplicate-announcement bug this exists to prevent.

    Two substrates, because a result can be claimed from either side of a
    restart:

    - A LIVE run is claimed in the registry, which is authoritative for this
      process. Its durable tee stays fail-open like every other non-acceptance
      write — the operator is present and waiting, so a disk hiccup must not
      swallow a result already in hand. Residual risk, accepted: if that tee
      specifically fails AND this process crashes before any later successful
      write for this run id, the persisted record stays "pending" and a
      reconnect may re-announce a result that was already spoken live.
    - A HISTORY-ONLY run — the reconnect case, whose process is gone — has no
      registry entry to carry the flag, so the durable record IS the claim and
      the write is fail-CLOSED. An unpersisted claim is not a claim, and
      speaking on one would re-announce the same result at every reconnect.
    """

    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if run is not None:
            # A run that has not landed has no result to claim, and claiming
            # it early would mark it delivered forever — the reconnect that
            # was owed the eventual output would then skip right past it.
            if run["status"] not in TERMINAL_STATUSES:
                return False
            if run.get("delivery") == DELIVERED:
                return False
            run["delivery"] = DELIVERED
            run["updated"] = time.time()
            tee = _terminal_tee_locked(run_id, run)
        else:
            tee = None
    if tee is not None:
        _append_history(tee)
        return True
    return _claim_delivery_in_history(run_id)


def _claim_delivery_in_history(run_id: int) -> bool:
    """Flip a history-only run to delivered. True iff this call flipped it.

    Compaction keeps the newest record per run, so appending a delivered copy
    of the whole record supersedes the pending one without losing a field.
    """

    record = _load_history().get(run_id)
    if record is None or record.get("delivery") == DELIVERED:
        return False
    if record.get("status") not in TERMINAL_STATUSES:
        # A record still marked running belonged to a dead process and reads
        # as `lost`; there is no result behind it to hand anyone.
        return False
    claimed = dict(record)
    claimed["delivery"] = DELIVERED
    claimed["updated"] = time.time()
    try:
        _append_history_strict(claimed)
    except Exception as exc:  # noqa: BLE001 — an unclaimed result is not spoken
        _log.warning("talk run %s delivery claim failed to persist: %s", run_id, exc)
        return False
    return True


def list_undelivered_for_session(hermes_session_id: str | None) -> list[dict]:
    """Terminal, unclaimed runs whose ticket names this Hermes session.

    The reconnect-adoption source: a resumed Talk connection standing on the
    same durable Hermes session owns these results and nobody else does.

    Fails closed in both directions. A run with no ticket (every record
    predating hermes-talk#35) and a ticket with no ``hermesSessionId`` never
    match, so history from before this fix surfaces exactly as it does today
    rather than being adopted by a stranger; and a caller with no session id
    of its own gets nothing instead of everything.
    """

    if not hermes_session_id:
        return []
    with _RUN_LOCK:
        live: dict[int, dict] = {}
        for run_id, run in _RUNS.items():
            snapshot = dict(run)
            snapshot["meta"] = dict(run["meta"])
            snapshot["runId"] = run_id
            live[run_id] = snapshot
    # Deliberately NOT list_runs(limit=100, ...): that limit is a UI display
    # cap applied before session filtering, so a busy install (100+ runs from
    # any lane since this session's own orphaned run finished) would silently
    # drop it from the adoption search. Scan the same bound _load_history()
    # already uses (_HISTORY_TAIL_LINES) instead of layering a second, tighter,
    # session-blind cap on top of it.
    merged: dict[int, dict] = dict(_load_history())
    merged.update(live)
    out: list[dict] = []
    for run in merged.values():
        if run.get("status") not in TERMINAL_STATUSES:
            continue
        if run.get("delivery") == DELIVERED:
            continue
        ticket = run.get("ticket")
        if not isinstance(ticket, dict):
            continue
        if ticket.get("hermesSessionId") != hermes_session_id:
            continue
        out.append(run)
    return out


def get_run(run_id: int) -> dict | None:
    """Snapshot one run for the watcher; ``None`` for unknown ids."""

    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            return None
        snapshot = dict(run)
        snapshot["meta"] = dict(run["meta"])
        snapshot["runId"] = run_id
        return snapshot


def list_runs(limit: int = 10, include_history: bool = False) -> list[dict]:
    """Most-recent runs first, newest ``limit`` entries.

    With ``include_history`` the live registry is merged over the persisted
    JSONL tail: live entries win, history-only entries carry
    ``fromHistory: True``, and a history entry still marked ``running`` with
    no live counterpart is reported as ``lost`` — it belonged to a process
    that died, and this one cannot know whether the detached child finished.
    """

    limit = max(1, min(int(limit), 100))
    with _RUN_LOCK:
        live: dict[int, dict] = {}
        for run_id, run in _RUNS.items():
            snapshot = dict(run)
            snapshot["meta"] = dict(run["meta"])
            snapshot["runId"] = run_id
            live[run_id] = snapshot

    if not include_history:
        run_ids = sorted(live.keys(), reverse=True)[:limit]
        return [live[rid] for rid in run_ids]

    merged: dict[int, dict] = {}
    for rid, rec in _load_history().items():
        status = str(rec.get("status") or "")
        merged[rid] = {
            "runId": rid,
            "kind": rec.get("kind"),
            "label": rec.get("label") or "",
            "status": status if status in TERMINAL_STATUSES else "lost",
            "output": str(rec.get("output") or ""),
            # Persisted meta (a durable stop receipt) survives the reload;
            # absent in old records, so default to empty.
            "meta": dict(rec.get("meta") or {}),
            # Same carry-through for the return route. A pre-#35 record has
            # neither, and an absent ticket is what makes it unadoptable.
            "ticket": dict(rec.get("ticket") or {}),
            "delivery": rec.get("delivery") or DELIVERY_PENDING,
            "ts": rec.get("ts"),
            "updated": rec.get("updated"),
            "fromHistory": True,
        }
    merged.update(live)
    run_ids = sorted(merged.keys(), reverse=True)[:limit]
    return [merged[rid] for rid in run_ids]


# -- detached process handles -------------------------------------------------
# In-memory only, NEVER in the JSONL history: a Popen handle is meaningless
# outside this process, and persisting a pid would invite killing a recycled
# one after restart. Holding the handle here is what makes the detached lane
# stoppable at all — stop_work's only channel to a `hermes -z` one-shot.

_PROCESSES: dict[int, object] = {}
_PROCESS_LOCK = threading.Lock()


def register_process(run_id: int, process: object) -> None:
    """Retain the detached child's Popen so stop_work can reach it."""

    with _PROCESS_LOCK:
        _PROCESSES[run_id] = process


def release_process(run_id: int) -> None:
    """Drop the handle once the child has been reaped."""

    with _PROCESS_LOCK:
        _PROCESSES.pop(run_id, None)


def terminate_process(run_id: int) -> bool:
    """Terminate a retained detached child. True iff a live handle was hit."""

    with _PROCESS_LOCK:
        process = _PROCESSES.get(run_id)
    if process is None:
        return False
    try:
        if process.poll() is not None:  # type: ignore[attr-defined]
            return False
        process.terminate()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — a dead/foreign handle is "not stopped"
        return False
    return True


def get_process(run_id: int) -> object | None:
    """Snapshot the retained handle so a confirm can outlive the registry.

    The detached worker releases its registry entry the moment it reaps the
    child — a confirm that re-looked-up by run id could then mistake a
    successfully dead child for an unconfirmable one (Codex v0.6.1 finding
    2). A captured Popen keeps answering ``poll()`` after release.
    """

    with _PROCESS_LOCK:
        return _PROCESSES.get(run_id)


def wait_process(run_id: int, timeout: float, *, process: object | None = None) -> int | None:
    """Bounded wait for a retained child to actually die (hermes-talk#2).

    ``terminate()`` is a signal, not a wait — this is the confirmation half.
    Returns the exit code once the process is gone, or ``None`` if it is
    still running when the budget runs out (or no handle is held). Pass the
    handle captured by :func:`get_process` to stay immune to the worker
    releasing the registry entry mid-wait. Polls outside the lock so a
    wedged child can never wedge the registry.
    """

    if process is None:
        with _PROCESS_LOCK:
            process = _PROCESSES.get(run_id)
    if process is None:
        return None
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            code = process.poll()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — a foreign handle proves nothing
            return None
        if code is not None:
            return int(code)
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def reset_for_tests() -> None:
    """Clear registry state between tests (never called in production).

    Clears the bound owner too: an owner leaking across tests would let a
    suite that never attached one still dispatch work, hiding exactly the
    fail-closed behaviour hermes-talk#35 added.
    """

    global _RUN_SEQ
    with _RUN_LOCK:
        _RUNS.clear()
        _RUN_SEQ = 0
    with _PROCESS_LOCK:
        _PROCESSES.clear()
    detach_owner()


__all__ = [
    "DELIVERED",
    "DELIVERY_PENDING",
    "HISTORY_OUTPUT_CAP",
    "RUN_KINDS",
    "TERMINAL_STATUSES",
    "RoutingUnavailable",
    "annotate_run",
    "attach_owner",
    "current_owner",
    "detach_owner",
    "finish_run",
    "get_process",
    "get_run",
    "list_runs",
    "list_undelivered_for_session",
    "mark_delivered",
    "register_process",
    "release_process",
    "reset_for_tests",
    "start_run",
    "started_sentinel",
    "terminate_process",
    "wait_process",
]
