"""The api_server lane — probe honesty, tier selection, and announced fallbacks.

Zero network. ``talk_apiserver`` is inert under pytest by design (same guard as
the run-history tee), so a test that means to exercise the lane opts in through
the ``lane_on`` fixture; every other test in the whole suite stays off it.
"""

from __future__ import annotations

import json
import threading
import time

import fixture_data
import httpx
import pytest

import talk_apiserver
import talk_config
import talk_host
import talk_runs
import talk_tools


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


CAPABILITIES_OK = {
    "object": "hermes.api_server.capabilities",
    "features": {"run_submission": True, "run_status": True},
}


def _wait_terminal(run_id: int, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = talk_runs.get_run(run_id)
        if run and run["status"] in talk_runs.TERMINAL_STATUSES:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Detached from Hermes, no spawn lane, no cached verdict."""

    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    talk_apiserver.reset_for_tests()
    # Runs are refused without a bound return route (hermes-talk#35), so the
    # suite attaches one. Tests that assert the REFUSAL detach it explicitly.
    talk_runs.attach_owner(
        talk_session_id="ts-test",
        generation_id="gen-test",
        hermes_session_id="sess-test",
        operator="test",
        profile=None,
    )
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: None)
    monkeypatch.delenv("TALK_API_SERVER_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("TALK_API_SERVER_URL", raising=False)
    monkeypatch.delenv("TALK_SESSION_KEY", raising=False)
    monkeypatch.delenv("TALK_MEMORY_SEARCH_TIMEOUT_S", raising=False)
    yield
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    talk_apiserver.reset_for_tests()


@pytest.fixture
def lane_on(monkeypatch):
    """Opt this test into the lane the rest of the suite keeps switched off."""

    monkeypatch.setattr(talk_apiserver, "_lane_enabled", lambda: True)


UP = talk_apiserver.ApiServerStatus(True, talk_apiserver.REASON_OK, "Hermes api server")
DOWN = talk_apiserver.ApiServerStatus(
    False, talk_apiserver.REASON_ABSENT, "the Hermes api server isn't reachable"
)


def _set_lane(monkeypatch, verdict) -> None:
    """Pin the verdict and cache it, the way a session start does.

    ``status()`` deliberately never waits for the network, so a test that only
    patches ``probe`` would read "still checking" — the same thing production
    reads before :func:`talk_apiserver.warm` has run. Warming here is not test
    scaffolding; it is the contract.
    """

    monkeypatch.setattr(talk_apiserver, "probe", lambda: verdict)
    talk_apiserver.reset_for_tests()
    talk_apiserver.warm()


class StubCtx:
    """A bound plugin context whose dispatch_tool answers for real."""

    def __init__(self, result):
        self.result = result
        self.calls: list = []

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        return self.result


class ByToolCtx:
    """A bound context that answers each tool differently.

    :class:`StubCtx` returns one canned result no matter what is dispatched,
    which stopped being enough once a memory read tries two tools in a row:
    a stub that answers "unknown tool: session_search" to a ``honcho_search``
    call describes a host that cannot exist, and the tier it exercises is not
    the tier the test names. Unlisted tools answer "unknown tool: <name>",
    the marker a real host sends for something it does not have.
    """

    def __init__(self, results: dict):
        self.results = results
        self.calls: list = []

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        if name in self.results:
            return self.results[name]
        return json.dumps({"error": f"unknown tool: {name}"})

    @property
    def tools_tried(self) -> list:
        return [name for name, _ in self.calls]


# -- the lane is off unless a test says otherwise -----------------------------


def test_lane_is_inert_under_pytest_by_default():
    # The guard that keeps every other suite off the network. If this ever
    # returns True by accident, unrelated tests start dialling port 8642.
    assert talk_apiserver._lane_enabled() is False
    verdict = talk_apiserver.status()
    assert verdict.available is False
    assert verdict.detail == talk_apiserver.INERT_DETAIL


# -- probe --------------------------------------------------------------------


def test_probe_reports_available_on_200(monkeypatch):
    monkeypatch.setattr(
        talk_apiserver.httpx, "get", lambda *a, **k: FakeResponse(200, CAPABILITIES_OK)
    )

    verdict = talk_apiserver.probe()

    assert verdict.available is True
    assert verdict.reason == talk_apiserver.REASON_OK


def test_probe_timeout_neither_raises_nor_waits_forever(monkeypatch):
    seen: dict = {}

    def timing_out(url, **kwargs):
        seen.update(kwargs)
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(talk_apiserver.httpx, "get", timing_out)
    monkeypatch.setenv("TALK_API_SERVER_PROBE_TIMEOUT_S", "0.25")

    verdict = talk_apiserver.probe()

    assert verdict.available is False
    assert verdict.reason == talk_apiserver.REASON_ABSENT
    # Bounded structurally rather than by wall clock: the budget the caller
    # configured is the budget handed to httpx. A timing assertion here would
    # be flaky on CI and would prove nothing on a fast box.
    assert seen["timeout"] == 0.25
    assert "API_SERVER_ENABLED" in verdict.detail


def test_probe_distinguishes_a_bad_key_from_an_absent_server(monkeypatch):
    monkeypatch.setattr(
        talk_apiserver.httpx, "get", lambda *a, **k: FakeResponse(401, text="nope")
    )

    verdict = talk_apiserver.probe()

    assert verdict.available is False
    assert verdict.reason == talk_apiserver.REASON_UNAUTHORIZED
    # The whole point of probing an AUTHENTICATED endpoint: "running but
    # rejected my key" sends the operator somewhere different than "not there".
    assert "rejected my key" in verdict.detail
    assert "API_SERVER_KEY" in verdict.detail


def test_probe_refuses_a_server_that_cannot_take_runs(monkeypatch):
    monkeypatch.setattr(
        talk_apiserver.httpx,
        "get",
        lambda *a, **k: FakeResponse(200, {"features": {"run_submission": False}}),
    )

    verdict = talk_apiserver.probe()

    assert verdict.available is False
    assert verdict.reason == talk_apiserver.REASON_ERROR


def test_is_available_follows_the_probe(monkeypatch, lane_on):
    _set_lane(monkeypatch, UP)
    assert talk_apiserver.is_available() is True

    _set_lane(monkeypatch, DOWN)
    assert talk_apiserver.is_available() is False


def test_a_warm_verdict_is_served_without_probing_again(monkeypatch, lane_on):
    calls = []

    def counting_probe():
        calls.append(1)
        return UP

    monkeypatch.setattr(talk_apiserver, "probe", counting_probe)
    talk_apiserver.warm()

    for _ in range(5):
        assert talk_apiserver.is_available() is True

    # One probe at warm-up, then cache. A voice turn must not pay the network.
    assert len(calls) == 1


def test_a_cold_status_answers_immediately_instead_of_waiting(monkeypatch, lane_on):
    """The invariant that makes this safe to call from a tool handler.

    Proven by making the probe block on an Event rather than by timing the
    call: ``status()`` must return the "still checking" verdict while the probe
    is demonstrably still in flight.
    """

    release = threading.Event()
    started = threading.Event()

    def slow_probe():
        started.set()
        release.wait(5.0)
        return UP

    monkeypatch.setattr(talk_apiserver, "probe", slow_probe)
    try:
        verdict = talk_apiserver.status()

        assert verdict.available is False
        assert verdict.detail == talk_apiserver.CHECKING_DETAIL
        assert started.wait(2.0), "the background probe never started"
        # Still in flight — so the return above did not wait for it.
        assert not release.is_set()
    finally:
        release.set()


def test_a_probe_that_explodes_is_not_fatal(monkeypatch, lane_on):
    def boom():
        raise RuntimeError("dns on fire")

    monkeypatch.setattr(talk_apiserver, "probe", boom)

    verdict = talk_apiserver.warm()

    assert verdict.available is False
    assert "RuntimeError" in verdict.detail


# -- auth ---------------------------------------------------------------------


def test_auth_header_is_sent_when_a_key_is_configured(monkeypatch):
    seen: dict = {}

    def capture(url, **kwargs):
        seen.update(kwargs)
        return FakeResponse(200, CAPABILITIES_OK)

    monkeypatch.setattr(talk_apiserver.httpx, "get", capture)
    monkeypatch.setenv("TALK_API_SERVER_KEY", "k-123")

    talk_apiserver.probe()

    assert seen["headers"] == {"Authorization": "Bearer k-123"}


def test_auth_header_is_absent_when_no_key_is_configured(monkeypatch):
    seen: dict = {}

    def capture(url, **kwargs):
        seen.update(kwargs)
        return FakeResponse(200, CAPABILITIES_OK)

    monkeypatch.setattr(talk_apiserver.httpx, "get", capture)

    talk_apiserver.probe()

    assert seen["headers"] == {}


def _capture_post(monkeypatch) -> dict:
    """Pin ``httpx.post`` (start_run's verb) and hand back what it saw."""

    seen: dict = {}

    def capture(url, **kwargs):
        seen.update(kwargs)
        return FakeResponse(202, {"run_id": "run_abc", "status": "started"})

    monkeypatch.setattr(talk_apiserver.httpx, "post", capture)
    return seen


def test_the_session_key_header_is_sent_when_configured(monkeypatch):
    seen = _capture_post(monkeypatch)
    monkeypatch.setenv("TALK_SESSION_KEY", "operator-pedro")

    talk_apiserver.start_run("do the thing", session_key=talk_config.session_key())

    assert seen["headers"]["X-Hermes-Session-Key"] == "operator-pedro"


def test_the_session_key_header_is_absent_when_unset(monkeypatch):
    seen = _capture_post(monkeypatch)

    talk_apiserver.start_run("do the thing", session_key=talk_config.session_key())

    assert seen["headers"] == {}


def test_the_session_key_rides_alongside_the_bearer_token(monkeypatch):
    """Two independent headers, not one replacing the other — the merge is
    the whole reason _auth_headers grew a parameter instead of a second
    function."""

    seen = _capture_post(monkeypatch)
    monkeypatch.setenv("TALK_API_SERVER_KEY", "k-123")
    monkeypatch.setenv("TALK_SESSION_KEY", "operator-pedro")

    talk_apiserver.start_run("do the thing", session_key=talk_config.session_key())

    assert seen["headers"] == {
        "Authorization": "Bearer k-123",
        "X-Hermes-Session-Key": "operator-pedro",
    }


def test_the_read_routes_send_no_session_key(monkeypatch):
    """``probe``/``get_run``/``stop_run`` address a run that already exists.
    Scoping them would claim a session boundary they do not create."""

    seen: dict = {}

    def capture(url, **kwargs):
        seen.update(kwargs)
        return FakeResponse(200, CAPABILITIES_OK)

    monkeypatch.setattr(talk_apiserver.httpx, "get", capture)
    monkeypatch.setenv("TALK_SESSION_KEY", "operator-pedro")

    talk_apiserver.probe()

    assert "X-Hermes-Session-Key" not in seen["headers"]


def test_a_run_started_by_the_host_carries_the_operators_scope(monkeypatch, lane_on):
    """The end-to-end property the knob exists for: a memory lookup routed to
    the api_server is scoped without any caller passing the key by hand."""

    _set_lane(monkeypatch, UP)
    monkeypatch.setenv("TALK_SESSION_KEY", "operator-pedro")
    seen: dict = {}

    def fake_run_to_completion(
        _task, *, session_id=None, session_key=None, on_start=None, on_event=None
    ):
        seen["session_key"] = session_key
        return "the answer"

    monkeypatch.setattr(talk_apiserver, "run_to_completion", fake_run_to_completion)

    out = talk_tools.execute_talk_tool("search_memory", {"query": "x"})

    _wait_terminal(int(out.split("WORK_STARTED #")[1].split()[0]))
    assert seen["session_key"] == "operator-pedro"


def test_key_falls_back_to_the_gateways_own_variable(monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "gateway-key")
    assert talk_config.api_server_key() == "gateway-key"

    monkeypatch.setenv("TALK_API_SERVER_KEY", "talk-key")
    assert talk_config.api_server_key() == "talk-key"


def test_a_blank_talk_key_is_an_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "gateway-key")
    monkeypatch.setenv("TALK_API_SERVER_KEY", "   ")

    # Set-but-blank means "send no key", not "use the other one" — the same
    # rule TALK_AGENT_PROFILE follows.
    assert talk_config.api_server_key() is None


# -- runs ---------------------------------------------------------------------


def test_start_run_returns_the_run_id(monkeypatch):
    monkeypatch.setattr(
        talk_apiserver.httpx,
        "post",
        lambda *a, **k: FakeResponse(202, {"run_id": "run_abc", "status": "started"}),
    )

    assert talk_apiserver.start_run("do the thing") == "run_abc"


def test_start_run_raises_a_speakable_error_on_5xx(monkeypatch):
    monkeypatch.setattr(
        talk_apiserver.httpx,
        "post",
        lambda *a, **k: FakeResponse(503, text="gateway draining"),
    )

    with pytest.raises(talk_apiserver.TalkApiServerError) as excinfo:
        talk_apiserver.start_run("do the thing")

    assert "503" in str(excinfo.value)


def test_run_to_completion_returns_the_agents_answer(monkeypatch):
    states = iter(
        [
            {"status": "running"},
            {"status": "completed", "output": "three attempts, exponential backoff"},
        ]
    )
    monkeypatch.setattr(talk_apiserver, "start_run", lambda *a, **k: "run_abc")
    monkeypatch.setattr(talk_apiserver, "get_run", lambda _rid: next(states))
    monkeypatch.setenv("TALK_API_SERVER_POLL_S", "0.01")

    assert talk_apiserver.run_to_completion("what did we decide?") == (
        "three attempts, exponential backoff"
    )


def test_a_failed_run_is_spoken_not_raised(monkeypatch):
    monkeypatch.setattr(talk_apiserver, "start_run", lambda *a, **k: "run_abc")
    monkeypatch.setattr(
        talk_apiserver,
        "get_run",
        lambda _rid: {"status": "failed", "error": "model refused"},
    )
    monkeypatch.setenv("TALK_API_SERVER_POLL_S", "0.01")

    out = talk_apiserver.run_to_completion("go")

    assert "failed" in out
    assert "model refused" in out


# -- catalog reads ------------------------------------------------------------


def _pin_get(monkeypatch, response) -> list[str]:
    """Answer every GET with ``response``, recording which URL was dialed."""

    urls: list[str] = []

    def fake_get(url, **_kwargs):
        urls.append(url)
        return response

    monkeypatch.setattr(talk_apiserver.httpx, "get", fake_get)
    return urls


SKILL = {"name": "web_search", "installed": True}
TOOLSET = {"name": "browser", "enabled": True, "configured": False, "tools": ["open"]}


def test_list_skills_accepts_a_bare_list(monkeypatch):
    urls = _pin_get(monkeypatch, FakeResponse(200, [SKILL]))

    assert talk_apiserver.list_skills() == [SKILL]
    assert urls == [talk_config.api_server_url() + talk_apiserver.SKILLS_PATH]


def test_list_skills_accepts_an_object_envelope(monkeypatch):
    """The envelope is unverified against a live gateway, so BOTH documented
    shapes parse — a cosmetic difference must not become a dead feature."""

    _pin_get(monkeypatch, FakeResponse(200, {"skills": [SKILL], "total": 1}))

    assert talk_apiserver.list_skills() == [SKILL]


def test_list_toolsets_accepts_both_shapes(monkeypatch):
    _pin_get(monkeypatch, FakeResponse(200, [TOOLSET]))
    assert talk_apiserver.list_toolsets() == [TOOLSET]

    _pin_get(monkeypatch, FakeResponse(200, {"toolsets": [TOOLSET]}))
    assert talk_apiserver.list_toolsets() == [TOOLSET]


def test_a_listing_drops_entries_that_are_not_objects(monkeypatch):
    _pin_get(monkeypatch, FakeResponse(200, ["bare-string", SKILL, 7]))

    assert talk_apiserver.list_skills() == [SKILL]


def test_an_unrecognized_listing_envelope_raises(monkeypatch):
    """Neither shape. Raising beats returning ``[]``, which would be spoken as
    "nothing is installed" — a confident answer that happens to be false."""

    _pin_get(monkeypatch, FakeResponse(200, {"data": {"skills": [SKILL]}}))

    with pytest.raises(talk_apiserver.TalkApiServerError, match="unrecognized"):
        talk_apiserver.list_skills()


def test_capabilities_payload_returns_the_raw_document(monkeypatch):
    """Distinct from probe(), which collapses this same path to a yes/no."""

    payload = {"features": {"run_submission": True}, "run_approval": True}
    urls = _pin_get(monkeypatch, FakeResponse(200, payload))

    assert talk_apiserver.capabilities_payload() == payload
    assert urls == [talk_config.api_server_url() + talk_apiserver.CAPABILITIES_PATH]


def test_health_detailed_returns_the_raw_document(monkeypatch):
    payload = {"active_runs": 2, "active_delegations": 0}
    urls = _pin_get(monkeypatch, FakeResponse(200, payload))

    assert talk_apiserver.health_detailed() == payload
    assert urls == [talk_config.api_server_url() + talk_apiserver.HEALTH_DETAILED_PATH]


@pytest.mark.parametrize(
    "read",
    ["list_skills", "list_toolsets", "capabilities_payload", "health_detailed"],
)
def test_every_catalog_read_raises_a_speakable_error_on_non_200(monkeypatch, read):
    _pin_get(monkeypatch, FakeResponse(503, text="gateway draining"))

    with pytest.raises(talk_apiserver.TalkApiServerError, match="503"):
        getattr(talk_apiserver, read)()


@pytest.mark.parametrize(
    "read",
    ["list_skills", "list_toolsets", "capabilities_payload", "health_detailed"],
)
def test_every_catalog_read_raises_on_a_non_json_body(monkeypatch, read):
    _pin_get(monkeypatch, FakeResponse(200, None, text="<html>nope</html>"))

    with pytest.raises(talk_apiserver.TalkApiServerError):
        getattr(talk_apiserver, read)()


@pytest.mark.parametrize(
    "read",
    ["list_skills", "list_toolsets", "capabilities_payload", "health_detailed"],
)
def test_every_catalog_read_raises_when_the_server_is_unreachable(monkeypatch, read):
    def boom(*_args, **_kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(talk_apiserver.httpx, "get", boom)

    with pytest.raises(talk_apiserver.TalkApiServerError, match="ConnectError"):
        getattr(talk_apiserver, read)()


@pytest.mark.parametrize("read", ["capabilities_payload", "health_detailed"])
def test_a_document_read_raises_when_the_body_is_not_an_object(monkeypatch, read):
    _pin_get(monkeypatch, FakeResponse(200, ["not", "a", "document"]))

    with pytest.raises(talk_apiserver.TalkApiServerError, match="invalid"):
        getattr(talk_apiserver, read)()


# -- tier selection -----------------------------------------------------------


def test_search_memory_tier_a_uses_the_bound_agent(monkeypatch, lane_on):
    _set_lane(monkeypatch, UP)
    ctx = StubCtx(json.dumps({"result": "we chose exponential backoff"}))
    talk_host.bind_ctx(ctx)

    out = talk_tools.execute_talk_tool("search_memory", {"query": "retry policy"})

    assert out == "we chose exponential backoff"
    assert ctx.calls[0][0] == talk_host.MEMORY_TOOL_NAME
    # Tier a answers inline — no receipt, nothing to watch.
    assert "WORK_STARTED" not in out


def test_search_memory_tier_b_hands_off_and_speaks_the_answer(monkeypatch, lane_on):
    _set_lane(monkeypatch, UP)
    monkeypatch.setattr(
        talk_apiserver,
        "run_to_completion",
        lambda prompt, **k: "you decided on three attempts",
    )

    out = talk_tools.execute_talk_tool("search_memory", {"query": "retry policy"})

    # The receipt, not the answer: this call is on the loop carrying the mic.
    assert "WORK_STARTED #" in out
    assert "through the api server" in out
    run_id = int(out.split("WORK_STARTED #")[1].split()[0])
    run = _wait_terminal(run_id)
    assert run["status"] == "done"
    assert run["output"] == "you decided on three attempts"
    assert run["meta"]["lane"] == talk_host.LANE_API_SERVER


def test_search_memory_tier_c_names_what_is_missing(monkeypatch, lane_on):
    _set_lane(monkeypatch, DOWN)

    out = talk_tools.execute_talk_tool("search_memory", {"query": "retry policy"})

    assert "memory isn't available" in out
    assert "isn't reachable" in out
    assert "WORK_STARTED" not in out


def test_the_four_search_memory_tiers_say_four_different_things(monkeypatch, lane_on):
    monkeypatch.setattr(talk_apiserver, "run_to_completion", lambda *a, **k: "answer")

    _set_lane(monkeypatch, UP)
    talk_host.bind_ctx(
        ByToolCtx({talk_host.MEMORY_TOOL_NAME: json.dumps({"result": "inline answer"})})
    )
    tier_a = talk_tools.execute_talk_tool("search_memory", {"query": "x"})

    # Same answer text, different tier: only the provenance marker separates
    # them, so identical content is the strongest form of this assertion.
    talk_host.bind_ctx(
        ByToolCtx(
            {talk_host.HONCHO_SEARCH_TOOL_NAME: json.dumps({"result": "inline answer"})}
        )
    )
    tier_b = talk_tools.execute_talk_tool("search_memory", {"query": "x"})

    talk_host.bind_ctx(None)
    tier_c = talk_tools.execute_talk_tool("search_memory", {"query": "x"})

    _set_lane(monkeypatch, DOWN)
    tier_d = talk_tools.execute_talk_tool("search_memory", {"query": "x"})

    # A silent downgrade is the failure this plugin exists to avoid.
    assert len({tier_a, tier_b, tier_c, tier_d}) == 4


def test_a_bound_host_without_either_memory_tool_falls_through(monkeypatch, lane_on):
    """Reaching the api_server now takes BOTH in-process tools being absent —
    a host that has neither is the only one with nothing left to try."""

    _set_lane(monkeypatch, UP)
    monkeypatch.setattr(talk_apiserver, "run_to_completion", lambda *a, **k: "found it")
    ctx = ByToolCtx({})  # knows nothing: every dispatch answers "unknown tool"
    talk_host.bind_ctx(ctx)

    out = talk_tools.execute_talk_tool("search_memory", {"query": "x"})

    assert "WORK_STARTED #" in out
    assert ctx.tools_tried == [
        talk_host.MEMORY_TOOL_NAME,
        talk_host.HONCHO_SEARCH_TOOL_NAME,
    ]


def test_search_memory_tier_1b_uses_honcho_when_session_search_is_absent(
    monkeypatch, lane_on
):
    _set_lane(monkeypatch, UP)
    # If this leaked through to tier 2 the assertion below would still need
    # explaining, so make the fall-through loud rather than plausible.
    monkeypatch.setattr(talk_apiserver, "run_to_completion", lambda *a, **k: "WRONG TIER")
    ctx = ByToolCtx(
        {
            talk_host.HONCHO_SEARCH_TOOL_NAME: json.dumps(
                {"result": "Dograh is the voice stack Pedro runs TaskChad on"}
            )
        }
    )
    talk_host.bind_ctx(ctx)

    out = talk_tools.execute_talk_tool("search_memory", {"query": "Dograh"})

    assert out == (
        f"{talk_host.REMEMBERED_PREFIX}Dograh is the voice stack Pedro runs TaskChad on"
    )
    assert ctx.tools_tried == [
        talk_host.MEMORY_TOOL_NAME,
        talk_host.HONCHO_SEARCH_TOOL_NAME,
    ]
    assert "WORK_STARTED" not in out


def test_a_transcript_hit_is_not_labelled_as_remembered(monkeypatch, lane_on):
    """The prefix is the ONLY thing separating a quote from a recollection on
    a surface with nothing on screen. A transcript hit wearing it would make
    a verbatim line sound like a guess, which is the same defect inverted."""

    _set_lane(monkeypatch, UP)
    talk_host.bind_ctx(
        ByToolCtx(
            {talk_host.MEMORY_TOOL_NAME: json.dumps({"result": "you said port 8642"})}
        )
    )

    out = talk_tools.execute_talk_tool("search_memory", {"query": "port"})

    assert out == "you said port 8642"
    assert talk_host.REMEMBERED_PREFIX not in out


def test_a_real_honcho_failure_is_not_routed_around(monkeypatch, lane_on):
    """Same rule the session_search tier follows: only "this tool is not here"
    falls through. A Honcho that IS here and refused made a decision that is
    its own, and routing around it would answer from a lane the host just
    declined to use."""

    _set_lane(monkeypatch, UP)
    monkeypatch.setattr(talk_apiserver, "run_to_completion", lambda *a, **k: "found it")
    talk_host.bind_ctx(
        ByToolCtx(
            {
                talk_host.HONCHO_SEARCH_TOOL_NAME: json.dumps(
                    {"error": "honcho index is rebuilding"}
                )
            }
        )
    )

    out = talk_tools.execute_talk_tool("search_memory", {"query": "x"})

    assert "WORK_STARTED" not in out
    assert "rebuilding" in out
    # A refusal is spoken, but it is NOT a recollection — the prefix marks
    # the provenance of a remembered FACT, and an error wearing it would be
    # relayed to the operator as one (review r2, F5).
    assert talk_host.REMEMBERED_PREFIX not in out


def test_a_raising_honcho_tier_is_spoken_not_swallowed(monkeypatch, lane_on):
    _set_lane(monkeypatch, UP)

    class Boom(ByToolCtx):
        def dispatch_tool(self, name, args):
            if name == talk_host.HONCHO_SEARCH_TOOL_NAME:
                raise RuntimeError("honcho socket closed")
            return super().dispatch_tool(name, args)

    talk_host.bind_ctx(Boom({}))

    out = talk_tools.execute_talk_tool("search_memory", {"query": "x"})

    assert "the memory lookup failed" in out
    assert "honcho socket closed" in out


def test_a_malicious_honcho_result_is_spoken_as_bounded_text_not_replayed(
    monkeypatch, lane_on
):
    """A remembered "fact" is attacker-reachable content: anything that can
    write to the operator's Honcho profile picks the display name. It reaches
    the model as a TOOL RESULT, never concatenated into a prompt block, so the
    containment that matters is _speakable's bounding — not escaping, which
    would fire on a path this text does not take. Asserted on the real path
    rather than on the defense that would not run."""

    _set_lane(monkeypatch, UP)
    payload = fixture_data.payload("adversarial/injection-ignore-env.fixture")
    talk_host.bind_ctx(
        ByToolCtx({talk_host.HONCHO_SEARCH_TOOL_NAME: json.dumps({"result": payload})})
    )

    out = talk_tools.execute_talk_tool("search_memory", {"query": "who am i"})

    # Inert text with its provenance attached, and bounded like every other
    # tool result — the model is told this is remembered, not instructed.
    assert out == f"{talk_host.REMEMBERED_PREFIX}{payload}"
    assert len(out) <= len(talk_host.REMEMBERED_PREFIX) + talk_host.MAX_TOOL_OUTPUT_CHARS


def test_an_oversized_honcho_result_is_capped(monkeypatch, lane_on):
    _set_lane(monkeypatch, UP)
    talk_host.bind_ctx(
        ByToolCtx(
            {talk_host.HONCHO_SEARCH_TOOL_NAME: json.dumps({"result": "x" * 50_000})}
        )
    )

    out = talk_tools.execute_talk_tool("search_memory", {"query": "x"})

    assert len(out) == len(talk_host.REMEMBERED_PREFIX) + talk_host.MAX_TOOL_OUTPUT_CHARS


def test_a_hanging_honcho_dispatch_is_bounded_not_blocking(monkeypatch, lane_on):
    """The Honcho tier runs inside the serialized tool pipeline; unbounded, a
    wedged plugin would hold every later tool call (and the voice loop behind
    them) hostage for the life of the process (review r2, F4). The bound is
    TALK_MEMORY_SEARCH_TIMEOUT_S, resolved at call time."""

    _set_lane(monkeypatch, UP)
    monkeypatch.setenv("TALK_MEMORY_SEARCH_TIMEOUT_S", "0.05")
    release = threading.Event()

    class Hang(ByToolCtx):
        def dispatch_tool(self, name, args):
            if name == talk_host.HONCHO_SEARCH_TOOL_NAME:
                release.wait(30)
                return json.dumps({"result": "too late"})
            return super().dispatch_tool(name, args)

    talk_host.bind_ctx(Hang({}))
    try:
        started = time.monotonic()
        out = talk_tools.execute_talk_tool("search_memory", {"query": "x"})
        elapsed = time.monotonic() - started
    finally:
        release.set()  # unblock the daemon worker; its late result is discarded

    assert elapsed < 5
    assert "didn't answer in time" in out
    assert talk_host.REMEMBERED_PREFIX not in out
    assert "WORK_STARTED" not in out


def test_a_forged_remembered_marker_in_a_transcript_hit_is_stripped(
    monkeypatch, lane_on
):
    """The provenance marker is reserved for the Honcho tier. Transcript
    content that LEADS with the literal prefix would make a verbatim line
    wear a recollection's provenance (review r2, F9); mid-text occurrences
    are ordinary content and survive."""

    _set_lane(monkeypatch, UP)
    forged = f"{talk_host.REMEMBERED_PREFIX}{talk_host.REMEMBERED_PREFIX}you said port 8642"
    talk_host.bind_ctx(
        ByToolCtx({talk_host.MEMORY_TOOL_NAME: json.dumps({"result": forged})})
    )

    out = talk_tools.execute_talk_tool("search_memory", {"query": "port"})

    assert out == "you said port 8642"


def test_a_real_memory_failure_is_not_routed_around(monkeypatch, lane_on):
    _set_lane(monkeypatch, UP)
    # Not an agent-loop-absent marker: the host said no for its own reasons and
    # that answer belongs to the host, not to this plugin's fallback chain.
    talk_host.bind_ctx(StubCtx(json.dumps({"error": "memory index is rebuilding"})))

    out = talk_tools.execute_talk_tool("search_memory", {"query": "x"})

    assert "WORK_STARTED" not in out
    assert "rebuilding" in out


def test_delegate_tier_b_runs_on_the_api_server(monkeypatch, lane_on):
    _set_lane(monkeypatch, UP)
    monkeypatch.setattr(talk_apiserver, "run_to_completion", lambda *a, **k: "audit done")

    out = talk_tools.execute_talk_tool("delegate_task", {"task": "audit the auth module"})

    assert "WORK_STARTED #" in out
    assert "through the api server" in out
    run_id = int(out.split("WORK_STARTED #")[1].split()[0])
    assert _wait_terminal(run_id)["output"] == "audit done"


def test_delegate_tier_c_still_spawns_when_the_lane_is_down(monkeypatch, lane_on):
    _set_lane(monkeypatch, DOWN)
    monkeypatch.setattr(talk_host, "hermes_binary", lambda: "C:/fake/hermes")
    monkeypatch.setattr(
        talk_host, "_detached_agent_worker", lambda task, binary: lambda _rid: "spawned"
    )

    out = talk_tools.execute_talk_tool("delegate_task", {"task": "audit"})

    assert "detached Hermes agent" in out
    assert "api server" not in out


def test_delegate_refusal_names_all_three_missing_lanes(monkeypatch, lane_on):
    _set_lane(monkeypatch, DOWN)

    out = talk_tools.execute_talk_tool("delegate_task", {"task": "audit"})

    assert "no Hermes agent" in out
    assert "api server isn't reachable" in out
    assert "PATH" in out


# -- the tri-state ------------------------------------------------------------


def test_agent_lane_reports_attached_when_a_ctx_is_bound(lane_on):
    talk_host.bind_ctx(StubCtx("{}"))

    assert talk_host.host().agent_lane() == "attached"


def test_agent_lane_reports_api_server_when_reachable(monkeypatch, lane_on):
    _set_lane(monkeypatch, UP)

    assert talk_host.host().agent_lane() == "api-server"


def test_agent_lane_reports_out_of_process_when_neither(monkeypatch, lane_on):
    _set_lane(monkeypatch, DOWN)

    # The exact string the dashboard tile already shipped — a rename here is a
    # silent UI regression, not a refactor.
    assert talk_host.host().agent_lane() == "out of process"


def test_agent_lane_survives_a_broken_probe(monkeypatch, lane_on):
    def boom():
        raise RuntimeError("socket layer gone")

    monkeypatch.setattr(talk_apiserver, "status", boom)

    assert talk_host.host().agent_lane() == talk_host.LANE_NONE


def test_talk_status_reports_the_same_lane(monkeypatch, lane_on):
    _set_lane(monkeypatch, UP)

    status = json.loads(talk_tools.execute_talk_tool("talk_status", {}))

    assert status["agent_lane"] == "api-server"
    # The older boolean answers a narrower question and must keep answering it.
    assert status["attached_to_hermes"] is False


# --- the tool surface refuses an unrouted dispatch (hermes-talk#35) ----------


def test_the_api_server_lane_refuses_before_running_when_unrouted(monkeypatch, lane_on):
    """Tier 2 must not consume the request when nothing can receive it."""

    _set_lane(monkeypatch, UP)
    ran = []
    monkeypatch.setattr(
        talk_apiserver, "run_to_completion", lambda *a, **k: ran.append(a) or "audit done"
    )
    talk_runs.detach_owner()

    out = talk_tools.execute_talk_tool("delegate_task", {"task": "audit the auth module"})

    assert out.startswith("I can't start that yet")
    assert "WORK_STARTED" not in out
    assert ran == []


def test_an_unrouted_memory_lookup_says_lookup_not_work(monkeypatch, lane_on):
    """The refusal names what was refused; a lookup is not a delegated task."""

    _set_lane(monkeypatch, UP)
    monkeypatch.setattr(talk_apiserver, "run_to_completion", lambda *a, **k: "found it")
    talk_runs.detach_owner()

    out = talk_tools.execute_talk_tool("search_memory", {"query": "retry policy"})

    assert out.startswith("I can't look that up yet")
    assert "WORK_STARTED" not in out


def test_check_work_still_reads_a_run_started_by_this_session(monkeypatch, lane_on):
    """The ticket is additive: the existing tool surface is unchanged."""

    _set_lane(monkeypatch, UP)
    monkeypatch.setattr(talk_apiserver, "run_to_completion", lambda *a, **k: "audit done")

    out = talk_tools.execute_talk_tool("delegate_task", {"task": "audit the auth module"})
    run_id = int(out.split("WORK_STARTED #")[1].split()[0])
    _wait_terminal(run_id)

    spoken = talk_tools.execute_talk_tool("check_work", {"run_id": run_id})

    assert "audit done" in spoken
    # Routing metadata stays out of anything the operator hears.
    assert "ts-test" not in spoken
    assert "sess-test" not in spoken


def test_no_lane_at_all_still_names_the_lanes_not_the_routing(monkeypatch, lane_on):
    """The routing refusal must not shadow the pre-existing tier-4 refusal.

    With no agent reachable by ANY lane, "there is nothing to run this on" is
    the useful answer; "there is nowhere to send the result" would be true but
    would hide the actionable half.
    """

    _set_lane(monkeypatch, DOWN)
    talk_runs.detach_owner()

    out = talk_tools.execute_talk_tool("delegate_task", {"task": "audit"})

    assert "no Hermes agent" in out
    assert "api server isn't reachable" in out
    assert "PATH" in out
    assert "I can't start that yet" not in out
