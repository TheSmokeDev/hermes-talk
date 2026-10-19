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


def test_the_ruff_version_is_pinned_not_floating():
    """A floating ruff makes every upstream release a possible red build.

    The rule set was already pinned; the VERSION was not. ruff 0.16 narrowed
    BLE001 to bound handlers, which retired three directives and failed CI on
    RUF100 while older dev boxes still required them — the two versions
    wanted opposite source, so only a pin makes one answer correct. The pin
    lives in requirements-lint.txt, which CI installs alongside the dev extra.
    """

    import re

    lint = (REPO_ROOT / "requirements-lint.txt").read_text(encoding="utf-8")
    ruff = re.search(r"^ruff(?P<spec>\S*)\s*$", lint, re.MULTILINE)
    assert ruff is not None, "ruff left requirements-lint.txt; CI installs it from here"
    assert ruff.group("spec").startswith("=="), (
        f'ruff must be pinned exactly, found "ruff{ruff.group("spec")}"'
    )

    # And CI must still be installing from that file, or the pin is decorative.
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'pip install -e ".[dev]" -r requirements-lint.txt' in ci


def test_no_extra_carries_an_exact_pin():
    """Hermes enables a plugin by locking every extra of it in Hermes's own uv
    workspace, next to Hermes core's dev group. An exact pin in any extra must
    then agree with whatever core pins, and the day they differ nobody can
    enable the plugin: `ruff==0.16.10` in the dev extra against core's
    `ruff==0.15.10` did exactly that (#180). Tooling pins belong in
    requirements-lint.txt, which only CI and contributors install."""

    import re

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    extras = re.search(
        r"^\[project\.optional-dependencies\]\n(?P<body>.*?)(?=^\[)", pyproject, re.M | re.S
    )
    assert extras is not None, "the extras table moved; this guard needs updating"

    pinned = [
        f"{name}: {dep}"
        for name, deps in re.findall(r"^(\w+) = \[([^\]]*)\]", extras.group("body"), re.M)
        for dep in re.findall(r'"([^"]+)"', deps)
        if "==" in dep
    ]
    assert not pinned, f"exact pins in extras block plugin admission: {pinned}"
