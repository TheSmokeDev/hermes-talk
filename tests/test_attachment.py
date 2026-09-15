"""Real HTTP fixture for passive-history v1 (host contract 813d255).

The fixture uses the public wire envelopes and independently checks exact request
fields, attachment/principal scope, busy admission, event receipts and retirement.
It has no execution route. No provider, microphone or installed host is contacted.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from talk_attachment import TalkAttachment
from talk_outbox import HistoryOutbox
from talk_passive import HistoryError, HistoryMessage, HistoryTransport

CAPS = {
    "version": 1,
    "passive_only": True,
    "origin_adoption": False,
    "operations": ["attach", "snapshot", "commit", "reconcile", "detach"],
    "max_request_bytes": 163840,
    "max_message_bytes": 65536,
    "max_messages": 2,
    "max_snapshot_messages": 20,
    "max_snapshot_bytes": 32768,
    "restart_requires_reattach": True,
}
ATTACHMENT_KEYS = {"tab_id", "attachment_id", "generation", "session_id"}
BODY_KEYS = {
    "attach": {"tab_id", "session_id"},
    "snapshot": ATTACHMENT_KEYS,
    "commit": ATTACHMENT_KEYS | {"event_id", "origin_turn_id", "messages"},
    "reconcile": {"session_id", "event_id"},
    "detach": ATTACHMENT_KEYS,
}


class FrozenHost:
    def __init__(self):
        self.caps = dict(CAPS)
        self.calls = []
        self.rows = [{"id": 1, "role": "user", "content": "Earlier typed message"}]
        self.attachments = {}
        self.receipts = {}
        self.saved_bodies = {}
        self.generation = 0
        self.busy = False
        self.deleted = False
        self.retired = False
        self.lose_commit = False
        self.override = {}
        self.before = None
        self.url = ""

    def error(self, code, status=409, retryable=False):
        return status, {"error": code, "retryable": retryable}

    def dispatch(self, operation, body, principal, profile):
        self.calls.append((operation, body, profile))
        if self.before:
            self.before(operation)
        if operation in self.override:
            return self.override[operation]
        if operation == "capabilities":
            return 200, self.caps
        assert set(body) == BODY_KEYS[operation]
        if self.deleted:
            return self.error("target_missing", 404)
        if body["session_id"] not in {"selected", "other"}:
            return self.error("target_missing", 404)
        scope = (principal, profile, body.get("tab_id"))
        if operation == "attach":
            self.generation += 1
            authority = {
                "tab_id": body["tab_id"],
                "session_id": body["session_id"],
                "attachment_id": f"attachment-{self.generation}",
                "generation": self.generation,
            }
            self.attachments[scope] = authority
            return 200, {"profile": profile, **authority, "snapshot": self.snapshot()}
        if operation == "reconcile":
            event = body["event_id"]
            if event in self.receipts and self.retired:
                return self.error("retired", 410)
            receipt = self.receipts.get(event)
            if receipt and self.saved_bodies[event]["session_id"] != body["session_id"]:
                return self.error("event_conflict")
            return 200, {
                "profile": profile,
                "status": "saved" if receipt else "unknown",
                "receipt": receipt,
            }
        if self.attachments.get(scope) != {key: body[key] for key in ATTACHMENT_KEYS}:
            return self.error("stale_attachment")
        if operation == "snapshot":
            return 200, {"profile": profile, **self.snapshot()}
        if operation == "detach":
            del self.attachments[scope]
            return 200, {"status": "detached"}
        assert operation == "commit", "zero execution: no other route exists"
        if self.busy:
            return self.error("busy", retryable=True)
        rows = body["messages"]
        assert 1 <= len(rows) <= 2
        assert all(set(row) == {"role", "content"} for row in rows)
        assert all(row["role"] in {"user", "assistant"} and row["content"].strip() for row in rows)
        event = body["event_id"]
        if event in self.receipts:
            prior = self.saved_bodies[event]
            if any(prior[key] != body[key] for key in ("origin_turn_id", "messages", "session_id")):
                return self.error("event_conflict")
            receipt = {**self.receipts[event], "replayed": True}
            return 200, {"profile": profile, "status": "already_saved", "receipt": receipt}
        ids = []
        for row in rows:
            ids.append(len(self.rows) + 1)
            self.rows.append({"id": ids[-1], **row})
        receipt = {
            "producer": "passive.ingress.v1",
            "event_id": event,
            "origin_turn_id": body["origin_turn_id"],
            "conversation_id": "root",
            "session_id": "compressed-tip",
            "message_ids": ids,
            "revision": len(self.receipts) + 1,
            "replayed": False,
        }
        self.receipts[event], self.saved_bodies[event] = receipt, body
        if self.lose_commit:
            self.lose_commit = False
            return None, None
        return 200, {"profile": profile, "status": "saved", "receipt": receipt}

    def snapshot(self):
        return {
            "conversation_id": "root",
            "session_id": "compressed-tip",
            "messages": list(self.rows),
            "truncated": False,
            "capabilities": self.caps,
        }


@pytest.fixture
def host():
    state = FrozenHost()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_call()

        def do_POST(self):
            self.handle_call()

        def handle_call(self):
            parsed = urlsplit(self.path)
            operation = parsed.path.rsplit("/", 1)[-1]
            assert operation in {*BODY_KEYS, "capabilities"}
            assert parsed.path in {
                f"/v1/passive-history/{operation}",
                f"/p/work/v1/passive-history/{operation}",
                f"/api/passive-history/{operation}",
            }
            principal = self.headers.get("Authorization") or self.headers.get(
                "X-Hermes-Session-Token"
            )
            assert principal in {"Bearer secret-key", "Bearer second-key", "secret-key"}
            profile = "work" if parsed.path.startswith("/p/work/") else "default"
            profile = parse_qs(parsed.query).get("profile", [profile])[0]
            body = None
            if self.command == "POST":
                size = int(self.headers["Content-Length"])
                assert size <= CAPS["max_request_bytes"]
                body = json.loads(self.rfile.read(size))
            status, result = state.dispatch(operation, body, principal, profile)
            if status is None:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            raw = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def client(host, tmp_path, *, selected="selected", tab="tab-one", **transport_options):
    profile = transport_options.pop("profile", "default")
    transport = HistoryTransport(host.url, profile, "secret-key", **transport_options)
    box = HistoryOutbox(tmp_path, profile=profile)
    return TalkAttachment(transport, box, selected_session=selected, connection_id=tab), box


def queued(attachment, *, text="Final ordinary dialogue", event_id="event-one"):
    token = attachment.capture_token
    event = attachment.enqueue(
        token,
        (HistoryMessage("user", text),),
        origin_turn_id="origin-one",
        finalized=True,
        disposition="dialogue",
        event_id=event_id,
    )
    return event, token


def test_snapshot_commit_readback_detach_and_no_execution(host, tmp_path):
    attachment, box = client(host, tmp_path)
    token = attachment.attach()
    assert attachment.owner.session_id == "selected"
    assert attachment.snapshot.session_id == "compressed-tip"
    assert attachment.snapshot.messages[0].content == "Earlier typed message"
    event, _ = queued(attachment)
    result = attachment.flush(event, token)
    assert result.state == "saved"
    assert result.receipt.message_ids == (2,)
    assert attachment.refresh_snapshot(token).messages[-1].content == "Final ordinary dialogue"
    assert box.diagnostics()["pending_bytes"] == 0
    attachment.close(token)
    assert attachment.snapshot is None
    with pytest.raises(HistoryError, match="stale_generation"):
        attachment.enqueue(
            token,
            (HistoryMessage("user", "late"),),
            origin_turn_id="late",
            finalized=True,
            disposition="dialogue",
        )
    assert {op for op, _, _ in host.calls} <= {*BODY_KEYS, "capabilities"}


def test_response_loss_restart_reconcile_exactly_once(host, tmp_path):
    attachment, box = client(host, tmp_path)
    attachment.attach()
    event, token = queued(attachment)
    host.lose_commit = True
    assert attachment.flush(event, token).state == "pending"
    assert len(host.rows) == 2
    # Both local process and host attachment epoch disappear; receipts survive.
    host.attachments.clear()
    restarted, _ = client(host, tmp_path)
    new_token = restarted.attach()
    assert restarted.flush(event, new_token).state == "saved"
    assert len(host.rows) == 2
    assert [op for op, _, _ in host.calls].count("commit") == 1
    assert [op for op, _, _ in host.calls][-3:] == ["capabilities", "reconcile", "attach"]
    assert box.get(restarted.owner, event).messages == ()


def test_busy_retry_unknown_reattaches_original_identity(host, tmp_path):
    attachment, box = client(host, tmp_path)
    attachment.attach()
    event, token = queued(attachment)
    host.busy = True
    assert attachment.flush(event, token).code == "busy"
    first = next(body for op, body, _ in host.calls if op == "commit")
    host.busy = False
    assert attachment.flush(event, token).state == "saved"
    ops = [op for op, _, _ in host.calls]
    assert ops[-3:] == ["reconcile", "attach", "commit"]
    last = host.saved_bodies[event]
    for key in ("event_id", "origin_turn_id", "session_id", "messages", "tab_id"):
        assert first[key] == last[key]
    assert last["attachment_id"] != first["attachment_id"]
    assert attachment.capture_token.generation > token.generation
    assert box.get(attachment.owner, event).attempts == 2


def test_new_selection_cannot_retarget_pending_event(host, tmp_path):
    old, box = client(host, tmp_path)
    token = old.attach()
    event, _ = queued(old)
    newer, _ = client(host, tmp_path, selected="other")
    new_token = newer.attach()
    before = len(host.calls)
    with pytest.raises(HistoryError, match="owner_mismatch"):
        newer.flush(event, new_token)
    with pytest.raises(HistoryError, match="stale_generation"):
        old.flush(event, token)
    assert len(host.calls) == before
    assert box.pending(old.owner) == (event,)
    recovered_token = old.attach()
    assert old.flush(event, recovered_token).state == "saved"
    assert host.saved_bodies[event]["session_id"] == "selected"


def test_late_attach_response_cannot_replace_newer_generation(host, tmp_path):
    old, _ = client(host, tmp_path)
    newer, _ = client(host, tmp_path)
    entered, release = threading.Event(), threading.Event()
    first = True

    def delay(operation):
        nonlocal first
        if operation == "attach" and first:
            first = False
            entered.set()
            assert release.wait(5)

    host.before = delay
    with ThreadPoolExecutor() as pool:
        future = pool.submit(old.attach)
        assert entered.wait(5)
        token = newer.attach()
        release.set()
        with pytest.raises(HistoryError, match="stale_generation"):
            future.result(timeout=5)
    with pytest.raises(HistoryError, match="stale_generation"):
        _ = old.snapshot
    # A late host attach can invalidate the newer remote lease. It must refuse,
    # never apply old context; reconnect explicitly obtains current authority.
    with pytest.raises(HistoryError, match="stale_attachment"):
        newer.refresh_snapshot(token)
    assert newer.snapshot is None
    assert newer.attach() != token


@pytest.mark.parametrize(
    "surface,profile,named",
    [
        ("gateway", "default", False),
        ("gateway", "work", True),
        ("dashboard", "work", False),
    ],
)
def test_fixed_profile_transport_routes(host, tmp_path, surface, profile, named):
    attachment, _ = client(host, tmp_path, surface=surface, profile=profile, named_profile=named)
    attachment.attach()
    event, token = queued(attachment)
    assert attachment.flush(event, token).state == "saved"
    assert {scope for _, _, scope in host.calls} == {profile}


def test_credential_and_profile_ownership_are_immutable(host, tmp_path):
    attachment, box = client(host, tmp_path)
    attachment.attach()
    event, _ = queued(attachment)
    transport = HistoryTransport(host.url, "default", "second-key")
    foreign = TalkAttachment(transport, box, selected_session="selected")
    token = foreign.attach()
    before = len(host.calls)
    with pytest.raises(HistoryError, match="owner_mismatch"):
        foreign.flush(event, token)
    assert len(host.calls) == before
    with pytest.raises(FrozenInstanceError):
        transport.credential = "mutated"
    with pytest.raises(FrozenInstanceError):
        attachment.owner.session_id = "other"
    with pytest.raises(HistoryError, match="owner_mismatch"):
        HistoryOutbox(tmp_path, profile="other")


@pytest.mark.parametrize(
    "mutation",
    [
        {"version": 2},
        {"version": True},
        {"passive_only": False},
        {"operations": ["attach"]},
        {"max_snapshot_bytes": 999999},
        {"origin_adoption": None},
    ],
)
def test_missing_or_incompatible_capability_never_attaches(host, tmp_path, mutation):
    host.caps.update(mutation)
    attachment, _ = client(host, tmp_path)
    with pytest.raises(HistoryError, match="unsupported"):
        attachment.attach()
    assert [op for op, _, _ in host.calls] == ["capabilities"]


def test_additive_host_capabilities_do_not_enable_execution(host, tmp_path):
    host.caps = {
        **CAPS,
        "origin_adoption": True,
        "origin_adoption_sources": ["api_runs"],
        "operations": [*CAPS["operations"], "adopt"],
    }
    attachment, _ = client(host, tmp_path)
    token = attachment.attach()
    with pytest.raises(HistoryError, match="not_passive"):
        attachment.enqueue(
            token,
            (HistoryMessage("user", "Do work"),),
            origin_turn_id="work",
            finalized=True,
            disposition="execution",
        )
    assert [op for op, _, _ in host.calls] == ["capabilities", "attach"]


@pytest.mark.parametrize(
    "finalized,disposition,role",
    [
        (False, "dialogue", "user"),
        (True, "fragment", "user"),
        (True, "tool_result", "assistant"),
        (True, "execution", "user"),
        (True, "steering", "user"),
        (True, "dialogue", "tool"),
    ],
)
def test_nonpassive_input_never_enters_outbox_or_http(host, tmp_path, finalized, disposition, role):
    attachment, box = client(host, tmp_path)
    token = attachment.attach()
    before = len(host.calls)
    with pytest.raises(HistoryError, match="not_passive"):
        attachment.enqueue(
            token,
            (HistoryMessage(role, "private text"),),
            origin_turn_id="origin",
            finalized=finalized,
            disposition=disposition,
        )
    assert box.diagnostics()["states"]["pending"] == 0
    assert len(host.calls) == before


@pytest.mark.parametrize(
    "code,status", [("target_missing", 404), ("retired", 410), ("target_unavailable", 409)]
)
def test_deletion_retirement_clear_derived_references(host, tmp_path, code, status):
    attachment, box = client(host, tmp_path)
    token = attachment.attach()
    event, _ = queued(attachment, text="erased-utterance-123")
    host.override["commit"] = host.error(code, status)
    result = attachment.flush(event, token)
    assert (result.state, result.code) == ("failed", code)
    assert attachment.snapshot is None
    assert box.diagnostics()["pending_bytes"] == 0
    with pytest.raises(HistoryError, match=code):
        box.get(attachment.owner, event)
    raw = (tmp_path / "state" / "talk-history-outbox.sqlite3").read_bytes()
    assert b"erased-utterance-123" not in raw
    assert b'"session_id": "selected"' not in raw


def test_message_retirement_after_lost_response_cannot_reinsert(host, tmp_path):
    attachment, box = client(host, tmp_path)
    token = attachment.attach()
    event, _ = queued(attachment)
    host.lose_commit = True
    attachment.flush(event, token)
    host.retired = True
    assert attachment.flush(event, token).code == "retired"
    assert len([op for op, _, _ in host.calls if op == "commit"]) == 1
    assert box.diagnostics()["pending_bytes"] == 0


@pytest.mark.parametrize(
    "response",
    [
        {"status": "saved", "profile": "default", "receipt": {}},
        {"status": "saved", "profile": "other", "receipt": {}},
        {"secret-key": "private text"},
        [],
    ],
)
def test_malformed_commit_response_stays_reconcilable_and_redacted(host, tmp_path, response):
    attachment, box = client(host, tmp_path)
    token = attachment.attach()
    event, _ = queued(attachment, text="private text")
    host.override["commit"] = (200, response)
    result = attachment.flush(event, token)
    assert (result.state, result.code) == ("pending", "malformed_response")
    assert attachment.snapshot is None
    assert box.pending(attachment.owner) == (event,)
    diag = json.dumps(attachment.diagnostics()) + repr(result)
    assert all(
        secret not in diag for secret in ("secret-key", "private text", str(tmp_path), host.url)
    )


def test_malformed_snapshot_and_missing_host_fail_closed(host, tmp_path):
    attachment, _ = client(host, tmp_path)
    host.override["capabilities"] = (404, {"error": "missing secret-key"})
    with pytest.raises(HistoryError, match="unsupported"):
        attachment.attach()
    host.override.clear()
    token = attachment.attach()
    malformed = {
        **host.snapshot(),
        "profile": "default",
        "messages": [{"id": True, "role": "system", "content": "private text"}],
    }
    host.override["snapshot"] = (200, malformed)
    with pytest.raises(HistoryError, match="malformed_response"):
        attachment.refresh_snapshot(token)
    assert attachment.snapshot is None


def test_network_failure_and_untrusted_error_text_are_safe():
    def fail(request):
        raise httpx.ConnectError("credential=secret-key /private/path", request=request)

    transport = HistoryTransport(
        "http://127.0.0.1:1", "default", "secret-key", _http_transport=httpx.MockTransport(fail)
    )
    with pytest.raises(HistoryError, match=r"^unavailable$") as error:
        transport.request("capabilities")
    assert error.value.retryable
    bad = replace(
        transport,
        _http_transport=httpx.MockTransport(
            lambda _: httpx.Response(
                409, json={"error": "credential=secret-key /private/path", "retryable": True}
            )
        ),
    )
    with pytest.raises(HistoryError, match=r"^malformed_response$"):
        bad.request("commit", {})


def test_outbox_count_bytes_ttl_and_generation_survive_reopen(host, tmp_path):
    now = [1000.0]
    transport = HistoryTransport(host.url, "default", "secret-key")
    box = HistoryOutbox(
        tmp_path, profile="default", max_events=1, max_bytes=100, ttl_s=10, clock=lambda: now[0]
    )
    attachment = TalkAttachment(transport, box, selected_session="selected")
    token = attachment.attach()
    event, _ = queued(attachment, text="short")
    with pytest.raises(HistoryError, match="outbox_full"):
        queued(attachment, event_id="second")
    now[0] += 11
    with pytest.raises(HistoryError, match="expired"):
        box.get(attachment.owner, event)
    assert b"short" not in (tmp_path / "state" / "talk-history-outbox.sqlite3").read_bytes()
    assert box.diagnostics()["states"]["failed"] == 1
    assert box.diagnostics()["pending_bytes"] == 0
    with pytest.raises(HistoryError, match="expired"):
        box.get(attachment.owner, event)
    # Pruned connection rows cannot recycle a generation and revive an old callback.
    fresh = attachment.attach()
    assert fresh.generation > token.generation
    with pytest.raises(HistoryError, match="stale_generation"):
        attachment.flush(event, token)
    with pytest.raises(HistoryError, match="outbox_full"):
        queued(attachment, text="x" * 101, event_id="too-large")
    queued(attachment, text="fits", event_id="new")
    reopened = HistoryOutbox(tmp_path, profile="default", clock=lambda: now[0])
    assert reopened.pending(attachment.owner) == ("new",)


def test_event_identity_cannot_be_reused_with_new_payload(host, tmp_path):
    attachment, box = client(host, tmp_path)
    attachment.attach()
    event, _ = queued(attachment)
    assert queued(attachment)[0] == event
    with pytest.raises(HistoryError, match="event_conflict"):
        queued(attachment, text="changed")
    assert box.diagnostics()["states"]["pending"] == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.com",
        "https://example.com/?credential=secret",
        "https://example.com/#fragment",
        "file:///private/path",
        "https://example.com/chat",
    ],
)
def test_arbitrary_endpoints_are_not_accepted(url):
    with pytest.raises(HistoryError, match="invalid_input"):
        HistoryTransport(url, "default", "secret-key")


def test_no_arbitrary_operation_or_redirects():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://external.invalid/steal"}, json={})

    transport = HistoryTransport(
        "https://configured.invalid",
        "default",
        "secret-key",
        _http_transport=httpx.MockTransport(respond),
    )
    with pytest.raises(HistoryError, match="unsupported"):
        transport.request("../../chat", {})
    assert not calls
    with pytest.raises(HistoryError, match="malformed_response"):
        transport.request("capabilities")
    assert calls == ["https://configured.invalid/v1/passive-history/capabilities"]


def test_oversized_response_and_auth_failure_are_bounded():
    for status, body, expected in [
        (200, "x" * 300000, "malformed_response"),
        (401, "secret-key private text", "unauthorized"),
    ]:
        transport = HistoryTransport(
            "https://configured.invalid",
            "default",
            "secret-key",
            _http_transport=httpx.MockTransport(
                lambda request, status=status, body=body: httpx.Response(status, text=body)
            ),
        )
        with pytest.raises(HistoryError, match=expected):
            transport.request("capabilities")


def test_late_commit_response_remains_pending_for_new_generation(host, tmp_path):
    old, box = client(host, tmp_path)
    token = old.attach()
    event, _ = queued(old)
    entered, release = threading.Event(), threading.Event()

    def delay(operation):
        if operation == "commit":
            entered.set()
            assert release.wait(5)

    host.before = delay
    with ThreadPoolExecutor() as pool:
        future = pool.submit(old.flush, event, token)
        assert entered.wait(5)
        # Another process revokes this callback while the HTTP request is in flight.
        box.begin(old.owner, token.connection_id)
        release.set()
        with pytest.raises(HistoryError, match="stale_generation"):
            future.result(timeout=5)
    assert box.pending(old.owner) == (event,)
    fresh = old.attach()
    assert old.flush(event, fresh).state == "saved"
    assert len(host.rows) == 2


def test_corrupt_outbox_never_submits_modified_payload(host, tmp_path):
    attachment, _ = client(host, tmp_path)
    token = attachment.attach()
    event, _ = queued(attachment)
    before = len(host.calls)
    with sqlite3.connect(tmp_path / "state" / "talk-history-outbox.sqlite3") as db:
        db.execute(
            "UPDATE events SET messages=? WHERE event_id=?",
            (json.dumps([{"role": "user", "content": "corrupted"}]), event),
        )
    with pytest.raises(HistoryError, match="outbox_unavailable"):
        attachment.flush(event, token)
    assert len(host.calls) == before
    with pytest.raises(HistoryError, match="outbox_unavailable"):
        HistoryOutbox(tmp_path / "state" / "talk-history-outbox.sqlite3", profile="default")


def test_capacity_serializes_independent_connections(host, tmp_path):
    transport = HistoryTransport(host.url, "default", "secret-key")
    one = HistoryOutbox(tmp_path, profile="default", max_events=1)
    two = HistoryOutbox(tmp_path, profile="default", max_events=1)
    clients = [TalkAttachment(transport, box, selected_session="selected") for box in (one, two)]
    for attachment in clients:
        attachment.attach()

    def admit(index):
        try:
            queued(clients[index], event_id=f"event-{index}")
            return "pending"
        except HistoryError as exc:
            return exc.code

    with ThreadPoolExecutor() as pool:
        assert sorted(pool.map(admit, range(2))) == ["outbox_full", "pending"]
    assert one.diagnostics()["states"]["pending"] == 1


def test_host_conflict_is_terminal_and_scrubs_text(host, tmp_path):
    attachment, box = client(host, tmp_path)
    token = attachment.attach()
    event, _ = queued(attachment)
    host.override["commit"] = host.error("event_conflict")
    result = attachment.flush(event, token)
    assert (result.state, result.code) == ("conflicted", "event_conflict")
    assert box.diagnostics()["pending_bytes"] == 0


@pytest.mark.parametrize("during_reconnect", [False, True])
def test_retired_event_preserves_unrelated_pending_dialogue(host, tmp_path, during_reconnect):
    attachment, box = client(host, tmp_path)
    token = attachment.attach()
    event_a, _ = queued(attachment, event_id="event-a", text="retired A")
    host.lose_commit = True
    assert attachment.flush(event_a, token).state == "pending"
    event_b, _ = queued(attachment, event_id="event-b", text="unsaved B")
    host.retired = True
    if during_reconnect:
        token = attachment.attach()
    else:
        assert attachment.flush(event_a, token).code == "retired"
        assert attachment.snapshot is None
    assert box.pending(attachment.owner) == (event_b,)
    assert box.get(attachment.owner, event_b).messages[0].content == "unsaved B"
    assert attachment.flush(event_b, attachment.attach()).state == "saved"
    assert [row["content"] for row in host.rows].count("unsaved B") == 1


def test_retry_reattach_cas_cannot_reclaim_newer_connection(host, tmp_path, monkeypatch):
    old, box = client(host, tmp_path)
    token = old.attach()
    event, _ = queued(old)
    host.busy = True
    assert old.flush(event, token).code == "busy"
    host.busy = False
    entered, release = threading.Event(), threading.Event()
    begin = box.begin

    def delayed_begin(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return begin(*args, **kwargs)

    monkeypatch.setattr(box, "begin", delayed_begin)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(old.flush, event, token)
        assert entered.wait(5)
        newer, _ = client(host, tmp_path)
        fresh = newer.attach()
        calls_before_release = len(host.calls)
        release.set()
        with pytest.raises(HistoryError, match="stale_generation"):
            future.result(timeout=5)
    assert len(host.calls) == calls_before_release
    assert newer.refresh_snapshot(fresh).conversation_id == "root"
    assert newer.flush(event, fresh).state == "saved"


def test_callback_invalidation_cas_rejects_late_writer(host, tmp_path, monkeypatch):
    old, box = client(host, tmp_path)
    token = old.attach()
    event, _ = queued(old)
    host.override["commit"] = host.error("target_missing", 404)
    entered, release = threading.Event(), threading.Event()
    invalidate = box.invalidate

    def delayed_invalidate(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return invalidate(*args, **kwargs)

    monkeypatch.setattr(box, "invalidate", delayed_invalidate)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(old.flush, event, token)
        assert entered.wait(5)
        newer, _ = client(host, tmp_path)
        fresh = newer.attach()
        release.set()
        with pytest.raises(HistoryError, match="stale_generation"):
            future.result(timeout=5)
    assert box.pending(newer.owner) == (event,)
    assert newer.refresh_snapshot(fresh).conversation_id == "root"
    host.override.clear()
    assert newer.flush(event, fresh).state == "saved"


@pytest.mark.parametrize("code,status", [("retired", 410), ("target_missing", 404)])
def test_stale_terminal_error_cannot_invalidate_new_generation(host, tmp_path, code, status):
    old, box = client(host, tmp_path)
    token = old.attach()
    event_a, _ = queued(old)
    entered, release = threading.Event(), threading.Event()

    def delay(operation):
        if operation == "commit":
            entered.set()
            assert release.wait(5)

    host.before = delay
    host.override["commit"] = host.error(code, status)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(old.flush, event_a, token)
        assert entered.wait(5)
        newer, _ = client(host, tmp_path)
        fresh = newer.attach()
        event_b, _ = queued(newer, event_id="event-b", text="new generation B")
        release.set()
        with pytest.raises(HistoryError, match="stale_generation"):
            future.result(timeout=5)
    assert box.pending(newer.owner) == (event_a, event_b)
    assert newer.refresh_snapshot(fresh).conversation_id == "root"
