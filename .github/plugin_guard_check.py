"""Run the upstream Hermes plugin_guard scanner against this repository.

The workflow downloads ``hermes_cli/plugins_install_scan.py`` and its
context module, plus ``tools/skills_guard.py``, its credential helper, and
``tools/threat_patterns.py`` from one resolved upstream commit into a directory
OUTSIDE this checkout. Legacy local scanner layouts remain supported.
This script then runs that scanner over the checkout and
fails unless the scan comes back with zero critical and zero high
findings — the two tiers upstream treats as blocking (dangerous) or
confirmation-gated (caution).

Usage: ``python .github/plugin_guard_check.py <scanner-home> <repo-root>``
where ``<scanner-home>`` is the directory containing ``tools/``.
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: plugin_guard_check.py <scanner-home> <repo-root>")
        return 2
    scanner_home = Path(argv[1]).resolve()
    repo_root = Path(argv[2]).resolve()
    if (scanner_home / "hermes_cli" / "plugins_install_scan.py").is_file():
        module_name = "hermes_cli.plugins_install_scan"
    elif (scanner_home / "tools" / "plugin_guard.py").is_file():
        module_name = "tools.plugin_guard"
    else:
        print(f"plugin-guard: no scanner at {scanner_home}")
        return 2
    if not (scanner_home / "tools" / "skills_guard.py").is_file():
        print(f"plugin-guard: no skills_guard at {scanner_home}/tools/skills_guard.py")
        return 2

    sys.path.insert(0, str(scanner_home))
    try:
        scanner = import_module(module_name)
    except ImportError as exc:
        print(f"plugin-guard: cannot import scanner — {exc}")
        return 2

    try:
        result = scanner.scan_plugin(repo_root, source="TheSmokeDev/hermes-talk")
        print(scanner.format_scan_report(result))
    except Exception as exc:  # noqa: BLE001 — a crashed scanner must not read as a verdict
        print(f"plugin-guard: scanner crashed — {exc}")
        return 2

    blocking = [f for f in result.findings if f.severity in ("critical", "high")]
    for finding in blocking:
        print(f"{finding.severity}: {finding.pattern_id} at {finding.file}:{finding.line}")
    if blocking:
        print(f"plugin-guard: FAIL — {len(blocking)} critical/high finding(s)")
        return 1
    print(f"plugin-guard: OK — verdict {result.verdict}, zero critical/high findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
