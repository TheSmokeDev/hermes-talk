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
