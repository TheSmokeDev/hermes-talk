"""The outbox under a busy writer: readers must never see "storage broken"."""

from __future__ import annotations

import sqlite3
import threading
import time

from talk_outbox import HistoryOutbox


def test_outbox_uses_write_ahead_logging(tmp_path):
    HistoryOutbox(tmp_path, profile="default")
    with sqlite3.connect(tmp_path / "state" / "talk-history-outbox.sqlite3") as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_readers_survive_a_writer_committing_in_a_tight_loop(tmp_path):
    """A liveness probe or lease renewal committing every few milliseconds must not
    starve a concurrent snapshot read into ``outbox_unavailable``.

    On a fast disk this also passes in rollback-journal mode, so the WAL assertion
    above is the load-bearing one; this case guards the behavior the mode exists for.
    """

    outbox = HistoryOutbox(tmp_path, profile="default")
    stop = threading.Event()
    writer_error = []

    def writer():
        try:
            while not stop.is_set():
                with outbox._db() as db:
                    db.execute("UPDATE metadata SET next_generation = next_generation + 1")
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            writer_error.append(exc)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    deadline = time.monotonic() + 1.0
    reads = 0
    try:
        while time.monotonic() < deadline:
            with outbox._db(write=False) as db:
                assert db.execute("SELECT next_generation FROM metadata").fetchone() is not None
            reads += 1
    finally:
        stop.set()
        thread.join(5)
    assert not writer_error
    assert reads > 0
