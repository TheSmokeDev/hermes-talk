"""Opt-in call journal: unbound Talk calls become an auditable text record.

The page's live captions are the only place a provider-owned Realtime call
exists; this suite pins what the journal records, what it refuses, and that
turning it off leaves the mint exactly as before.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

import talk_call_journal as journal
import talk_config


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("TALK_CALL_JOURNAL", raising=False)
    monkeypatch.delenv("TALK_CALL_JOURNAL_DAYS", raising=False)


def _start():
    return journal.start(engine={"provider": "openai", "model": "gpt-realtime-2.1"}, lane="desktop")


def _rows(call_id):
    return journal.read(call_id)


@pytest.mark.parametrize("value", ["1", "true", "ON", " yes "])
def test_the_switch_turns_on_only_when_explicit(monkeypatch, value):
    monkeypatch.setenv("TALK_CALL_JOURNAL", value)
    assert journal.enabled() is True


def test_the_switch_is_off_by_default_and_refuses_junk(monkeypatch):
    assert journal.enabled() is False
    monkeypatch.setenv("TALK_CALL_JOURNAL", "maybe")
    with pytest.raises(talk_config.TalkConfigError, match="TALK_CALL_JOURNAL"):
        journal.enabled()


@pytest.mark.parametrize("value", ["0", "-3", "3651", "thirty"])
def test_retention_refuses_out_of_range_values(monkeypatch, value):
    monkeypatch.setenv("TALK_CALL_JOURNAL_DAYS", value)
    with pytest.raises(talk_config.TalkConfigError, match="TALK_CALL_JOURNAL_DAYS"):
        journal.retention_days()


def test_start_records_the_host_session_and_drops_a_malformed_one():
    linked = journal.start(engine={}, lane="desktop", host_session_id="20261030_090000_abc123")
    assert journal.read(linked)[0]["host_session_id"] == "20261030_090000_abc123"
    assert "Host session: 20261030_090000_abc123" in journal.render_markdown(linked)
    bogus = journal.start(engine={}, lane="desktop", host_session_id="../../x y")
    assert journal.read(bogus)[0]["host_session_id"] is None


def test_start_writes_an_owner_only_file_with_the_engine():
    call_id = _start()
    path = journal.journal_dir() / f"{call_id}.jsonl"
    assert call_id.startswith("call_") and len(call_id) == 37
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(journal.journal_dir().stat().st_mode) == 0o700
    (row,) = _rows(call_id)
    assert row["kind"] == "call_start" and row["seq"] == 0 and row["lane"] == "desktop"
    assert row["engine"] == {"model": "gpt-realtime-2.1", "provider": "openai"}


def test_events_are_appended_bounded_redacted_and_ordered():
    call_id = _start()
    written = journal.record(
        call_id,
        [
            {"kind": "assistant", "seq": 2, "text": "Bonjour, je t'écoute."},
            {"kind": "user", "seq": 1, "text": "Tu m'entends ?", "source": "voice"},
            {
                "kind": "tool_result",
                "seq": 3,
                "tool": "desktop_create_session",
                "session_id": "20261030_101010_abcdef",
                "text": "token sk-" + "a" * 24 + " " + "x" * 9000,
                "path": "/etc/passwd",
            },
        ],
    )
    assert written == 3
    rows = _rows(call_id)
    assert [row["kind"] for row in rows] == ["call_start", "user", "assistant", "tool_result"]
    tool = rows[-1]
    assert tool["session_id"] == "20261030_101010_abcdef"
    assert "sk-aaaa" not in tool["text"] and "<redacted-secret>" in tool["text"]
    assert tool["text"].endswith("[truncated]")
    assert "path" not in tool


@pytest.mark.parametrize(
    "events",
    [
        [],
        [{"kind": "call_start", "seq": 1}],
        [{"kind": "user", "seq": 0, "text": "x"}],
        [{"kind": "user", "seq": True, "text": "x"}],
        [{"kind": "audio", "seq": 1}],
        "not a list",
    ],
)
def test_malformed_batches_are_refused(events):
    call_id = _start()
    with pytest.raises(journal.JournalError):
        journal.record(call_id, events)
    assert len(_rows(call_id)) == 1


@pytest.mark.parametrize(
    "call_id", ["../../etc/passwd", "call_" + "0" * 31, None, "call_" + "g" * 32]
)
def test_a_page_cannot_name_a_path_or_create_a_journal(call_id):
    with pytest.raises(journal.JournalError) as raised:
        journal.record(call_id, [{"kind": "user", "seq": 1, "text": "x"}])
    assert raised.value.status == 404


def test_an_unminted_but_well_formed_id_is_refused():
    with pytest.raises(journal.JournalError) as raised:
        journal.record("call_" + "0" * 32, [{"kind": "user", "seq": 1, "text": "x"}])
    assert raised.value.status == 404
    assert not list(journal.journal_dir().glob("*.jsonl"))


def test_a_full_journal_refuses_instead_of_growing(monkeypatch):
    call_id = _start()
    monkeypatch.setattr(journal, "MAX_FILE_BYTES", 400)
    with pytest.raises(journal.JournalError) as raised:
        journal.record(call_id, [{"kind": "user", "seq": 1, "text": "y" * 500}])
    assert raised.value.status == 413


def test_retention_prunes_old_journals_when_the_next_call_starts(monkeypatch):
    old = _start()
    old_path = journal.journal_dir() / f"{old}.jsonl"
    stale = old_path.stat().st_mtime - 31 * 86400
    os.utime(old_path, (stale, stale))
    fresh = _start()
    assert not old_path.exists()
    assert (journal.journal_dir() / f"{fresh}.jsonl").exists()


def test_markdown_shows_turns_tools_sessions_and_an_open_call():
    call_id = _start()
    journal.record(
        call_id,
        [
            {"kind": "user", "seq": 1, "text": "Ouvre une session de test."},
            {"kind": "interrupted", "seq": 2, "status": "barge_in"},
            {
                "kind": "tool_result",
                "seq": 3,
                "tool": "desktop_create_session",
                "session_id": "20261030_101010_abcdef",
                "text": '{"ok": true}',
            },
        ],
    )
    md = journal.render_markdown(call_id)
    assert md.startswith(f"# Talk call {call_id}")
    assert "> Ouvre une session de test." in md
    assert "Interrupted** — status: `barge_in`" in md
    assert "session_id: `20261030_101010_abcdef`" in md
    assert "No `call_end` event" in md
    journal.record(call_id, [{"kind": "call_end", "seq": 4}])
    assert "No `call_end` event" not in journal.render_markdown(call_id)


def test_listing_reports_sessions_and_completion():
    call_id = _start()
    journal.record(
        call_id,
        [
            {"kind": "tool_result", "seq": 1, "session_id": "20261030_101010_abcdef"},
            {"kind": "call_end", "seq": 2},
        ],
    )
    (listed,) = journal.list_calls()
    assert listed["call_id"] == call_id
    assert listed["sessions"] == ["20261030_101010_abcdef"]
    assert listed["ended"] is True and listed["events"] == 3
    json.dumps(listed)


def test_cli_lists_calls_and_prints_one_as_markdown(capsys):
    import talk_cli

    assert talk_cli._journal_entry(None, False) == 0
    assert "No call journal yet. TALK_CALL_JOURNAL is off" in capsys.readouterr().out
    call_id = _start()
    journal.record(call_id, [{"kind": "user", "seq": 1, "text": "Bonjour"}])
    assert talk_cli._journal_entry(None, False) == 0
    listing = capsys.readouterr().out
    assert call_id in listing and "(no call_end)" in listing
    assert talk_cli._journal_entry(call_id, False) == 0
    assert "> Bonjour" in capsys.readouterr().out
    assert talk_cli._journal_entry(call_id, True) == 0
    assert json.loads(capsys.readouterr().out)[-1]["text"] == "Bonjour"
    assert talk_cli._journal_entry("../x", False) == 1
