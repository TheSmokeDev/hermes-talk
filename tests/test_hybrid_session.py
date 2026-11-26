"""Exercise hybrid startup and dispatch through the real provider session loop."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_fake_provider_session import Audio, FakeProviderSession
from test_fake_provider_session import _offline_policy as _offline_policy
from test_operator_auth import OPERATOR_ID, OTHER_ID, _pcm, _speaker

import talk_cli
import talk_delegated
import talk_operator_auth
import talk_realtime as rt


def binding(ctx):
    return talk_delegated.Binding(
        ctx,
        "discord:room",
        "conversation",
        Path("/unused"),
        str(OPERATOR_ID),
        "room",
        hybrid=True,
    )


@pytest.mark.parametrize("speaker,allowed", [(OPERATOR_ID, True), (OTHER_ID, False)])
def test_hybrid_session_loads_catalog_and_dispatches_only_joining_speaker(
    monkeypatch, speaker, allowed
):
    class ProviderUntilToolsFinish(FakeProviderSession):
        def __init__(self):
            super().__init__()
            self.results_sent = asyncio.Event()

        async def send(self, commands):
            await super().send(commands)
            if (
                sum(
                    isinstance(command, rt.SubmitToolResult)
                    for batch in self.sent
                    for command in batch
                )
                == 2
            ):
                self.results_sent.set()

        async def __anext__(self):
            if not self.events:
                await asyncio.wait_for(self.results_sent.wait(), 5)
            return await super().__anext__()

    fake, audio = ProviderUntilToolsFinish(), Audio()
    audio.discord_speaker_authorization = True
    catalog_reads, executions = [], []
    owner_loop = None

    async def stored_catalog():
        return await asyncio.to_thread(lambda: {
            "ok": True,
            "tools": [
                {"name": name, "description": "A host tool.", "parameters": {"type": "object"}}
                for name in ("terminal", "delegate_task")
            ],
        })

    def catalog(**identity):
        catalog_reads.append(identity)
        return asyncio.run_coroutine_threadsafe(stored_catalog(), owner_loop).result(timeout=3)

    def execute(**request):
        executions.append(request)
        return {
            "ok": True,
            "status": "terminal",
            "request_id": request["request_id"],
            "outputs": [{"call_id": request["calls"][0]["call_id"], "output": "native result"}],
        }

    real_ledger = talk_operator_auth.DiscordToolAuthorizationLedger

    def ledger_for_session(**kwargs):
        ledger = real_ledger(**kwargs)
        ledger.record_packet(_speaker(speaker), _pcm(20))
        ledger.note_speech_started({"item_id": "input", "audio_start_ms": 0})
        ledger.note_speech_stopped({"item_id": "input", "audio_end_ms": 20})
        create = ledger.response_for_commit({"item_id": "input"})
        fake.events.extend(
            [
                rt.ResponseStarted(response_id="response", metadata=create["response"]["metadata"]),
                rt.FunctionCall(
                    response_id="response",
                    call_id="short",
                    name="terminal",
                    arguments=json.dumps({"command": "pwd"}),
                ),
                rt.FunctionCall(
                    response_id="response",
                    call_id="long",
                    name="delegate_work",
                    arguments=json.dumps({"task": "Run the backend tests"}),
                ),
                rt.ResponseFinished(response_id="response"),
            ]
        )
        return ledger

    monkeypatch.setattr(talk_operator_auth, "DiscordToolAuthorizationLedger", ledger_for_session)
    monkeypatch.setattr(talk_delegated.ConversationTail, "read", lambda _tail: [])
    ctx = SimpleNamespace(conversation_tools=catalog, execute_tools=execute)
    async def run():
        nonlocal owner_loop
        owner_loop = asyncio.get_running_loop()
        owner_loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        return await talk_cli.run_talk_session(
            audio=audio,
            session_factory=lambda _auth: fake,
            delegated_binding=binding(ctx),
            lane="discord",
        )

    result = asyncio.run(run())
    assert result == 0 and fake.closed and audio.stopped
    assert catalog_reads == [{"session_key": "discord:room", "expected_session_id": "conversation"}]
    assert {tool.name for tool in fake.setup.tools} >= {
        "terminal",
        "delegate_work",
        "delegation_control",
    }
    assert "delegate_task" not in {tool.name for tool in fake.setup.tools}
    assert not fake.setup.automatic_response
    outputs = [
        json.loads(command.output)
        for batch in fake.sent
        for command in batch
        if isinstance(command, rt.SubmitToolResult)
    ]
    assert len(outputs) == 2
    assert [row["ok"] for row in outputs] == [allowed, allowed], outputs
    if allowed:
        assert [request["calls"][0]["name"] for request in executions] == [
            "terminal",
            "delegate_task",
        ]
        assert [request["require_background"] for request in executions] == [False, True]
        assert all(
            request["session_key"] == "discord:room"
            and request["expected_session_id"] == "conversation"
            for request in executions
        )
        assert executions[1]["calls"][0]["arguments"]["tasks"][0]["goal"] == "Run the backend tests"
    else:
        assert executions == []


def test_hybrid_catalog_refusal_stops_before_provider_connection():
    fake, audio = FakeProviderSession(), Audio()
    audio.discord_speaker_authorization = True
    factories = []
    ctx = SimpleNamespace(
        conversation_tools=lambda **_: {"ok": False, "error": "permission_denied"}
    )
    result = asyncio.run(
        talk_cli.run_talk_session(
            audio=audio,
            session_factory=lambda auth: factories.append(auth) or fake,
            delegated_binding=binding(ctx),
            lane="discord",
        )
    )
    assert result != 0
    assert factories == [] and fake.setup is None


@pytest.mark.parametrize("valid", [True, False])
def test_hybrid_session_uses_upstream_custom_instructions_or_refuses(monkeypatch, valid):
    import talk_identity

    custom = "Keep application development updates brief."
    talk_identity.TALK_INSTRUCTIONS_PATH.write_text(
        custom if valid else "x" * (talk_identity.TALK_INSTRUCTIONS_LIMIT + 1), encoding="utf-8"
    )
    monkeypatch.setattr(talk_delegated.ConversationTail, "read", lambda _tail: [])
    fake, audio, factories = FakeProviderSession(), Audio(), []
    audio.discord_speaker_authorization = True
    ctx = SimpleNamespace(conversation_tools=lambda **_: {"ok": True, "tools": []})
    result = asyncio.run(talk_cli.run_talk_session(
        audio=audio, delegated_binding=binding(ctx), lane="discord",
        session_factory=lambda auth: factories.append(auth) or fake,
    ))
    if valid:
        assert result == 0 and len(factories) == 1
        assert custom in fake.setup.instructions
        assert fake.setup.instructions.startswith(talk_identity.HYBRID_PREAMBLE)
        assert "permissions and approvals still apply" in fake.setup.instructions
        assert "delegation_control" in fake.setup.instructions
    else:
        assert result != 0 and not factories and fake.setup is None
