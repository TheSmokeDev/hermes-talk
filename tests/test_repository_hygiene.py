"""Repository-only source hygiene contracts."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_local_review_and_tdd_artifacts_are_gitignored():
    lines = {
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }

    assert ".hermes-*-review.md" in lines
    assert ".hermes-*-tdd.md" in lines
def test_every_version_surface_matches_pyproject():
    """The release flow bumps pyproject.toml; plugin.yaml (what `hermes
    plugins list` shows) and dashboard/manifest.json drifted at 0.8.0 for
    seven releases before anyone noticed. One source of truth, pinned."""

    import json
    import re
    from pathlib import Path

    root = Path(__file__).parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)

    plugin_yaml = (root / "plugin.yaml").read_text(encoding="utf-8")
    assert f"version: {version}" in plugin_yaml, (
        f"plugin.yaml version drifted from pyproject ({version})"
    )

    manifest = json.loads((root / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == version, (
        f"dashboard/manifest.json version drifted from pyproject ({version})"
    )
