from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import fixture_data
import pytest

import talk_transcript


def _long_turn(seed: str) -> str:
    return f"{seed} " + ("durable context " * 12)


def test_capture_writes_one_closed_jsonl_row_per_completed_turn(tmp_path):
    capture = talk_transcript.TranscriptCapture(tmp_path)

    capture.append_turn("user", "ship on Friday")
    renamed = capture.path.with_suffix(".moved")
    os.rename(capture.path, renamed)
    os.rename(renamed, capture.path)
    capture.append_turn("assistant", "I will remember that")

    rows = [json.loads(line) for line in capture.path.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {"role": "user", "text": "ship on Friday"},
        {"role": "assistant", "text": "I will remember that"},
    ]


def test_capture_paths_are_unique_and_contained_even_for_hostile_session_ids(tmp_path):
    first = talk_transcript.TranscriptCapture(tmp_path, session_id="../../outside")
    second = talk_transcript.TranscriptCapture(tmp_path, session_id="../../outside")
    first.append_turn("user", "one")
    second.append_turn("user", "two")

    root = (tmp_path / "state" / "talk-transcripts").resolve()
    assert first.path != second.path
    assert first.path.resolve().parent == root
    assert second.path.resolve().parent == root
    assert "outside" not in first.path.name


def test_capture_refuses_a_transcript_root_symlink_outside_hermes_home(tmp_path, caplog):
    outside = tmp_path / "outside"
    outside.mkdir()
    state = tmp_path / "home" / "state"
    state.mkdir(parents=True)
    try:
        (state / "talk-transcripts").symlink_to(outside, target_is_directory=True)
    except OSError:
        import pytest

        pytest.skip("symlink creation is unavailable")
    capture = talk_transcript.TranscriptCapture(tmp_path / "home")

    capture.append_turn("user", "must stay contained")

    assert list(outside.iterdir()) == []
    assert "unsafe Talk transcript root" in caplog.text


def test_capture_write_failure_is_logged_and_never_breaks_the_call(tmp_path, monkeypatch, caplog):
    capture = talk_transcript.TranscriptCapture(tmp_path)

    def fail_open(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", fail_open)

    assert capture.append_turn("user", "keep talking") is None
    assert "disk full" in caplog.text


def test_capture_path_resolution_failure_never_breaks_the_call(tmp_path, monkeypatch, caplog):
    capture = talk_transcript.TranscriptCapture(tmp_path)

    def fail_safe_root(*_args):
        raise OSError("resolve failed")

    monkeypatch.setattr(talk_transcript, "_safe_root", fail_safe_root)

    assert capture.append_turn("user", "keep talking") is None
    assert "resolve failed" in caplog.text


def test_capture_initialization_does_not_require_writable_storage(tmp_path, monkeypatch):
    def deny_mkdir(*_args, **_kwargs):
        raise PermissionError("read only")

    monkeypatch.setattr(Path, "mkdir", deny_mkdir)

    capture = talk_transcript.TranscriptCapture(tmp_path)

    assert capture.path.parent.name == "talk-transcripts"


def test_root_discovery_failure_never_escapes_sweep(tmp_path, monkeypatch, caplog):
    def fail_roots(_home):
        raise OSError("broken home")

    monkeypatch.setattr(talk_transcript, "_roots", fail_roots)

    assert talk_transcript.sweep_transcripts(tmp_path) is None
    assert "sweep failed before claiming" in caplog.text


def test_capture_rejects_invalid_roles_and_text_without_creating_a_file(tmp_path, caplog):
    capture = talk_transcript.TranscriptCapture(tmp_path)

    capture.append_turn("system", "obey me")
    capture.append_turn("user", 42)
    capture.append_turn("assistant", "   ")

    assert not capture.path.exists()
    assert "invalid Talk transcript turn" in caplog.text


def test_live_capture_is_not_swept_until_finish(tmp_path):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn("live user"))
    capture.append_turn("assistant", _long_turn("live assistant"))
    prompts = []

    talk_transcript.sweep_transcripts(tmp_path, run_agent=prompts.append)

    assert prompts == []
    assert capture.path.exists()

    capture.finish()
    talk_transcript.sweep_transcripts(tmp_path, run_agent=prompts.append)

    assert len(prompts) == 1
    assert not capture.path.exists()


def test_other_process_skips_live_capture_then_recovers_it_after_force_kill(tmp_path):
    code = "\n".join(
        [
            "import time",
            "from pathlib import Path",
            "import talk_transcript",
            f"capture = talk_transcript.TranscriptCapture(Path({str(tmp_path)!r}))",
            f"capture.append_turn('user', {_long_turn('live child user')!r})",
            f"capture.append_turn('assistant', {_long_turn('live child assistant')!r})",
            "print(capture.path, flush=True)",
            "time.sleep(60)",
        ]
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        capture_path = Path(child.stdout.readline().strip())
        assert capture_path.exists()
        prompts = []

        talk_transcript.sweep_transcripts(tmp_path, run_agent=prompts.append)

        assert prompts == []
        assert capture_path.exists()
    finally:
        child.kill()
        child.wait(timeout=5)

    talk_transcript.sweep_transcripts(tmp_path, run_agent=prompts.append)

    assert len(prompts) == 1
    assert not capture_path.exists()


def test_sweep_claims_and_flushes_a_qualifying_orphan(tmp_path):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn("deployment detail"))
    capture.append_turn("assistant", _long_turn("acknowledged detail"))
    capture.finish()
    prompts = []

    talk_transcript.sweep_transcripts(tmp_path, run_agent=prompts.append)

    assert len(prompts) == 1
    assert "memory" in prompts[0].lower()
    assert "deployment detail" in prompts[0]
    assert list((tmp_path / "state" / "talk-transcripts").iterdir()) == []


def test_successful_handoff_status_retains_only_the_child_session_identity(tmp_path):
    talk_transcript.reset_status_for_tests()
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn("deployment detail"))
    capture.append_turn("assistant", _long_turn("acknowledged detail"))
    capture.finish()

    talk_transcript.sweep_transcripts(
        tmp_path,
        run_agent=lambda _prompt: "WORK_STARTED — session 20260809_132351_722cfd accepted",
    )

    assert talk_transcript.handoff_status() == {
        "state": "handoff pending",
        "child_session_id": "20260809_132351_722cfd",
    }


def test_next_start_recovers_a_claim_left_by_a_killed_sweeper(tmp_path):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn("orphan user"))
    capture.append_turn("assistant", _long_turn("orphan assistant"))
    capture.finish()
    orphaned_claim = capture.path.with_name(capture.path.name + ".claimed-deadgateway")
    os.rename(capture.path, orphaned_claim)
    prompts = []

    talk_transcript.sweep_transcripts(tmp_path, run_agent=prompts.append)

    assert len(prompts) == 1
    assert not orphaned_claim.exists()


def test_sweep_drops_a_symlink_that_escapes_the_transcript_directory(tmp_path):
    root = tmp_path / "state" / "talk-transcripts"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"role":"user","text":"secret"}\n', encoding="utf-8")
    link = root / "escape.jsonl"
    try:
        link.symlink_to(outside)
    except OSError:
        import pytest

        pytest.skip("symlink creation is unavailable")

    talk_transcript.sweep_transcripts(tmp_path, run_agent=lambda _prompt: None)

    assert outside.exists()
    assert not link.exists()


def test_path_swap_between_validation_and_claim_never_reads_outside_root(
    tmp_path, monkeypatch
):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn("safe user"))
    capture.append_turn("assistant", _long_turn("safe assistant"))
    capture.finish()
    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "text": _long_turn("OUTSIDE SECRET user")}),
                json.dumps(
                    {"role": "assistant", "text": _long_turn("OUTSIDE SECRET assistant")}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    original_rename = os.rename
    swapped = False

    def swap_then_rename(source, destination):
        nonlocal swapped
        if Path(source) == capture.path and not swapped:
            swapped = True
            capture.path.unlink()
            try:
                capture.path.symlink_to(outside)
            except OSError:
                import pytest

                pytest.skip("symlink creation is unavailable")
        return original_rename(source, destination)

    monkeypatch.setattr(os, "rename", swap_then_rename)
    prompts = []

    talk_transcript.sweep_transcripts(tmp_path, run_agent=prompts.append)

    assert swapped
    assert all("OUTSIDE SECRET" not in prompt for prompt in prompts)
    assert outside.exists()


def test_noise_below_turn_or_character_gate_is_dropped(tmp_path):
    one_turn = talk_transcript.TranscriptCapture(tmp_path)
    one_turn.append_turn("user", "x" * 300)
    one_turn.finish()
    too_short = talk_transcript.TranscriptCapture(tmp_path)
    too_short.append_turn("user", "hello")
    too_short.append_turn("assistant", "hi")
    too_short.finish()
    prompts = []

    talk_transcript.sweep_transcripts(tmp_path, run_agent=prompts.append)

    assert prompts == []
    assert list((tmp_path / "state" / "talk-transcripts").iterdir()) == []


def test_malformed_and_truncated_rows_are_ignored_without_losing_valid_turns(tmp_path):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn("valid user"))
    capture.append_turn("assistant", _long_turn("valid assistant"))
    with capture.path.open("a", encoding="utf-8") as stream:
        stream.write("not json\n")
        stream.write('{"role":"user","text":"truncated')
    capture.finish()
    prompts = []

    talk_transcript.sweep_transcripts(tmp_path, run_agent=prompts.append)

    assert len(prompts) == 1
    assert "valid user" in prompts[0]
    assert "truncated" not in prompts[0]


def test_hostile_unhashable_role_row_is_ignored(tmp_path):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn("valid user"))
    with capture.path.open("a", encoding="utf-8") as stream:
        stream.write('{"role":[],"text":"hostile"}\n')
    capture.append_turn("assistant", _long_turn("valid assistant"))
    capture.finish()
    prompts = []

    talk_transcript.sweep_transcripts(tmp_path, run_agent=prompts.append)

    assert len(prompts) == 1
    assert "hostile" not in prompts[0]


def test_hostile_transcript_delimiters_remain_json_quoted_untrusted_data(tmp_path):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    hostile = _long_turn(fixture_data.payload("adversarial/injection-transcript-delimiter.fixture"))
    capture.append_turn("user", hostile)
    capture.append_turn("assistant", _long_turn("safe assistant"))
    capture.finish()
    prompts = []

    talk_transcript.sweep_transcripts(tmp_path, run_agent=prompts.append)

    assert len(prompts) == 1
    prompt = prompts[0]
    assert "UNTRUSTED quoted JSON data" in prompt
    assert "<talk_transcript>" not in prompt
    assert "</talk_transcript>" not in prompt
    quoted = json.dumps(hostile.strip(), ensure_ascii=True)
    quoted = quoted.replace("<", "\\u003c").replace(">", "\\u003e")
    assert quoted in prompt


def test_handoff_refusal_is_logged_and_dropped(tmp_path, caplog):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn("refused user"))
    capture.append_turn("assistant", _long_turn("refused assistant"))
    capture.finish()

    talk_transcript.sweep_transcripts(
        tmp_path,
        run_agent=lambda _prompt: "I can't hand off work right now",
    )

    assert "memory handoff was refused" in caplog.text
    assert not capture.path.exists()


def test_default_handoff_never_blocks_sweep_startup(tmp_path, monkeypatch):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn("slow user"))
    capture.append_turn("assistant", _long_turn("slow assistant"))
    capture.finish()
    entered = threading.Event()
    release = threading.Event()

    def blocked(_prompt):
        entered.set()
        release.wait(timeout=2)
        return "WORK_STARTED — accepted"

    monkeypatch.setattr(talk_transcript, "_default_run_agent", blocked)
    started = time.monotonic()

    talk_transcript.sweep_transcripts(tmp_path)

    assert time.monotonic() - started < 0.5
    assert entered.wait(timeout=1)
    release.set()


def test_detached_handoff_lease_blocks_another_process_until_done(tmp_path, monkeypatch):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn("detached user"))
    capture.append_turn("assistant", _long_turn("detached assistant"))
    capture.finish()
    entered = threading.Event()
    release = threading.Event()

    def blocked(_prompt):
        entered.set()
        release.wait(timeout=5)
        return "WORK_STARTED — accepted"

    monkeypatch.setattr(talk_transcript, "_default_run_agent", blocked)
    talk_transcript.sweep_transcripts(tmp_path)
    assert entered.wait(timeout=1)
    code = (
        "from pathlib import Path; import talk_transcript; "
        f"talk_transcript.sweep_transcripts(Path({str(tmp_path)!r}), "
        "lambda _prompt: print('OTHER_FLUSHED'))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout == ""
    release.set()
    root = tmp_path / "state" / "talk-transcripts"
    deadline = time.monotonic() + 2
    while any(root.glob("*.claimed-*")) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not list(root.glob("*.claimed-*"))


def test_force_killed_sweeper_claim_is_recovered_by_next_process(tmp_path):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn("crashed sweep user"))
    capture.append_turn("assistant", _long_turn("crashed sweep assistant"))
    capture.finish()
    code = "\n".join(
        [
            "import time",
            "from pathlib import Path",
            "import talk_transcript",
            "def block(_prompt):",
            "    print('CLAIMED', flush=True)",
            "    time.sleep(60)",
            f"talk_transcript.sweep_transcripts(Path({str(tmp_path)!r}), block)",
        ]
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "CLAIMED"
    finally:
        child.kill()
        child.wait(timeout=5)
    prompts = []

    talk_transcript.sweep_transcripts(tmp_path, run_agent=prompts.append)

    assert len(prompts) == 1


def test_failed_claimed_flush_is_logged_dropped_and_does_not_block_next_file(
    tmp_path, caplog
):
    for seed in ("first", "second"):
        capture = talk_transcript.TranscriptCapture(tmp_path)
        capture.append_turn("user", _long_turn(seed + " user"))
        capture.append_turn("assistant", _long_turn(seed + " assistant"))
        capture.finish()
    calls = []

    def flaky(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("host unavailable")

    talk_transcript.sweep_transcripts(tmp_path, run_agent=flaky)

    assert len(calls) == 2
    assert "host unavailable" in caplog.text
    assert list((tmp_path / "state" / "talk-transcripts").iterdir()) == []


def test_two_sweepers_racing_flush_a_file_exactly_once(tmp_path, monkeypatch):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn("race user"))
    capture.append_turn("assistant", _long_turn("race assistant"))
    capture.finish()
    barrier = threading.Barrier(2)
    prompts = []
    prompt_lock = threading.Lock()

    def flush(prompt):
        with prompt_lock:
            prompts.append(prompt)

    def sweep_together():
        barrier.wait(timeout=2)
        talk_transcript.sweep_transcripts(tmp_path, flush)

    threads = [threading.Thread(target=sweep_together) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert len(prompts) == 1


def test_child_process_does_not_reclaim_parent_process_live_claim(tmp_path):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn("parent user"))
    capture.append_turn("assistant", _long_turn("parent assistant"))
    capture.finish()
    child_outputs = []

    def flush_while_child_sweeps(_prompt):
        code = (
            "from pathlib import Path; import talk_transcript; "
            f"talk_transcript.sweep_transcripts(Path({str(tmp_path)!r}), "
            "lambda _prompt: print('CHILD_FLUSHED'))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            check=True,
        )
        child_outputs.append(result.stdout)

    talk_transcript.sweep_transcripts(tmp_path, run_agent=flush_while_child_sweeps)

    assert child_outputs == [""]


def test_dead_claim_is_recovered_when_pid_now_belongs_to_unrelated_live_process(tmp_path):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        root = tmp_path / "state" / "talk-transcripts"
        root.mkdir(parents=True)
        claimed = root / f"orphan.jsonl.claimed-{sleeper.pid}-reused"
        claimed.write_text(
            "\n".join(
                [
                    json.dumps({"role": "user", "text": _long_turn("reused pid user")}),
                    json.dumps(
                        {"role": "assistant", "text": _long_turn("reused pid assistant")}
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        prompts = []

        talk_transcript.sweep_transcripts(tmp_path, run_agent=prompts.append)

        assert len(prompts) == 1
        assert not claimed.exists()
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)


# -- the capability-bridge flush lane: no bound owner required --------------------
#
# 2026-08-28 regression: a Discord-lane session ended, the sweep's handoff
# routed through the ticketed run lane, and talk_runs refused — "no Talk
# connection is bound, so there's nowhere to deliver the result" — because the
# session's teardown had already detached the owner. The transcript was
# dropped unread. The flush is maintenance, not user-visible work: it needs no
# return route.


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _finished_capture(tmp_path, seed: str):
    capture = talk_transcript.TranscriptCapture(tmp_path)
    capture.append_turn("user", _long_turn(seed + " user"))
    capture.append_turn("assistant", _long_turn(seed + " assistant"))
    capture.finish()
    return capture


@pytest.fixture
def flush_lanes(monkeypatch, tmp_path):
    """No host attached, no spawnable hermes, no api server — until the test
    turns one on. flush_agent's tiers are driven at the module seam, and the
    real Hermes home is walled off (profile detection must never read it)."""

    import talk_apiserver
    import talk_host
    import talk_runs

    talk_host.bind_ctx(None)
    talk_runs.detach_owner()
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: None)
    monkeypatch.setattr(talk_apiserver, "is_available", lambda: False)
    yield
    talk_host.bind_ctx(None)
    talk_runs.detach_owner()


def test_session_end_sweep_flushes_without_a_bound_owner(tmp_path, monkeypatch, flush_lanes):
    """The Discord-lane regression: teardown detached the owner BEFORE the
    sweep, and the handoff refused. The flush lane needs no ticket — the
    transcript is reviewed and consumed, not dropped."""

    import talk_apiserver

    _finished_capture(tmp_path, "discord lane")
    reviewed = []
    done = threading.Event()

    def fake_run_to_completion(prompt, *, session_id, session_key):
        reviewed.append(prompt)
        done.set()
        return "reviewed"

    monkeypatch.setattr(talk_apiserver, "is_available", lambda: True)
    monkeypatch.setattr(talk_apiserver, "run_to_completion", fake_run_to_completion)

    talk_transcript.reset_status_for_tests()
    talk_transcript.sweep_transcripts(tmp_path)  # the default handoff

    assert done.wait(3.0), "the flush never reached the api-server lane"
    assert reviewed and "memory" in reviewed[0].lower()
    assert "discord lane user" in reviewed[0]
    root = tmp_path / "state" / "talk-transcripts"
    # The claim's rename makes the ORIGINAL path vanish at claim time, so
    # watching it proves nothing; the claimed copy's deletion is the handoff
    # thread's last step — poll the directory clean instead of racing it.
    assert _wait_for(
        lambda: not list(root.glob("*.jsonl")) and not list(root.glob("*.claimed-*"))
    )
    assert talk_transcript.handoff_status()["state"] == "handoff pending"


def test_a_flush_with_no_lane_defers_and_the_next_sweep_retries(
    tmp_path, monkeypatch, flush_lanes
):
    """No host, no api server, no hermes on PATH: the transcript is RESTORED
    under its original name for the next sweep, never dropped."""

    import talk_apiserver

    capture = _finished_capture(tmp_path, "deferred")

    talk_transcript.reset_status_for_tests()
    talk_transcript.sweep_transcripts(tmp_path)

    assert _wait_for(
        lambda: talk_transcript.handoff_status()["state"] == "handoff deferred"
    )
    root = tmp_path / "state" / "talk-transcripts"
    # The status flips before the claim's finally restores the file — wait for
    # the restore itself, not the bookkeeping, before asserting or re-sweeping.
    assert _wait_for(
        lambda: capture.path.exists() and not list(root.glob("*.lease"))
    )
    assert not list(root.glob("*.claimed-*"))

    reviewed = []
    done = threading.Event()

    def fake_run_to_completion(prompt, *, session_id, session_key):
        reviewed.append(prompt)
        done.set()
        return "reviewed"

    monkeypatch.setattr(talk_apiserver, "is_available", lambda: True)
    monkeypatch.setattr(talk_apiserver, "run_to_completion", fake_run_to_completion)

    talk_transcript.sweep_transcripts(tmp_path)

    assert done.wait(3.0), "the deferred transcript was never retried"
    assert reviewed and "deferred user" in reviewed[0]
    assert _wait_for(lambda: not capture.path.exists())


def test_the_attached_host_tier_still_carries_the_flush(tmp_path, monkeypatch, flush_lanes):
    """Tier 1 unchanged: a live parent agent takes the handoff inline."""

    import talk_host

    _finished_capture(tmp_path, "attached")
    calls = []

    class Ctx:
        def dispatch_tool(self, name, args):
            calls.append((name, args))
            return json.dumps({"result": "delegated: session 20260828_185300_ab12cd accepted"})

    talk_host.bind_ctx(Ctx())

    talk_transcript.reset_status_for_tests()
    talk_transcript.sweep_transcripts(tmp_path)

    assert _wait_for(lambda: bool(calls))
    assert calls and calls[0][0] == "delegate_task"
    assert _wait_for(
        lambda: "child_session_id" in talk_transcript.handoff_status()
    )
    root = tmp_path / "state" / "talk-transcripts"
    assert _wait_for(lambda: not list(root.glob("*.claimed-*")))


def test_a_host_refusal_still_drops_the_transcript(tmp_path, flush_lanes):
    """Deferral is for ABSENT lanes only. A real lane that answers with a
    refusal made a decision — the transcript is dropped, exactly as before.
    (The wording is pinned by test_handoff_refusal_is_logged_and_dropped on
    the synchronous path; this asserts the outcome, not the log race.)"""

    import talk_host

    capture = _finished_capture(tmp_path, "refused")

    class Ctx:
        def dispatch_tool(self, name, args):
            return json.dumps({"error": "delegation is paused", "success": False})

    talk_host.bind_ctx(Ctx())

    talk_transcript.reset_status_for_tests()
    talk_transcript.sweep_transcripts(tmp_path)

    assert _wait_for(lambda: not capture.path.exists())
    assert _wait_for(
        lambda: talk_transcript.handoff_status()["state"] == "handoff failed"
    )


def test_the_detached_spawn_tier_flushes_when_nothing_else_is_up(
    tmp_path, monkeypatch, flush_lanes
):
    """No host, no api server, but a hermes binary: the flush spawns detached."""

    import talk_host

    _finished_capture(tmp_path, "detached flush")
    spawned = threading.Event()
    seen = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            seen.append(argv)
            spawned.set()

        def communicate(self, timeout=None):
            return ("reviewed", "")

    monkeypatch.setattr(talk_host, "hermes_binary", lambda: "/usr/local/bin/hermes")
    monkeypatch.setattr(talk_host.subprocess, "Popen", FakePopen)

    talk_transcript.sweep_transcripts(tmp_path)

    assert spawned.wait(3.0), "the detached flush never spawned"
    assert seen and seen[0][0] == "/usr/local/bin/hermes"
    assert "-z" in seen[0] and "memory" in seen[0][-1].lower()
    root = tmp_path / "state" / "talk-transcripts"
    assert _wait_for(lambda: not list(root.glob("*.claimed-*")))
