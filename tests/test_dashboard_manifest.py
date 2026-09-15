"""Dashboard manifest — the host reads this, so a typo is a silent dead tab.

``_discover_dashboard_plugins`` swallows a bad manifest with a log warning and
moves on, and ``serve_plugin_asset`` 404s a missing entry file. Neither failure
reaches the operator as anything but an empty tab, so the shape is asserted
here instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = PLUGIN_ROOT / "dashboard"

#: The host only serves these suffixes to a browser (web_server.py's asset
#: allowlist); anything else 404s no matter what the manifest names.
BROWSER_SUFFIXES = {".js", ".mjs", ".css", ".json", ".html", ".svg", ".png"}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((DASHBOARD_DIR / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_names_this_plugin(manifest):
    """The name is the MOUNT KEY — routes land at /api/plugins/<name>/.

    Version is only asserted for shape, deliberately: pinning it to
    ``plugin.yaml`` would turn every release bump into a red build in a file
    the bumper has no reason to open.
    """

    plugin_yaml = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")

    assert manifest["name"] == "hermes-talk"
    assert f"name: {manifest['name']}" in plugin_yaml
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])


def test_entry_and_css_exist_and_are_browser_servable(manifest):
    for field in ("entry", "css"):
        target = DASHBOARD_DIR / manifest[field]
        assert target.is_file(), f"manifest {field}={manifest[field]} does not exist"
        assert target.suffix in BROWSER_SUFFIXES
        assert target.stat().st_size > 0


def test_api_is_a_relative_path_inside_the_dashboard_dir(manifest):
    # The host REJECTS an absolute or traversing api path (GHSA-5qr3-c538-wm9j)
    # and mounts no backend at all, so the tab would render against 404s.
    api_field = manifest["api"]
    assert not Path(api_field).is_absolute()
    resolved = (DASHBOARD_DIR / api_field).resolve()
    resolved.relative_to(DASHBOARD_DIR.resolve())
    assert resolved.is_file()


def test_entry_registers_under_the_manifest_name(manifest):
    entry = (DASHBOARD_DIR / manifest["entry"]).read_text(encoding="utf-8")

    # The host renders whatever the bundle registered under this exact name; a
    # mismatch is a blank tab with no error anywhere.
    assert f'__HERMES_PLUGINS__.register("{manifest["name"]}"' in entry


def test_dashboard_tool_request_has_a_client_abort_bound(manifest):
    entry = (DASHBOARD_DIR / manifest["entry"]).read_text(encoding="utf-8")

    assert "const TOOL_TIMEOUT_MS" in entry
    assert "new AbortController()" in entry
    assert re.search(r'apiPost\(\s*"/tool",[\s\S]*?TOOL_TIMEOUT_MS\s*\)', entry)


def test_tab_declares_a_route(manifest):
    assert manifest["tab"]["path"].startswith("/")
    assert manifest["tab"]["position"]


def test_icon_is_one_the_host_can_resolve(manifest):
    # web/src/App.tsx ICON_MAP — anything else silently falls back to Puzzle.
    host_icons = {
        "Activity", "BarChart3", "Clock", "Cpu", "FileText", "FolderOpen",
        "KeyRound", "MessageSquare", "Package", "Settings", "Puzzle",
        "Sparkles", "Terminal", "Globe", "Database", "Shield", "Users",
        "Wrench", "Zap", "Heart", "Star", "Code", "Eye",
    }
    assert manifest["icon"] in host_icons


def test_styles_stay_namespaced(manifest):
    """A plugin stylesheet loads into the HOST document. Unprefixed = collateral."""

    css = (DASHBOARD_DIR / manifest["css"]).read_text(encoding="utf-8")
    selectors = [
        line.split("{")[0].strip()
        for line in css.splitlines()
        if "{" in line and not line.strip().startswith(("@", "/*", "*"))
    ]
    assert selectors
    for selector in selectors:
        assert selector.startswith(".ht-"), selector


def test_every_talk_module_is_packaged():
    """v0.5 shipped a wheel missing ``talk_steer``: the plugin imported fine
    from a git checkout and died on a real install, because a flat-module
    layout needs every module named by hand. A hand-maintained list drifts
    silently, so it is checked against the files on disk instead of trusted.
    """

    on_disk = {path.stem for path in PLUGIN_ROOT.glob("talk_*.py")}
    declared = set(
        re.findall(
            r'"(talk_[a-z_]+)"',
            (PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        )
    )

    assert on_disk - declared == set(), "module on disk is missing from py-modules"
    assert declared - on_disk == set(), "py-modules names a module that does not exist"


def test_plugin_manifest_does_not_block_codex_oauth_only_installs():
    manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")

    assert "requires_env:" not in manifest
