"""Shared suite plumbing."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _ephemeral_runs_optin(monkeypatch):
    """The suite's EXPLICIT opt-in to non-durable run acceptance.

    The run-history tee is inert under pytest by design (see
    ``talk_runs._history_enabled``), and ``start_run`` refuses to accept work
    it cannot record durably unless the caller opts in by name — silence is
    exactly how a "durable" acceptance quietly stops being durable. Tests
    that exercise durability monkeypatch the tee ON (and this variable is
    then never consulted); the test asserting the refusal itself deletes it.
    """

    monkeypatch.setenv("TALK_RUNS_ALLOW_EPHEMERAL", "1")
