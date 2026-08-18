"""The capability catalog — source preference, honest absence, and bounding.

Zero network. ``talk_capabilities`` keeps its own REST switch off under pytest
for the reason its docstring gives (the four catalog reads bypass ``probe``,
so pinning a fake verdict is not enough to keep a suite offline), so a test
that means to exercise the REST tier opts in through ``rest_lane_on``.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

import talk_apiserver
import talk_capabilities
import talk_config
import talk_host

FAKE_SKILLS = [{"name": "web_search", "installed": True}]
FAKE_TOOLSETS = [
    {"name": "browser", "enabled": True, "configured": True, "tools": ["navigate"]},
    {"name": "email", "enabled": False, "configured": False, "tools": []},
]
FAKE_CAPABILITIES = {
    "platform": "hermes-agent",
    "features": {"run_submission": True, "run_approval_response": True},
}
FAKE_HEALTH = {
    "active_runs": 2,
    "active_delegations": 1,
    "internal_debug_dump": {"tokens": "sk-abcdefgh12345678"},
}

UP = talk_apiserver.ApiServerStatus(True, talk_apiserver.REASON_OK, "Hermes api server")
DOWN = talk_apiserver.ApiServerStatus(
    False, talk_apiserver.REASON_UNAUTHORIZED, "it rejected my key"
)


@pytest.fixture(autouse=True)
def clean():
    """Detached from Hermes, no cached snapshot, no cached lane verdict."""

    talk_host.bind_ctx(None)
    talk_capabilities.reset_for_tests()
    talk_apiserver.reset_for_tests()
    yield
    talk_host.bind_ctx(None)
    talk_capabilities.reset_for_tests()
    talk_apiserver.reset_for_tests()


@pytest.fixture
def rest_lane_on(monkeypatch):
    """Opt this test into the REST tier the rest of the suite keeps switched off."""

    monkeypatch.setattr(talk_capabilities, "_rest_lane_enabled", lambda: True)


class StubCtx:
    """A bound plugin context whose dispatch_tool answers for real."""

    def __init__(self, result):
        self.result = result
        self.calls: list = []

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _rest_up(monkeypatch, **overrides):
    """Pin a reachable api_server whose four catalog reads answer with fakes."""

    monkeypatch.setattr(talk_apiserver, "status", lambda: UP)
    reads = {
        "list_skills": lambda: list(FAKE_SKILLS),
        "list_toolsets": lambda: list(FAKE_TOOLSETS),
        "capabilities_payload": lambda: dict(FAKE_CAPABILITIES),
        "health_detailed": lambda: dict(FAKE_HEALTH),
    }
    reads.update(overrides)
    for name, fn in reads.items():
        monkeypatch.setattr(talk_apiserver, name, fn)


def _no_rest(monkeypatch):
    """Make any REST catalog read an outright test failure."""

    def forbidden():
        raise AssertionError("the REST tier was used when the host answered")

    for name in ("list_skills", "list_toolsets", "capabilities_payload", "health_detailed"):
        monkeypatch.setattr(talk_apiserver, name, forbidden)


def _rest_spy(monkeypatch) -> list[str]:
    """A REST tier that WORKS, and records every read it is asked for.

    Preference has to be proven against a live second source: pinning the
    api_server down would let a reversed resolution order pass, because the
    host would win by being the only thing answering.
    """

    reads: list[str] = []
    monkeypatch.setattr(talk_apiserver, "status", lambda: UP)
    payloads = {
        "list_skills": lambda: list(FAKE_SKILLS),
        "list_toolsets": lambda: list(FAKE_TOOLSETS),
        "capabilities_payload": lambda: dict(FAKE_CAPABILITIES),
        "health_detailed": lambda: dict(FAKE_HEALTH),
    }
    for name, payload in payloads.items():
        def spy(_name=name, _payload=payload):
            reads.append(_name)
            return _payload()

        monkeypatch.setattr(talk_apiserver, name, spy)
    return reads


# -- source preference ---------------------------------------------------------


def test_the_attached_host_is_preferred_over_the_api_server(monkeypatch, rest_lane_on):
    reads = _rest_spy(monkeypatch)
    ctx = StubCtx(
        json.dumps(
            {
                "skills": FAKE_SKILLS,
                "toolsets": FAKE_TOOLSETS,
                "capabilities": FAKE_CAPABILITIES,
                "health": FAKE_HEALTH,
            }
        )
    )
    talk_host.bind_ctx(ctx)

    snapshot = talk_capabilities.warm()

    assert snapshot.source == talk_capabilities.SOURCE_IN_PROCESS
    assert ctx.calls == [(talk_host.CAPABILITY_CATALOG_TOOL_NAME, {})]
    # The api_server was reachable and answering the whole time, and was still
    # never asked — that is what "preferred" has to mean to be worth testing.
    assert reads == []
    assert snapshot.skills == tuple(FAKE_SKILLS)
    assert snapshot.toolsets == tuple(FAKE_TOOLSETS)


def test_a_host_returning_a_native_dict_is_accepted(monkeypatch, rest_lane_on):
    """dispatch_tool is not contractually required to pre-serialize — the
    in-process tier must accept a native dict the same as a JSON string."""

    ctx = StubCtx(
        {
            "skills": FAKE_SKILLS,
            "toolsets": FAKE_TOOLSETS,
            "capabilities": FAKE_CAPABILITIES,
            "health": FAKE_HEALTH,
        }
    )
    talk_host.bind_ctx(ctx)

    snapshot = talk_capabilities.warm()

    assert snapshot.source == talk_capabilities.SOURCE_IN_PROCESS
    assert snapshot.skills == tuple(FAKE_SKILLS)


def test_no_bound_host_falls_through_to_the_api_server(monkeypatch, rest_lane_on):
    _rest_up(monkeypatch)

    snapshot = talk_capabilities.warm()

    assert snapshot.source == talk_capabilities.SOURCE_API_SERVER
    assert snapshot.skills == tuple(FAKE_SKILLS)
    assert snapshot.capabilities == FAKE_CAPABILITIES


def test_a_host_without_the_catalog_tool_falls_through(monkeypatch, rest_lane_on):
    """The exact marker shape ``_agent_loop_absent`` reads, for the real tool
    name — a Hermes that has never heard of it must degrade, not fail."""

    _rest_up(monkeypatch)
    talk_host.bind_ctx(
        StubCtx(
            json.dumps(
                {
                    "error": (
                        f"unknown tool: {talk_host.CAPABILITY_CATALOG_TOOL_NAME}"
                    )
                }
            )
        )
    )

    assert talk_capabilities.warm().source == talk_capabilities.SOURCE_API_SERVER


def test_a_host_that_raises_falls_through(monkeypatch, rest_lane_on):
    _rest_up(monkeypatch)
    talk_host.bind_ctx(StubCtx(RuntimeError("registry offline")))

    assert talk_capabilities.warm().source == talk_capabilities.SOURCE_API_SERVER


def test_a_host_returning_junk_falls_through(monkeypatch, rest_lane_on):
    """Not JSON, and JSON that is not an object, are both "no answer here"."""

    _rest_up(monkeypatch)
    for junk in ("not json at all", "[1, 2, 3]"):
        talk_capabilities.reset_for_tests()
        talk_host.bind_ctx(StubCtx(junk))

        assert talk_capabilities.warm().source == talk_capabilities.SOURCE_API_SERVER


def test_an_unrecognized_dict_envelope_falls_through(monkeypatch, rest_lane_on):
    """A dict alone is not a catalog. The in-process tool name is a GUESS, so
    a real-but-different tool answering under it must not impersonate the
    catalog — an envelope with none of the catalog keys is "no answer here"."""

    _rest_up(monkeypatch)
    talk_host.bind_ctx(
        StubCtx(json.dumps({"result": "ok", "text": "sure — here is what I can do"}))
    )

    assert talk_capabilities.warm().source == talk_capabilities.SOURCE_API_SERVER


def test_a_top_level_error_dict_falls_through(monkeypatch, rest_lane_on):
    """Any error envelope — not just the two ``_agent_loop_absent`` markers —
    reads as "no answer here", even when it also carries a catalog-shaped key."""

    _rest_up(monkeypatch)
    talk_host.bind_ctx(
        StubCtx(json.dumps({"error": "registry exploded", "skills": []}))
    )

    assert talk_capabilities.warm().source == talk_capabilities.SOURCE_API_SERVER


def test_a_wrongly_typed_catalog_key_falls_through(monkeypatch, rest_lane_on):
    """``skills`` only vouches for a payload when it is a LIST; a dict there is
    an arbitrary envelope wearing a catalog key, not a catalog."""

    _rest_up(monkeypatch)
    talk_host.bind_ctx(StubCtx(json.dumps({"skills": {"name": "web_search"}})))

    assert talk_capabilities.warm().source == talk_capabilities.SOURCE_API_SERVER


# -- honest absence ------------------------------------------------------------


def test_neither_source_reachable_says_so_without_claiming_an_empty_install(
    monkeypatch, rest_lane_on
):
    monkeypatch.setattr(talk_apiserver, "status", lambda: DOWN)

    snapshot = talk_capabilities.warm()

    assert snapshot.source == talk_capabilities.SOURCE_NONE
    assert snapshot.skills == ()
    assert snapshot.toolsets == ()
    # The lane's own reason survives, so "rejected my key" is not flattened
    # into a generic "not reachable" that sends the operator to the wrong place.
    assert "rejected my key" in snapshot.detail
    assert "Traceback" not in snapshot.detail


def test_the_rest_tier_is_inert_under_pytest_by_default(monkeypatch):
    """The guard that keeps the dashboard's session-start warm off the network.
    If this ever returns True by accident, unrelated suites dial port 8642."""

    assert talk_capabilities._rest_lane_enabled() is False
    monkeypatch.setattr(talk_apiserver, "status", lambda: UP)
    _no_rest(monkeypatch)

    snapshot = talk_capabilities.warm()

    assert snapshot.source == talk_capabilities.SOURCE_NONE
    assert snapshot.detail == talk_capabilities.INERT_DETAIL


def test_a_failed_catalog_read_is_spoken_not_raised(monkeypatch, rest_lane_on):
    """One read failing fails the WHOLE catalog on purpose: a half-read
    catalog would be spoken as though the missing half did not exist."""

    def boom():
        raise talk_apiserver.TalkApiServerError("the Hermes api server answered 503")

    _rest_up(monkeypatch, list_toolsets=boom)

    snapshot = talk_capabilities.warm()

    assert snapshot.source == talk_capabilities.SOURCE_NONE
    assert "503" in snapshot.detail
    assert snapshot.skills == ()


def test_an_unexpected_explosion_is_still_speakable(monkeypatch, rest_lane_on):
    def boom():
        raise ZeroDivisionError("nobody expects this")

    monkeypatch.setattr(talk_apiserver, "status", lambda: UP)
    _rest_up(monkeypatch, list_skills=boom)

    snapshot = talk_capabilities.warm()

    assert snapshot.source == talk_capabilities.SOURCE_NONE
    assert "ZeroDivisionError" in snapshot.detail


# -- bounding ------------------------------------------------------------------


def test_health_is_bounded_to_known_counters(monkeypatch, rest_lane_on):
    """/health/detailed is a health surface, not a catalog one. What a future
    gateway adds to it must not silently start riding a voice transcript."""

    _rest_up(monkeypatch)

    health = talk_capabilities.warm().health

    assert health == {"active_runs": 2, "active_delegations": 1}
    assert "internal_debug_dump" not in health


def test_non_integer_counters_are_dropped(monkeypatch, rest_lane_on):
    _rest_up(
        monkeypatch,
        health_detailed=lambda: {
            "active_runs": "several",
            "active_delegations": True,
        },
    )

    # ``True`` is an int in Python and would otherwise report as a count of 1.
    assert talk_capabilities.warm().health == {}


def test_capabilities_are_bounded_to_known_fields(monkeypatch, rest_lane_on):
    """/v1/capabilities gets the same discipline as HEALTH_COUNTERS: a future
    gateway field must not silently start riding a voice transcript. Only the
    named fields and the documented boolean feature flags survive."""

    _rest_up(
        monkeypatch,
        capabilities_payload=lambda: {
            "platform": "hermes-agent",
            "model": "Hermes-4.3-405B",
            "auth": {"type": "bearer", "required": True},
            "endpoints": {"runs": {"method": "POST", "path": "/v1/runs"}},
            "internal_routing_table": {"next": "hop"},
            "features": {
                "run_submission": True,
                "session_chat": False,
                "internal_next_big_thing": True,
            },
        },
    )

    capabilities = talk_capabilities.warm().capabilities

    assert capabilities == {
        "platform": "hermes-agent",
        "model": "Hermes-4.3-405B",
        "features": {"run_submission": True, "session_chat": False},
    }


def test_in_process_capabilities_are_bounded_the_same_way(monkeypatch, rest_lane_on):
    """The in-process tier's capabilities dict is upstream text too."""

    talk_host.bind_ctx(
        StubCtx(
            json.dumps(
                {
                    "skills": [],
                    "capabilities": {
                        "features": {"run_submission": True},
                        "surprise_field": "riding along",
                    },
                }
            )
        )
    )

    snapshot = talk_capabilities.warm()

    assert snapshot.source == talk_capabilities.SOURCE_IN_PROCESS
    assert snapshot.capabilities == {"features": {"run_submission": True}}


def test_non_dict_catalog_entries_are_dropped(monkeypatch, rest_lane_on):
    talk_host.bind_ctx(
        StubCtx(json.dumps({"skills": ["bare-string", {"name": "real"}], "toolsets": "junk"}))
    )

    snapshot = talk_capabilities.warm()

    assert snapshot.skills == ({"name": "real"},)
    assert snapshot.toolsets == ()


# -- the cache -----------------------------------------------------------------


def _await_snapshot(timeout: float = 3.0) -> talk_capabilities.CatalogSnapshot:
    """Wait for a background refresh to land, so no thread outlives the test."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        with talk_capabilities._LOCK:
            cached = talk_capabilities._SNAPSHOT
        if cached is not None:
            return cached
        time.sleep(0.01)
    raise AssertionError("the background refresh never stored a snapshot")


def test_a_cold_status_answers_immediately_instead_of_waiting(monkeypatch, rest_lane_on):
    monkeypatch.setattr(talk_apiserver, "status", lambda: UP)

    def slow_skills():
        time.sleep(0.2)
        return list(FAKE_SKILLS)

    _rest_up(monkeypatch, list_skills=slow_skills)

    started = time.monotonic()
    snapshot = talk_capabilities.status()
    elapsed = time.monotonic() - started

    assert elapsed < 0.1, "status() waited on the network"
    # Unavailable, not empty: a catalog we cannot vouch for is not a complete one.
    assert snapshot.source == talk_capabilities.SOURCE_NONE
    assert snapshot.detail == talk_capabilities.CHECKING_DETAIL
    assert _await_snapshot().source == talk_capabilities.SOURCE_API_SERVER


def test_a_warm_snapshot_is_served_without_reading_again(monkeypatch, rest_lane_on):
    _rest_up(monkeypatch)
    talk_capabilities.warm()
    _no_rest(monkeypatch)

    assert talk_capabilities.status().source == talk_capabilities.SOURCE_API_SERVER


def test_an_expired_snapshot_is_served_while_it_refreshes(monkeypatch, rest_lane_on):
    _rest_up(monkeypatch)
    talk_capabilities.warm()
    monkeypatch.setattr(talk_config, "capability_catalog_ttl_s", lambda: 0.0)

    reads: list[int] = []
    monkeypatch.setattr(
        talk_apiserver, "list_skills", lambda: reads.append(1) or list(FAKE_SKILLS)
    )

    # The STALE snapshot comes back now; the refresh happens behind it.
    assert talk_capabilities.status().source == talk_capabilities.SOURCE_API_SERVER

    deadline = time.time() + 3.0
    while not reads and time.time() < deadline:
        time.sleep(0.01)
    assert reads, "an expired snapshot never scheduled a refresh"


def test_only_one_refresh_runs_at_a_time(monkeypatch, rest_lane_on):
    """The stampede guard. Concurrent status() callers on a live call must not
    each start their own catalog read."""

    release = []
    resolved: list[int] = []

    def blocking_resolve():
        resolved.append(1)
        while not release:
            time.sleep(0.01)
        return talk_capabilities._empty("done")

    monkeypatch.setattr(talk_capabilities, "_resolve_or_explain", blocking_resolve)

    for _ in range(5):
        talk_capabilities.status()

    time.sleep(0.05)
    assert resolved == [1]
    release.append(1)
    _await_snapshot()


def test_a_stale_refresh_finishing_late_does_not_clobber_a_newer_snapshot(monkeypatch):
    """Last-writer-wins would let an old slow failure overwrite a newer healthy
    snapshot. The store-sequence guard drops the read that started first."""

    entered = threading.Event()
    release = threading.Event()
    stale = talk_capabilities._empty("a slow failure from before")

    def slow_resolve():
        entered.set()
        release.wait(3.0)
        return stale

    monkeypatch.setattr(talk_capabilities, "_resolve_or_explain", slow_resolve)
    talk_capabilities.status()  # cold cache → schedules the slow background read
    assert entered.wait(3.0), "the background refresh never started"

    # A newer read starts AND lands while the old one is still in flight.
    healthy = talk_capabilities.CatalogSnapshot(
        source=talk_capabilities.SOURCE_API_SERVER,
        skills=({"name": "web_search"},),
        toolsets=(),
        capabilities={},
        health={},
        detail="the Hermes api server",
    )
    monkeypatch.setattr(talk_capabilities, "_resolve_or_explain", lambda: healthy)
    assert talk_capabilities.warm() is healthy

    workers = [
        thread
        for thread in threading.enumerate()
        if thread.name == "talk-capabilities-resolve"
    ]
    release.set()
    for thread in workers:
        thread.join(3.0)

    with talk_capabilities._LOCK:
        assert talk_capabilities._SNAPSHOT is healthy, (
            "the stale refresh overwrote the newer snapshot"
        )


def test_reset_for_tests_clears_the_cache(monkeypatch, rest_lane_on):
    _rest_up(monkeypatch)
    talk_capabilities.warm()
    assert talk_capabilities.status().source == talk_capabilities.SOURCE_API_SERVER

    talk_capabilities.reset_for_tests()

    assert talk_capabilities._SNAPSHOT is None
    assert talk_capabilities._SNAPSHOT_AT == 0.0
    assert talk_capabilities._REFRESHING is False
