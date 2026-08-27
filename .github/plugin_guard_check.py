"""Run the upstream Hermes plugin_guard scanner against this repository.

The plugin-guard workflow downloads ``tools/plugin_guard.py`` and
``tools/skills_guard.py`` from NousResearch/hermes-agent, pinned to the
commit it resolved from upstream main, into a directory OUTSIDE this
checkout (the scanner's own pattern table would otherwise be scanned as
repo content). This script then runs that scanner over the checkout and
fails unless the scan comes back with zero critical and zero high
findings — the two tiers upstream treats as blocking (dangerous) or
confirmation-gated (caution).

Usage: ``python .github/plugin_guard_check.py <scanner-home> <repo-root>``
where ``<scanner-home>`` is the directory containing ``tools/``.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: plugin_guard_check.py <scanner-home> <repo-root>")
        return 2
    scanner_home = Path(argv[1]).resolve()
    repo_root = Path(argv[2]).resolve()
    if not (scanner_home / "tools" / "plugin_guard.py").is_file():
        print(f"plugin-guard: no scanner at {scanner_home}/tools/plugin_guard.py")
        return 2
    if not (scanner_home / "tools" / "skills_guard.py").is_file():
        print(f"plugin-guard: no skills_guard at {scanner_home}/tools/skills_guard.py")
        return 2

    sys.path.insert(0, str(scanner_home))
    try:
        from tools.plugin_guard import format_scan_report, scan_plugin
    except ImportError as exc:
        print(f"plugin-guard: cannot import scanner — {exc}")
        return 2

    try:
        result = scan_plugin(repo_root, source="TheSmokeDev/hermes-talk")
        print(format_scan_report(result))
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
