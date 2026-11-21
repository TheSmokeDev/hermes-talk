"""Opt-in real gateway/native-child integration using an Agent checkout."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("HERMES_AGENT_REPO"),
    reason="requires a Hermes Agent checkout with the exact plugin execution API",
)


def test_native_background_child_remains_responsive_to_talk(tmp_path):
    agent_root = Path(os.environ["HERMES_AGENT_REPO"]).resolve()
    talk_root = Path(__file__).resolve().parents[1]
    env = {key: os.environ[key] for key in ("PATH", "LANG", "TZ") if key in os.environ}
    env.update(
        HERMES_HOME=str(tmp_path),
        HERMES_TEST_ISOLATION=str(tmp_path),
        HERMES_DISABLE_LAZY_INSTALLS="1",
        PYTHONPATH=str(agent_root),
        PYTHONUNBUFFERED="1",
        HERMES_AGENT_REPO=str(agent_root),
        HERMES_TALK_REPO=str(talk_root),
    )
    peer = Path(__file__).parent / "fixtures" / "hybrid_agent_peer.py"
    result = subprocess.run(
        [sys.executable, str(peer)],
        cwd=agent_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=110,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    receipt = json.loads((tmp_path / "hybrid-receipt.json").read_text())
    assert receipt["direct_completed_while_child_running"]
    assert receipt["steer_observed_by_child"]
    assert receipt["child_session"] != receipt["parent_session"]
