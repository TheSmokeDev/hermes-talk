"""``hermes talk check`` — prove the whole voice path end to end, right now.

Doctor (:mod:`talk_doctor`) is read-only by design: it inspects config,
models, devices, and host capabilities and never leaves the machine. That
receipt can be all green while the mint, the socket, or the delegation lane
is broken. This command is the other half. Three steps, in order:

1. **static** — the doctor checks, verbatim. A failing doctor check skips the
   live steps: there is no point spending a session on a known-broken config.
2. **provider_session** — a REAL session on the configured provider through
   the same adapter, credential resolution, and neutral contract the voice
   session uses (:mod:`talk_realtime`): connect, wait for ``SessionReady``,
   send one text turn (``AddContext`` + ``StartResponse``), wait for
   ``ResponseFinished``.
3. **hermes_run** — ONE bounded Hermes run through the existing delegation
   path (``HostAdapter.run_agent`` -> :mod:`talk_runs`) whose output must
   contain :data:`MAGIC_WORD`. The run rides a check-owned ticket with no
   Hermes session id, so no later voice session can adopt or speak it.

Every live step has a hard wall-clock bound; a run that outlives its budget
is stopped through the same lifecycle verb the voice uses. The report never
carries tokens or paths: strings pass through doctor's secret redaction and
a path scrub before they are emitted.

**A mock cannot go green.** The report's ``provider`` is validated against
:data:`LIVE_PROVIDERS` (the fail-closed list :mod:`talk_config` already
enforces), ``--provider mock`` is refused by the parser and again here, and
the live steps refuse under the test harness (``PYTEST_CURRENT_TEST``) unless
a test opts in by name — the same idiom :mod:`talk_apiserver` uses for its
lane. The provider and delegation seams exist for that opt-in; production
always injects the real ones.

Not read-only: the check spends one short provider turn and one short agent
run, and the run is recorded in the run history like any other.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Any

try:
    from . import talk_config, talk_doctor, talk_host, talk_realtime, talk_runs
except ImportError:  # pragma: no cover - flat-module fallback (pip -e install)
    import talk_config
    import talk_doctor
    import talk_host
    import talk_realtime
    import talk_runs

SCHEMA_VERSION = 1
COMMAND = "hermes talk check"
STEP_ORDER = ("static", "provider_session", "hermes_run")
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

#: The only provider names a green report may carry. Same fail-closed list
#: the config layer enforces; restated here so the report's own gate cannot
#: drift from it.
LIVE_PROVIDERS = talk_config.TALK_PROVIDERS

#: What the Hermes run must echo. A fixed literal, not a nonce: the point is
#: that a REAL agent read the prompt and answered, and a stub that cannot
#: reach one has nothing to copy it from.
MAGIC_WORD = "HERMES_TALK_CHECK_OK"

PROVIDER_STEP_TIMEOUT_S = 60.0
RUN_STEP_TIMEOUT_S = 180.0
RUN_POLL_S = 0.5
CLOSE_TIMEOUT_S = 5.0

CHECK_INSTRUCTIONS = (
    "You are the hermes-talk self-check. This is a connectivity test, not a "
    "conversation. Answer with one short spoken word and nothing else."
)
CHECK_TURN_TEXT = "Self-check turn: say the word 'ready' and nothing else."
RUN_PROMPT = (
    "This is an automated hermes-talk connectivity check. Do not use any tools. "
    f"Reply with exactly this token and nothing else: {MAGIC_WORD}"
)

#: Mirror of ``talk_runs.started_sentinel``'s format — a WIRE contract
#: between a tool's return text and whoever watches the run, same as the
#: session watcher's own copy in :mod:`talk_cli`. ``test_run_id_regex_matches_
#: the_started_sentinel`` is the tripwire.
_RUN_ID_RE = re.compile(r"WORK_STARTED #(\d+) kind=(\w+)")

#: Absolute-path shapes that must never reach the report: Windows drive
#: paths, the usual POSIX roots, and ``~/``. Applied after the secret
#: redaction to every free-text string the report carries.
_PATH_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]+"
    r"|(?<![\w/])/(?:home|Users|root|tmp|var|opt|etc|usr|mnt|private)/[^\s\"'<>|]+"
    r"|(?<![\w/])~/[^\s\"'<>|]+)"
)
_MAX_DETAIL_CHARS = 300

Seam = Callable[..., Any]


def _live_enabled() -> bool:
    """Inert under pytest unless a test explicitly opts in.

    Same guard, and the same reason, as ``talk_apiserver._lane_enabled``: a
    green report must mean a REAL provider answered and a REAL agent ran,
    and a suite that reaches this module transitively must never be able to
    mint one. Tests that mean to exercise the orchestration monkeypatch this
    to ``lambda: True`` and inject their doubles through the seams.
    """

    return "PYTEST_CURRENT_TEST" not in os.environ


def scrub_text(value: str) -> str:
    """Secret redaction plus a path scrub, for any free text bound for the report."""

    return _PATH_RE.sub("<path>", talk_doctor.redact_text(value))


def _bounded(value: str) -> str:
    """Scrub FIRST, then collapse and cap — a cut can never expose a token tail."""

    text = " ".join(scrub_text(str(value)).split())
    if len(text) > _MAX_DETAIL_CHARS:
        text = text[: _MAX_DETAIL_CHARS - 3] + "..."
    return text


def _step(
    step_id: str,
    status: str,
    summary: str,
    details: dict[str, Any],
    *,
    duration_ms: float,
    remediation: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """One stable, JSON-safe step envelope — doctor's shape plus a duration."""

    return {
        "id": step_id,
        "status": status,
        "summary": scrub_text(summary),
        "details": _scrub_value(talk_doctor.redact_value(details)),
        "remediation": [scrub_text(action) for action in remediation],
        "duration_ms": round(duration_ms),
    }


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return _PATH_RE.sub("<path>", value)
    if isinstance(value, dict):
        return {key: _scrub_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item) for item in value)
    return value


def _skipped(step_id: str, reason: str) -> dict[str, Any]:
    return _step(step_id, STATUS_SKIP, f"skipped: {reason}", {"reason": reason}, duration_ms=0)


def _refused(step_id: str, reason: str) -> dict[str, Any]:
    return _step(step_id, STATUS_FAIL, f"refused: {reason}", {"refused": reason}, duration_ms=0)


def _elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000.0


# -- step 1: static ------------------------------------------------------------


def run_static_step() -> dict[str, Any]:
    """The doctor report, folded to one step: ids, statuses, and summaries only.

    Details are deliberately dropped — the identity check's details name the
    Hermes home, and this report carries no paths. Run ``hermes talk doctor``
    for the full receipt.
    """

    started = time.monotonic()
    report = talk_doctor.collect_report()
    checks = [
        {"id": check["id"], "status": check["status"], "summary": check["summary"]}
        for check in report["checks"]
    ]
    failed = [check["id"] for check in report["checks"] if check["status"] == "fail"]
    remediation = [
        action
        for check in report["checks"]
        if check["status"] == "fail"
        for action in check["remediation"]
    ]
    totals = report["summary"]
    details = {"summary": dict(totals), "failed": failed, "checks": checks}
    if failed:
        return _step(
            "static",
            STATUS_FAIL,
            f"{len(failed)} doctor check(s) failed: {', '.join(failed)}",
            details,
            duration_ms=_elapsed_ms(started),
            remediation=remediation,
        )
    return _step(
        "static",
        STATUS_PASS,
        f"doctor: {totals['pass']} pass, {totals['warn']} warn, 0 fail",
        details,
        duration_ms=_elapsed_ms(started),
    )


# -- step 2: provider session ---------------------------------------------------


def _event_failure(event: talk_realtime.RealtimeEvent) -> str | None:
    """The reason this event ends the turn unsuccessfully, or ``None``."""

    if isinstance(event, talk_realtime.ProviderFailure):
        kind = "terminal provider failure" if event.terminal else "provider failure"
        return f"{kind}: {event.detail or 'no detail'}"
    if isinstance(event, talk_realtime.SessionTerminated):
        detail = f": {event.detail}" if event.detail else ""
        return f"the provider ended the session ({event.state.value}){detail}"
    return None


async def run_provider_turn(
    session: talk_realtime.RealtimeSession,
    setup: talk_realtime.SessionSetup,
    *,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Connect, wait for ``SessionReady``, send one text turn, wait for the finish.

    One wall-clock budget covers the whole turn — connect included — so a
    provider that accepts the socket and then says nothing cannot hold this
    command open. The session is always closed, on a bounded wait of its
    own. The returned facts are counts and flags only: no transcript text,
    no audio, no identifiers beyond what the report already names.
    """

    if timeout_s is None:
        timeout_s = PROVIDER_STEP_TIMEOUT_S
    deadline = time.monotonic() + timeout_s
    facts: dict[str, Any] = {
        "session_ready": False,
        "response_started": False,
        "response_finished": False,
        "events": 0,
        "audio_bytes": 0,
        "transcript_chars": 0,
        "error": None,
    }

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    phase = "connect"
    try:
        await asyncio.wait_for(session.connect(setup), remaining())
        iterator = session.__aiter__()
        phase = "session ready"
        while not facts["session_ready"]:
            event = await asyncio.wait_for(iterator.__anext__(), remaining())
            facts["events"] += 1
            failure = _event_failure(event)
            if failure is not None:
                facts["error"] = f"before session ready: {failure}"
                return facts
            if isinstance(event, talk_realtime.SessionReady):
                facts["session_ready"] = True
        phase = "send turn"
        await asyncio.wait_for(
            session.send(
                (
                    talk_realtime.AddContext(
                        item_id=f"talk-check-{uuid.uuid4().hex[:12]}",
                        text=CHECK_TURN_TEXT,
                    ),
                    talk_realtime.StartResponse(),
                )
            ),
            remaining(),
        )
        phase = "response"
        while not facts["response_finished"]:
            event = await asyncio.wait_for(iterator.__anext__(), remaining())
            facts["events"] += 1
            failure = _event_failure(event)
            if failure is not None:
                facts["error"] = f"during the turn: {failure}"
                return facts
            if isinstance(event, talk_realtime.ResponseStarted):
                facts["response_started"] = True
            elif isinstance(event, talk_realtime.OutputAudio):
                facts["audio_bytes"] += len(event.data)
            elif isinstance(event, talk_realtime.Transcript):
                if event.final:
                    facts["transcript_chars"] += len(event.text)
            elif isinstance(event, talk_realtime.ResponseFinished):
                facts["response_finished"] = True
    except TimeoutError:
        facts["error"] = f"timed out after {timeout_s:.0f}s while waiting for {phase}"
    except StopAsyncIteration:
        facts["error"] = f"the provider closed the stream while waiting for {phase}"
    except Exception as exc:  # noqa: BLE001 - a check reports failure, never raises
        detail = str(exc).strip() or type(exc).__name__
        facts["error"] = f"{type(exc).__name__} during {phase}: {detail}"
    finally:
        # Best-effort and bounded: a close that hangs must not outlive the
        # budget, and a close that fails changes nothing about the verdict.
        with suppress(Exception):
            await asyncio.wait_for(session.close(), CLOSE_TIMEOUT_S)
    return facts


def run_provider_step(
    *,
    lane_resolver: Seam,
    session_factory: Seam,
    timeout_s: float | None = None,
) -> tuple[dict[str, Any], Any | None]:
    """Resolve the lane exactly as a session would, then run one live turn.

    Returns the step and the resolved lane (``None`` when resolution
    failed) so the run step can bind its ticket to the same operator.
    """

    started = time.monotonic()
    try:
        lane = lane_resolver()
    except Exception as exc:  # noqa: BLE001 - config/auth refusals are the finding
        return (
            _step(
                "provider_session",
                STATUS_FAIL,
                f"provider lane could not be resolved: {_bounded(str(exc) or type(exc).__name__)}",
                {"error_type": type(exc).__name__},
                duration_ms=_elapsed_ms(started),
                remediation=("Run `hermes talk doctor` for the configuration receipt.",),
            ),
            None,
        )
    details: dict[str, Any] = {
        "provider": lane.provider,
        "model": lane.model,
        "voice": lane.voice,
        "auth_source": lane.auth.source,
        "timeout_s": float(timeout_s if timeout_s is not None else PROVIDER_STEP_TIMEOUT_S),
    }
    if lane.provider not in LIVE_PROVIDERS:
        details["refused"] = "not a live provider"
        return (
            _step(
                "provider_session",
                STATUS_FAIL,
                f"refused: {lane.provider!r} is not a live realtime provider",
                details,
                duration_ms=_elapsed_ms(started),
            ),
            None,
        )
    setup = talk_realtime.SessionSetup(
        model=lane.model,
        voice=lane.voice,
        instructions=CHECK_INSTRUCTIONS,
        tools=(),
        automatic_response=True,
        text_output=False,
    )
    try:
        session = session_factory(lane.auth)
    except Exception as exc:  # noqa: BLE001 - an adapter that cannot be built is the finding
        details["error_type"] = type(exc).__name__
        return (
            _step(
                "provider_session",
                STATUS_FAIL,
                f"provider adapter could not be built: {_bounded(str(exc) or type(exc).__name__)}",
                details,
                duration_ms=_elapsed_ms(started),
            ),
            lane,
        )
    facts = asyncio.run(run_provider_turn(session, setup, timeout_s=timeout_s))
    error = facts.pop("error")
    details.update(facts)
    if error is not None:
        return (
            _step(
                "provider_session",
                STATUS_FAIL,
                f"{lane.provider} session failed: {_bounded(error)}",
                details,
                duration_ms=_elapsed_ms(started),
                remediation=(
                    "Run `hermes talk doctor` for the configuration receipt; the wire "
                    "error above names the failing phase.",
                ),
            ),
            lane,
        )
    return (
        _step(
            "provider_session",
            STATUS_PASS,
            f"{lane.provider} {lane.model} answered one text turn "
            f"({facts['audio_bytes']} audio bytes)",
            details,
            duration_ms=_elapsed_ms(started),
        ),
        lane,
    )


# -- step 3: hermes run -----------------------------------------------------------


def _run_failure(
    summary: str, details: dict[str, Any], started: float, *actions: str
) -> dict[str, Any]:
    return _step(
        "hermes_run",
        STATUS_FAIL,
        summary,
        details,
        duration_ms=_elapsed_ms(started),
        remediation=actions,
    )


def run_hermes_step(
    *,
    run_agent: Seam,
    stop_work: Seam,
    operator: str,
    timeout_s: float | None = None,
    poll_s: float | None = None,
) -> dict[str, Any]:
    """One bounded run through the real delegation path; its output must echo the token.

    The ticket is minted here for exactly this run: a check-owned session id,
    no Hermes session id (so nothing can adopt the result later), the
    operator the provider step resolved. It is detached again on every exit.
    A run that outlives ``timeout_s`` is stopped with the same verb the voice
    uses (``stop_work``) and reported as a failure.
    """

    if timeout_s is None:
        timeout_s = RUN_STEP_TIMEOUT_S
    if poll_s is None:
        poll_s = RUN_POLL_S
    started = time.monotonic()
    details: dict[str, Any] = {"timeout_s": float(timeout_s)}
    talk_runs.attach_owner(
        talk_session_id=f"talk-check-{uuid.uuid4().hex}",
        generation_id=uuid.uuid4().hex[:12],
        hermes_session_id=None,
        operator=operator,
        profile=talk_config.agent_profile(),
    )
    try:
        try:
            receipt = run_agent(RUN_PROMPT)
        except Exception as exc:  # noqa: BLE001 - delegation raising is the finding
            details["error_type"] = type(exc).__name__
            return _run_failure(
                f"delegation raised {type(exc).__name__}: "
                f"{_bounded(str(exc) or 'no detail')}",
                details,
                started,
            )
        receipt = receipt if isinstance(receipt, str) else str(receipt)
        match = _RUN_ID_RE.search(receipt)
        if match is None:
            if receipt.startswith("WORK_STARTED"):
                # Tier 1: the attached agent loop took the task and will
                # answer through Hermes itself — nothing here can read it.
                return _run_failure(
                    "the attached agent loop accepted the task, but its output is "
                    "not observable from this command",
                    details,
                    started,
                    "Run the check from a terminal (`hermes talk check`), where "
                    "delegation lands on the api-server or detached lane.",
                )
            return _run_failure(
                f"delegation did not start a run: {_bounded(receipt)}",
                details,
                started,
                "Enable the api-server lane or put `hermes` on the PATH so a "
                "background agent can run; `hermes talk doctor` names the gap.",
            )
        run_id = int(match.group(1))
        details["run_id"] = run_id
        deadline = started + timeout_s
        while True:
            run = talk_runs.get_run(run_id)
            if run is None:
                return _run_failure(
                    f"run #{run_id} vanished from the registry before it finished",
                    details,
                    started,
                )
            meta = run.get("meta") if isinstance(run.get("meta"), dict) else {}
            details["lane"] = meta.get("lane") or "detached"
            details["profile"] = meta.get("profile")
            if run.get("status") in talk_runs.TERMINAL_STATUSES:
                break
            if time.monotonic() >= deadline:
                stopped = "not attempted"
                try:
                    stopped = _bounded(str(stop_work(str(run_id))))
                except Exception as exc:  # noqa: BLE001 - the stop receipt is informational
                    stopped = f"stop failed: {type(exc).__name__}"
                details["status"] = "running"
                details["stop_receipt"] = stopped
                return _run_failure(
                    f"run #{run_id} did not finish within {timeout_s:.0f}s; a stop was requested",
                    details,
                    started,
                    "Raise --timeout if the agent is merely slow; check the agent's "
                    "own logs if it never finishes.",
                )
            time.sleep(poll_s)
        output = run.get("output") if isinstance(run.get("output"), str) else ""
        token_found = MAGIC_WORD in output
        details.update(
            {
                "status": run.get("status"),
                "output_chars": len(output),
                "token_found": token_found,
            }
        )
        if run.get("status") != "done":
            return _run_failure(
                f"run #{run_id} failed: {_bounded(output or 'no output')}",
                details,
                started,
                "Run `hermes -z 'say hi'` by hand to see the agent's own error.",
            )
        if not token_found:
            return _run_failure(
                f"run #{run_id} finished but its output did not contain {MAGIC_WORD}",
                details,
                started,
                "The agent ran but did not follow a one-line instruction; check its "
                "model configuration.",
            )
        return _step(
            "hermes_run",
            STATUS_PASS,
            f"run #{run_id} on the {details['lane']} lane echoed {MAGIC_WORD}",
            details,
            duration_ms=_elapsed_ms(started),
        )
    finally:
        talk_runs.detach_owner()


# -- the report ----------------------------------------------------------------------


def _default_seams() -> tuple[Seam, Seam]:
    """The production provider factory and lane resolver, imported late.

    :mod:`talk_cli` dispatches this command and injects these itself; the
    late import only serves a direct ``talk_check.cli_entry()`` call and
    keeps the module dependency one-directional.
    """

    try:
        from . import talk_cli
    except ImportError:  # pragma: no cover - flat-module fallback (pip -e install)
        import talk_cli
    return talk_cli._realtime_session, talk_cli.resolve_provider_lane


def run_check(
    *,
    no_run: bool = False,
    timeout_s: float | None = None,
    provider_timeout_s: float | None = None,
    refuse: str | None = None,
    session_factory: Seam | None = None,
    lane_resolver: Seam | None = None,
    run_agent: Seam | None = None,
    stop_work: Seam | None = None,
) -> dict[str, Any]:
    """Run the three steps and return the versioned report.

    ``ok`` is true only when every non-skipped step passed. ``refused``
    names why the live steps did not run, when they did not: an explicit
    caller refusal (a non-live ``--provider``), the test harness, or an
    unsupported configured provider.
    """

    started = time.monotonic()
    if session_factory is None or lane_resolver is None:
        default_factory, default_resolver = _default_seams()
        session_factory = session_factory or default_factory
        lane_resolver = lane_resolver or default_resolver
    if run_agent is None:
        run_agent = talk_host.host().run_agent
    if stop_work is None:
        stop_work = talk_host.host().stop_work

    provider: str | None
    try:
        provider = talk_config.talk_provider()
    except talk_config.TalkConfigError as exc:
        provider = None
        config_refusal: str | None = str(exc)
    else:
        config_refusal = None

    refused = refuse
    if refused is None and not _live_enabled():
        refused = "live steps are switched off under the test harness"
    if refused is None and config_refusal is not None:
        refused = config_refusal
    if refused is None and provider not in LIVE_PROVIDERS:
        refused = f"provider {provider!r} is not a live realtime provider"

    steps = [run_static_step()]
    if refused is not None:
        steps.append(_refused("provider_session", refused))
        steps.append(_refused("hermes_run", refused))
    elif steps[0]["status"] == STATUS_FAIL:
        reason = "static checks failed; fix those first"
        steps.append(_skipped("provider_session", reason))
        steps.append(_skipped("hermes_run", reason))
    else:
        provider_step, lane = run_provider_step(
            lane_resolver=lane_resolver,
            session_factory=session_factory,
            timeout_s=provider_timeout_s,
        )
        steps.append(provider_step)
        if no_run:
            steps.append(_skipped("hermes_run", "--no-run"))
        else:
            steps.append(
                run_hermes_step(
                    run_agent=run_agent,
                    stop_work=stop_work,
                    operator=lane.auth.source if lane is not None else "hermes-talk-check",
                    timeout_s=timeout_s,
                )
            )
    assert tuple(step["id"] for step in steps) == STEP_ORDER
    summary = {
        status: sum(step["status"] == status for step in steps)
        for status in (STATUS_PASS, STATUS_FAIL, STATUS_SKIP)
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "command": COMMAND,
        "read_only": False,
        "ok": summary[STATUS_FAIL] == 0,
        "provider": provider if provider in LIVE_PROVIDERS else None,
        "refused": scrub_text(refused) if refused is not None else None,
        "summary": summary,
        "duration_ms": round(_elapsed_ms(started)),
        "steps": steps,
    }


def _format_duration(duration_ms: int) -> str:
    if duration_ms >= 1000:
        return f"{duration_ms / 1000:.1f} s"
    return f"{duration_ms} ms"


def render_human(report: dict[str, Any]) -> str:
    """Render the same receipt for a person."""

    lines = ["Hermes Talk check (live: one provider turn, one bounded Hermes run)"]
    for step in report["steps"]:
        lines.append(
            f"[{step['status'].upper()}] {step['id']}: {step['summary']} "
            f"({_format_duration(step['duration_ms'])})"
        )
        details = step["details"]
        if step["id"] == "provider_session" and "session_ready" in details:
            lines.append(
                "  receipt: "
                f"auth={details.get('auth_source')}, "
                f"ready={'yes' if details['session_ready'] else 'no'}, "
                f"response={'finished' if details['response_finished'] else 'unfinished'}, "
                f"audio={details['audio_bytes']} bytes, "
                f"transcript={details['transcript_chars']} chars"
            )
        elif step["id"] == "hermes_run" and "run_id" in details:
            lines.append(
                "  receipt: "
                f"run=#{details['run_id']}, lane={details.get('lane')}, "
                f"status={details.get('status')}, "
                f"token={'found' if details.get('token_found') else 'missing'}"
            )
        for action in step["remediation"]:
            lines.append(f"  -> {action}")
    totals = report["summary"]
    verdict = (
        "PASS: the whole voice path works right now"
        if report["ok"]
        else "FAIL: see the failing step above"
    )
    lines.append(
        f"Summary: {totals['pass']} pass, {totals['fail']} fail, {totals['skip']} skip. "
        f"{verdict}."
    )
    lines.append(
        "This check spent one provider turn and at most one agent run; "
        "no credentials or setup were changed."
    )
    return "\n".join(lines)


def cli_entry(
    *,
    json_output: bool = False,
    no_run: bool = False,
    timeout_s: float | None = None,
    provider: str | None = None,
    session_factory: Seam | None = None,
    lane_resolver: Seam | None = None,
) -> int:
    """Print one check report and return nonzero unless every live step passed.

    ``provider`` overrides ``TALK_PROVIDER`` for this process only, and only
    with a live provider name; anything else is refused without touching the
    environment, so a mock can never be smuggled in through the flag.
    """

    refuse: str | None = None
    if provider is not None:
        if provider in LIVE_PROVIDERS:
            os.environ["TALK_PROVIDER"] = provider
        else:
            refuse = (
                f"--provider {provider!r} is not a live realtime provider "
                f"({', '.join(LIVE_PROVIDERS)}); a mock or stub can never produce a "
                "green report"
            )
    if timeout_s is not None and timeout_s <= 0:
        refuse = refuse or "--timeout must be a positive number of seconds"
    report = run_check(
        no_run=no_run,
        timeout_s=timeout_s,
        refuse=refuse,
        session_factory=session_factory,
        lane_resolver=lane_resolver,
    )
    if json_output:
        print(json.dumps(report, sort_keys=True))
    else:
        print(render_human(report))
    return 0 if report["ok"] else 1


__all__ = [
    "COMMAND",
    "LIVE_PROVIDERS",
    "MAGIC_WORD",
    "PROVIDER_STEP_TIMEOUT_S",
    "RUN_STEP_TIMEOUT_S",
    "SCHEMA_VERSION",
    "STEP_ORDER",
    "cli_entry",
    "render_human",
    "run_check",
    "run_hermes_step",
    "run_provider_step",
    "run_provider_turn",
    "run_static_step",
    "scrub_text",
]
