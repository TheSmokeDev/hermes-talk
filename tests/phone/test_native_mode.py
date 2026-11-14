import json
import threading
from types import SimpleNamespace

import pytest

import talk_host
import talk_operator_auth
import talk_tools
from talk_phone import plugin
from talk_phone.bridge_store import BridgeStore
from talk_phone.context import PhoneContext
from talk_phone.notifications import native_canary, pending_text, watch_call

from .conftest import OWNER, SESSION, prepared
from .test_plugin import Context


def test_phone_tools_are_hidden_without_configuration():
    ctx = Context()
    ctx.get_config = lambda key, default=None: default
    plugin.register(ctx, include_cli=False)
    assert len(ctx.tools) == 9
    assert all(not tool["check_fn"]() for tool in ctx.tools.values())
    assert "dograh" not in ctx.commands and "dograh-bridge" not in ctx.commands


def test_phone_settings_and_voice_tools_use_the_talk_context(monkeypatch):
    ctx = Context()
    wrapped = PhoneContext(ctx)
    assert wrapped.get_config("enabled", False) is True
    monkeypatch.setattr(talk_host, "get_ctx", lambda: ctx)
    names = {t["name"] for t in talk_tools.default_talk_tools()}
    phone_names = {t.name for t in plugin.TOOLS}
    assert phone_names <= names
    assert phone_names <= talk_operator_auth.MUTATING_TALK_TOOLS
    assert all(talk_operator_auth.classify_host_tool(n) == "permit" for n in phone_names)


@pytest.mark.parametrize("choice", [None, "cancel"])
def test_native_review_cancel_does_not_approve_or_dial(setup_phone, monkeypatch, choice):
    api, provider, _ = setup_phone
    monkeypatch.setattr(plugin, "service", lambda ctx: api)
    ctx = Context()
    ctx.ask_choice = lambda *args: choice
    call_id = prepared(api)
    result = json.loads(
        plugin.command(
            ctx,
            f"review {call_id}",
            session_id=SESSION,
            platform="local",
            source=SimpleNamespace(user_id="synthetic-owner"),
        )
    )
    assert result["status"] == ("cancelled" if choice == "cancel" else "not_approved")
    assert not provider.posts
    assert api.store.get(call_id, SESSION)["status"] == (
        "cancelled" if choice == "cancel" else "prepared"
    )


def test_native_review_grants_one_use_permission_without_dialing(setup_phone, monkeypatch):
    api, provider, _ = setup_phone
    monkeypatch.setattr(plugin, "service", lambda ctx: api)
    ctx = Context()
    choices = []
    ctx.ask_choice = lambda question, options: choices.append((question, options)) or "approve"
    call_id = prepared(api)
    result = json.loads(
        plugin.command(
            ctx,
            f"review {call_id}",
            session_id=SESSION,
            platform="local",
            source=SimpleNamespace(user_id="synthetic-owner"),
        )
    )
    assert result["status"] == "approved" and not provider.posts
    assert result["call_placed"] is False
    again = json.loads(
        plugin.command(
            ctx,
            f"review {call_id}",
            session_id=SESSION,
            platform="local",
            source=SimpleNamespace(user_id="synthetic-owner"),
        )
    )
    assert "error" in again and not provider.posts
    assert api.store.get(call_id, SESSION)["approved_by"] == OWNER
    assert choices and "Carrier/model charges" in choices[0][0]
    api.call(SESSION, call_id)
    assert len(provider.posts) == 1


def test_late_native_confirmation_cannot_grant_permission(setup_phone, monkeypatch):
    api, provider, _ = setup_phone
    monkeypatch.setattr(plugin, "service", lambda ctx: api)
    clock = iter([0, 26])
    monkeypatch.setattr(plugin, "monotonic", lambda: next(clock))
    ctx = Context()
    ctx.ask_choice = lambda *args: "approve"
    call_id = prepared(api)
    result = json.loads(
        plugin.command(
            ctx,
            f"review {call_id}",
            session_id=SESSION,
            platform="local",
            source=SimpleNamespace(user_id="synthetic-owner"),
        )
    )
    assert result["status"] == "review_expired" and not provider.posts
    assert api.store.get(call_id, SESSION)["status"] == "prepared"


def test_same_conversation_notice_and_owner_answer_return(setup_phone, tmp_path, monkeypatch):
    api, _, now = setup_phone
    api.store = BridgeStore(tmp_path / "phone.db", clock=lambda: now[0])
    call_id = prepared(api)
    api.approve(SESSION, call_id, OWNER)
    api.call(SESSION, call_id)
    connection = api.store.connect("real-call-transport")
    api.store.pair(connection["code"], call_id, SESSION)
    request_id = api.store.enqueue(
        connection["connection"], "question-1", "question", "Which test phrase?"
    )
    received = threading.Event()
    updates = []

    class NativeHandle:
        ok = True

        def edit(self, text):
            updates.append(text)
            received.set()
            return {"ok": True}

    class NativeContext(Context):
        def post_message(self, text):
            self.initial = text
            return NativeHandle()

    ctx = NativeContext()
    assert watch_call(ctx, api.store, SESSION, call_id, interval=0.01)
    assert received.wait(2)
    assert "Which test phrase?" in updates[0] and request_id in updates[0]
    monkeypatch.setattr(plugin, "service", lambda ctx: api)
    response = json.loads(
        plugin.command(
            ctx,
            f"answer {request_id} amber river",
            session_id=SESSION,
            platform="local",
            source=SimpleNamespace(user_id="synthetic-owner"),
        )
    )
    assert response["shared_with_caller"]
    assert (
        api.store.remote_result(connection["connection"], request_id)["owner_answer"]
        == "amber river"
    )
    assert pending_text(api.store, SESSION, call_id) is None
    assert not watch_call(ctx, api.store, "different-session", call_id)
    api.store.finish(call_id, SESSION, "completed")


def test_refused_native_message_never_chooses_another_chat(setup_phone):
    api, _, _ = setup_phone
    call_id = prepared(api)
    api.approve(SESSION, call_id, OWNER)
    api.call(SESSION, call_id)
    ctx = SimpleNamespace(post_message=lambda text: SimpleNamespace(ok=False))
    assert watch_call(ctx, api.store, SESSION, call_id) is False


def test_no_dial_canary_edits_only_the_bound_native_handle():
    messages = []

    class Handle:
        ok = True

        def edit(self, text):
            messages.append(text)
            return {"ok": True}

    ctx = SimpleNamespace(post_message=lambda text: Handle())
    result = native_canary(ctx)
    assert result == {
        "native_notice_accepted": True,
        "native_notice_edited": True,
        "call_placed": False,
    }
    assert len(messages) == 1 and "no-dial diagnostic" in messages[0]
