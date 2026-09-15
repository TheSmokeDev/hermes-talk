"""Private transcript spooling survives disposal and cannot cross canonical owners."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest
from test_native_live import close
from test_native_live_recovery import controller_for, transcript

from talk_native_api import NativeTaskError
from talk_native_capture_store import NativeCaptureStore


def own(handle, store, *, connection="original", generation=1, **changes):
    control = handle.controller
    control.api.context = {"connection_id": connection, "generation": generation}
    control.context = dict(control.api.context)
    control.attachment["task"] = {
        **control.context,
        "target_id": "opaque-principal-bound-target",
        "session_id": "task-session",
        "profile": "default",
        "peer_id": "local",
        **changes,
    }
    control.capture_store = store
    control.CAPTURE_RETRIES = (0, 0)
    return control._capture_owner()


def test_failed_close_has_durable_receipt_and_reconnect_replays_only_original_capture(tmp_path):
    async def scenario():
        failed_requests, replayed = [], []

        async def unavailable(request):
            failed_requests.append(json.loads(request.content))
            return httpx.Response(503, json={"error": "busy"})

        path = tmp_path / "private" / "capture.sqlite3"
        first = await controller_for(unavailable)
        owner = own(first, NativeCaptureStore(path))
        await first.controller.handle(transcript(" Keep  exactly these words ", "source-event"))
        with pytest.raises(NativeTaskError):
            await first.controller.flush_captures()
        await close(first)
        assert any(row.get("receipt") == NativeCaptureStore.receipt(owner) for row in first.notices)
        store = NativeCaptureStore(path)
        old_session, pending = store.pending(owner)
        assert len(pending) == 1

        async def healthy(request):
            replayed.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"ok": True, "captured": 1})

        second = await controller_for(healthy)
        own(second, store, connection="fresh-binding", generation=2)
        second.controller.provider_session_id = "different-provider-session"
        await second.controller.reconcile_captures()
        assert len(replayed) == 1 and replayed[0][0].endswith("/live/transcript")
        body = replayed[0][1]
        assert body["provider_session_id"] == old_session
        assert body["fragments"] == pending == failed_requests[0]["fragments"]
        assert body["connection_id"] == "fresh-binding" and body["generation"] == 2
        assert store.pending(owner) == (None, [])
        await close(second)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changed",
    [
        {"target_id": "other-principal"},
        {"session_id": "other-session"},
        {"profile": "other-profile"},
        {"peer_id": "other-host"},
    ],
)
def test_reconnect_never_migrates_capture_to_another_canonical_owner(tmp_path, changed):
    async def scenario():
        writes = []

        async def handler(request):
            writes.append(request)
            return httpx.Response(200, json={"ok": True})

        h = await controller_for(handler)
        store = NativeCaptureStore(tmp_path / "capture.sqlite3")
        owner = own(h, store)
        fragment = {
            "event_id": "one",
            "role": "user",
            "text": "Original private words",
            "final": False,
        }
        store.put(owner, h.controller.context, "old-session", [fragment])
        own(h, store, connection="fresh", generation=2, **changed)
        await h.controller.reconcile_captures()
        assert not writes and store.pending(owner)[1] == [fragment]
        await close(h)

    asyncio.run(scenario())


def test_spool_rejects_overflow_atomically_and_preserves_exact_identity(tmp_path):
    store = NativeCaptureStore(tmp_path / "capture.sqlite3", max_fragments=1)
    owner, context = "exact-owner", {"connection_id": "original", "generation": 1}
    original = {"event_id": "one", "role": "user", "text": " space ", "final": False}
    store.put(owner, context, "session", [original])
    store.put(owner, context, "session", [original])
    with pytest.raises(NativeTaskError, match="queue is full"):
        store.put(owner, context, "session", [{**original, "event_id": "two"}])
    with pytest.raises(NativeTaskError, match="identity changed"):
        store.put(owner, context, "session", [{**original, "text": "changed"}])
    assert store.pending(owner)[1] == [original]
    store.acknowledge(owner, "session", [{**original, "text": "changed"}])
    assert store.pending(owner)[1] == [original]
    store.acknowledge(owner, "session", [original])
    assert store.pending(owner) == (None, [])


def test_spool_persists_only_allowed_source_fields_and_never_headers(tmp_path):
    store = NativeCaptureStore(tmp_path / "capture.sqlite3")
    task = {
        "target_id": "target",
        "session_id": "session",
        "profile": "default",
        "peer_id": "local",
    }
    owner = store.owner(
        "http://127.0.0.1",
        {
            "task": task,
            "surface_context": {
                "surface": "discord",
                "operator_user_id": "operator",
                "surface_token": "private-secret",
            },
        },
    )
    assert "private-secret" not in owner
    context = {"connection_id": "source", "generation": 1}
    fragment = {"event_id": "one", "role": "user", "text": " exact ", "final": False}
    with pytest.raises(NativeTaskError):
        store.put(owner, {**context, "Authorization": "private-secret"}, "provider", [fragment])
    with pytest.raises(NativeTaskError):
        store.put(owner, context, "provider", [{**fragment, "access_token": "private-secret"}])
    store.put(owner, context, "provider", [fragment])
    with sqlite3.connect(store.path) as db:
        row = db.execute("SELECT source_context,fragment FROM captures").fetchone()
    assert json.loads(row[0]) == context and json.loads(row[1]) == fragment
    assert b"private-secret" not in store.path.read_bytes()


def test_failed_close_spools_fragments_queued_after_capture_retry_exhaustion(tmp_path):
    async def scenario():
        async def handler(request):
            return httpx.Response(503, json={"error": "busy"})

        h = await controller_for(handler)
        store = NativeCaptureStore(tmp_path / "capture.sqlite3")
        owner = own(h, store)
        await h.controller.handle(transcript("first ", "first"))
        with pytest.raises(NativeTaskError):
            await h.controller.flush_captures()
        for index in range(70):
            await h.controller.handle(transcript("more ", f"more-{index}"))
        await close(h)
        with sqlite3.connect(store.path) as db:
            count = db.execute("SELECT count(*) FROM captures WHERE owner=?", (owner,)).fetchone()[
                0
            ]
        assert count == 71

    asyncio.run(scenario())
