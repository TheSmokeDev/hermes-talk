"""Host catalog discovery cannot pin a caller or accumulate stuck readers."""

from __future__ import annotations

import contextvars
import json
import threading
from pathlib import Path

import pytest

import talk_config
import talk_host

HOME: contextvars.ContextVar[str] = contextvars.ContextVar(
    "test_hermes_home", default="/profiles/default"
)


@pytest.fixture(autouse=True)
def isolated_probe(monkeypatch):
    monkeypatch.setattr(talk_host, "_CATALOG_READS", {})
    yield
    _join_readers()


@pytest.fixture
def profiles(monkeypatch):
    """Resolve the Hermes home per context, as a multiplexed gateway does."""
    monkeypatch.setattr(talk_config, "get_hermes_home", lambda: Path(HOME.get()))


def _join_readers():
    for worker in threading.enumerate():
        if worker.name == "talk-catalog-probe":
            worker.join(3)


def _probe():
    return talk_host.HostAdapter().capability_catalog_probe()


def _as_profile(home, fn):
    def turn():
        HOME.set(home)
        return fn()

    return contextvars.copy_context().run(turn)


def test_stalled_host_catalog_returns_and_reuses_its_pending_reader(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    calls = []
    results = []

    def read():
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return {"skills": []}

    def probe():
        results.append(talk_host.HostAdapter().capability_catalog_probe())
        returned.set()

    monkeypatch.setattr(talk_host, "_catalog_from_host_modules", read)
    monkeypatch.setattr(talk_config, "api_server_probe_timeout_s", lambda: 0.02)
    caller = threading.Thread(target=probe, daemon=True)
    caller.start()
    try:
        assert entered.wait(3)
        assert returned.wait(3), "host discovery held its caller until the reader finished"
        assert results == [None]
        assert talk_host.HostAdapter().capability_catalog_probe() is None
        assert calls == [1], "a repeated timeout started another stuck reader"
    finally:
        release.set()
        caller.join(3)
        _join_readers()

    monkeypatch.setattr(
        talk_host, "_catalog_from_host_modules", lambda: {"skills": [{"name": "fresh"}]}
    )
    monkeypatch.setattr(talk_config, "api_server_probe_timeout_s", lambda: 5.0)
    # The read that outlived its callers is kept for the next probe, not dropped.
    assert json.loads(_probe()) == {"skills": []}
    payload = talk_host.HostAdapter().capability_catalog_probe()
    assert json.loads(payload)["skills"] == [{"name": "fresh"}]


def test_failed_host_catalog_read_returns_none_and_can_be_retried(monkeypatch):
    def fail():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(talk_host, "_catalog_from_host_modules", fail)
    assert talk_host.HostAdapter().capability_catalog_probe() is None
    monkeypatch.setattr(talk_host, "_catalog_from_host_modules", lambda: {"skills": []})
    assert json.loads(talk_host.HostAdapter().capability_catalog_probe()) == {"skills": []}


def test_a_host_slower_than_the_budget_still_reaches_the_in_process_tier(monkeypatch):
    gates = [threading.Event() for _ in range(3)]
    calls: list[int] = []

    def read():
        number = len(calls)
        calls.append(number)
        assert gates[number].wait(10)
        return {"skills": [{"name": f"read-{number}"}]}

    monkeypatch.setattr(talk_host, "_catalog_from_host_modules", read)
    monkeypatch.setattr(talk_config, "api_server_probe_timeout_s", lambda: 0.02)
    try:
        assert _probe() is None
        gates[0].set()
        _join_readers()
        # The late read answers this probe, which also starts the next read.
        assert json.loads(_probe())["skills"] == [{"name": "read-0"}]
        assert _probe() is None, "the next read is still pending"
        gates[1].set()
        _join_readers()
        assert json.loads(_probe())["skills"] == [{"name": "read-1"}]
        assert calls == [0, 1, 2]
    finally:
        for gate in gates:
            gate.set()


def test_a_stale_late_result_is_not_reused(monkeypatch):
    release = threading.Event()

    def slow():
        assert release.wait(10)
        return {"skills": [{"name": "stale"}]}

    monkeypatch.setattr(talk_host, "_catalog_from_host_modules", slow)
    monkeypatch.setattr(talk_config, "api_server_probe_timeout_s", lambda: 0.02)
    try:
        assert _probe() is None
    finally:
        release.set()
        _join_readers()

    # Older than the snapshot TTL: a fresh read answers instead.
    monkeypatch.setattr(talk_config, "capability_catalog_ttl_s", lambda: -1.0)
    monkeypatch.setattr(
        talk_host, "_catalog_from_host_modules", lambda: {"skills": [{"name": "fresh"}]}
    )
    monkeypatch.setattr(talk_config, "api_server_probe_timeout_s", lambda: 5.0)
    assert json.loads(_probe())["skills"] == [{"name": "fresh"}]


def test_profiles_keep_separate_pending_reads(profiles, monkeypatch):
    """A stuck read for profile A neither blocks nor answers profile B, each
    read runs in its caller's profile, and A's late result goes only to A."""

    entered_a = threading.Event()
    release_a = threading.Event()
    seen: list[str] = []

    def read():
        home = HOME.get()
        seen.append(home)
        if home == "/profiles/a":
            entered_a.set()
            assert release_a.wait(10)
        return {"skills": [{"name": home}]}

    monkeypatch.setattr(talk_host, "_catalog_from_host_modules", read)
    monkeypatch.setattr(talk_config, "api_server_probe_timeout_s", lambda: 0.05)
    try:
        assert _as_profile("/profiles/a", _probe) is None
        assert entered_a.wait(3)
        assert _as_profile("/profiles/a", _probe) is None
        monkeypatch.setattr(talk_config, "api_server_probe_timeout_s", lambda: 5.0)
        answer_b = _as_profile("/profiles/b", _probe)
        assert json.loads(answer_b)["skills"] == [{"name": "/profiles/b"}]
        assert seen == ["/profiles/a", "/profiles/b"], "A's stuck read was shared or retried"
    finally:
        release_a.set()
        _join_readers()

    # B already received its answer: it reads again and never gets A's late one.
    assert json.loads(_as_profile("/profiles/b", _probe))["skills"] == [{"name": "/profiles/b"}]
    # A's read outlived its callers: A takes it now.
    assert json.loads(_as_profile("/profiles/a", _probe))["skills"] == [{"name": "/profiles/a"}]
    _join_readers()
    assert seen[:3] == ["/profiles/a", "/profiles/b", "/profiles/b"]
    assert set(seen) == {"/profiles/a", "/profiles/b"}
