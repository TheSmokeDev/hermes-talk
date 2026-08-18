"""Dashboard plugin backend — the mint's secrecy, the tool contract, the gate.

The module is loaded THE WAY THE DASHBOARD LOADS IT (by path, no parent
package), so these tests also prove the sys.path shim in ``plugin_api`` — the
first thing that breaks if that import is ever rewritten as a relative one.

Nothing here reaches the network: the mint's single HTTP call
(``talk_wire.post_client_secret``) is replaced in every test that reaches it.
"""

from __future__ import annotations

import asyncio
import hmac
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

import talk_apiserver
import talk_capabilities
import talk_config
import talk_host
import talk_runs
import talk_tools
import talk_vault
import talk_wire

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"

#: A credential shaped like the real thing, so a leak is greppable rather than
#: a subtle substring match.
RAW_KEY = "sk-proj-DASHBOARDLEAKCANARY0123456789"
EPHEMERAL = "ek_dashboard_test_secret"

#: Anything that looks like an OpenAI key MUST NOT appear in a response.
CREDENTIAL_RE = re.compile(r"sk-[A-Za-z0-9_-]{8,}")


def _load_plugin_api():
    """Import dashboard/plugin_api.py exactly as web_server does."""

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_talk", DASHBOARD_DIR / "plugin_api.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # web_server registers before exec so string annotations stay resolvable.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


class FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class FakeRequest:
    """The three things the handlers touch: headers, peer, body."""

    def __init__(self, *, headers=None, host="127.0.0.1", body=None, raise_json=False):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.client = FakeClient(host) if host is not None else None
        self._body = body if body is not None else {}
        self._raise_json = raise_json

    async def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._body


def call(handler, request):
    return asyncio.run(handler(request))


def serialized(payload) -> str:
    return json.dumps(payload, default=str)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Every test starts detached, credential-scoped, and token-free."""

    monkeypatch.delenv(api.DASHBOARD_TOKEN_ENV, raising=False)
    monkeypatch.delenv("TALK_VOICE", raising=False)
    monkeypatch.delenv("TALK_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("TALK_OPENAI_API_KEY", RAW_KEY)
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()
    # Runs are refused without a bound return route (hermes-talk#35), so the
    # suite attaches one. Tests that assert the REFUSAL detach it explicitly.
    talk_runs.attach_owner(
        talk_session_id="ts-test",
        generation_id="gen-test",
        hermes_session_id="sess-test",
        operator="test",
        profile=None,
    )
    yield
    talk_host.bind_ctx(None)
    talk_runs.reset_for_tests()


@pytest.fixture
def minted(monkeypatch):
    """Replace the ONE network call, and record the token it was handed."""

    seen: dict = {}

    def fake_post(auth_token: str, session: dict) -> dict:
        seen["auth_token"] = auth_token
        seen["session"] = session
        return {"value": EPHEMERAL, "expires_at": 1_700_000_000}

    monkeypatch.setattr(talk_wire, "post_client_secret", fake_post)
    return seen


# -- the mint -----------------------------------------------------------------


def test_mint_returns_the_ephemeral_secret_and_the_session_shape(minted):
    body = call(api.create_session, FakeRequest(body={}))

    assert body["ok"] is True
    assert body["clientSecret"] == EPHEMERAL
    assert body["clientSecret"].startswith("ek_")
    assert body["offerUrl"] == talk_wire.OPENAI_REALTIME_OFFER_URL
    assert body["model"] == talk_config.DEFAULT_TALK_MODEL
    assert body["voice"] == talk_config.DEFAULT_TALK_VOICE
    assert body["authSource"] == "configured"
    # The raw key WAS used — one endpoint, one time.
    assert minted["auth_token"] == RAW_KEY


def test_mint_response_contains_no_raw_credential(minted):
    body = call(api.create_session, FakeRequest(body={}))
    blob = serialized(body)

    # Both directions: the exact canary, and anything key-shaped at all.
    assert RAW_KEY not in blob
    assert CREDENTIAL_RE.search(blob) is None
    # The handler returns a plain mapping — no header carries the credential
    # either, because no handler sets one.
    assert all(not str(value).startswith("sk-") for value in body.values())


def test_mint_advertises_the_full_tool_surface(minted, monkeypatch):
    # Pinned OFF: vault availability is a property of the box, and the
    # browser lane must advertise the same surface everywhere.
    monkeypatch.setattr(talk_vault, "available", lambda: False)
    call(api.create_session, FakeRequest(body={}))
    names = [tool["name"] for tool in minted["session"]["tools"]]

    assert names == [
        "search_memory",
        "delegate_task",
        "check_work",
        "list_agents",
        "steer_agent",
        "redirect_agent",
        "stop_work",
        "talk_status",
        "talk_capabilities",
    ]
    assert minted["session"]["tool_choice"] == "auto"


def test_mint_honours_a_requested_voice(minted):
    body = call(api.create_session, FakeRequest(body={"voice": "Marin"}))

    assert body["voice"] == "marin"
    assert minted["session"]["audio"]["output"]["voice"] == "marin"


def test_mint_rejects_an_unknown_voice(minted):
    with pytest.raises(api.HTTPException) as excinfo:
        call(api.create_session, FakeRequest(body={"voice": "gilbert"}))

    assert excinfo.value.status_code == 400
    assert "gilbert" in str(excinfo.value.detail)


def test_mint_reports_a_missing_credential_as_unavailable(monkeypatch):
    monkeypatch.delenv("TALK_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # No key AND no Codex OAuth lane: point CODEX_HOME at an empty directory.
    monkeypatch.setenv("CODEX_HOME", str(DASHBOARD_DIR))

    with pytest.raises(api.HTTPException) as excinfo:
        call(api.create_session, FakeRequest(body={}))

    assert excinfo.value.status_code == 503


def test_mint_surfaces_an_upstream_failure_without_the_credential(monkeypatch):
    def boom(_auth_token, _session):
        raise talk_wire.TalkUpstreamError("OpenAI Realtime client secret failed (401)")

    monkeypatch.setattr(talk_wire, "post_client_secret", boom)

    with pytest.raises(api.HTTPException) as excinfo:
        call(api.create_session, FakeRequest(body={}))

    assert excinfo.value.status_code == 502
    assert RAW_KEY not in str(excinfo.value.detail)


def test_mint_rejects_a_non_json_body(minted):
    with pytest.raises(api.HTTPException) as excinfo:
        call(api.create_session, FakeRequest(raise_json=True))

    assert excinfo.value.status_code == 400


# -- the tool relay -----------------------------------------------------------


def test_unknown_tool_is_a_client_error():
    with pytest.raises(api.HTTPException) as excinfo:
        call(api.run_tool, FakeRequest(body={"name": "launch_missiles"}))

    assert excinfo.value.status_code == 400
    assert "launch_missiles" in str(excinfo.value.detail)


def test_failing_handler_answers_200_with_speakable_text(monkeypatch):
    def boom(_arguments):
        raise RuntimeError("disk on fire")

    monkeypatch.setitem(talk_tools._HANDLERS, "talk_status", boom)

    body = call(api.run_tool, FakeRequest(body={"name": "talk_status"}))

    # The contract that keeps a live call alive: the model HEARS the failure.
    assert body["ok"] is True
    assert "talk_status failed" in body["output"]
    assert "disk on fire" in body["output"]


def test_dashboard_tool_wait_is_bounded_and_honest(monkeypatch):
    def slow_tool(_name, _arguments):
        # A finite wait keeps a broken implementation's RED run bounded too.
        import time

        time.sleep(0.2)
        return "late result"

    monkeypatch.setattr(api.talk_tools, "execute_talk_tool", slow_tool)
    monkeypatch.setattr(api, "TOOL_EXECUTION_WAIT_S", 0.01)

    async def scenario():
        loop = asyncio.get_running_loop()
        started = loop.time()
        body = await api.run_tool(FakeRequest(body={"name": "slow_tool"}))
        return body, loop.time() - started

    body, elapsed = asyncio.run(scenario())

    assert elapsed < 0.1
    assert body["ok"] is True
    assert "still running" in body["output"]
    assert "result won't return" in body["output"]


def test_dashboard_worker_saturation_reports_that_tool_was_not_started(monkeypatch):
    import threading

    release = threading.Event()

    def stuck_tool(_name, _arguments):
        release.wait()
        return "late"

    pool = api.talk_relay._DaemonWorkerPool(max_workers=1, max_pending=1)
    monkeypatch.setattr(api.talk_relay, "_TOOL_POOL", pool)
    monkeypatch.setattr(api.talk_tools, "execute_talk_tool", stuck_tool)
    monkeypatch.setattr(api, "TOOL_EXECUTION_WAIT_S", 0.01)

    async def scenario():
        requests = [FakeRequest(body={"name": f"tool_{i}"}) for i in range(10)]
        return await asyncio.gather(*(api.run_tool(request) for request in requests))

    bodies = asyncio.run(scenario())
    release.set()

    refused = [body["output"] for body in bodies if "not started" in body["output"]]
    assert refused
    assert all("detached it" not in output for output in refused)


def test_tool_arguments_reach_the_handler():
    seen: dict = {}

    def handler(arguments):
        seen.update(arguments)
        return "ok"

    talk_tools._HANDLERS["_probe"] = handler
    try:
        body = call(
            api.run_tool,
            FakeRequest(body={"name": "_probe", "arguments": {"query": "retry policy"}}),
        )
    finally:
        talk_tools._HANDLERS.pop("_probe", None)

    assert body["output"] == "ok"
    assert seen == {"query": "retry policy"}


def test_non_object_arguments_degrade_to_empty(monkeypatch):
    monkeypatch.setitem(talk_tools._HANDLERS, "talk_status", lambda args: json.dumps(args))

    body = call(api.run_tool, FakeRequest(body={"name": "talk_status", "arguments": "nope"}))

    assert body["output"] == "{}"


# -- runs + status ------------------------------------------------------------


def test_runs_route_reports_the_registry():
    finished = []
    run_id = talk_runs.start_run("skill", "canary", lambda _rid: "done here")
    for _ in range(300):
        run = talk_runs.get_run(run_id)
        if run and run["status"] in talk_runs.TERMINAL_STATUSES:
            finished.append(run)
            break
    assert finished, "the worker never terminated"

    body = call(api.list_runs, FakeRequest())
    rows = {row["runId"]: row for row in body["runs"]}

    assert body["ok"] is True
    assert rows[run_id]["status"] == "done"
    assert rows[run_id]["output"] == "done here"


def test_status_never_exposes_a_credential():
    body = call(api.talk_status, FakeRequest())
    blob = serialized(body)

    assert body["configured"] is True
    assert body["source"] == "configured"
    assert body["model"] == talk_config.DEFAULT_TALK_MODEL
    assert body["voices"] == list(talk_config.OPENAI_REALTIME_VOICES)
    # Tri-state lane, not a bool. No plugin context is bound here and the
    # api_server lane is inert under pytest, so the tile reads the exact string
    # it read before the lane existed.
    assert body["agentLoop"] == talk_host.LANE_NONE
    assert body["agentLoop"] == "out of process"
    assert RAW_KEY not in blob
    assert CREDENTIAL_RE.search(blob) is None


def test_status_serializes_the_api_server_lane(monkeypatch):
    """The tile's third state — proven through the ROUTE, not just the helper."""

    monkeypatch.setattr(talk_apiserver, "_lane_enabled", lambda: True)
    monkeypatch.setattr(
        talk_apiserver, "probe", lambda: talk_apiserver.ApiServerStatus(True, "ok", "up")
    )
    talk_apiserver.reset_for_tests()

    body = call(api.talk_status, FakeRequest())

    assert body["agentLoop"] == talk_host.LANE_API_SERVER
    assert body["agentLoop"] == "api-server"
    # A string, so the page must render it verbatim — the old boolean tile
    # would have read "out of process" as truthy and shown "attached".
    assert isinstance(body["agentLoop"], str)


def test_status_serializes_the_attached_lane():
    class StubCtx:
        def dispatch_tool(self, *_args, **_kwargs):
            return "{}"

    talk_host.bind_ctx(StubCtx())

    body = call(api.talk_status, FakeRequest())

    assert body["agentLoop"] == talk_host.LANE_ATTACHED


def test_status_warms_the_capability_catalog(monkeypatch):
    """`/status` is the one place a lane is paid for eagerly — proven through
    the ROUTE, matching how the sibling api_server lane is proven above."""

    calls = []
    monkeypatch.setattr(talk_capabilities, "warm", lambda: calls.append(1))

    call(api.talk_status, FakeRequest())

    assert calls == [1]


def test_status_stays_answerable_when_the_voice_is_unusable(monkeypatch):
    monkeypatch.setenv("TALK_VOICE", "gilbert")

    body = call(api.talk_status, FakeRequest())

    assert body["ok"] is True
    assert body["voice"] == ""
    assert "TALK_VOICE unusable" in body["detail"]


# -- the gate -----------------------------------------------------------------


def test_loopback_passes_when_no_token_is_configured():
    for host in sorted(api.LOOPBACK_HOSTS):
        api.require_dashboard_auth(FakeRequest(host=host))


def test_non_loopback_without_a_token_is_refused():
    with pytest.raises(api.HTTPException) as excinfo:
        api.require_dashboard_auth(FakeRequest(host="10.0.0.7"))

    assert excinfo.value.status_code == 403
    assert api.DASHBOARD_TOKEN_ENV in str(excinfo.value.detail)


def test_an_unidentifiable_peer_is_treated_as_remote():
    # Never fail open: a stripped peer address is refused, not trusted.
    with pytest.raises(api.HTTPException) as excinfo:
        api.require_dashboard_auth(FakeRequest(host=None))

    assert excinfo.value.status_code == 403

    with pytest.raises(api.HTTPException):
        api.require_dashboard_auth(FakeRequest(host="   "))


def test_configured_token_is_required_even_on_loopback(monkeypatch):
    monkeypatch.setenv(api.DASHBOARD_TOKEN_ENV, "s3cret")

    with pytest.raises(api.HTTPException) as excinfo:
        api.require_dashboard_auth(FakeRequest(host="127.0.0.1"))

    assert excinfo.value.status_code == 401
    assert api.DASHBOARD_TOKEN_HEADER in str(excinfo.value.detail)


def test_configured_token_accepts_both_header_forms(monkeypatch):
    monkeypatch.setenv(api.DASHBOARD_TOKEN_ENV, "s3cret")

    api.require_dashboard_auth(
        FakeRequest(headers={"x-talk-token": "s3cret"}, host="10.0.0.7")
    )
    api.require_dashboard_auth(
        FakeRequest(headers={"Authorization": "Bearer s3cret"}, host="10.0.0.7")
    )


def test_a_near_miss_token_is_rejected(monkeypatch):
    monkeypatch.setenv(api.DASHBOARD_TOKEN_ENV, "s3cret")

    for wrong in ("s3crat", "s3cre", "s3secret", ""):
        with pytest.raises(api.HTTPException) as excinfo:
            api.require_dashboard_auth(FakeRequest(headers={"x-talk-token": wrong}))
        assert excinfo.value.status_code == 401


def test_token_comparison_is_constant_time(monkeypatch):
    """Structural proof, not a timing measurement.

    A timing assertion would be flaky on CI; what actually matters is that the
    comparison goes through ``hmac.compare_digest`` rather than ``==``, so that
    is what is asserted.
    """

    calls: list = []
    real = hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr(api.hmac, "compare_digest", spy)
    monkeypatch.setenv(api.DASHBOARD_TOKEN_ENV, "s3cret")

    api.require_dashboard_auth(FakeRequest(headers={"x-talk-token": "s3cret"}))

    assert calls == [(b"s3cret", b"s3cret")]


def test_a_blank_token_env_reads_as_unset(monkeypatch):
    monkeypatch.setenv(api.DASHBOARD_TOKEN_ENV, "   ")

    assert api.dashboard_token() is None
    # Falls back to the loopback rule rather than demanding an unsettable token.
    api.require_dashboard_auth(FakeRequest(host="127.0.0.1"))


def test_every_route_is_gated():
    """A new route that forgets the guard fails HERE, not in production."""

    remote = FakeRequest(host="203.0.113.9")
    for handler in api.ROUTE_HANDLERS:
        with pytest.raises(api.HTTPException) as excinfo:
            call(handler, remote)
        assert excinfo.value.status_code == 403, handler.__name__


def test_route_handlers_covers_every_declared_route():
    """ROUTE_HANDLERS is the gate test's input — it must not drift from the file."""

    source = (DASHBOARD_DIR / "plugin_api.py").read_text(encoding="utf-8")
    decorated = re.findall(r"@router\.(?:get|post|put|patch|delete)\(", source)

    assert len(decorated) == len(api.ROUTE_HANDLERS)


# -- the browser lane's return route (hermes-talk#35) -------------------------


def test_the_mint_binds_a_return_route_for_the_browser_lane(minted):
    """/tool can start real work, so the mint has to bind a destination.

    Without this the browser lane would refuse every delegation: the CLI
    attaches at connect, and the web server process never runs that code.
    """

    talk_runs.detach_owner()

    call(api.create_session, FakeRequest(body={}))
    owner = talk_runs.current_owner()

    assert owner is not None
    assert owner["operator"].startswith("dashboard:")
    assert owner["talkSessionId"]
    # No plugin context is ever bound in the web server process, so there is
    # no durable identity to adopt a run BY — and claiming one would be a lie.
    assert owner["hermesSessionId"] is None


def test_the_browser_ticket_never_carries_the_credential(minted):
    """The ticket is written to disk; the descriptor holds a live secret."""

    talk_runs.detach_owner()

    call(api.create_session, FakeRequest(body={}))
    blob = serialized(talk_runs.current_owner())

    assert EPHEMERAL not in blob
    assert RAW_KEY not in blob
    assert CREDENTIAL_RE.search(blob) is None
