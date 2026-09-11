"""Owner-bound derived task observations and delivery receipts, never execution authority.

All I/O is worker-only. Sources are supplied by authenticated host integration code;
this module opens no stream and never calls an inference or approval-submit route.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

try:
    from .talk_attachment import CaptureToken, HistoryDelivery
    from .talk_outbox import HistoryOutbox
    from .talk_passive import HistoryOwner, digest, identifier, session_id
    from .talk_task_sources import (
        RunReference,
        TaskEventError,
        TaskObservation,
        api_poll_observation,
        hook_observation,
        integer,
        rpc_observations,
    )
except ImportError:  # pragma: no cover - flat plugin load
    from talk_attachment import CaptureToken, HistoryDelivery
    from talk_outbox import HistoryOutbox
    from talk_passive import HistoryOwner, digest, identifier, session_id
    from talk_task_sources import (
        RunReference,
        TaskEventError,
        TaskObservation,
        api_poll_observation,
        hook_observation,
        integer,
        rpc_observations,
    )


@dataclass(frozen=True, slots=True)
class SourceLease:
    owner_key: str
    source_id: str
    mode: str
    session_id: str
    epoch: str | None
    revision: int
    run_id: int | None


@dataclass(frozen=True, slots=True)
class SpeechAttempt:
    event_id: str
    attempt_id: str
    capture: CaptureToken


@dataclass(frozen=True, slots=True)
class ApprovalReader:
    """An authenticated, owner-bound read of approval.pending or list_gateway_approvals.

    Host integration constructs this, not request/model arguments. The callback
    returns the authoritative current list, not cached event or API poll metadata.
    Submission remains with the existing host resolver using the exact request ID.
    """

    owner: HistoryOwner
    session_id: str
    read_pending: Callable[[], list[dict]] = field(repr=False)


class TaskEvents:
    UPDATE_MODES = ("important", "completion", "frequent")

    def __init__(
        self,
        outbox: HistoryOutbox,
        token: CaptureToken,
        *,
        max_events: int = 256,
        ttl_s: float = 86400,
        clock: Callable[[], float] = time.time,
    ):
        if type(max_events) is not int or not 1 <= max_events <= 512:
            raise TaskEventError("invalid_event")
        if not math.isfinite(ttl_s) or not 0 < ttl_s <= 86400:
            raise TaskEventError("invalid_event")
        self._outbox, self._owner = outbox, token.owner
        self._max_events, self._ttl, self._clock = max_events, ttl_s, clock
        with self._fenced(token) as db:
            # Task settings outlive derived observations. Do not attach this table
            # to task_event_owners, whose rows expire with the replay cache.
            db.execute("""CREATE TABLE IF NOT EXISTS task_preferences (
                owner TEXT PRIMARY KEY, update_mode TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS task_event_owners (
                owner TEXT PRIMARY KEY, floor_idx INTEGER NOT NULL DEFAULT 0,
                expires REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS task_event_sources (
                owner TEXT NOT NULL REFERENCES task_event_owners(owner) ON DELETE CASCADE,
                source_id TEXT NOT NULL, mode TEXT NOT NULL, session_id TEXT NOT NULL,
                epoch TEXT, revision INTEGER NOT NULL, run_id INTEGER,
                cursor INTEGER NOT NULL DEFAULT 0,
                latest INTEGER NOT NULL DEFAULT 0, gap TEXT NOT NULL, occurred REAL,
                availability TEXT NOT NULL, expires REAL NOT NULL,
                PRIMARY KEY(owner,source_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS task_event_runs (
                owner TEXT NOT NULL REFERENCES task_event_owners(owner) ON DELETE CASCADE,
                run_id INTEGER NOT NULL, binding TEXT NOT NULL, operator_hash TEXT NOT NULL,
                expires REAL NOT NULL,
                PRIMARY KEY(owner,run_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS task_events (
                idx INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL REFERENCES task_event_owners(owner) ON DELETE CASCADE,
                event_id TEXT NOT NULL, source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
                epoch TEXT, source_seq INTEGER, occurred REAL, canonical_event TEXT,
                data TEXT NOT NULL,
                generation INTEGER NOT NULL, connection_id TEXT NOT NULL, live INTEGER NOT NULL,
                expires REAL NOT NULL, UNIQUE(owner,event_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS task_event_speech (
                event_idx INTEGER PRIMARY KEY REFERENCES task_events(idx) ON DELETE CASCADE,
                attempt_id TEXT NOT NULL, generation INTEGER NOT NULL, connection_id TEXT NOT NULL,
                state TEXT NOT NULL, playback_supported INTEGER NOT NULL)""")
            self._prune(db)
            self._ensure_owner(db)
            # Reconnection is not proof an interrupted handoff played. Never requeue.
            db.execute(
                """UPDATE task_event_speech SET state='unknown' WHERE connection_id=?
                AND generation!=? AND state IN ('queued','sent') AND event_idx IN
                (SELECT idx FROM task_events WHERE owner=?)""",
                (token.connection_id, token.generation, self._owner.key),
            )

    def preferences(self, token):
        with self._fenced(token) as db:
            row = db.execute(
                "SELECT update_mode FROM task_preferences WHERE owner=?", (self._owner.key,)
            ).fetchone()
            return {"update_mode": row[0] if row else "important"}

    def set_update_preference(self, token, mode):
        with self._fenced(token) as db:
            return self._set_update_preference(db, mode)

    def _set_update_preference(self, db, mode):
        if mode not in self.UPDATE_MODES:
            raise TaskEventError("invalid_event")
        exists = db.execute(
            "SELECT 1 FROM task_preferences WHERE owner=?", (self._owner.key,)
        ).fetchone()
        count = db.execute("SELECT count(*) FROM task_preferences").fetchone()[0]
        if not exists and count >= 256:
            raise TaskEventError("capacity")
        db.execute(
            "INSERT INTO task_preferences(owner,update_mode) VALUES (?,?) "
            "ON CONFLICT(owner) DO UPDATE SET update_mode=excluded.update_mode",
            (self._owner.key, mode),
        )
        return {"update_mode": mode}

    def _fenced(self, token):
        if not isinstance(token, CaptureToken) or token.owner != self._owner:
            raise TaskEventError("foreign_owner")
        return self._outbox.fenced(self._owner, token.connection_id, token.generation)

    def _ensure_owner(self, db):
        if not db.execute(
            "SELECT 1 FROM task_event_owners WHERE owner=?", (self._owner.key,)
        ).fetchone():
            if db.execute("SELECT count(*) FROM task_event_owners").fetchone()[0] >= 32:
                raise TaskEventError("capacity")
            db.execute(
                "INSERT INTO task_event_owners(owner,expires) VALUES (?,?)",
                (self._owner.key, self._clock() + self._ttl),
            )

    def _prune(self, db):
        now = self._clock()
        for owner, floor in db.execute(
            "SELECT owner,max(idx) FROM task_events WHERE expires<=? GROUP BY owner",
            (now,),
        ).fetchall():
            db.execute(
                "UPDATE task_event_owners SET floor_idx=max(floor_idx,?) WHERE owner=?",
                (floor, owner),
            )
        # Profile-wide admission counts require profile-wide expiry reclamation.
        # Only expired records qualify; capacity pressure never evicts live foreign state.
        db.execute("DELETE FROM task_events WHERE expires<=?", (now,))
        db.execute("DELETE FROM task_event_sources WHERE expires<=?", (now,))
        db.execute("DELETE FROM task_event_runs WHERE expires<=?", (now,))
        db.execute(
            """DELETE FROM task_event_owners WHERE expires<=?
            AND NOT EXISTS (SELECT 1 FROM task_events WHERE owner=task_event_owners.owner)
            AND NOT EXISTS (SELECT 1 FROM task_event_sources WHERE owner=task_event_owners.owner)
            AND NOT EXISTS (SELECT 1 FROM task_event_runs WHERE owner=task_event_owners.owner)""",
            (now,),
        )

    def _touch_owner(self, db):
        db.execute(
            "UPDATE task_event_owners SET expires=max(expires,?) WHERE owner=?",
            (self._clock() + self._ttl, self._owner.key),
        )

    def _touch_source(self, db, lease):
        expires = self._clock() + self._ttl
        db.execute(
            "UPDATE task_event_sources SET availability='available',expires=? "
            "WHERE owner=? AND source_id=?",
            (expires, self._owner.key, lease.source_id),
        )
        if lease.run_id is not None:
            db.execute(
                "UPDATE task_event_runs SET expires=? WHERE owner=? AND run_id=?",
                (expires, self._owner.key, lease.run_id),
            )
        self._touch_owner(db)

    @contextmanager
    def _db(self, token):
        # Expiration commits even when the requested observation/read is later refused.
        with self._fenced(token) as db:
            self._prune(db)
        with self._fenced(token) as db:
            self._ensure_owner(db)
            yield db

    def bind_run(
        self,
        token: CaptureToken,
        run: dict,
        *,
        operator: str,
        worker_session_id: str | None,
        api_run_id: str | None = None,
        origin_turn_id: str | None = None,
    ) -> RunReference:
        binding = RunReference.from_ticket(
            self._owner,
            run,
            operator=operator,
            worker_session_id=worker_session_id,
            api_run_id=api_run_id,
            origin_turn_id=origin_turn_id,
        )
        data = json.dumps(asdict(binding), sort_keys=True)
        with self._db(token) as db:
            prior = db.execute(
                "SELECT binding,operator_hash FROM task_event_runs WHERE owner=? AND run_id=?",
                (self._owner.key, binding.local_run_id),
            ).fetchone()
            if prior and tuple(prior) != (data, digest(operator)):
                raise TaskEventError("event_conflict")
            if not prior:
                if db.execute("SELECT count(*) FROM task_event_runs").fetchone()[0] >= 128:
                    raise TaskEventError("capacity")
                db.execute(
                    "INSERT INTO task_event_runs VALUES (?,?,?,?,?)",
                    (
                        self._owner.key,
                        binding.local_run_id,
                        data,
                        digest(operator),
                        self._clock() + self._ttl,
                    ),
                )
            else:
                db.execute(
                    "UPDATE task_event_runs SET expires=? WHERE owner=? AND run_id=?",
                    (self._clock() + self._ttl, self._owner.key, binding.local_run_id),
                )
            self._touch_owner(db)
        return binding

    def _binding(self, db, run_id):
        row = db.execute(
            "SELECT binding,operator_hash FROM task_event_runs WHERE owner=? AND run_id=?",
            (self._owner.key, integer(run_id, minimum=1)),
        ).fetchone()
        if row is None:
            raise TaskEventError("missing_reference")
        try:
            return RunReference(**json.loads(row[0])), row[1]
        except (TypeError, ValueError):
            raise TaskEventError("invalid_event") from None

    def open_source(
        self,
        token: CaptureToken,
        source_id: str,
        *,
        mode: str,
        source_session: str,
        epoch: str | None = None,
        previous: SourceLease | None = None,
        run_id: int | None = None,
    ) -> SourceLease:
        """Use the real RPC handshake epoch; API polls/hooks have no source epoch."""
        identifier(source_id)
        session_id(source_session)
        if mode not in {"rpc", "api_poll", "hook"} or (mode == "rpc") != (epoch is not None):
            raise TaskEventError("unsupported")
        if epoch is not None:
            identifier(epoch)
        with self._db(token) as db:
            if mode == "api_poll" or source_session != self._owner.session_id or run_id is not None:
                if run_id is None:
                    raise TaskEventError("missing_reference")
                binding, _ = self._binding(db, run_id)
                if binding.worker_session_id != source_session:
                    raise TaskEventError("foreign_owner")
            prior = db.execute(
                "SELECT * FROM task_event_sources WHERE owner=? AND source_id=?",
                (self._owner.key, source_id),
            ).fetchone()
            if previous is not None:
                self._source(db, previous)
                if previous.source_id != source_id:
                    raise TaskEventError("stale_source")
            elif prior is not None:
                raise TaskEventError("stale_source")
            if prior and (
                prior["mode"] != mode
                or prior["session_id"] != source_session
                or prior["run_id"] != run_id
            ):
                raise TaskEventError("foreign_owner")
            if (
                prior is None
                and db.execute("SELECT count(*) FROM task_event_sources").fetchone()[0] >= 64
            ):
                raise TaskEventError("capacity")
            revision = db.execute(
                "UPDATE metadata SET next_generation=next_generation+1 RETURNING next_generation"
            ).fetchone()[0]
            same_epoch = prior is not None and prior["epoch"] == epoch
            gap = prior["gap"] if same_epoch else ("epoch_reset" if prior else "none")
            if mode != "rpc":
                gap = "snapshot_only" if mode == "api_poll" else "unsequenced"
            db.execute(
                """INSERT OR REPLACE INTO task_event_sources
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self._owner.key,
                    source_id,
                    mode,
                    source_session,
                    epoch,
                    revision,
                    run_id,
                    prior["cursor"] if same_epoch else 0,
                    prior["latest"] if same_epoch else 0,
                    gap,
                    prior["occurred"] if same_epoch else None,
                    "available",
                    self._clock() + self._ttl,
                ),
            )
            lease = SourceLease(
                self._owner.key, source_id, mode, source_session, epoch, revision, run_id
            )
            self._touch_source(db, lease)
            return lease

    def _source(self, db, lease):
        if lease.owner_key != self._owner.key:
            raise TaskEventError("foreign_owner")
        row = db.execute(
            "SELECT * FROM task_event_sources WHERE owner=? AND source_id=?",
            (self._owner.key, lease.source_id),
        ).fetchone()
        if row is None or (
            row["mode"],
            row["session_id"],
            row["epoch"],
            row["revision"],
            row["run_id"],
        ) != (
            lease.mode,
            lease.session_id,
            lease.epoch,
            lease.revision,
            lease.run_id,
        ):
            raise TaskEventError("stale_source")
        return row

    def resume_source(self, token: CaptureToken, source_id: str) -> SourceLease:
        with self._db(token) as db:
            row = db.execute(
                "SELECT * FROM task_event_sources WHERE owner=? AND source_id=?",
                (self._owner.key, identifier(source_id)),
            ).fetchone()
            if row is None:
                raise TaskEventError("missing_reference")
            return SourceLease(
                self._owner.key,
                source_id,
                row["mode"],
                row["session_id"],
                row["epoch"],
                row["revision"],
                row["run_id"],
            )

    def _append(self, db, token, lease, observation, *, live):
        if type(live) is not bool:
            raise TaskEventError("invalid_event")
        data = json.dumps(observation.wire(), sort_keys=True, ensure_ascii=True)
        if len(data) > 4096:
            raise TaskEventError("capacity")
        prior = db.execute(
            "SELECT idx,data FROM task_events WHERE owner=? AND event_id=?",
            (self._owner.key, observation.event_id),
        ).fetchone()
        if prior:
            if prior["data"] != data:
                raise TaskEventError("event_conflict")
            return prior["idx"]
        idx = db.execute(
            """INSERT INTO task_events
            (owner,event_id,source_id,source_revision,epoch,source_seq,occurred,canonical_event,
             data,generation,connection_id,live,expires)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING idx""",
            (
                self._owner.key,
                observation.event_id,
                lease.source_id,
                lease.revision,
                lease.epoch,
                observation.source_seq,
                observation.occurred_at,
                observation.action_id if observation.kind == "history_saved" else None,
                data,
                token.generation,
                token.connection_id,
                int(live),
                self._clock() + self._ttl,
            ),
        ).fetchone()[0]
        self._touch_owner(db)
        while True:
            count = db.execute(
                "SELECT count(*) FROM task_events WHERE owner=?", (self._owner.key,)
            ).fetchone()[0]
            if count <= self._max_events:
                break
            oldest = db.execute(
                "SELECT idx,owner FROM task_events WHERE owner=? ORDER BY idx LIMIT 1",
                (self._owner.key,),
            ).fetchone()
            db.execute(
                "UPDATE task_event_owners SET floor_idx=max(floor_idx,?) WHERE owner=?",
                tuple(oldest),
            )
            db.execute("DELETE FROM task_events WHERE idx=?", (oldest["idx"],))
        total, size = db.execute(
            "SELECT count(*),coalesce(sum(length(data)),0) FROM task_events"
        ).fetchone()
        if total > 512 or size > 1024 * 1024:
            raise TaskEventError("capacity")
        return idx

    def observe_rpc(self, token, lease, payload, *, live=False):
        if lease.mode != "rpc":
            raise TaskEventError("unsupported")
        observations, sequences = rpc_observations(
            payload,
            source_id=lease.source_id,
            expected_epoch=lease.epoch,
            source_session=lease.session_id,
        )
        with self._db(token) as db:
            source = self._source(db, lease)
            cursor = source["cursor"]
            ids = []
            for observation in observations:
                # Retained duplicate rows are verified; expired/evicted old rows stay retired.
                retained = db.execute(
                    "SELECT 1 FROM task_events WHERE owner=? AND event_id=?",
                    (self._owner.key, observation.event_id),
                ).fetchone()
                if observation.source_seq > cursor or retained:
                    ids.append(self._append(db, token, lease, observation, live=live))
            latest = max(source["latest"], payload["latest_seq"])
            seen = set(sequences)
            seen.update(
                row[0]
                for row in db.execute(
                    "SELECT source_seq FROM task_events WHERE owner=? "
                    "AND source_id=? AND epoch=? AND source_seq>?",
                    (self._owner.key, lease.source_id, lease.epoch, cursor),
                )
            )
            while cursor + 1 in seen:
                cursor += 1
            gap = source["gap"]
            if payload["truncated"]:
                cursor, gap = latest, "truncated"
            elif cursor < latest and gap in {"none", "missing_sequence"}:
                gap = "missing_sequence"
            elif cursor == latest and gap == "missing_sequence":
                gap = "none"
            db.execute(
                "UPDATE task_event_sources SET cursor=?,latest=?,gap=? "
                "WHERE owner=? AND source_id=?",
                (cursor, latest, gap, self._owner.key, lease.source_id),
            )
            self._touch_source(db, lease)
            return tuple(ids)

    def observe_poll(self, token, lease, run_id, payload, *, live=False):
        if lease.mode != "api_poll" or lease.run_id != run_id:
            raise TaskEventError("unsupported")
        with self._db(token) as db:
            source = self._source(db, lease)
            binding, _ = self._binding(db, run_id)
            observation = api_poll_observation(payload, binding, source_id=lease.source_id)
            if observation.session_id != lease.session_id:
                raise TaskEventError("foreign_owner")
            terminal = {"completed", "failed", "cancelled", "lost"}
            if observation.state not in terminal:
                prior = db.execute(
                    "SELECT data FROM task_events WHERE owner=? AND source_id=? "
                    "ORDER BY idx DESC LIMIT 1",
                    (self._owner.key, lease.source_id),
                ).fetchone()
                if prior and json.loads(prior[0]).get("state") in terminal:
                    return None
            if source["occurred"] is not None and observation.occurred_at < source["occurred"]:
                return None
            retained = db.execute(
                "SELECT 1 FROM task_events WHERE owner=? AND event_id=?",
                (self._owner.key, observation.event_id),
            ).fetchone()
            if source["occurred"] == observation.occurred_at and not retained:
                return None
            idx = self._append(db, token, lease, observation, live=live)
            db.execute(
                "UPDATE task_event_sources SET occurred=?,gap='snapshot_only' "
                "WHERE owner=? AND source_id=?",
                (observation.occurred_at, self._owner.key, lease.source_id),
            )
            self._touch_source(db, lease)
            return idx

    def observe_hook(self, token, lease, run_id, kind, payload, *, observation_id, live=False):
        if lease.mode != "hook" or lease.run_id != run_id:
            raise TaskEventError("unsupported")
        with self._db(token) as db:
            self._source(db, lease)
            binding, _ = self._binding(db, run_id)
            observation = hook_observation(
                kind, payload, observation_id=observation_id, binding=binding
            )
            if observation.session_id != lease.session_id:
                raise TaskEventError("foreign_owner")
            idx = self._append(db, token, lease, observation, live=live)
            self._touch_source(db, lease)
            return idx

    def source_unavailable(self, token, lease):
        with self._db(token) as db:
            self._source(db, lease)
            db.execute(
                "UPDATE task_event_sources SET availability='unavailable' "
                "WHERE owner=? AND source_id=?",
                (self._owner.key, lease.source_id),
            )

    def observe_commit(self, token, lease, delivery: HistoryDelivery):
        """Link an already verified P2a receipt; no canonical write or order inference."""
        if (
            not isinstance(delivery, HistoryDelivery)
            or delivery.state != "saved"
            or not delivery.receipt
        ):
            raise TaskEventError("missing_reference")
        receipt = delivery.receipt
        with self._db(token) as db:
            self._source(db, lease)
            if lease.mode != "hook" or lease.session_id != self._owner.session_id:
                raise TaskEventError("foreign_owner")
            row = db.execute(
                "SELECT state,origin_turn_id,conversation_id FROM events "
                "WHERE event_id=? AND owner=?",
                (receipt.event_id, self._owner.key),
            ).fetchone()
            if row is None or tuple(row) != (
                "saved",
                receipt.origin_turn_id,
                receipt.conversation_id,
            ):
                raise TaskEventError("missing_reference")
            event = TaskObservation(
                digest([lease.source_id, "receipt", receipt.event_id]),
                "history_saved",
                self._owner.session_id,
                action_id=receipt.event_id,
                origin_turn_id=receipt.origin_turn_id,
                canonical_revision=receipt.revision,
            )
            idx = self._append(db, token, lease, event, live=False)
            self._touch_source(db, lease)
            return idx

    def page(self, token, *, after=0, limit=50):
        integer(after)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise TaskEventError("invalid_event")
        with self._db(token) as db:
            floor = db.execute(
                "SELECT floor_idx FROM task_event_owners WHERE owner=?", (self._owner.key,)
            ).fetchone()[0]
            rows = db.execute(
                """SELECT e.*,s.state AS delivery FROM task_events e
                LEFT JOIN task_event_speech s ON s.event_idx=e.idx
                WHERE e.owner=? AND e.idx>? ORDER BY e.idx LIMIT ?""",
                (self._owner.key, after, limit),
            ).fetchall()
            events = [
                {
                    **json.loads(row["data"]),
                    "observed_index": row["idx"],
                    "source_epoch": row["epoch"],
                    "delivery": row["delivery"] or "unclaimed",
                    "replay": True,
                }
                for row in rows
            ]
            sources = [
                dict(row)
                for row in db.execute(
                    "SELECT source_id,mode,epoch,cursor,latest,gap,availability "
                    "FROM task_event_sources WHERE owner=?",
                    (self._owner.key,),
                )
            ]
            return {
                "events": events,
                "next_cursor": rows[-1]["idx"] if rows else after,
                "retention_gap": after < floor,
                "snapshot_refetch_required": not sources
                or after < floor
                or any(
                    source["gap"] in {"truncated", "epoch_reset", "missing_sequence"}
                    or source["availability"] == "unavailable"
                    for source in sources
                ),
                "sources": sources,
                "speak": False,
            }

    @staticmethod
    def _speech_eligible(event, mode):
        state = event.get("state")
        if state in {"completed", "failed", "cancelled", "lost"}:
            return True
        if mode != "completion" and (
            state == "waiting_for_approval"
            or event["kind"] in {"approval_reference", "source_error"}
        ):
            return True
        return mode == "frequent" and (
            event["kind"] == "tool_completed" or state == "running"
        )

    def speech_candidates(self, token):
        with self._db(token) as db:
            preference = db.execute(
                "SELECT update_mode FROM task_preferences WHERE owner=?", (self._owner.key,)
            ).fetchone()
            mode = preference[0] if preference else "important"
            rows = db.execute(
                "SELECT e.*,s.state AS delivery FROM task_events e "
                "LEFT JOIN task_event_speech s ON s.event_idx=e.idx "
                "WHERE e.owner=? ORDER BY e.idx DESC", (self._owner.key,)
            ).fetchall()
            latest, selected, result = {}, set(), []
            for row in rows:
                event = json.loads(row["data"])
                signature = tuple(
                    event.get(key) for key in ("kind", "state", "label", "approval_id")
                )
                source = row["source_id"]
                latest.setdefault(source, signature)
                if (source in selected or signature != latest[source] or not row["live"]
                    or row["connection_id"] != token.connection_id
                    or row["generation"] != token.generation):
                    continue
                selected.add(source)
                if row["delivery"] is None and self._speech_eligible(event, mode):
                    result.append(event)
            return list(reversed(result[:8]))

    def queue_speech(self, token, event_id, *, playback_supported=False, respect_preference=False):
        with self._db(token) as db:
            event = self._event(db, event_id)
            if respect_preference:
                preference = db.execute(
                    "SELECT update_mode FROM task_preferences WHERE owner=?", (self._owner.key,)
                ).fetchone()
                if not self._speech_eligible(
                    json.loads(event["data"]), preference[0] if preference else "important"
                ):
                    raise TaskEventError("replay_not_speakable")
            if not event["live"] or (event["generation"], event["connection_id"]) != (
                token.generation,
                token.connection_id,
            ):
                raise TaskEventError("replay_not_speakable")
            if db.execute(
                "SELECT 1 FROM task_event_speech WHERE event_idx=?", (event["idx"],)
            ).fetchone():
                raise TaskEventError("delivery_exists")
            attempt = uuid.uuid4().hex
            db.execute(
                "INSERT INTO task_event_speech VALUES (?,?,?,?,?,?)",
                (
                    event["idx"],
                    attempt,
                    token.generation,
                    token.connection_id,
                    "queued",
                    int(playback_supported is True),
                ),
            )
            return SpeechAttempt(event_id, attempt, token)

    def acknowledge_speech(self, token, attempt, state):
        if attempt.capture != token or state not in {"sent", "playback_acknowledged", "unknown"}:
            raise TaskEventError("invalid_delivery")
        with self._db(token) as db:
            event = self._event(db, attempt.event_id)
            row = db.execute(
                "SELECT * FROM task_event_speech WHERE event_idx=? AND attempt_id=?",
                (event["idx"], attempt.attempt_id),
            ).fetchone()
            if row is None or (row["generation"], row["connection_id"]) != (
                token.generation,
                token.connection_id,
            ):
                raise TaskEventError("invalid_delivery")
            allowed = {
                "queued": {"sent", "unknown"},
                "sent": {"unknown"},
                "unknown": set(),
                "playback_acknowledged": set(),
            }
            if row["playback_supported"]:
                allowed["sent"].add("playback_acknowledged")
            if state != row["state"] and state not in allowed[row["state"]]:
                raise TaskEventError("invalid_delivery")
            db.execute(
                "UPDATE task_event_speech SET state=? WHERE event_idx=?", (state, event["idx"])
            )

    def _event(self, db, event_id):
        row = db.execute(
            "SELECT * FROM task_events WHERE owner=? AND event_id=?",
            (self._owner.key, identifier(event_id)),
        ).fetchone()
        if row is None:
            raise TaskEventError("missing_reference")
        return row

    def result_view(self, token, run_id, *, resolve_run=None):
        with self._db(token) as db:
            binding, operator_hash = self._binding(db, run_id)
        if resolve_run is None:
            try:
                from . import talk_runs
            except ImportError:  # pragma: no cover - flat plugin load
                import talk_runs
            resolve_run = talk_runs.resolve_run_record
        try:
            run = resolve_run(run_id)
        except Exception:  # noqa: BLE001 - callback failures yield no recovered authority/data
            raise TaskEventError("unavailable") from None
        with self._db(token):
            if not isinstance(run, dict):
                raise TaskEventError("unavailable")
            ticket = run.get("ticket")
            if not isinstance(ticket, dict):
                raise TaskEventError("foreign_owner")
            if (
                ticket.get("hermesSessionId"),
                ticket.get("profile"),
                ticket.get("requestId"),
                digest(ticket.get("operator")),
            ) != (
                self._owner.session_id,
                self._owner.profile,
                binding.request_id,
                operator_hash,
            ) or run.get("runId") != run_id:
                raise TaskEventError("foreign_owner")
            if run.get("status") not in {"running", "done", "failed", "lost"}:
                raise TaskEventError("invalid_event")
            output = run.get("output") if run["status"] != "running" else None
            return {
                "status": run.get("status"),
                "output": output[:4000] if isinstance(output, str) else "",
                "speak": False,
                "source": "current_run_record",
            }

    def approval_view(self, token, event_id, reader: ApprovalReader | None):
        with self._db(token) as db:
            data = json.loads(self._event(db, event_id)["data"])
        reference = data["approval_id"]
        if reader is None or not reference:
            return {"state": "unsupported", "actionable": False}
        if reader.owner != self._owner or reader.session_id != data["session_id"]:
            raise TaskEventError("foreign_owner")
        try:
            current = reader.read_pending()
        except Exception:  # noqa: BLE001 - never restore permission from cached events on failure
            raise TaskEventError("unavailable") from None
        with self._db(token):
            if not isinstance(current, list) or len(current) > 64:
                raise TaskEventError("invalid_event")
            matches = [
                row
                for row in current
                if isinstance(row, dict) and row.get("request_id") == reference
            ]
            if len(matches) > 1:
                raise TaskEventError("invalid_event")
            if not matches:
                return {"state": "gone", "actionable": False}
            choices = matches[0].get("choices")
            if not isinstance(choices, list):
                choices = []
            return {
                "state": "pending_observed",
                "approval_id": reference,
                "choices": [choice for choice in ("once", "session", "deny") if choice in choices],
                "actionable": False,
                "submit_boundary": "existing_host_resolver",
            }

    def delete_owner(self, token):
        """Caller must have confirmed canonical deletion for this exact authorized owner."""
        if token.owner != self._owner:
            raise TaskEventError("foreign_owner")
        self._outbox.invalidate(
            self._owner,
            code="target_missing",
            connection_id=token.connection_id,
            generation=token.generation,
        )

    def diagnostics(self, token):
        with self._db(token) as db:
            return {
                "events": db.execute(
                    "SELECT count(*) FROM task_events WHERE owner=?", (self._owner.key,)
                ).fetchone()[0],
                "sources": db.execute(
                    "SELECT count(*) FROM task_event_sources WHERE owner=?", (self._owner.key,)
                ).fetchone()[0],
                "canonical_history_writes": 0,
                "stream_consumers": 0,
            }
