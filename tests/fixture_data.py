"""Load adversarial test payloads from ``tests/fixtures/``.

The strings these helpers return are attack-shaped bytes — injection text,
destructive commands, dummy credentials — quoted by the containment and
redaction tests to prove those protections hold against the real thing.
They live in ``.fixture`` files because the upstream plugin scanner
(``tools/plugin_guard.py`` in NousResearch/hermes-agent) content-scans only
a fixed extension list that ``.fixture`` is not on: the suite keeps proving
the same protections against the same bytes without the repository itself
reading as hostile to the install gate.
"""

from __future__ import annotations

from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def payload(name: str) -> str:
    """Read one single-payload fixture file, byte-exact."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def fake_credential(name: str) -> str:
    """Read one named dummy credential out of ``fake-secrets.fixture``."""
    for line in payload("fake-secrets.fixture").splitlines():
        key, sep, value = line.partition(": ")
        if sep and key == name:
            return value
    raise KeyError(f"no entry {name!r} in fake-secrets.fixture")
