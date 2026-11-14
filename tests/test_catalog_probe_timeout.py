"""Host catalog discovery cannot pin a caller or accumulate stuck readers."""

from __future__ import annotations

import json
import threading

import pytest

import talk_config
import talk_host


@pytest.fixture(autouse=True)
def isolated_probe(monkeypatch):
    monkeypatch.setattr(talk_host, "_CATALOG_PROBE_FUTURE", None, raising=False)


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
        for worker in threading.enumerate():
            if worker.name == "talk-catalog-probe":
                worker.join(3)

    monkeypatch.setattr(
        talk_host, "_catalog_from_host_modules", lambda: {"skills": [{"name": "fresh"}]}
    )
    payload = talk_host.HostAdapter().capability_catalog_probe()
    assert json.loads(payload)["skills"] == [{"name": "fresh"}]


def test_failed_host_catalog_read_returns_none_and_can_be_retried(monkeypatch):
    def fail():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(talk_host, "_catalog_from_host_modules", fail)
    assert talk_host.HostAdapter().capability_catalog_probe() is None
    monkeypatch.setattr(talk_host, "_catalog_from_host_modules", lambda: {"skills": []})
    assert json.loads(talk_host.HostAdapter().capability_catalog_probe()) == {"skills": []}
