"""Call updates through Hermes's existing, conversation-bound message stream."""

import threading
import time
from contextlib import suppress
from contextvars import copy_context


def native_canary(ctx):
    """Exercise the real host message/edit path without a provider call or model turn."""
    post = getattr(ctx, "post_message", None)
    if not callable(post):
        return {"native_notice_accepted": False, "call_placed": False}
    handle = post("Hermes Talk Phone: checking this conversation's native message stream.")
    if not getattr(handle, "ok", False):
        return {"native_notice_accepted": False, "call_placed": False}
    finished = threading.Event()
    outcome = {}

    def edit():
        try:
            outcome.update(
                handle.edit(
                    "Hermes Talk Phone: native message delivery passed in this conversation. "
                    "This was a no-dial diagnostic."
                )
            )
        finally:
            finished.set()

    threading.Thread(target=edit, name="talk-phone-notice-check", daemon=True).start()
    finished.wait(2)
    return {
        "native_notice_accepted": True,
        "native_notice_edited": bool(outcome.get("ok")) if finished.is_set() else None,
        "call_placed": False,
    }


def input_canary(ctx):
    """Exercise the same-session input queue without making a phone call."""
    inject = getattr(ctx, "inject_message", None)
    accepted = callable(inject) and inject(
        "Hermes Talk Phone no-dial input check: using this conversation's history, "
        "name the earlier two-word call-test phrase. Do not dial or use phone tools."
    )
    return {"session_input_accepted": bool(accepted), "call_placed": False}


def pending_text(store, session, call_id):
    rows = [
        r
        for r in store.inbox(session)
        if r["call_id"] == call_id and not r["expired"] and r["state"] == "pending"
    ]
    if not rows:
        return None
    lines = ["Hermes Talk Phone: input in this conversation."]
    for row in rows:
        lines.append(f"{row['kind']}: {row['content']}")
        if row["kind"] == "question":
            lines.append(
                f"Answer in this conversation: /phone answer {row['request_id']} YOUR ANSWER"
            )
        else:
            lines.append(f"Review in this conversation: /phone inbox (request {row['request_id']})")
    return "\n".join(lines)[:3800]


def watch_call(ctx, store, session, call_id, *, interval=0.3):
    """Continue the owning session through the normal Hermes input queue."""
    post = getattr(ctx, "post_message", None)
    if not callable(post):
        return False
    try:
        row = store.get(call_id, session)
        handle = post(
            f"Hermes Talk Phone: call active ({call_id}). "
            "Connected to this conversation's memory and tools."
        )
        if not getattr(handle, "ok", False):
            return False
        deadline = row["started_at"] + row["draft"]["max_duration_seconds"]
    except Exception:  # noqa: BLE001 - external boundary; keep host/provider details private
        return False

    route_key = session_route()
    context = copy_context()

    def run():
        previous = None
        while store.clock() < deadline:
            try:
                if store.get(call_id, session)["status"] != "started":
                    break
                text = pending_text(store, session, call_id)
                if text and text != previous:
                    result = handle.edit(text)
                    if not result.get("ok"):
                        return
                    previous = text
                inject = getattr(ctx, "inject_message", None)
                if callable(inject):
                    for item in store.inbox(session):
                        if item["call_id"] != call_id or item["expired"]:
                            continue
                        claimed = store.claim_turn(item["request_id"], session)
                        if not claimed:
                            continue
                        message = (
                            "[Hermes Talk Phone input — same conversation]\n"
                            + claimed["content"]
                            + "\nUse this conversation's existing memory, tools and permissions. "
                            "Reply naturally, then send the spoken answer using phone_reply "
                            f"with request_id={claimed['id']}. No separate Phone approval or "
                            "pairing step is needed."
                        )
                        accepted = inject(
                            message, **({"session_key": route_key} if route_key else {})
                        )
                        if not accepted:
                            store.dispatch_failed(claimed["id"], session)
                            handle.edit(
                                "Hermes Talk Phone: this conversation could not accept "
                                "the phone input. Check Phone setup."
                            )
            except Exception:  # noqa: BLE001 - external boundary; keep host/provider details private
                return
            time.sleep(interval)
        with suppress(Exception):
            handle.edit(
                "Hermes Talk Phone: call window ended. Use phone_status for the carrier result."
            )

    threading.Thread(
        target=context.run, args=(run,), name="talk-phone-notices", daemon=True
    ).start()
    return True


def session_route():
    from gateway.session_context import get_session_env

    return get_session_env("HERMES_SESSION_KEY", "")
