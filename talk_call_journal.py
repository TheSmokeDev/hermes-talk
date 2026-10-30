"""Opt-in, text-only, local journal of one unbound Talk call (``TALK_CALL_JOURNAL``).

A provider-owned Realtime call lives in the browser: the operator's words and
the voice's answers reach Hermes only as the briefs the voice chooses to
delegate, and the captions die with the tab. When the operator turns this on,
the page reports each FINAL utterance, interruption and tool exchange as it
happens, and this module appends one JSON line per event to
``$HERMES_HOME/state/talk-calls/<call_id>.jsonl``.

Boundaries, on purpose:
- Text and event metadata only. Never audio, never a credential: the mint
  creates the file, so a browser cannot name a path, and every string is
  bounded and passed through the same credential redaction the doctor uses.
- Lines already written survive a crash or a closed tab; nothing is buffered
  server-side.
- Bound task calls are NOT journaled here: they already persist canonical
  history through the task-origin contract.
- Retention is bounded (``TALK_CALL_JOURNAL_DAYS``, default 30) and enforced
  when the next call starts.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

try:
    from . import talk_config
    from .talk_doctor import redact_text
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_config
    from talk_doctor import redact_text

SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 3650
MAX_TEXT_CHARS = 8000
MAX_TOKEN_CHARS = 128
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_EVENTS_PER_BATCH = 64

#: What a page may report. ``call_start`` is written by the mint itself and is
#: refused from the browser.
CLIENT_KINDS = frozenset(
    {
        "user",
        "assistant",
        "interrupted",
        "response_done",
        "tool_call",
        "tool_result",
        "error",
        "call_end",
    }
)
_CALL_ID_RE = re.compile(r"^call_[0-9a-f]{32}$")
_TOKEN_RE = re.compile(rf"^[A-Za-z0-9_.:\-]{{1,{MAX_TOKEN_CHARS}}}$")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})
_WRITE_LOCK = threading.Lock()


class JournalError(Exception):
    """A refused journal write. ``status`` is the HTTP status a route answers."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def enabled() -> bool:
    """``TALK_CALL_JOURNAL``: off unless explicitly on. Fail-closed on junk."""

    raw = (os.environ.get("TALK_CALL_JOURNAL") or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise talk_config.TalkConfigError(
        f"TALK_CALL_JOURNAL '{raw}' is not a switch: use 1/true/on or 0/false/off"
    )


def retention_days() -> int:
    """``TALK_CALL_JOURNAL_DAYS``: whole days a journal is kept (1-3650)."""

    raw = (os.environ.get("TALK_CALL_JOURNAL_DAYS") or "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_RETENTION_DAYS:
        raise talk_config.TalkConfigError(
            f"TALK_CALL_JOURNAL_DAYS must be a whole number of days from 1 to {MAX_RETENTION_DAYS}"
        )
    return int(raw)


def journal_dir() -> Path:
    path = talk_config.state_dir() / "talk-calls"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # filesystems without POSIX modes
        path.chmod(0o700)
    return path


def _now_iso(clock=time.time) -> str:
    return datetime.fromtimestamp(clock(), UTC).isoformat(timespec="milliseconds")


def _text(value) -> str | None:
    if value is None:
        return None
    text = redact_text(str(value))
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + " [truncated]"
    return text


def _token(value) -> str | None:
    if value is None:
        return None
    value = str(value)
    return value if _TOKEN_RE.match(value) else None


def _path(call_id: str) -> Path:
    if not isinstance(call_id, str) or not _CALL_ID_RE.match(call_id):
        raise JournalError("unknown call journal", 404)
    return journal_dir() / f"{call_id}.jsonl"


def _append(path: Path, records: list[dict]) -> int:
    lines = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    ).encode("utf-8")
    with _WRITE_LOCK:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            raise JournalError("unknown call journal", 404) from None
        if size + len(lines) > MAX_FILE_BYTES:
            raise JournalError("call journal is full", 413)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.write(fd, lines)
        finally:
            os.close(fd)
    return len(records)


def prune(*, days: int | None = None, clock=time.time) -> int:
    """Delete journals older than the retention window. Returns the count."""

    cutoff = clock() - (days or retention_days()) * 86400
    removed = 0
    for path in journal_dir().glob("call_*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def start(
    *, engine: dict, lane: str, host_session_id: str | None = None, clock=time.time
) -> str:
    """Create one call's journal and write its ``call_start`` line.

    ``host_session_id`` is the saved Desktop conversation hosting the call,
    when the page knows it: the one verifiable link from a call to the
    conversation it was started from. It is an opaque, bounded token.
    """

    prune(clock=clock)
    call_id = "call_" + secrets.token_hex(16)
    path = journal_dir() / f"{call_id}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    _append(
        path,
        [
            {
                "v": SCHEMA_VERSION,
                "seq": 0,
                "ts": _now_iso(clock),
                "kind": "call_start",
                "lane": _token(lane),
                "host_session_id": _token(host_session_id),
                "engine": {key: _token(value) for key, value in sorted(engine.items())},
            }
        ],
    )
    return call_id


def record(call_id: str, events, *, clock=time.time) -> int:
    """Append a batch of page-reported events. Unknown fields are dropped."""

    path = _path(call_id)
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS_PER_BATCH:
        raise JournalError(f"send 1 to {MAX_EVENTS_PER_BATCH} events")
    rows = []
    for event in events:
        if not isinstance(event, dict) or event.get("kind") not in CLIENT_KINDS:
            raise JournalError("invalid journal event")
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or not 1 <= seq <= 10**7:
            raise JournalError("invalid journal sequence")
        row = {"v": SCHEMA_VERSION, "seq": seq, "ts": _now_iso(clock), "kind": event["kind"]}
        client_ts = event.get("client_ts")
        if isinstance(client_ts, (int, float)) and not isinstance(client_ts, bool):
            row["client_ts"] = int(client_ts)
        for key in ("tool", "tool_call_id", "item_id", "status", "session_id", "source"):
            value = _token(event.get(key))
            if value is not None:
                row[key] = value
        text = _text(event.get("text"))
        if text:
            row["text"] = text
        rows.append(row)
    return _append(path, rows)


def read(call_id: str) -> list[dict]:
    path = _path(call_id)
    if not path.is_file():
        raise JournalError("unknown call journal", 404)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"kind": "unreadable"})
    rows.sort(key=lambda row: (row.get("seq", 0), row.get("ts", "")))
    return rows


def list_calls(limit: int = 20) -> list[dict]:
    """Newest calls first: id, start time, engine, event and session counts."""

    paths = sorted(
        journal_dir().glob("call_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    calls = []
    for path in paths[:limit]:
        rows = read(path.stem)
        start_row = next((row for row in rows if row.get("kind") == "call_start"), {})
        calls.append(
            {
                "call_id": path.stem,
                "started": start_row.get("ts"),
                "host_session_id": start_row.get("host_session_id"),
                "engine": start_row.get("engine", {}),
                "events": len(rows),
                "sessions": sorted({row["session_id"] for row in rows if row.get("session_id")}),
                "ended": any(row.get("kind") == "call_end" for row in rows),
            }
        )
    return calls


_LABELS = {
    "user": "You",
    "assistant": "Voice",
    "interrupted": "Interrupted",
    "response_done": "Response ended",
    "tool_call": "Tool call",
    "tool_result": "Tool result",
    "error": "Error",
    "call_end": "Call ended",
}


def render_markdown(call_id: str) -> str:
    """A readable transcript of one call, in event order."""

    rows = read(call_id)
    start_row = next((row for row in rows if row.get("kind") == "call_start"), {})
    engine = ", ".join(f"{key}={value}" for key, value in start_row.get("engine", {}).items())
    lines = [
        f"# Talk call {call_id}",
        "",
        f"- Started: {start_row.get('ts', 'unknown')}",
        f"- Lane: {start_row.get('lane') or 'unknown'}",
        f"- Host session: {start_row.get('host_session_id') or 'unknown'}",
        f"- Engine: {engine or 'unknown'}",
        "- Content: recognized and generated text, not audio; partial captions are excluded.",
        "",
    ]
    if not any(row.get("kind") == "call_end" for row in rows):
        lines += ["> No `call_end` event: the call is still open, or the page closed abruptly.", ""]
    for row in rows:
        kind = row.get("kind")
        if kind == "call_start":
            continue
        stamp = (row.get("ts") or "")[11:19]
        label = _LABELS.get(str(kind), str(kind or "event"))
        extra = []
        for key in ("tool", "status", "session_id"):
            if row.get(key):
                extra.append(f"{key}: `{row[key]}`")
        head = f"**{stamp} · {label}**" + (" — " + ", ".join(extra) if extra else "")
        lines.append(head)
        if row.get("text"):
            lines.append("")
            lines.extend("> " + part if part else ">" for part in row["text"].splitlines())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "CLIENT_KINDS",
    "SCHEMA_VERSION",
    "JournalError",
    "enabled",
    "journal_dir",
    "list_calls",
    "prune",
    "read",
    "record",
    "render_markdown",
    "retention_days",
    "start",
]
