"""Desktop Realtime Talk plugin contract regressions."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JS = ROOT / "desktop" / "plugin.js"


def source() -> str:
    return PLUGIN_JS.read_text(encoding="utf-8")


def test_desktop_plugin_uses_only_runtime_supported_imports():
    imported = re.findall(r"^import .*? from ['\"]([^'\"]+)['\"]", source(), re.MULTILINE)

    assert imported == ["@hermes/plugin-sdk", "react/jsx-runtime"]


def test_desktop_plugin_is_opt_in_and_uses_canonical_relay_mode():
    text = source()

    assert "defaultEnabled: false" in text
    assert "area: 'composer.actions'" in text
    assert "body: { mode: 'desktop-relay' }" in text
    assert "this.controller.acquire" not in text
    assert "lease = await voiceController?.acquire()" in text
    assert "const next = new DesktopTalkTransport(session, voiceController, {" in text
    assert "this.controller.submitText(turn.text)" in text
    assert "this.controller.subscribeAssistant" in text
    assert "context.fallback" not in text
    assert "isTurnBusy" not in text
    assert "acquireLease" not in text


def test_desktop_plugin_mounts_through_the_host_data_render_contract():
    text = source()

    assert "renderPrimary" not in text
    assert (
        "data: { label: 'Realtime Talk', start: startTalk, "
        "render: props => jsx(TalkPrimary, props) }"
        in text
    )
    assert "useComposerVoiceController" in text
    assert "const controller = useComposerVoiceController()" in text
    assert "startTalk(controller)" in text


def test_desktop_plugin_keeps_secrets_ephemeral_and_out_of_storage():
    text = source()

    assert "clientSecret" in text
    assert "Authorization:" in text
    assert "localStorage" not in text
    assert "sessionStorage" not in text
    assert ".storage" not in text
    assert "console.log" not in text


def test_desktop_plugin_waits_for_useful_channel_and_cleans_up_media():
    text = source()

    assert "await this.channelOpenPromise" in text
    assert "this.offerAbort?.abort()" in text
    assert "this.unsubscribeAssistant?.()" in text
    assert "this.channel?.close()" in text
    assert "this.peer?.close()" in text
    assert "this.media?.getTracks().forEach(track => track.stop())" in text
    assert "this.audio.remove()" in text


def test_desktop_plugin_fails_closed_on_microphone_acquisition_and_releases_controller():
    text = source()

    assert "lease = await voiceController?.acquire()" in text
    assert "if (!lease) throw new Error('The microphone is unavailable.')" in text
    assert "activeController = lease" in text
    assert "function releaseController" in text
    assert "controller.release?.()" in text
    assert "releaseController(lease)" in text
    stop_talk = text[text.index("function stopTalk") : text.index("async function startTalk")]
    start_error = text[text.index("} catch (error)") : text.index("function toggleMute")]
    assert "releaseController()" in stop_talk
    assert "releaseController(lease)" in start_error
    assert "clearTransport(instance)" in text
    assert "cleanupStaleStart(next, lease)" in text
    assert "ctx.onDispose(() => { stopTalk(); pluginContext = null })" in text


def test_desktop_plugin_subscribes_to_canonical_assistant_responses_and_unsubscribes_on_stop():
    text = source()

    assert "this.controller.subscribeAssistant(response =>" in text
    assert "this.handleAssistantResponse(response)" in text
    assert "this.unsubscribeAssistant?.()" in text
    assert "this.activeTurnGeneration !== this.turnGeneration" in text


def test_desktop_relay_never_exposes_realtime_tools_or_auto_answering():
    text = source()

    assert "tools: []" in text
    assert "tool_choice: 'none'" in text
    assert "Speak the sole user input verbatim" in text
    assert "event.item_id !== this.latestSpeechItemId" in text
    assert "response_id: this.responseId" in text
    assert "output_audio_buffer.clear" in text
