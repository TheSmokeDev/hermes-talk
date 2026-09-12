"""Private, bounded storage of unacknowledged native transcript fragments."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

try:
    from . import talk_config
    from .talk_native_api import NativeTaskError
except ImportError:
    import talk_config
    from talk_native_api import NativeTaskError


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class NativeCaptureStore:
    def __init__(self, path, *, max_fragments=4096, max_bytes=1024 * 1024):
        self.path = Path(path)
        self.max_fragments, self.max_bytes = max_fragments, max_bytes
        if (
            not self.path.is_absolute()
            or not 1 <= max_fragments <= 4096
            or not 1 <= max_bytes <= 1024 * 1024
        ):
            raise NativeTaskError("Invalid local transcript storage configuration")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.touch(exist_ok=True, mode=0o600)
            self.path.chmod(0o600)
            with self._db(write=True) as db:
                db.execute("""CREATE TABLE IF NOT EXISTS captures (
                    owner TEXT NOT NULL, provider_session TEXT NOT NULL, event_id TEXT NOT NULL,
                    source_context TEXT NOT NULL, fragment TEXT NOT NULL, bytes INTEGER NOT NULL,
                    PRIMARY KEY(owner,provider_session,event_id))""")
        except OSError:
            raise NativeTaskError(
                "Local transcript storage is unavailable", category="storage"
            ) from None

    @classmethod
    def configured(cls):
        return cls(talk_config.get_hermes_home() / "state" / "talk-native-capture.sqlite3")

    @staticmethod
    def owner(origin, attachment):
        task = attachment.get("task") or {}
        values = {key: task.get(key) for key in ("target_id", "session_id", "profile", "peer_id")}
        values["origin"] = origin
        if any(
            not isinstance(value, str) or not value or len(value) > 2048
            for value in values.values()
        ):
            raise NativeTaskError("Transcript recovery requires an exact authorized task owner")
        surface = attachment.get("surface_context") or {}
        values["surface"] = {
            key: surface[key]
            for key in (
                "surface",
                "operator_user_id",
                "guild_id",
                "channel_id",
                "surface_profile",
                "anchor_session_id",
            )
            if key in surface
        }
        if any(
            not isinstance(value, str) or len(value) > 256 for value in values["surface"].values()
        ):
            raise NativeTaskError("Invalid transcript recovery surface")
        return encode(values)

    @contextmanager
    def _db(self, *, write=False):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=0.25)
            db.row_factory = sqlite3.Row
            if write:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except (sqlite3.Error, OSError):
            raise NativeTaskError(
                "Local transcript storage is unavailable", category="storage"
            ) from None
        finally:
            if db is not None:
                db.close()

    def put(self, owner, context, provider_session, fragments):
        if (
            set(context) != {"connection_id", "generation"}
            or not isinstance(context["connection_id"], str)
            or type(context["generation"]) is not int
        ):
            raise NativeTaskError("Invalid transcript source binding")
        if not isinstance(provider_session, str) or not 1 <= len(provider_session) <= 256:
            raise NativeTaskError("Invalid transcript provider identity")
        allowed = {
            "event_id",
            "role",
            "text",
            "final",
            "synthetic",
            "start_ms",
            "end_ms",
            "item_id",
            "finality",
        }
        with self._db(write=True) as db:
            for fragment in fragments:
                if set(fragment) - allowed or not isinstance(fragment.get("event_id"), str):
                    raise NativeTaskError("Invalid local transcript fragment")
                encoded = encode(fragment)
                prior = db.execute(
                    "SELECT fragment FROM captures WHERE owner=? "
                    "AND provider_session=? AND event_id=?",
                    (owner, provider_session, fragment["event_id"]),
                ).fetchone()
                if prior and prior["fragment"] != encoded:
                    raise NativeTaskError(
                        "Local transcript event identity changed", category="validation"
                    )
                db.execute(
                    "INSERT OR IGNORE INTO captures VALUES (?,?,?,?,?,?)",
                    (
                        owner,
                        provider_session,
                        fragment["event_id"],
                        encode(context),
                        encoded,
                        len((owner + encode(context) + provider_session + encoded).encode()),
                    ),
                )
            count, size = db.execute(
                "SELECT count(*),coalesce(sum(bytes),0) FROM captures"
            ).fetchone()
            if count > self.max_fragments or size > self.max_bytes:
                raise NativeTaskError(
                    "Local transcript queue is full; pending captures were retained",
                    category="capacity",
                )

    def pending(self, owner):
        with self._db() as db:
            rows = db.execute(
                "SELECT provider_session,fragment FROM captures WHERE owner=? "
                "ORDER BY rowid LIMIT 32",
                (owner,),
            ).fetchall()
        batch, size, session = [], 0, None
        for row in rows:
            fragment = json.loads(row["fragment"])
            amount = len(row["fragment"].encode())
            if batch and (session != row["provider_session"] or size + amount > 8192):
                break
            session = row["provider_session"]
            batch.append(fragment)
            size += amount
        return session, batch

    def acknowledge(self, owner, provider_session, fragments):
        with self._db(write=True) as db:
            for fragment in fragments:
                db.execute(
                    "DELETE FROM captures WHERE owner=? AND provider_session=? "
                    "AND event_id=? AND fragment=?",
                    (owner, provider_session, fragment["event_id"], encode(fragment)),
                )

    @staticmethod
    def receipt(owner):
        return "capture_" + hashlib.sha256(owner.encode()).hexdigest()[:24]
