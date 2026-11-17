"""Repo self-check against the upstream plugin_guard scanner.

Offline mirror of ``.github/workflows/plugin-guard.yml``: when the scanner
is present locally this test runs it over the repository root and asserts
zero critical and zero high findings (the tiers upstream treats as
blocking or confirmation-gated). It skips cleanly otherwise, so the suite
stays hermetic.

To run the gate locally, point ``TALK_PLUGIN_GUARD_HOME`` at a directory
holding the current ``hermes_cli/plugins_install_scan.py`` and its supporting
modules, or the legacy ``tools/plugin_guard.py`` layout, from
NousResearch/hermes-agent (keep it OUTSIDE this checkout — the scanner's
own pattern table would otherwise be scanned as repo content).
"""

from __future__ import annotations

import os
import subprocess
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
    scanner_present = (home / "hermes_cli" / "plugins_install_scan.py").is_file() or (
        home / "tools" / "plugin_guard.py"
    ).is_file()
    if scanner_present and (home / "tools" / "skills_guard.py").is_file():
        return home
    return None


def test_repo_scans_clean_under_upstream_plugin_guard():
    raw = os.environ.get(SCANNER_HOME_ENV)
    home = _scanner_home()
    if home is None:
        if raw:
            pytest.skip(
                f"{SCANNER_HOME_ENV}={raw!r} is set but scanner files not found at that path"
            )
        pytest.skip(f"scanner not present locally; set {SCANNER_HOME_ENV} to run this gate")
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / ".github" / "plugin_guard_check.py"),
            str(home),
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
