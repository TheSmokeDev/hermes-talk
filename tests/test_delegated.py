"""The opt-in same-chat route never falls back to detached work."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_operator_auth import OPERATOR_ID, OTHER_ID, _bind_response, _pcm, _speaker

import talk_delegated
import talk_operator_auth


def binding(ctx):
    return talk_delegated.Binding(
        ctx, "discord:chat", "session", Path("/unused"), str(OPERATOR_ID), "chat", "nonce"
    )


def event(call_id="one", name="submit_task", **args):
    return dict(call_id=call_id, name=name, arguments=json.dumps(args or {"task": "Run the tests"}))


def result(commands):
    return json.loads(commands[0].output)


def test_submission_uses_captured_identity_and_stable_request_id():
    calls = []

    def submit(content, **identity):
        calls.append((content, identity))
        return {"ok": True, "status": "queued", "request_id": identity["request_id"]}

    relay = talk_delegated.SubmissionRelay(
        binding(SimpleNamespace(submit_message=submit)), lambda *_: None
    )
    first = asyncio.run(relay.handle_tool_call_async(event()))
    second = asyncio.run(relay.handle_tool_call_async(event()))
    assert result(first) == result(second)
    assert calls[0] == calls[1]
    assert calls[0][1]["session_key"] == "discord:chat"
    assert calls[0][1]["expected_session_id"] == "session"
    changed = asyncio.run(relay.handle_tool_call_async(event(task="Delete everything")))
    assert not result(changed)["ok"]
    assert len(calls) == 2


def test_refusal_and_unknown_status_do_not_trigger_another_backend():
    def refuse(*_, **identity):
        return {"ok": False, "status": "refused", "error": "permission_denied"}

    relay = talk_delegated.SubmissionRelay(
        binding(SimpleNamespace(submit_message=refuse)), lambda *_: None
    )
    assert (
        result(asyncio.run(relay.handle_tool_call_async(event())))["error"] == "permission_denied"
    )
    unknown = event(name="submission_status", request_id="foreign-request")
    assert not result(asyncio.run(relay.handle_tool_call_async(unknown)))["ok"]


def test_authorization_refusal_never_submits():
    relay = talk_delegated.SubmissionRelay(binding(object()), lambda *_: "wrong speaker")
    assert result(asyncio.run(relay.handle_tool_call_async(event())))["error"] == "wrong speaker"


@pytest.mark.parametrize("speaker,allowed", [(OPERATOR_ID, True), (OTHER_ID, False), (None, False)])
def test_only_attributed_joining_user_can_submit(speaker, allowed):
    ledger = talk_operator_auth.DiscordToolAuthorizationLedger(initiating_user_id=str(OPERATOR_ID))
    ledger.record_packet(_speaker(speaker), _pcm(20))
    _bind_response(ledger)
    bound = ledger.bind_tool_event(dict(event(), response_id="resp_1"))
    assert (ledger.authorize_tool("submit_task", bound) is None) is allowed
    assert ledger.authorize_tool("submit_task", bound) is not None


def test_binding_control_scope():
    captured = binding(object())
    assert captured.owns(SimpleNamespace(user_id=str(OPERATOR_ID), chat_id="chat"), "discord:chat")
    assert not captured.owns(SimpleNamespace(user_id=str(OTHER_ID), chat_id="chat"), "discord:chat")
    assert not captured.owns(
        SimpleNamespace(user_id=str(OPERATOR_ID), chat_id="other"), "discord:chat"
    )


def test_delegated_prompt_does_not_advertise_legacy_tools():
    import talk_identity

    prompt = talk_identity.build_instructions(
        {"SOUL": "Be helpful."}, tools=talk_delegated.TOOLS, delegated_work=True, lane="discord"
    )
    assert "submit_task" in prompt and "submission_status" in prompt
    assert "delegate_task" not in prompt and "talk_capabilities" not in prompt
    assert "Be helpful." in prompt


def test_terminal_watcher_uses_exact_status_result_and_closes_on_revocation():
    async def run():
        relay = talk_delegated.SubmissionRelay(binding(object()), lambda *_: None)
        relay.requests["first"] = "one"
        relay.requests["second"] = "two"

        async def status(request_id):
            if request_id == "first":
                return {
                    "status": "terminal",
                    "outcome": "success",
                    "result": {"final_response": "Exact host result"},
                }
            return {"status": "refused", "error": "permission_denied"}

        relay.status = status
        queue = asyncio.Queue()

        async def send_context(_):
            pass

        await relay.watch(queue, lambda title, body: (title, body), send_context)
        assert (await queue.get())[1] == "Exact host result"
        assert (await queue.get())[1] == "permission_denied"
        assert relay.closed

    asyncio.run(run())


def test_watcher_does_not_poll_an_inflight_submission():
    import threading

    async def run():
        entered, release = threading.Event(), threading.Event()

        def submit(*_, **identity):
            entered.set()
            assert release.wait(5)
            return {"status": "queued", "request_id": identity["request_id"]}

        relay = talk_delegated.SubmissionRelay(
            binding(SimpleNamespace(submit_message=submit)),
            lambda *_: None,
        )

        async def forbidden_status(_):
            pytest.fail("status polled before submission returned")

        relay.status = forbidden_status
        submission = asyncio.create_task(relay.handle_tool_call_async(event()))
        try:
            assert await asyncio.to_thread(entered.wait, 5)

            class StopPoll(Exception):
                pass

            async def sleep(_):
                raise StopPoll

            async def context(_):
                pass

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(talk_delegated.asyncio, "sleep", sleep)
                await relay.watch(asyncio.Queue(), lambda *parts: parts, context)
            # The artificial StopPoll closes the watcher; status itself was never consulted.
        finally:
            release.set()
            await submission
        assert not relay.pending

    asyncio.run(run())


def test_capture_pins_invocation_profile_user_and_guild(monkeypatch, tmp_path):
    import sys

    ctx = SimpleNamespace(submit_message=lambda *_: None, submission_status=lambda *_: None)
    source = SimpleNamespace(user_id=str(OPERATOR_ID), chat_id="chat", scope_id="123")
    monkeypatch.setitem(
        sys.modules,
        "persistence_assembly",
        SimpleNamespace(
            profile_scope=lambda: SimpleNamespace(profile_dir=tmp_path),
        ),
    )
    captured = talk_delegated.Binding.capture(ctx, source, "discord:chat", "session", "discord")
    source.user_id, source.scope_id = str(OTHER_ID), "456"
    assert captured.ctx is ctx
    assert captured.profile_dir == tmp_path
    assert captured.user_id == str(OPERATOR_ID)
    assert captured.guild_id == 123
    assert captured.session_key == "discord:chat" and captured.session_id == "session"
    with pytest.raises(ValueError, match="does not provide"):
        talk_delegated.Binding.capture(object(), source, "discord:chat", "session", "discord")


def test_context_budget_keeps_latest_complete_rows():
    rows = [{"role": "user", "content": str(index) + "a" * 1990} for index in range(16)]
    kept, text = talk_delegated.bounded_context(rows)
    assert len(text) <= 16000
    assert json.loads(text) == kept
    assert kept[-1] == rows[-1]
    assert kept[0] != rows[0]
