"""Native ``hermes talk check`` — the end-to-end proof and its refusals."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import types

import pytest

import talk_check
import talk_cli
import talk_doctor
import talk_host
import talk_realtime as rt
import talk_runs
import talk_tools

TOKEN = talk_check.MAGIC_WORD


# -- doubles ---------------------------------------------------------------------


class FakeProviderSession:
    """A scripted provider behind the neutral contract. No socket, no key."""

    def __init__(self, events=(), *, connect_error=None, send_error=None, hang=False):
        self.state = rt.SessionState.NEW
        self.events = list(events)
        self.connect_error = connect_error
        self.send_error = send_error
        self.hang = hang
        self.setup = None
        self.sent: list[tuple] = []
        self.closed = False

    async def connect(self, setup):
        if self.connect_error is not None:
            self.state = rt.SessionState.FAILED
            raise self.connect_error
        self.setup = setup
        self.state = rt.SessionState.CONNECTED

    async def send(self, commands):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(tuple(commands))

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if self.events:
            return self.events.pop(0)
        if self.hang:
            await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def close(self):
        self.closed = True
        if self.state is not rt.SessionState.FAILED:
            self.state = rt.SessionState.CLOSED


def _happy_events():
    return [
        rt.SessionReady(session_id="sess_check"),
        rt.ResponseStarted(response_id="resp_1"),
        rt.OutputAudio(data=b"\x00\x01" * 100, item_id="item_1", response_id="resp_1"),
        rt.Transcript(
            role=rt.TranscriptRole.ASSISTANT,
            text="ready",
            final=True,
            provenance=rt.TranscriptProvenance.OUTPUT_AUDIO,
            response_id="resp_1",
        ),
        rt.ResponseFinished(response_id="resp_1"),
    ]


def _lane(provider="openai", source="codex-oauth"):
    return talk_cli.ProviderLane(
        provider=provider,
        auth=types.SimpleNamespace(token="sk-never-print", source=source),
        model="gpt-realtime-2.1",
        voice="cedar",
    )


def _run_agent_returning(output, *, delay_s=0.0, raise_in_worker=False):
    """A delegation double that starts a REAL registry run, like the host does."""

    def run_agent(prompt):
        assert TOKEN in prompt

        def worker(_run_id):
            if delay_s:
                time.sleep(delay_s)
            if raise_in_worker:
                raise RuntimeError(output)
            return output

        run_id = talk_runs.start_run("agent", "check", worker)
        return f"{talk_runs.started_sentinel(run_id, 'agent', 'check')} — running detached"

    return run_agent


def _healthy_doctor():
    return {
        "schema_version": 1,
        "command": "hermes talk doctor",
        "read_only": True,
        "ok": True,
        "summary": {"pass": 8, "warn": 2, "fail": 0},
        "checks": [
            {
                "id": check_id,
                "status": "warn" if check_id in {"plugin", "host"} else "pass",
                "summary": f"{check_id} looks fine",
                "details": {"resolved_home": "C:\\Users\\someone\\.hermes"},
                "remediation": [],
            }
            for check_id in talk_doctor.CHECK_ORDER
        ],
    }


def _failing_doctor():
    report = _healthy_doctor()
    report["ok"] = False
    report["summary"] = {"pass": 7, "warn": 2, "fail": 1}
    report["checks"][2] = {
        "id": "auth",
        "status": "fail",
        "summary": "no usable Realtime authentication lane was found",
        "details": {},
        "remediation": ["Set TALK_OPENAI_API_KEY or OPENAI_API_KEY, or run `codex login`."],
    }
    return report


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("TALK_PROVIDER", raising=False)
    monkeypatch.delenv("TALK_AGENT_PROFILE", raising=False)
    monkeypatch.setattr(talk_doctor, "collect_report", _healthy_doctor)
    monkeypatch.setattr(talk_check, "RUN_POLL_S", 0.01)
    talk_host.bind_ctx(None)
    talk_tools.REGISTRATION_FAILURES.clear()
    talk_tools.REGISTRATION_RECEIPTS.clear()
    talk_runs.reset_for_tests()
    yield
    talk_runs.reset_for_tests()
    talk_host.bind_ctx(None)


@pytest.fixture
def live(monkeypatch):
    """The suite's EXPLICIT opt-in to running the live steps against doubles."""

    monkeypatch.setattr(talk_check, "_live_enabled", lambda: True)


def _steps(report):
    return {step["id"]: step for step in report["steps"]}


def _run(**overrides):
    kwargs = {
        "lane_resolver": _lane,
        "session_factory": lambda _auth: FakeProviderSession(_happy_events()),
        "run_agent": _run_agent_returning(f"Sure. {TOKEN}"),
        "stop_work": lambda target: f"stopped {target}",
    }
    kwargs.update(overrides)
    return talk_check.run_check(**kwargs)


# -- the green path and its shape -------------------------------------------------


def test_json_shape_is_stable_and_every_step_passes(live):
    report = _run()

    assert set(report) == {
        "schema_version",
        "command",
        "read_only",
        "ok",
        "provider",
        "refused",
        "summary",
        "duration_ms",
        "steps",
    }
    assert report["schema_version"] == 1
    assert report["command"] == "hermes talk check"
    assert report["read_only"] is False
    assert report["ok"] is True
    assert report["provider"] == "openai"
    assert report["refused"] is None
    assert report["summary"] == {"pass": 3, "fail": 0, "skip": 0}
    assert isinstance(report["duration_ms"], int)
    assert [step["id"] for step in report["steps"]] == list(talk_check.STEP_ORDER)
    for step in report["steps"]:
        assert set(step) == {"id", "status", "summary", "details", "remediation", "duration_ms"}
        assert isinstance(step["duration_ms"], int) and step["duration_ms"] >= 0
    json.dumps(report)


def test_provider_step_drives_the_neutral_contract_end_to_end(live):
    session = FakeProviderSession(_happy_events())

    report = _run(session_factory=lambda _auth: session)
    step = _steps(report)["provider_session"]

    assert step["status"] == "pass"
    # One text turn: a system-context item plus an explicit response trigger,
    # sent only AFTER SessionReady arrived.
    assert len(session.sent) == 1
    add, start = session.sent[0]
    assert isinstance(add, rt.AddContext) and add.text == talk_check.CHECK_TURN_TEXT
    assert isinstance(start, rt.StartResponse)
    assert session.setup.tools == ()
    assert session.setup.automatic_response is True
    assert session.setup.text_output is False
    assert session.closed is True
    assert step["details"]["session_ready"] is True
    assert step["details"]["response_finished"] is True
    assert step["details"]["audio_bytes"] == 200
    assert step["details"]["transcript_chars"] == 5
    assert step["details"]["auth_source"] == "codex-oauth"
    assert "sk-never-print" not in json.dumps(report)


def test_hermes_run_step_polls_the_registry_and_finds_the_token(live):
    report = _run()
    step = _steps(report)["hermes_run"]

    assert step["status"] == "pass"
    assert step["details"]["token_found"] is True
    assert step["details"]["status"] == "done"
    assert step["details"]["lane"] == "detached"
    assert isinstance(step["details"]["run_id"], int)
    # The check's ticket is detached again: nothing can dispatch on it later.
    assert talk_runs.current_owner() is None


def test_run_ticket_carries_no_hermes_session_and_the_operator_lane(live):
    seen = {}

    def run_agent(prompt):
        seen["owner"] = talk_runs.current_owner()
        run_id = talk_runs.start_run("agent", "check", lambda _rid: TOKEN)
        return talk_runs.started_sentinel(run_id, "agent", "check")

    _run(run_agent=run_agent)

    assert seen["owner"]["hermesSessionId"] is None
    assert seen["owner"]["operator"] == "codex-oauth"
    assert seen["owner"]["talkSessionId"].startswith("talk-check-")


def test_run_id_regex_matches_the_started_sentinel():
    sentinel = talk_runs.started_sentinel(42, "agent", "check")
    match = talk_check._RUN_ID_RE.search(sentinel)
    assert match is not None and match.group(1) == "42"
    assert talk_cli.WORK_STARTED_RE.pattern == talk_check._RUN_ID_RE.pattern


# -- refusals: a mock can never go green -----------------------------------------


def test_live_steps_refuse_under_the_test_harness_without_the_opt_in():
    touched = []

    report = _run(
        session_factory=lambda _auth: touched.append("session") or FakeProviderSession(),
        run_agent=lambda prompt: touched.append("run") or "",
    )
    steps = _steps(report)

    assert touched == []
    assert report["ok"] is False
    assert report["refused"] == "live steps are switched off under the test harness"
    assert steps["static"]["status"] == "pass"
    assert steps["provider_session"]["status"] == "fail"
    assert steps["hermes_run"]["status"] == "fail"
    assert steps["provider_session"]["summary"].startswith("refused:")


def test_cli_refuses_a_mock_provider_without_touching_the_environment(live, monkeypatch, capsys):
    monkeypatch.delenv("TALK_PROVIDER", raising=False)
    touched = []

    code = talk_check.cli_entry(
        json_output=True,
        provider="mock",
        session_factory=lambda _auth: touched.append("session") or FakeProviderSession(),
        lane_resolver=_lane,
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 1
    assert touched == []
    assert "TALK_PROVIDER" not in os.environ
    assert report["ok"] is False
    assert "'mock' is not a live realtime provider" in report["refused"]
    assert _steps(report)["provider_session"]["status"] == "fail"


def test_cli_parser_refuses_a_mock_provider_outright(capsys):
    parser = argparse.ArgumentParser()
    talk_cli.setup_cli(parser)

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["check", "--provider", "mock"])

    assert excinfo.value.code == 2
    assert "invalid choice: 'mock'" in capsys.readouterr().err


def test_cli_live_provider_override_scopes_talk_provider_to_this_process(live, monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "openai")
    seen = {}

    def resolver():
        seen["provider"] = os.environ["TALK_PROVIDER"]
        return _lane(provider="grok", source="configured")

    code = talk_check.cli_entry(
        json_output=True,
        no_run=True,
        provider="grok",
        session_factory=lambda _auth: FakeProviderSession(_happy_events()),
        lane_resolver=resolver,
    )

    assert code == 0
    assert seen["provider"] == "grok"


def test_a_lane_that_is_not_live_is_refused_even_through_the_seam(live):
    touched = []

    report = _run(
        lane_resolver=lambda: _lane(provider="mock"),
        session_factory=lambda _auth: touched.append("session") or FakeProviderSession(),
    )
    step = _steps(report)["provider_session"]

    assert touched == []
    assert step["status"] == "fail"
    assert "not a live realtime provider" in step["summary"]
    assert report["ok"] is False


def test_an_unsupported_configured_provider_is_refused(live, monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "nope")

    report = _run()

    assert report["ok"] is False
    assert report["provider"] is None
    assert "not a realtime voice provider" in report["refused"]


# -- static gate and skips ----------------------------------------------------------


def test_static_failure_skips_the_live_steps_and_carries_remediation(live, monkeypatch):
    monkeypatch.setattr(talk_doctor, "collect_report", _failing_doctor)
    touched = []

    report = _run(session_factory=lambda _auth: touched.append("s") or FakeProviderSession())
    steps = _steps(report)

    assert touched == []
    assert report["ok"] is False
    assert report["summary"] == {"pass": 0, "fail": 1, "skip": 2}
    assert steps["static"]["summary"] == "1 doctor check(s) failed: auth"
    assert steps["static"]["details"]["failed"] == ["auth"]
    assert steps["static"]["remediation"] == [
        "Set TALK_OPENAI_API_KEY or OPENAI_API_KEY, or run `codex login`."
    ]
    assert steps["provider_session"]["status"] == "skip"
    assert steps["hermes_run"]["status"] == "skip"


def test_static_step_carries_check_summaries_but_never_doctor_details(live):
    step = _steps(_run())["static"]

    assert step["status"] == "pass"
    assert [check["id"] for check in step["details"]["checks"]] == list(talk_doctor.CHECK_ORDER)
    assert all(set(check) == {"id", "status", "summary"} for check in step["details"]["checks"])
    assert "resolved_home" not in json.dumps(step)
    assert "someone" not in json.dumps(step)


def test_no_run_skips_the_hermes_step_and_still_passes(live):
    touched = []

    report = _run(no_run=True, run_agent=lambda prompt: touched.append("run") or "")

    assert touched == []
    assert report["ok"] is True
    assert report["summary"] == {"pass": 2, "fail": 0, "skip": 1}
    assert _steps(report)["hermes_run"]["summary"] == "skipped: --no-run"


def test_exit_code_is_nonzero_when_any_non_skipped_step_fails(live, capsys):
    code = talk_check.cli_entry(
        json_output=True,
        no_run=True,
        session_factory=lambda _auth: FakeProviderSession([rt.SessionReady(session_id="s")]),
        lane_resolver=_lane,
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 1
    assert report["summary"] == {"pass": 1, "fail": 1, "skip": 1}


# -- provider step failure paths ----------------------------------------------------


def test_provider_timeout_is_bounded_and_closes_the_session(live):
    session = FakeProviderSession([rt.SessionReady(session_id="s")], hang=True)
    started = time.monotonic()

    report = _run(session_factory=lambda _auth: session, provider_timeout_s=0.3, no_run=True)
    step = _steps(report)["provider_session"]

    assert time.monotonic() - started < 5.0
    assert step["status"] == "fail"
    assert "timed out after 0s while waiting for response" in step["summary"]
    assert session.closed is True
    assert step["details"]["session_ready"] is True
    assert step["details"]["response_finished"] is False


def test_provider_that_never_becomes_ready_times_out_in_that_phase(live):
    session = FakeProviderSession([], hang=True)

    report = _run(session_factory=lambda _auth: session, provider_timeout_s=0.2, no_run=True)
    step = _steps(report)["provider_session"]

    assert step["status"] == "fail"
    assert "waiting for session ready" in step["summary"]
    assert session.sent == []


def test_provider_failure_event_fails_the_step_with_its_detail(live):
    events = [
        rt.SessionReady(session_id="s"),
        rt.ProviderFailure(detail="insufficient_quota", terminal=True),
    ]

    step = _steps(_run(session_factory=lambda _auth: FakeProviderSession(events)))[
        "provider_session"
    ]

    assert step["status"] == "fail"
    assert "terminal provider failure: insufficient_quota" in step["summary"]


def test_session_closing_before_the_turn_finishes_fails(live):
    events = [
        rt.SessionReady(session_id="s"),
        rt.ResponseStarted(response_id="r"),
        rt.SessionTerminated(state=rt.SessionState.CLOSED),
    ]

    step = _steps(_run(session_factory=lambda _auth: FakeProviderSession(events)))[
        "provider_session"
    ]

    assert step["status"] == "fail"
    assert "the provider ended the session (closed)" in step["summary"]


def test_stream_eof_before_the_turn_finishes_fails(live):
    session = FakeProviderSession([rt.SessionReady(session_id="s")])

    step = _steps(_run(session_factory=lambda _auth: session))["provider_session"]

    assert step["status"] == "fail"
    assert "closed the stream while waiting for response" in step["summary"]


def test_connect_failure_is_reported_without_the_token_or_paths(live):
    error = rt.RealtimeSessionError(
        "OpenAI Realtime auth failed (401): token sk-abcdefghijklmnop rejected; "
        "see C:\\Users\\someone\\.codex\\auth.json and /home/someone/.codex/auth.json"
    )

    report = _run(session_factory=lambda _auth: FakeProviderSession(connect_error=error))
    payload = json.dumps(report)
    step = _steps(report)["provider_session"]

    assert step["status"] == "fail"
    assert "during connect" in step["summary"]
    assert "sk-abcdefghijklmnop" not in payload
    assert "someone" not in payload
    assert "<redacted-secret>" in step["summary"]
    assert "<path>" in step["summary"]


def test_lane_resolution_failure_is_the_finding(live):
    def resolver():
        raise RuntimeError("Realtime voice needs a credential")

    report = _run(lane_resolver=resolver)
    step = _steps(report)["provider_session"]

    assert step["status"] == "fail"
    assert "provider lane could not be resolved: Realtime voice needs a credential" in (
        step["summary"]
    )
    # The run step still runs, on a neutral operator: the two halves are
    # independent findings.
    assert _steps(report)["hermes_run"]["status"] == "pass"


def test_adapter_construction_failure_is_the_finding(live):
    def factory(_auth):
        raise RuntimeError("aiohttp is required for the voice session")

    step = _steps(_run(session_factory=factory))["provider_session"]

    assert step["status"] == "fail"
    assert "provider adapter could not be built: aiohttp is required" in step["summary"]


# -- hermes run failure paths ----------------------------------------------------------


def test_run_timeout_requests_a_stop_and_detaches_the_owner(live):
    release = threading.Event()
    stops = []

    def run_agent(prompt):
        run_id = talk_runs.start_run("agent", "slow", lambda _rid: release.wait(5) and TOKEN)
        return talk_runs.started_sentinel(run_id, "agent", "slow")

    try:
        started = time.monotonic()
        report = _run(
            run_agent=run_agent,
            stop_work=lambda target: stops.append(target) or "stop requested",
            timeout_s=0.3,
        )
    finally:
        release.set()
    step = _steps(report)["hermes_run"]

    assert time.monotonic() - started < 5.0
    assert step["status"] == "fail"
    assert "did not finish within 0s; a stop was requested" in step["summary"]
    assert stops == [str(step["details"]["run_id"])]
    assert step["details"]["stop_receipt"] == "stop requested"
    assert talk_runs.current_owner() is None


def test_run_output_without_the_token_fails(live):
    step = _steps(_run(run_agent=_run_agent_returning("hello there")))["hermes_run"]

    assert step["status"] == "fail"
    assert step["details"]["token_found"] is False
    assert f"did not contain {TOKEN}" in step["summary"]


def test_failed_run_is_reported_without_paths(live):
    run_agent = _run_agent_returning(
        "the agent exited 1: cannot read C:\\Users\\someone\\.hermes\\config.yaml",
        raise_in_worker=True,
    )

    report = _run(run_agent=run_agent)
    step = _steps(report)["hermes_run"]

    assert step["status"] == "fail"
    assert step["details"]["status"] == "failed"
    assert "someone" not in json.dumps(report)
    assert "<path>" in step["summary"]


def test_delegation_refusal_without_a_run_id_fails(live):
    step = _steps(
        _run(run_agent=lambda prompt: "I can't hand off work right now — no `hermes` on the PATH.")
    )["hermes_run"]

    assert step["status"] == "fail"
    assert step["summary"].startswith("delegation did not start a run: I can't hand off work")
    assert step["remediation"]


def test_attached_loop_acceptance_is_not_observable_and_fails_honestly(live):
    step = _steps(_run(run_agent=lambda prompt: "WORK_STARTED — delegated to a child"))[
        "hermes_run"
    ]

    assert step["status"] == "fail"
    assert "not observable from this command" in step["summary"]


def test_delegation_raising_is_the_finding_and_the_owner_is_detached(live):
    def run_agent(prompt):
        raise RuntimeError("boom sk-abcdefghijklmnop")

    step = _steps(_run(run_agent=run_agent))["hermes_run"]

    assert step["status"] == "fail"
    assert step["summary"] == "delegation raised RuntimeError: boom <redacted-secret>"
    assert talk_runs.current_owner() is None


def test_run_vanishing_from_the_registry_fails(live):
    def run_agent(prompt):
        return talk_runs.started_sentinel(99_999, "agent", "ghost")

    step = _steps(_run(run_agent=run_agent))["hermes_run"]

    assert step["status"] == "fail"
    assert "run #99999 vanished from the registry" in step["summary"]


# -- rendering and the CLI wiring -------------------------------------------------------


def test_human_render_names_every_step_and_the_verdict(live):
    text = talk_check.render_human(_run())

    assert text.startswith("Hermes Talk check (live: one provider turn, one bounded Hermes run)")
    assert "[PASS] static: doctor: 8 pass, 2 warn, 0 fail" in text
    assert "[PASS] provider_session: openai gpt-realtime-2.1 answered one text turn" in text
    assert "receipt: auth=codex-oauth, ready=yes, response=finished, audio=200 bytes" in text
    assert f"echoed {TOKEN}" in text
    assert "receipt: run=#" in text and "token=found" in text
    assert "PASS: the whole voice path works right now" in text


def test_human_render_shows_remediation_and_the_fail_verdict(live, monkeypatch):
    monkeypatch.setattr(talk_doctor, "collect_report", _failing_doctor)

    text = talk_check.render_human(_run())

    assert "[FAIL] static: 1 doctor check(s) failed: auth" in text
    assert "  -> Set TALK_OPENAI_API_KEY or OPENAI_API_KEY, or run `codex login`." in text
    assert "[SKIP] provider_session: skipped: static checks failed" in text
    assert "FAIL: see the failing step above" in text


def test_cli_parser_and_dispatch_make_check_a_native_subcommand(monkeypatch):
    parser = argparse.ArgumentParser()
    talk_cli.setup_cli(parser)
    args = parser.parse_args(
        ["check", "--json", "--no-run", "--timeout", "12", "--provider", "grok"]
    )
    seen = []
    monkeypatch.setattr(talk_cli.talk_check, "cli_entry", lambda **kwargs: seen.append(kwargs) or 0)

    assert talk_cli.cli_entry(args) == 0
    assert seen == [
        {
            "json_output": True,
            "no_run": True,
            "timeout_s": 12.0,
            "provider": "grok",
            "session_factory": talk_cli._realtime_session,
            "lane_resolver": talk_cli.resolve_provider_lane,
        }
    ]


def test_cli_dispatch_raises_system_exit_on_a_failed_check(monkeypatch):
    parser = argparse.ArgumentParser()
    talk_cli.setup_cli(parser)
    monkeypatch.setattr(talk_cli.talk_check, "cli_entry", lambda **kwargs: 1)

    with pytest.raises(SystemExit) as excinfo:
        talk_cli.cli_entry(parser.parse_args(["check"]))

    assert excinfo.value.code == 1


def test_cli_refuses_a_non_positive_timeout(live, capsys):
    code = talk_check.cli_entry(json_output=True, timeout_s=0, lane_resolver=_lane)
    report = json.loads(capsys.readouterr().out)

    assert code == 1
    assert report["refused"] == "--timeout must be a positive number of seconds"


def test_native_cli_json_path_emits_the_check_envelope_and_no_secret(live, monkeypatch, capsys):
    monkeypatch.setattr(
        talk_cli, "_realtime_session", lambda _auth: FakeProviderSession(_happy_events())
    )
    monkeypatch.setattr(talk_cli, "resolve_provider_lane", _lane)
    monkeypatch.setattr(talk_host.host(), "run_agent", _run_agent_returning(TOKEN))
    parser = argparse.ArgumentParser()
    talk_cli.setup_cli(parser)

    assert talk_cli.cli_entry(parser.parse_args(["check", "--json"])) == 0
    payload = capsys.readouterr().out

    report = json.loads(payload)
    assert report["command"] == "hermes talk check"
    assert report["ok"] is True
    assert "sk-never-print" not in payload


def test_resolve_provider_lane_uses_the_host_credential_for_openai(monkeypatch):
    host = types.SimpleNamespace(
        resolve_auth=lambda: types.SimpleNamespace(token="token", source="codex-oauth")
    )
    monkeypatch.setattr(talk_cli.talk_host, "host", lambda: host)
    monkeypatch.setenv("TALK_MODEL", "gpt-realtime-2.1")
    monkeypatch.setenv("TALK_VOICE", "cedar")

    lane = talk_cli.resolve_provider_lane()

    assert lane == talk_cli.ProviderLane(
        provider="openai", auth=lane.auth, model="gpt-realtime-2.1", voice="cedar"
    )
    assert lane.auth.source == "codex-oauth"


def test_resolve_provider_lane_routes_grok_and_gemini_to_their_own_auth(monkeypatch):
    grok = types.SimpleNamespace(token="g", source="configured")
    gemini = types.SimpleNamespace(token="k", source="env")
    monkeypatch.setattr(talk_cli, "_grok_auth", lambda: grok)
    monkeypatch.setattr(talk_cli, "_gemini_auth", lambda: gemini)

    monkeypatch.setenv("TALK_PROVIDER", "grok")
    assert talk_cli.resolve_provider_lane().auth is grok
    monkeypatch.setenv("TALK_PROVIDER", "gemini")
    assert talk_cli.resolve_provider_lane().auth is gemini


def test_scrub_text_covers_secrets_and_path_shapes_but_not_urls():
    scrubbed = talk_check.scrub_text(
        "wss://api.openai.com/v1/realtime failed for sk-abcdefghijklmnop at "
        "C:\\Users\\someone\\x and /home/someone/y and ~/.codex/auth.json"
    )

    assert scrubbed == (
        "wss://api.openai.com/v1/realtime failed for <redacted-secret> at "
        "<path> and <path> and <path>"
    )
