"""Repo self-check against the upstream plugin_guard scanner.

Offline mirror of ``.github/workflows/plugin-guard.yml``: when the scanner
is present locally this test runs it over the repository root and asserts
zero critical and zero high findings (the tiers upstream treats as
blocking or confirmation-gated). It skips cleanly otherwise, so the suite
stays hermetic.

To run the gate locally, point ``TALK_PLUGIN_GUARD_HOME`` at a directory
whose ``tools/`` holds ``plugin_guard.py`` and ``skills_guard.py`` from
NousResearch/hermes-agent (keep it OUTSIDE this checkout — the scanner's
own pattern table would otherwise be scanned as repo content).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER_HOME_ENV = "TALK_PLUGIN_GUARD_HOME"


def _scanner_home() -> Path | None:
    raw = os.environ.get(SCANNER_HOME_ENV)
    if not raw:
        return None
    home = Path(raw)
    if (home / "tools" / "plugin_guard.py").is_file() and (
        home / "tools" / "skills_guard.py"
    ).is_file():
        return home
    return None


def test_repo_scans_clean_under_upstream_plugin_guard():
    home = _scanner_home()
    if home is None:
        pytest.skip(f"scanner not present locally; set {SCANNER_HOME_ENV} to run this gate")
    sys.path.insert(0, str(home))
    try:
        from tools.plugin_guard import scan_plugin
    finally:
        sys.path.remove(str(home))

    result = scan_plugin(REPO_ROOT, source="TheSmokeDev/hermes-talk (local gate)")

    blocking = [f for f in result.findings if f.severity in ("critical", "high")]
    assert not blocking, "\n".join(
        f"{f.severity}: {f.pattern_id} at {f.file}:{f.line}" for f in blocking
    )
