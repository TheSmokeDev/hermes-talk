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

The tee is fail-open at every seam EXCEPT the acceptance record and the
delivery claim, which are fail-closed for exactly that reason. History IO
must never break the registry, and the file is compacted in place when it
outgrows its cap.

**The file is SHARED BETWEEN PROCESSES.** The CLI lane and the dashboard lane
(``dashboard/plugin_api.py`` imports this module into the Hermes web server
process, and its ``/tool`` route reaches :func:`start_run` through
``talk_host``) both write the same ``state/talk-runs.jsonl``, so every
load-modify-append — id allocation, claims, compaction — holds an OS-level
file lock, not just this process's ``threading.Lock``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
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
# Lock ordering: _HISTORY_LOCK (thread-level) is outermost, the cross-process
# file lock nests inside it, and _RUN_LOCK may nest inside BOTH (run-id
# allocation only). Never take _HISTORY_LOCK or the file lock while already
# holding _RUN_LOCK — tees happen after the registry mutation completes.
_HISTORY_LOCK = threading.Lock()

#: Cross-process serialization for the shared JSONL file (see the module
#: docstring: the CLI lane and the dashboard lane are separate PROCESSES).
#: Same one-byte msvcrt/fcntl mechanism as talk_transcript's writer lease,
#: but blocking with a bounded retry: a concurrent writer should WAIT for
#: its sibling, not fail.
_HISTORY_LOCK_SUFFIX = ".lock"
_HISTORY_LOCK_TIMEOUT_S = 5.0
_HISTORY_LOCK_POLL_S = 0.02

#: Explicit opt-in to accept runs while the history tee is disabled. Without
#: it, :func:`start_run` REFUSES: a disabled tee means no durable return
#: route can be minted, and inheriting that silently is exactly how results
#: get lost. The test suite (where the tee is inert by design) is the
#: intended consumer.
ALLOW_EPHEMERAL_ENV = "TALK_RUNS_ALLOW_EPHEMERAL"

#: Delivery states — a two-phase claim. ``pending`` is the mint-time value;
#: ``claimed`` records WHO is about to speak the result (claimant Talk
#: session + timestamp) without yet consuming it; ``delivered`` is set only
#: after the announcement batch was actually handed to the wire. A record
#: still ``claimed`` by a session that is no longer the current one counts
#: as undelivered and is re-adoptable: the loss window (claimed durably,
#: then torn down before speaking) is closed, and the duplication window is
#: bounded to a crash BETWEEN the wire hand-off and the delivered flip —
#: saying a result twice across a crash is the correct trade against never
#: saying it at all.
DELIVERY_PENDING = "pending"
DELIVERY_CLAIMED = "claimed"
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


def _history_lock_path(path):
    """The sidecar lock path, tolerant of test doubles that stub _history_path."""

    parent = getattr(path, "parent", None)
    name = getattr(path, "name", None)
    if parent is None or not isinstance(name, str):
        return None
    return parent / (name + _HISTORY_LOCK_SUFFIX)


def _lock_fd(fd: int) -> None:
    """Take the one-byte exclusive lock, non-blocking. Raises OSError when held."""

    if os.name == "nt":
        import msvcrt

        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _history_file_lock(path):
    """Hold the OS-level exclusive lock for one load-modify-append.

    The caller holds ``_HISTORY_LOCK`` already, so at most one thread per
    process is ever inside; the OS lock arbitrates BETWEEN processes (the
    dashboard lane's web server and the CLI lane share the file). Raises
    ``TimeoutError`` when a sibling holds it past the bound — strict callers
    turn that into a refusal, fail-open callers into a logged drop. A path
    whose lock sidecar cannot even be resolved (test doubles stubbing
    ``_history_path``) degrades to thread-level locking only, which is the
    pre-lock behaviour.
    """

    lock_path = _history_lock_path(path)
    if lock_path is None:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + _HISTORY_LOCK_TIMEOUT_S
        while True:
            try:
                _lock_fd(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"the talk run history lock ({lock_path}) stayed held "
                        f"for over {_HISTORY_LOCK_TIMEOUT_S:g}s"
                    ) from None
                time.sleep(_HISTORY_LOCK_POLL_S)
        try:
            yield
        finally:
            try:
                _unlock_fd(fd)
            except OSError as exc:  # pragma: no cover - release is best-effort
                _log.warning("talk run history lock release failed: %s", exc)
    finally:
        os.close(fd)


def _append_line_locked(path, record: dict) -> None:
    """Append one record and maybe compact. Caller holds BOTH history locks.

    Raises on append failure. Compaction stays best-effort: it is
    maintenance, not this record's durability contract — the write above
    already landed, and a compaction failure must never be mistaken for
    "this record didn't make it."
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        if path.stat().st_size > _HISTORY_MAX_BYTES:
            _compact_history_locked(path)
    except Exception as exc:  # noqa: BLE001 — compaction is best-effort
        _log.warning("talk run history compaction failed: %s", exc)


def _append_history(record: dict) -> None:
    """Fail-open tee. History IO must never break the registry."""

    if not _history_enabled():
        return
    try:
        with _HISTORY_LOCK:
            path = _history_path()
            with _history_file_lock(path):
                _append_line_locked(path, record)
    except Exception as exc:  # noqa: BLE001 — telemetry, not truth
        _log.warning("talk run history append failed: %s", exc)


def _append_history_strict(record: dict) -> None:
    """Fail-CLOSED tee, for records that ARE a durable contract (hermes-talk#35).

    Everything else about this file is telemetry and degrades quietly. The
    acceptance record and the durable resume handle are not: losing them
    means no durable destination or handle was ever minted, so the caller
    must refuse (or escalate) rather than pretend. Raises whatever the IO
    raised, deliberately unwrapped.

    A disabled tee REFUSES here instead of silently succeeding: a strict
    caller is asking for durability, and a configuration where nothing can
    be durable must surface as a loud no, not a quiet yes. Callers that
    legitimately run without durability opt in by name (see
    ``ALLOW_EPHEMERAL_ENV``) and are expected to gate BEFORE calling this.
    """

    if not _history_enabled():
        raise RuntimeError(
            "the run history tee is disabled, so this record cannot be made "
            f"durable (set {ALLOW_EPHEMERAL_ENV}=1 to explicitly accept "
            "in-memory-only runs)"
        )
    with _HISTORY_LOCK:
        path = _history_path()
        with _history_file_lock(path):
            _append_line_locked(path, record)


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


def _seed_floor_locked(path) -> int:
    """Highest persisted run id, distinguishing 'no history' from 'unreadable'.

    Caller holds BOTH history locks. An absent file floors at zero. A file
    that EXISTS but cannot be read must NOT — seeding from zero would mint
    ids that collide with the persisted history the merge and compactor key
    on. Wall-clock seconds is a floor no plausible sequence has reached, so
    the process stays collision-free at the cost of a big id.
    """

    try:
        if not path.exists():
            return 0
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:  # noqa: BLE001 — unreadable-but-present history
        _log.warning("talk run history unreadable at seed time: %s", exc)
        return int(time.time())
    ids = []
    for line in lines:
        try:
            ids.append(int(json.loads(line)["runId"]))
        except Exception:  # noqa: BLE001 — a torn line costs itself only
            continue
    return max(ids, default=0)


def _tail_records_from_path(path) -> dict[int, dict]:
    """Newest record per run from the JSONL tail. Caller holds the locks.

    ``errors="replace"``: a torn byte must cost one line, not the file.
    """

    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    records: dict[int, dict] = {}
    for line in lines[-_HISTORY_TAIL_LINES:]:
        try:
            rec = json.loads(line)
            records[int(rec["runId"])] = rec
        except Exception:  # noqa: BLE001 — a torn line costs itself only
            continue
    return records


def _load_history() -> dict[int, dict]:
    """Newest record per run from the JSONL tail. Fail-open to empty."""

    if not _history_enabled():
        return {}
    try:
        with _HISTORY_LOCK:
            path = _history_path()
            with _history_file_lock(path):
                return _tail_records_from_path(path)
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
    connection is bound, when the acceptance record cannot be persisted, or
    when the history tee is disabled without the explicit
    ``ALLOW_EPHEMERAL_ENV`` opt-in. All three mean the same thing: there is
    no exact place to send the result, so accepting the job would be a
    promise this process cannot keep.
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
    # ever route.
    run_id = _accept_run(entry)
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


def _ephemeral_runs_allowed() -> bool:
    """The explicit opt-in to non-durable acceptance, resolved at call time."""

    return (os.environ.get(ALLOW_EPHEMERAL_ENV) or "").strip().lower() in {"1", "true", "yes"}


def _accept_run(entry: dict) -> int:
    """Allocate a collision-free run id and persist the acceptance record.

    The id is allocated INSIDE the cross-process file lock, floored on the
    file's own highest persisted id at THAT moment, so two processes sharing
    the file (the CLI lane and the dashboard lane) can never mint the same
    id — the history merge and the compactor key on ``runId``, and a
    collision would let one process's terminal record overwrite the other's.
    A burned sequence number on refusal is the deliberate cost; ids only
    have to be unique and monotonic, never gapless.

    A disabled tee refuses unless the caller opted into ephemeral routing by
    name — silence here is exactly how a "durable" acceptance quietly stops
    being durable.
    """

    global _RUN_SEQ
    if not _history_enabled():
        if not _ephemeral_runs_allowed():
            raise RoutingUnavailable(
                "the run history tee is disabled, so no durable return route "
                f"can be minted — set {ALLOW_EPHEMERAL_ENV}=1 to explicitly "
                "accept in-memory-only routing"
            )
        with _RUN_LOCK:
            _RUN_SEQ += 1
            return _RUN_SEQ
    try:
        with _HISTORY_LOCK:
            path = _history_path()
            with _history_file_lock(path):
                floor = _seed_floor_locked(path)
                with _RUN_LOCK:
                    _RUN_SEQ = max(_RUN_SEQ, floor) + 1
                    run_id = _RUN_SEQ
                _append_line_locked(path, {"runId": run_id, **entry})
        return run_id
    except Exception as exc:
        # Re-raised, never swallowed: no durable route means no acceptance.
        _log.warning(
            "talk run acceptance write failed, refusing dispatch: %s: %s",
            type(exc).__name__, exc,
        )
        raise RoutingUnavailable(
            f"the run couldn't be recorded durably: {type(exc).__name__}: {exc}"
        ) from exc


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
        "deliveryClaim": dict(run.get("deliveryClaim") or {}),
        "ts": run["ts"],
        "updated": run["updated"],
    }


def annotate_run(
    run_id: int, *, tee: bool = False, durable: bool = False, **fields: Any
) -> None:
    """Merge worker-observed facts (pid, phase) into the entry.

    ``tee=True`` also persists a full snapshot to the JSONL history — for
    facts that must SURVIVE this process, like a stop receipt promised to
    the operator (Codex v0.6.1 finding 1: a daemon thread's receipt
    otherwise died with the session, and history reloads rebuilt
    ``meta={}``, leaving the promise unkeepable). Fail-open: a disk hiccup
    costs the receipt, never the run.

    ``durable=True`` (implies tee) is for the one fact that is a RESUME
    HANDLE rather than a receipt: the api-server lane's remote run id. It
    rides the strict, cross-process-locked append with one retry; if the
    write still cannot land, the failure is ESCALATED to an error log naming
    the run — the run itself keeps going (its live watcher still delivers),
    but a reconnect would not be able to resume tracking it, and that must
    be visible rather than swallowed as telemetry.
    """

    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            return
        run["meta"].update(fields)
        run["updated"] = time.time()
        record = _terminal_tee_locked(run_id, run) if (tee or durable) else None
    if record is None:
        return
    if not durable:
        _append_history(record)
        return
    if not _history_enabled():
        # The ephemeral opt-in (or an explicit history test fixture) already
        # declared in-memory-only routing acceptable; there is nothing to
        # escalate about a tee that is inert by configuration.
        return
    for attempt in (1, 2):
        try:
            _append_history_strict(record)
            return
        except Exception as exc:  # noqa: BLE001 — retry once, then escalate
            if attempt == 2:
                _log.error(
                    "talk run %s: the durable annotate (%s) could not be "
                    "persisted after a retry — a reconnect cannot resume this "
                    "run: %s: %s",
                    run_id,
                    ", ".join(sorted(fields)),
                    type(exc).__name__,
                    exc,
                )


def claim_delivery(run_id: int, *, claimant: str) -> bool:
    """Phase one: stake this session's claim on a terminal result.

    True iff THIS call staked the claim. Claim BEFORE queueing the
    announcement — losing means another route already owns this result, and
    speaking it anyway is the duplicate-announcement bug this exists to
    prevent. Flip with :func:`mark_delivered` only after the batch is
    actually handed to the wire: a claim alone never consumes the result, so
    a teardown between the two leaves it re-adoptable instead of lost.

    Two substrates, because a result can be claimed from either side of a
    restart:

    - A LIVE run is claimed in the registry, which is authoritative for this
      process, and only from ``pending`` — a live claim held by another
      route means someone in this process is already about to speak it. The
      durable tee of the claim stays fail-open like every other
      non-acceptance write (the operator is present and waiting; a disk
      hiccup must not swallow a result already in hand).
    - A HISTORY-ONLY run — the reconnect case, whose process is gone — has
      no registry entry to carry the flag, so the durable record IS the
      claim and the write is fail-CLOSED, atomically under the
      cross-process file lock. A stale claim by a DIFFERENT session is
      stolen here on purpose: its claimant died with its process, and
      honouring it forever would strand the result.
    """

    claimant = str(claimant or "")
    if not claimant:
        return False
    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if run is not None:
            # A run that has not landed has no result to claim, and claiming
            # it early would consume it — the reconnect that was owed the
            # eventual output would then skip right past it.
            if run["status"] not in TERMINAL_STATUSES:
                return False
            if run.get("delivery") != DELIVERY_PENDING:
                return False
            run["delivery"] = DELIVERY_CLAIMED
            run["deliveryClaim"] = {"claimant": claimant, "ts": time.time()}
            run["updated"] = time.time()
            tee = _terminal_tee_locked(run_id, run)
        else:
            tee = None
    if tee is not None:
        _append_history(tee)
        return True
    return _claim_in_history(run_id, claimant)


def _claim_in_history(run_id: int, claimant: str) -> bool:
    """Stake a durable claim on a history-only run. True iff this call did.

    One atomic load-check-append under the cross-process file lock: without
    it, the CLI lane and the dashboard lane could both read ``pending`` and
    both append a claim — the cross-process double-claim this lock exists to
    prevent. Compaction keeps the newest record per run, so the appended
    claimed copy supersedes the pending one without losing a field.
    """

    if not _history_enabled():
        return False
    try:
        with _HISTORY_LOCK:
            path = _history_path()
            with _history_file_lock(path):
                record = _tail_records_from_path(path).get(run_id)
                if record is None or record.get("status") not in TERMINAL_STATUSES:
                    # A record still marked running belonged to a dead process
                    # and reads as `lost`; there is no result behind it.
                    return False
                if record.get("delivery") == DELIVERED:
                    return False
                claim = record.get("deliveryClaim") or {}
                if (
                    record.get("delivery") == DELIVERY_CLAIMED
                    and claim.get("claimant") == claimant
                ):
                    # Already ours — queueing it again would duplicate.
                    return False
                claimed = dict(record)
                claimed["delivery"] = DELIVERY_CLAIMED
                claimed["deliveryClaim"] = {"claimant": claimant, "ts": time.time()}
                claimed["updated"] = time.time()
                _append_line_locked(path, claimed)
        return True
    except Exception as exc:  # noqa: BLE001 — an unclaimed result is not spoken
        _log.warning("talk run %s delivery claim failed to persist: %s", run_id, exc)
        return False


def mark_delivered(run_id: int, *, claimant: str) -> bool:
    """Phase two: flip a claimed result to delivered, post-wire.

    True iff THIS call performed the flip. Only the session that HOLDS the
    claim may flip it — asserting the claimant closes the any-caller
    denial-of-delivery hole where a stranger could consume a result it never
    claimed. Call it at the announcement pump's post-send point, never at
    enqueue: flipping early re-opens the loss window :func:`claim_delivery`
    exists to close.

    The flip is fail-open on both substrates — the result has already been
    handed to the wire, so a disk hiccup here must not matter. A flip that
    fails to persist leaves the record claimed; a LATER session sees a stale
    claim and re-adopts, which is the bounded-duplication side of the trade
    and the right one.
    """

    claimant = str(claimant or "")
    if not claimant:
        return False
    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if run is not None:
            if run["status"] not in TERMINAL_STATUSES:
                return False
            claim = run.get("deliveryClaim") or {}
            if run.get("delivery") != DELIVERY_CLAIMED or claim.get("claimant") != claimant:
                return False
            run["delivery"] = DELIVERED
            run["updated"] = time.time()
            tee = _terminal_tee_locked(run_id, run)
        else:
            tee = None
    if tee is not None:
        _append_history(tee)
        return True
    return _flip_delivered_in_history(run_id, claimant)


def _flip_delivered_in_history(run_id: int, claimant: str) -> bool:
    """Flip a history-only claim this claimant holds. True iff it flipped."""

    if not _history_enabled():
        return False
    try:
        with _HISTORY_LOCK:
            path = _history_path()
            with _history_file_lock(path):
                record = _tail_records_from_path(path).get(run_id)
                if record is None or record.get("delivery") != DELIVERY_CLAIMED:
                    return False
                claim = record.get("deliveryClaim") or {}
                if claim.get("claimant") != claimant:
                    return False
                flipped = dict(record)
                flipped["delivery"] = DELIVERED
                flipped["updated"] = time.time()
                _append_line_locked(path, flipped)
        return True
    except Exception as exc:  # noqa: BLE001 — the spoken result stands either way
        _log.warning("talk run %s delivered flip failed to persist: %s", run_id, exc)
        return False


def list_undelivered_for_session(
    hermes_session_id: str | None,
    *,
    operator: str | None,
    profile: str | None,
    claimant: str | None = None,
) -> list[dict]:
    """Terminal, undelivered runs whose ticket names this exact binding.

    The reconnect-adoption source: a resumed Talk connection standing on the
    same durable Hermes session — under the SAME operator and profile
    binding — owns these results and nobody else does. The ticket's
    ``operator`` and ``profile`` are ENFORCED, not just recorded: a ticket
    accepted under a different binding is not adopted even behind the same
    Hermes session id; the mismatch is logged and skipped.

    Delivery-state filter, per the two-phase claim: ``delivered`` never
    surfaces; ``claimed`` by ``claimant`` (this caller's own in-flight
    announcements) never surfaces; ``claimed`` by anyone ELSE surfaces —
    that claimant's process is gone by definition on this path, and a claim
    whose announcement was never handed to the wire must stay collectable
    (see :func:`claim_delivery`).

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
        delivery = run.get("delivery")
        if delivery == DELIVERED:
            continue
        claim = run.get("deliveryClaim") or {}
        if delivery == DELIVERY_CLAIMED and claimant and claim.get("claimant") == claimant:
            continue
        ticket = run.get("ticket")
        if not isinstance(ticket, dict):
            continue
        if ticket.get("hermesSessionId") != hermes_session_id:
            continue
        if ticket.get("operator") != operator or ticket.get("profile") != profile:
            _log.info(
                "talk run %s not adopted: its ticket is bound to operator=%r "
                "profile=%r, not this session's operator=%r profile=%r",
                run.get("runId"),
                ticket.get("operator"),
                ticket.get("profile"),
                operator,
                profile,
            )
            continue
        out.append(run)
    _report_adoption_tail_falloff(hermes_session_id)
    return out


def _report_adoption_tail_falloff(hermes_session_id: str) -> None:
    """Visibility only: count owed results the adoption tail can no longer see.

    The adoption scan is bounded to the newest ``_HISTORY_TAIL_LINES`` lines.
    A terminal, undelivered record for this session sitting BEYOND that bound
    will never be adopted; the bound is the design, but falling off it must
    be reported, not silent. Fail-open at every seam — a visibility check
    must never break adoption itself.
    """

    if not _history_enabled():
        return
    try:
        with _HISTORY_LOCK:
            path = _history_path()
            if not path.exists():
                return
            with _history_file_lock(path):
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= _HISTORY_TAIL_LINES:
            return
        tail_ids: set[int] = set()
        for line in lines[-_HISTORY_TAIL_LINES:]:
            try:
                tail_ids.add(int(json.loads(line)["runId"]))
            except Exception:  # noqa: BLE001 — a torn line costs itself only
                continue
        dropped: dict[int, dict] = {}
        for line in lines[:-_HISTORY_TAIL_LINES]:
            try:
                rec = json.loads(line)
                dropped[int(rec["runId"])] = rec
            except Exception:  # noqa: BLE001 — a torn line costs itself only
                continue
        lost = sorted(
            rid
            for rid, rec in dropped.items()
            if rid not in tail_ids
            and rec.get("status") in TERMINAL_STATUSES
            and rec.get("delivery") != DELIVERED
            and isinstance(rec.get("ticket"), dict)
            and rec["ticket"].get("hermesSessionId") == hermes_session_id
        )
        if lost:
            _log.warning(
                "talk run adoption: %d owed result(s) for this session fell off "
                "the %d-line history tail and will not be adopted: runs %s",
                len(lost),
                _HISTORY_TAIL_LINES,
                lost,
            )
    except Exception as exc:  # noqa: BLE001 — visibility must never break adoption
        _log.debug("talk run adoption tail check failed: %s", exc)


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
            "deliveryClaim": dict(rec.get("deliveryClaim") or {}),
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
    "ALLOW_EPHEMERAL_ENV",
    "DELIVERED",
    "DELIVERY_CLAIMED",
    "DELIVERY_PENDING",
    "HISTORY_OUTPUT_CAP",
    "RUN_KINDS",
    "TERMINAL_STATUSES",
    "RoutingUnavailable",
    "annotate_run",
    "attach_owner",
    "claim_delivery",
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
