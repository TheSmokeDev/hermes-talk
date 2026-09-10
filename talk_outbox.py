"""Bounded, profile-resolved derived history queue. Never opens a host database."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    from .talk_passive import HistoryError, HistoryMessage, HistoryOwner, dialogue_messages, digest
except ImportError:  # pragma: no cover - flat Hermes plugin load
    from talk_passive import HistoryError, HistoryMessage, HistoryOwner, dialogue_messages, digest


@dataclass(frozen=True, slots=True)
class PendingHistory:
    event_id: str
    origin_turn_id: str
    owner: HistoryOwner
    conversation_id: str
    connection_id: str
    generation: int
    messages: tuple[HistoryMessage, ...] = field(repr=False)
    state: str
    attempts: int
    code: str


class HistoryOutbox:
    """Pass an absolute profile home resolved by trusted host code, never model text.

    There is intentionally no root-home fallback or profile auto-detection. SQLite
    writer transactions serialize capacity admission and generation changes across
    processes. No transaction spans a network request. Content is scrubbed on save,
    failure, expiration and retirement; this is not a second conversation archive.
    """

    def __init__(
        self,
        profile_home: Path,
        *,
        profile: str,
        max_events: int = 128,
        max_bytes: int = 1024 * 1024,
        ttl_s: float = 86400,
        clock: Callable[[], float] = time.time,
    ):
        if (
            not profile_home.is_absolute()
            or not profile
            or type(max_events) is not int
            or not 1 <= max_events <= 128
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= 1024 * 1024
            or not math.isfinite(ttl_s)
            or not 0 < ttl_s <= 86400
        ):
            raise HistoryError("invalid_input")
        self._profile = profile
        self._max_events, self._max_bytes, self._ttl_s = max_events, max_bytes, ttl_s
        self._clock = clock
        self._path = profile_home / "state" / "talk-history-outbox.sqlite3"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._path.touch(mode=0o600, exist_ok=True)
            self._path.chmod(0o600)
        except OSError:
            raise HistoryError("outbox_unavailable") from None
        with self._db(prune=False) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS metadata "
                "(profile TEXT NOT NULL, next_generation INTEGER NOT NULL)"
            )
            row = db.execute("SELECT profile FROM metadata").fetchone()
            if row is None:
                db.execute("INSERT INTO metadata VALUES (?,0)", (profile,))
            elif row[0] != profile:
                raise HistoryError("owner_mismatch")
            db.execute("""CREATE TABLE IF NOT EXISTS connections (
                scope TEXT PRIMARY KEY, owner TEXT NOT NULL, generation INTEGER NOT NULL,
                touched REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, origin_turn_id TEXT NOT NULL, owner TEXT NOT NULL,
                owner_json TEXT NOT NULL, conversation_id TEXT NOT NULL,
                connection_id TEXT NOT NULL, generation INTEGER NOT NULL,
                messages TEXT NOT NULL, payload_hash TEXT NOT NULL, bytes INTEGER NOT NULL,
                created REAL NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                code TEXT NOT NULL DEFAULT '')""")

    @contextmanager
    def _db(self, *, prune: bool = True) -> Iterator[sqlite3.Connection]:
        db = None
        try:
            db = sqlite3.connect(self._path, timeout=2)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            if prune:
                # Retention cleanup must survive a subsequent admission/read refusal.
                self._prune(db)
                db.commit()
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except (sqlite3.Error, OSError):
            raise HistoryError("outbox_unavailable") from None
        finally:
            if db is not None:
                db.close()

    def _prune(self, db):
        cutoff = self._clock() - self._ttl_s
        # Preserve a content-free failed state for one TTL so expiration is observable.
        db.execute("DELETE FROM events WHERE created < ?", (cutoff - self._ttl_s,))
        db.execute(
            """UPDATE events SET state='failed',code='expired',messages='',bytes=0,
            owner='',owner_json='',conversation_id='',origin_turn_id='',connection_id=''
            WHERE created < ?""",
            (cutoff,),
        )
        db.execute("DELETE FROM connections WHERE touched < ?", (cutoff,))

    def _scope(self, owner: HistoryOwner, connection_id: str) -> str:
        if owner.profile != self._profile:
            raise HistoryError("owner_mismatch")
        return digest([owner.host, owner.profile, owner.principal, connection_id])

    def _check(self, db, owner: HistoryOwner, connection_id: str, generation: int):
        row = db.execute(
            "SELECT owner,generation FROM connections WHERE scope=?",
            (self._scope(owner, connection_id),),
        ).fetchone()
        if row is None or row["owner"] != owner.key or row["generation"] != generation:
            raise HistoryError("stale_generation")

    def begin(
        self, owner: HistoryOwner, connection_id: str, *, expected_generation: int | None = None
    ) -> int:
        scope = self._scope(owner, connection_id)
        with self._db() as db:
            if expected_generation is not None:
                self._check(db, owner, connection_id, expected_generation)
            row = db.execute(
                "SELECT generation FROM connections WHERE scope=?", (scope,)
            ).fetchone()
            if row is None and db.execute("SELECT count(*) FROM connections").fetchone()[0] >= 128:
                raise HistoryError("outbox_full")
            generation = db.execute(
                "UPDATE metadata SET next_generation=next_generation+1 RETURNING next_generation"
            ).fetchone()[0]
            db.execute(
                "INSERT OR REPLACE INTO connections VALUES (?,?,?,?)",
                (scope, owner.key, generation, self._clock()),
            )
            return generation

    def check(self, owner: HistoryOwner, connection_id: str, generation: int):
        with self._db() as db:
            self._check(db, owner, connection_id, generation)

    def end(self, owner: HistoryOwner, connection_id: str, generation: int):
        with self._db() as db:
            self._check(db, owner, connection_id, generation)
            db.execute(
                "UPDATE connections SET owner='',generation=generation+1 WHERE scope=?",
                (self._scope(owner, connection_id),),
            )

    def add(self, event: PendingHistory):
        encoded = json.dumps([row.wire() for row in event.messages], ensure_ascii=False)
        size = len(encoded.encode("utf-8"))
        fingerprint = digest([event.owner.key, event.origin_turn_id, encoded])
        with self._db() as db:
            self._check(db, event.owner, event.connection_id, event.generation)
            prior = db.execute(
                "SELECT payload_hash FROM events WHERE event_id=?", (event.event_id,)
            ).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise HistoryError("event_conflict")
                return
            count, used = db.execute(
                "SELECT count(*),coalesce(sum(bytes),0) FROM events"
            ).fetchone()
            if count >= self._max_events:
                # Only terminal, content-free entries may give way to fresh dialogue.
                oldest = db.execute(
                    "SELECT event_id FROM events WHERE state!='pending' ORDER BY created LIMIT 1"
                ).fetchone()
                if oldest:
                    db.execute("DELETE FROM events WHERE event_id=?", (oldest[0],))
                    count -= 1
            if count >= self._max_events or used + size > self._max_bytes:
                raise HistoryError("outbox_full")
            db.execute(
                """INSERT INTO events
                (event_id,origin_turn_id,owner,owner_json,conversation_id,connection_id,
                 generation,messages,payload_hash,bytes,created,state)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.event_id,
                    event.origin_turn_id,
                    event.owner.key,
                    json.dumps(asdict(event.owner)),
                    event.conversation_id,
                    event.connection_id,
                    event.generation,
                    encoded,
                    fingerprint,
                    size,
                    self._clock(),
                    "pending",
                ),
            )

    def get(self, owner: HistoryOwner, event_id: str) -> PendingHistory:
        with self._db() as db:
            row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise HistoryError("unknown_event")
            if row["code"] in {"expired", "retired", "target_missing", "target_unavailable"}:
                raise HistoryError(row["code"])
            if row["owner"] != owner.key or owner.profile != self._profile:
                raise HistoryError("owner_mismatch")
            try:
                rows = json.loads(row["messages"]) if row["messages"] else []
                stored_owner = HistoryOwner(**json.loads(row["owner_json"]))
                if stored_owner != owner:
                    raise HistoryError("owner_mismatch")
                messages = tuple(HistoryMessage(**message) for message in rows)
                if row["state"] == "pending":
                    dialogue_messages(messages)
                    fingerprint = digest([owner.key, row["origin_turn_id"], row["messages"]])
                    if fingerprint != row["payload_hash"]:
                        raise HistoryError("outbox_unavailable")
                return PendingHistory(
                    row["event_id"],
                    row["origin_turn_id"],
                    stored_owner,
                    row["conversation_id"],
                    row["connection_id"],
                    row["generation"],
                    messages,
                    row["state"],
                    row["attempts"],
                    row["code"],
                )
            except (ValueError, TypeError, KeyError):
                raise HistoryError("outbox_unavailable") from None

    def mark(
        self,
        owner: HistoryOwner,
        event_id: str,
        *,
        connection_id: str,
        generation: int,
        state: str = "pending",
        code: str = "",
        attempted: bool = False,
    ):
        if state not in {"pending", "saved", "failed", "conflicted"}:
            raise HistoryError("invalid_input")
        safe_code = HistoryError(code).code if code else ""
        with self._db() as db:
            self._check(db, owner, connection_id, generation)
            result = db.execute(
                """UPDATE events SET state=?,code=?,attempts=attempts+?,
                messages=CASE WHEN ?='pending' THEN messages ELSE '' END,
                bytes=CASE WHEN ?='pending' THEN bytes ELSE 0 END
                WHERE event_id=? AND owner=? AND state='pending'""",
                (state, safe_code, int(attempted), state, state, event_id, owner.key),
            )
            if result.rowcount != 1:
                prior = db.execute(
                    "SELECT state FROM events WHERE event_id=? AND owner=?", (event_id, owner.key)
                ).fetchone()
                if prior is None:
                    raise HistoryError("unknown_event")
                return prior[0]
            return state

    def invalidate(
        self,
        owner: HistoryOwner,
        *,
        code: str,
        connection_id: str,
        generation: int,
        event_id: str | None = None,
    ):
        if code not in {"retired", "target_missing", "target_unavailable"}:
            raise HistoryError("invalid_input")
        if event_id is None and code != "target_missing":
            raise HistoryError("invalid_input")
        with self._db() as db:
            self._check(db, owner, connection_id, generation)
            db.execute(
                """UPDATE events SET state='failed',code=?,messages='',bytes=0,
                owner='',owner_json='',conversation_id='',origin_turn_id='',connection_id=''
                WHERE owner=?"""
                + (" AND event_id=?" if event_id is not None else ""),
                (code, owner.key, event_id) if event_id is not None else (code, owner.key),
            )
            if event_id is None:
                db.execute(
                    "UPDATE connections SET owner='',generation=generation+1 WHERE owner=?",
                    (owner.key,),
                )

    def pending(self, owner: HistoryOwner) -> tuple[str, ...]:
        if owner.profile != self._profile:
            raise HistoryError("owner_mismatch")
        with self._db() as db:
            return tuple(
                row[0]
                for row in db.execute(
                    "SELECT event_id FROM events WHERE owner=? AND state='pending' "
                    "ORDER BY created,rowid",
                    (owner.key,),
                )
            )

    def diagnostics(self) -> dict:
        """Counts and enumerated states only; no text, paths, credentials or owner IDs."""
        with self._db() as db:
            counts = {state: 0 for state in ("pending", "saved", "failed", "conflicted")}
            for state, count in db.execute("SELECT state,count(*) FROM events GROUP BY state"):
                if state in counts:
                    counts[state] = count
            return {
                "states": counts,
                "pending_bytes": db.execute("SELECT coalesce(sum(bytes),0) FROM events").fetchone()[
                    0
                ],
            }
