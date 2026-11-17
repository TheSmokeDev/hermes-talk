"""The CI scanner loader must run current upstream layouts and fail closed."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).resolve().parents[1] / ".github" / "plugin_guard_check.py"


def scanner_tree(home, *, modern, severity):
    package = "hermes_cli" if modern else "tools"
    module = "plugins_install_scan" if modern else "plugin_guard"
    (home / package).mkdir(parents=True, exist_ok=True)
    (home / package / "__init__.py").touch()
    (home / "tools").mkdir(exist_ok=True)
    (home / "tools" / "__init__.py").touch()
    (home / "tools" / "skills_guard.py").touch()
    findings = (
        "[]"
        if severity is None
        else (
            f"[SimpleNamespace(severity={severity!r}, pattern_id='sample', "
            "file='file.py', line=1)]"
        )
    )
    (home / package / f"{module}.py").write_text(
        "from types import SimpleNamespace\n"
        "def scan_plugin(root, source):\n"
        f"    return SimpleNamespace(verdict='safe', findings={findings})\n"
        "def format_scan_report(result): return 'scanner executed'\n"
    )


@pytest.mark.parametrize("modern", [True, False])
@pytest.mark.parametrize("severity", [None, "high", "critical"])
def test_checker_runs_upstream_layout_and_preserves_blocking_findings(tmp_path, modern, severity):
    home = tmp_path / "scanner"
    scanner_tree(home, modern=modern, severity=severity)
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(home), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (0 if severity is None else 1), result.stdout + result.stderr
    assert "scanner executed" in result.stdout


@pytest.mark.parametrize("missing", ["scanner", "engine"])
def test_checker_refuses_incomplete_scanner_tree(tmp_path, missing):
    home = tmp_path / "scanner"
    scanner_tree(home, modern=True, severity=None)
    relative = (
        "hermes_cli/plugins_install_scan.py" if missing == "scanner" else "tools/skills_guard.py"
    )
    (home / relative).unlink()
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(home), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
