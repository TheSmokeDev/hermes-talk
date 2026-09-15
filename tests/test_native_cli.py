"""Native CLI mode routing refuses ambiguous or unbound execution before capture."""

import argparse
import asyncio
from types import SimpleNamespace

import pytest
from test_native_controller import Audio

import talk_cli
import talk_live_config
import talk_live_realtime


def test_live_voice_mode_overrides_legacy_provider_without_changing_its_setting(monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "gemini")
    monkeypatch.setenv("TALK_LIVE_AUTH", "api")
    monkeypatch.setattr(talk_cli.talk_config, "voice_mode", lambda: "live")
    credential = object()
    monkeypatch.setattr(talk_live_config, "resolve_live_auth", lambda **kw: credential)
    monkeypatch.setattr(talk_live_realtime, "LiveRealtimeSession", lambda **kw: kw)
    pick = talk_cli.resolve_provider_lane()
    assert pick.provider == "live" and pick.auth is credential and pick.model == "gpt-live-1"
    session = talk_cli._realtime_session(pick.auth)
    assert session["config"].auth_mode == "api" and session["auth"] is credential
    assert talk_cli.talk_config.talk_provider() == "gemini"


def test_invalid_live_auth_setting_refuses_without_credential_resolution(monkeypatch):
    monkeypatch.setattr(talk_cli.talk_config, "voice_mode", lambda: "live")
    monkeypatch.setenv("TALK_LIVE_AUTH", "guess")
    monkeypatch.setattr(
        talk_live_config, "resolve_live_auth", lambda **kw: pytest.fail("auth read")
    )
    with pytest.raises(talk_cli.talk_config.TalkConfigError, match="TALK_LIVE_AUTH"):
        talk_cli.resolve_provider_lane()


def test_unbound_live_refuses_before_credentials_or_audio(monkeypatch, capsys):
    monkeypatch.setattr(talk_cli.talk_config, "voice_mode", lambda: "live")
    monkeypatch.setattr(talk_cli, "resolve_provider_lane", lambda: pytest.fail("auth read"))
    audio = Audio()
    assert asyncio.run(talk_cli.run_talk_session(audio=audio)) == 1
    assert not audio.started and "--targets" in capsys.readouterr().err


def test_explicit_task_api_enters_canonical_mode_even_without_target(monkeypatch):
    parser = argparse.ArgumentParser()
    talk_cli.setup_cli(parser)
    args = parser.parse_args(["--task-api", "http://127.0.0.1:8642"])
    captured = []

    async def run(**kwargs):
        captured.append(kwargs)
        return 0

    monkeypatch.setattr(talk_cli, "run_talk_session", run)
    assert talk_cli.cli_entry(args, keyboard_control=False) == 0
    assert captured[0]["native_task"]["origin"] == "http://127.0.0.1:8642"
    assert captured[0]["native_task"]["target_id"] is None


def test_discord_missing_proof_refuses_before_audio_or_provider(monkeypatch):
    monkeypatch.setattr(
        talk_cli, "resolve_provider_lane", lambda: SimpleNamespace(provider="openai")
    )
    audio = Audio()

    class API:
        async def close(self):
            pass

    assert (
        asyncio.run(
            talk_cli.run_native_talk_session(
                lane="discord", audio=audio, task={"target_id": "explicit"}, api=API()
            )
        )
        == 1
    )
    assert not audio.started


class AttachmentAPI:
    def __init__(self):
        self.context = None
        self.headers = {}
        self.attachment = None
        self.stack = []
        self.closed = False
        self.generation = 0
        self.event_started = self.event_release = None

    async def catalog(self, **kwargs):
        return {
            "targets": [
                {"target_id": name, "session_id": name, "label": name}
                for name in ("task-a", "task-b")
            ]
        }

    async def attach(self, *, target_id=None, back=False, **kwargs):
        previous = self.attachment and self.attachment["task"]["target_id"]
        if back:
            target_id = self.stack.pop()
        elif previous and previous != target_id:
            self.stack.append(previous)
        self.generation += 1
        self.context = {
            "connection_id": f"connection-{self.generation}",
            "generation": self.generation,
        }
        self.attachment = {
            "task": {
                **self.context,
                "target_id": target_id,
                "session_id": target_id,
                "profile": "default",
                "peer_id": "local",
                "history": {"session_id": target_id, "messages": []},
                "return_depth": len(self.stack),
            },
            "instructions": "Authorized task instructions",
            "tools": [],
            "ok": True,
        }
        return self.attachment

    async def request(self, path, body=None, **kwargs):
        if path == "/event" and self.event_started is not None:
            self.event_started.set()
            await self.event_release.wait()
        if path == "/state":
            return {
                "task": self.attachment["task"],
                "history": self.attachment["task"]["history"],
                "announcements": [],
            }
        return {"ok": True}

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("provider", ["openai", "grok", "gemini", "live"])
def test_runner_select_return_reconnect_replaces_only_voice_and_replays_silently(
    monkeypatch, provider, tmp_path
):
    from test_native_controller import Session

    from talk_native_capture_store import NativeCaptureStore
    from talk_native_live import NativeLiveTaskController

    monkeypatch.setattr(
        NativeCaptureStore, "configured", lambda: NativeCaptureStore(tmp_path / "capture.sqlite3")
    )

    async def scenario():
        monkeypatch.setattr(
            talk_cli,
            "resolve_provider_lane",
            lambda: SimpleNamespace(
                provider=provider, model="fixture", voice="fixture", auth=object()
            ),
        )
        monkeypatch.setattr(
            talk_cli.talk_config, "voice_mode", lambda: "live" if provider == "live" else "native"
        )
        created, selected = [], asyncio.Queue()
        api, audio = AttachmentAPI(), Audio()
        api.origin = "http://127.0.0.1/api/plugins/hermes-talk"

        class Provider(Session):
            async def connect(self, setup):
                await super().connect(setup)
                self.uses_client_delegation = provider == "gemini"

            async def close(self):
                await super().close()

        def factory(auth):
            session = Provider()
            created.append(session)
            return session

        running = asyncio.create_task(
            talk_cli.run_native_talk_session(
                audio=audio,
                session_factory=factory,
                task={"target_id": "task-a"},
                api=api,
                on_controller=selected.put_nowait,
            )
        )
        first = await asyncio.wait_for(selected.get(), 5)
        assert isinstance(first, NativeLiveTaskController) == (provider in {"gemini", "live"})
        await first.command("/select task-b")
        second = await asyncio.wait_for(selected.get(), 5)
        assert first.closed and second.attachment["task"]["target_id"] == "task-b"
        await second.command("/return")
        third = await asyncio.wait_for(selected.get(), 5)
        assert third.attachment["task"]["target_id"] == "task-a"
        await third.command("/reconnect")
        fourth = await asyncio.wait_for(selected.get(), 5)
        assert fourth.context != third.context and not api.stack
        assert all(not session.sent for session in created)
        assert all(session.setup.task_continuity for session in created)
        assert all(
            session.setup.automatic_response == (provider == "gemini") for session in created
        )
        assert not running.done()
        await fourth.session.close()
        assert await asyncio.wait_for(running, 5) == 0
        assert audio.started and audio.stopped and api.closed

    asyncio.run(scenario())


def test_runner_failed_provider_after_selection_exits_without_restoring_old_target(monkeypatch):
    from test_native_controller import Session

    import talk_realtime as rt

    async def scenario():
        monkeypatch.setattr(
            talk_cli,
            "resolve_provider_lane",
            lambda: SimpleNamespace(
                provider="openai", model="fixture", voice="fixture", auth=object()
            ),
        )
        api, audio, selected, created = AttachmentAPI(), Audio(), asyncio.Queue(), []

        class Provider(Session):
            async def connect(self, setup):
                if len(created) > 1:
                    raise rt.RealtimeSessionError("Connection failed after task selection")
                await super().connect(setup)

        def factory(auth):
            session = Provider()
            created.append(session)
            return session

        running = asyncio.create_task(
            talk_cli.run_native_talk_session(
                audio=audio,
                session_factory=factory,
                task={"target_id": "task-a"},
                api=api,
                on_controller=selected.put_nowait,
            )
        )
        first = await asyncio.wait_for(selected.get(), 5)
        assert await first.command("/select task-b") == {"selected": False}
        assert await asyncio.wait_for(running, 5) == 1
        assert api.attachment["task"]["target_id"] == "task-b"
        assert all(session.closed for session in created) and audio.stopped

    asyncio.run(scenario())


def test_old_input_cancelled_during_activation_does_not_terminate_replacement(monkeypatch):
    from test_native_controller import Session

    import talk_realtime as rt

    async def scenario():
        monkeypatch.setattr(
            talk_cli,
            "resolve_provider_lane",
            lambda: SimpleNamespace(
                provider="openai", model="fixture", voice="fixture", auth=object()
            ),
        )
        api, selected = AttachmentAPI(), asyncio.Queue()
        api.event_started, api.event_release = asyncio.Event(), asyncio.Event()
        running = asyncio.create_task(
            talk_cli.run_native_talk_session(
                audio=Audio(),
                session_factory=lambda auth: Session(),
                task={"target_id": "task-a"},
                api=api,
                on_controller=selected.put_nowait,
            )
        )
        first = await asyncio.wait_for(selected.get(), 5)
        first.session.events.put_nowait(
            rt.Transcript(
                rt.TranscriptRole.USER,
                "Original speech",
                True,
                rt.TranscriptProvenance.INPUT_AUDIO,
                item_id="old-input",
            )
        )
        await asyncio.wait_for(api.event_started.wait(), 5)
        await first.command("/select task-b")
        second = await asyncio.wait_for(selected.get(), 5)
        api.event_release.set()
        second.session.events.put_nowait(rt.SessionReady("replacement-session"))
        await asyncio.sleep(0)
        assert not running.done() and second.current
        await second.session.close()
        assert await asyncio.wait_for(running, 5) == 0

    asyncio.run(scenario())


def test_explicit_native_factory_provider_does_not_follow_midcall_environment(monkeypatch):
    monkeypatch.setenv("TALK_PROVIDER", "gemini")
    monkeypatch.setenv("TALK_VOICE_MODE", "live")
    monkeypatch.setattr(talk_cli.talk_openai_realtime, "OpenAIRealtimeSession", lambda **kw: kw)
    monkeypatch.setattr(talk_cli, "_import_aiohttp", lambda: object())
    session = talk_cli._realtime_session(
        SimpleNamespace(token="fixture", source="fixture"), provider="openai"
    )
    assert session["auth_token"] == "fixture" and "mint_session" in session
