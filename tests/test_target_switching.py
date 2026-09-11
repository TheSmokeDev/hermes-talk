"""Target switching over real attachment/events/staging and the frozen HTTP envelopes."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_dashboard_tasks import Host, child, input_event

from talk_dashboard_gateway import DashboardTaskError
from talk_dashboard_tasks import DashboardOwnerContext, DashboardTasks
from talk_passive import HistoryError, HistoryTransport, digest
from talk_target_catalog import TargetCatalog
from talk_target_selection import TargetSelection
from talk_target_state import TargetState


class CatalogHost(Host):
    def __init__(self, store_id):
        super().__init__()
        self.store_id = store_id
        self.offline, self.denied = False, False
        self.titles = {
            ("default", "task-a"): "Shared task",
            ("default", "task-b"): "Second task",
            ("default", "bot-main"): "Bot Chat",
            ("alpha", "bot-alpha"): "Bot Chat",
        }
        self.rows.update({("default", "bot-main"): [], ("alpha", "bot-alpha"): []})
        self.requests_all = []

    def __call__(self, request):
        self.requests_all.append(request)
        if self.offline:
            raise httpx.ConnectError(
                "private URL and credential must never enter diagnostics", request=request
            )
        if self.denied or request.headers["Authorization"] != "Bearer fixture-gateway-key":
            return self.response(401, {"error": "unauthorized"})
        path, profile = request.url.path, "default"
        if path.startswith("/p/"):
            _, _, profile, rest = path.split("/", 3)
            path = "/" + rest
        if path == "/api/sessions":
            assert request.method == "GET"  # Never create a Bot or a replacement task.
            bot = request.url.params.get("title") == "Bot Chat"
            if bot:
                assert request.url.params.get("include_hidden") == "1"
            return self.response(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": session, "title": title}
                        for (scope, session), title in self.titles.items()
                        if scope == profile
                        and (title == "Bot Chat" if bot else title != "Bot Chat")
                    ],
                },
            )
        if path.startswith("/api/sessions/"):
            selected = path.rsplit("/", 1)[-1]
            if (profile, selected) not in self.titles:
                return self.response(404, {"error": "not_found"})
            return self.response(
                200,
                {
                    "object": "hermes.session",
                    "session": {"id": selected, "title": self.titles[(profile, selected)]},
                },
            )
        return super().__call__(request)


@pytest.fixture
def fleet(tmp_path):
    hosts = {
        "local": CatalogHost("local-store"),
        "east": CatalogHost("east-store"),
        "west": CatalogHost("west-store"),
    }
    request = SimpleNamespace(state=SimpleNamespace(principal="actor-one"))
    denied_profiles = set()
    peers = {
        name: (f"https://{name}.fixture.invalid", "fixture-gateway-key")
        for name in ("east", "west")
    }

    def resolve(req, profile=None):
        profile = profile or "default"
        if profile in denied_profiles or profile not in {"default", "alpha"}:
            raise DashboardTaskError("context_denied", 403)
        return DashboardOwnerContext(
            req.state.principal, "verified_subject", profile, tmp_path / profile, "local-store"
        )

    def local(context):
        return HistoryTransport(
            "http://127.0.0.1:8642",
            context.profile_name,
            "fixture-gateway-key",
            named_profile=context.profile_name != "default",
            actor_scope=digest([context.principal_id, context.store_id]),
            _http_transport=httpx.MockTransport(hosts["local"]),
        )

    def remote(url, profile, key, **kwargs):
        name = url.split("//")[1].split(".")[0]
        return HistoryTransport(
            url, profile, key, **kwargs, _http_transport=httpx.MockTransport(hosts[name])
        )

    manager = DashboardTasks(context_resolver=resolve, transport_factory=local)
    catalog = TargetCatalog(
        manager,
        profiles=lambda: [
            {"name": "default", "display_name": "Main Bot"},
            {"name": "alpha", "display_name": "Alpha Bot"},
        ],
        peers=lambda: list(peers),
        resolve_peer=lambda name, profile, home: peers[name],
        local_factory=local,
        remote_factory=remote,
    )
    selection = TargetSelection(manager, catalog=catalog)
    return SimpleNamespace(
        manager=manager,
        catalog=catalog,
        selection=selection,
        request=request,
        hosts=hosts,
        root=tmp_path,
        peers=peers,
        denied=denied_profiles,
    )


def target(fleet, session="task-a", *, profile="default", peer="local"):
    rows = fleet.catalog.catalog(fleet.request, {"peer_id": peer, "profile": profile})["targets"]
    return next(row["target_id"] for row in rows if row["session_id"] == session)


def activate(fleet, selected=None, *, old=None, back=False, tab="tab-one"):
    body = {"back": True} if back else {"target_id": selected}
    body.update(old or {"tab_id": tab})
    prepared = fleet.selection.prepare(fleet.request, body, initial=old is None)
    selection = fleet.selection.activate(fleet.request, prepared)
    bound = prepared.bound
    return bound, {"connection_id": bound.connection_id, "generation": bound.generation}, selection


def test_local_tasks_bots_return_original_job_and_generation(fleet):
    catalog = fleet.catalog.catalog(fleet.request, {})
    assert {(row["kind"], row["profile"], row["label"]) for row in catalog["targets"]} >= {
        ("bot", "default", "Main Bot"),
        ("bot", "alpha", "Alpha Bot"),
    }
    a, context, _ = activate(fleet, target(fleet))
    environment = (fleet.manager, fleet.request, fleet.hosts["local"], fleet.root)
    original = input_event(environment, context)
    child(environment, context, original)
    fleet.hosts["local"].jobs["remote-1"].update(
        status="completed", output="Complete A result", updated_at=101
    )
    b, b_context, switched = activate(
        fleet, target(fleet, "bot-alpha", profile="alpha"), old=context
    )
    assert switched["return_depth"] == 1 and b.context.profile_name == "alpha"
    assert not fleet.manager.state(fleet.request, b_context)["jobs"]
    with pytest.raises(DashboardTaskError, match="no longer current"):
        fleet.manager.event(
            fleet.request,
            {
                **context,
                "kind": "input.final",
                "input_id": "late",
                "input_type": "typed",
                "text": "late",
            },
        )
    returned, resumed, info = activate(fleet, old=b_context, back=True)
    assert returned.attachment.owner == a.attachment.owner and info["return_depth"] == 0
    view = fleet.manager.state(fleet.request, resumed)
    assert (
        fleet.manager.result(fleet.request, {**resumed, "run_id": view["jobs"][0]["run_id"]})[
            "output"
        ]
        == "Complete A result"
    )
    assert len(fleet.hosts["local"].jobs) == 1
    assert [row["content"] for row in view["history"]["messages"]] == [
        "Earlier typed task",
        "Original genuine user words",
    ]
    assert not fleet.hosts["local"].rows[("alpha", "bot-alpha")]


def test_explicit_peer_same_ids_ambiguity_and_offline_return(fleet):
    a, context, _ = activate(fleet, target(fleet))
    east, west = target(fleet, peer="east"), target(fleet, peer="west")
    assert len({east, west, a.target_record["target_id"]}) == 3
    ambiguous = fleet.catalog.resolve(fleet.request, "Shared task")
    assert ambiguous["state"] == "ambiguous" and len(ambiguous["choices"]) == 3
    remote, remote_context, _ = activate(fleet, east, old=context)
    assert remote.context.store_id == "east-store"
    assert remote.attachment.owner != a.attachment.owner
    input_event(
        (fleet.manager, fleet.request, fleet.hosts["east"], fleet.root),
        remote_context,
        text="East only",
    )
    assert fleet.hosts["east"].rows[("default", "task-a")][-1]["content"] == "East only"
    assert len(fleet.hosts["local"].rows[("default", "task-a")]) == 1
    assert all(req.url.path.startswith("/p/default/") for req in fleet.hosts["east"].requests_all)
    fleet.hosts["east"].offline = True
    local, _, _ = activate(fleet, old=remote_context, back=True)
    assert local.attachment.owner == a.attachment.owner
    assert all(req.url.host == "east.fixture.invalid" for req in fleet.hosts["east"].requests_all)


@pytest.mark.parametrize(
    "failure", ["denied", "offline", "store", "credentials", "deleted", "unsupported"]
)
def test_refused_target_preserves_current_owner_and_stack(fleet, failure):
    a, context, _ = activate(fleet, target(fleet))
    selected = target(fleet, peer="east")
    host = fleet.hosts["east"]
    if failure in {"denied", "offline"}:
        setattr(host, failure, True)
    elif failure == "store":
        host.store_id = "replacement-store"
    elif failure == "credentials":
        fleet.peers["east"] = (fleet.peers["east"][0], "rotated-key")
    elif failure == "deleted":
        del host.titles[("default", "task-a")]
    else:
        host.child_supported = False
    with pytest.raises((DashboardTaskError, HistoryError)):
        fleet.selection.prepare(fleet.request, {**context, "target_id": selected})
    assert fleet.manager.binding(fleet.request, context) is a
    snapshot = fleet.catalog.state(fleet.request).snapshot("tab-one")
    assert not snapshot["stack"] and snapshot["current"]["connection_id"] == a.connection_id


def test_prepared_cancel_actor_fence_nonce_race_and_named_permission(fleet):
    a, context, _ = activate(fleet, target(fleet))
    selected = target(fleet, "bot-alpha", profile="alpha")
    prepared = fleet.selection.prepare(fleet.request, {**context, "target_id": selected})
    assert fleet.manager.binding(fleet.request, context) is a  # Preparing did not activate.
    with pytest.raises(DashboardTaskError, match="already being prepared"):
        fleet.selection.prepare(fleet.request, {**context, "target_id": selected})
    foreign = SimpleNamespace(state=SimpleNamespace(principal="actor-two"))
    with pytest.raises(DashboardTaskError):
        fleet.selection.activate(foreign, prepared)
    fleet.selection.cancel(prepared)
    newer = fleet.selection.prepare(fleet.request, {**context, "target_id": selected})
    with pytest.raises(DashboardTaskError, match="no longer current"):
        fleet.selection.activate(fleet.request, prepared)
    fleet.denied.add("alpha")
    with pytest.raises(DashboardTaskError):
        fleet.selection.activate(fleet.request, newer)
    fleet.selection.cancel(newer)
    assert fleet.manager.binding(fleet.request, context) is a


def test_catalog_redaction_expired_scope_and_injected_endpoint_refusal(fleet):
    fleet.hosts["local"].titles[("default", "task-a")] = "fixture-gateway-key title"
    catalog = fleet.catalog.catalog(fleet.request, {})
    encoded = json.dumps(catalog)
    assert "fixture-gateway-key" not in encoded and str(fleet.root) not in encoded
    assert "store_id" not in encoded and "route_fingerprint" not in encoded
    for peer in ["https://attacker.invalid", "../east"]:
        with pytest.raises(DashboardTaskError):
            fleet.catalog.catalog(fleet.request, {"peer_id": peer})
    other = SimpleNamespace(state=SimpleNamespace(principal="actor-two"))
    with pytest.raises(DashboardTaskError):
        fleet.catalog.materialize(other, catalog["targets"][0]["target_id"])
    state = TargetState(fleet.catalog.actor(fleet.request), clock=lambda: 10**12)
    assert state.targets() == [] and state.snapshot("tab-one")["current"] is None
    with pytest.raises(DashboardTaskError):
        fleet.selection.prepare(
            fleet.request, {"tab_id": "tab", "target_id": {**catalog["targets"][0]}}, initial=True
        )


def test_same_target_switch_refused_before_attachment_and_foreign_connection(fleet):
    selected = target(fleet)
    a, context, _ = activate(fleet, selected)
    before = dict(fleet.hosts["local"].attachments)
    with pytest.raises(DashboardTaskError, match="already selected"):
        fleet.selection.prepare(fleet.request, {**context, "target_id": selected})
    assert fleet.hosts["local"].attachments == before
    other = SimpleNamespace(state=SimpleNamespace(principal="actor-two"))
    with pytest.raises(DashboardTaskError):
        fleet.manager.binding(other, context)
    assert fleet.manager.binding(fleet.request, context) is a


def test_remote_lost_receipt_reconnect_truncated_history_no_steering(fleet, monkeypatch):
    host = fleet.hosts["east"]
    snapshot = host.snapshot
    monkeypatch.setattr(host, "snapshot", lambda *args: {**snapshot(*args), "truncated": True})
    remote, context, _ = activate(fleet, target(fleet, peer="east"))
    environment = (fleet.manager, fleet.request, host, fleet.root)
    original = input_event(environment, context, text="Remote original")
    host.drop_run = True
    child(environment, context, original)
    assert len(host.jobs) == 1
    _, local, _ = activate(fleet, target(fleet), old=context)
    returned, current, _ = activate(fleet, old=local, back=True)
    assert returned.attachment.owner == remote.attachment.owner
    state = fleet.manager.state(fleet.request, current)
    assert state["history"]["truncated"] is True and len(state["jobs"]) == 1
    assert len(host.jobs) == 1
    posts = [item for item in host.requests if item[0:2] == ("POST", "/v1/runs")]
    assert len(posts) == 2 and posts[0][2:] == posts[1][2:]
    assert (
        json.loads(fleet.manager._read_or_control(returned, "talk_capabilities", {}))["steering"]
        == "unsupported"
    )


def test_late_child_receipt_after_switch_stays_with_original_action(fleet):
    a, context, _ = activate(fleet, target(fleet))
    environment = (fleet.manager, fleet.request, fleet.hosts["local"], fleet.root)
    original = input_event(environment, context)
    entered, release = threading.Event(), threading.Event()
    fleet.hosts["local"].before_run = lambda: (entered.set(), release.wait(5))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(child, environment, context, original)
        assert entered.wait(3)
        _, bcontext, _ = activate(fleet, target(fleet, "task-b"), old=context)
        release.set()
        with pytest.raises(DashboardTaskError):
            pending.result()
    fleet.hosts["local"].before_run = None
    returned, current, _ = activate(fleet, old=bcontext, back=True)
    assert returned.attachment.owner == a.attachment.owner
    assert len(fleet.manager.state(fleet.request, current)["jobs"]) == 1
    assert len(fleet.hosts["local"].jobs) == 1


def test_api_switch_mint_failure_then_success_and_tool_intent(fleet, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("target_api_fixture", source)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    monkeypatch.setattr(api, "TASKS", fleet.manager)
    monkeypatch.setattr(api, "TARGETS", fleet.selection)
    monkeypatch.setattr(api, "require_dashboard_auth", lambda request: None)
    monkeypatch.setattr(api, "_resolve_voice_mode", lambda: "native")
    monkeypatch.setattr(api, "_resolve_voice", lambda value: "marin")
    monkeypatch.setattr(
        api.talk_auth,
        "resolve_auth",
        lambda: SimpleNamespace(token="fixture-provider", source="test"),
    )
    monkeypatch.setattr(
        api,
        "_mint",
        lambda *args, **kwargs: SimpleNamespace(to_wire=lambda: {"clientSecret": "ephemeral"}),
    )
    body = {"task": {"target_id": target(fleet), "tab_id": "tab-one"}}

    async def read():
        return body

    fleet.request.json = read
    response = asyncio.run(api.create_session(fleet.request))
    context = {key: response["task"][key] for key in ("connection_id", "generation")}
    selected = target(fleet, "task-b")
    body = {**context, "target_id": selected}

    def fail(*args, **kwargs):
        raise api.talk_wire.TalkWireError("Fixture mint unavailable")

    monkeypatch.setattr(api, "_mint", fail)
    with pytest.raises(api.HTTPException):
        asyncio.run(api.task_switch(fleet.request))
    assert fleet.manager.binding(fleet.request, context)
    monkeypatch.setattr(
        api,
        "_mint",
        lambda *args, **kwargs: SimpleNamespace(to_wire=lambda: {"clientSecret": "ephemeral"}),
    )

    async def disconnected():
        return True

    fleet.request.is_disconnected = disconnected
    with pytest.raises(api.HTTPException):
        asyncio.run(api.task_switch(fleet.request))
    assert fleet.manager.binding(fleet.request, context)
    del fleet.request.is_disconnected
    switched = asyncio.run(api.task_switch(fleet.request))
    assert (
        switched["selection"]["target_id"] == selected and switched["task"]["tab_id"] == "tab-one"
    )
    current = {key: switched["task"][key] for key in ("connection_id", "generation")}
    environment = (fleet.manager, fleet.request, fleet.hosts["local"], fleet.root)
    original = input_event(environment, current, text="Return to my previous task")
    fleet.manager.event(
        fleet.request,
        {
            **current,
            "kind": "response.started",
            "interaction_id": original["interaction_id"],
            "response_id": "response",
        },
    )
    body = {
        **current,
        "interaction_id": original["interaction_id"],
        "response_id": "response",
        "call_id": "call",
        "name": "return_to_previous",
        "arguments": {},
    }
    intent = asyncio.run(api.run_tool(fleet.request))
    assert intent["selection"] == {"back": True}
    assert fleet.manager.binding(fleet.request, current)  # Intent did not activate.
    assert not fleet.hosts["local"].jobs
