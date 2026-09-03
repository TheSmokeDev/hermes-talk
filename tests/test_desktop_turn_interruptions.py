"""Behavior regressions for Desktop Realtime Talk interruption ordering."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JS = ROOT / "desktop" / "plugin.js"


def _transport_source() -> str:
    source = PLUGIN_JS.read_text(encoding="utf-8")
    start = source.index("class DesktopTalkTransport")
    end = source.index("function TalkPrimary", start)
    return source[start:end]


def _run_transport_scenario(body: str) -> dict[str, object]:
    script = f"""
const OFFER_TIMEOUT_MS = 30_000
const INTERRUPT_SETTLE_TIMEOUT_MS = 5_000
const RENDER_INSTRUCTIONS = 'render'
globalThis.window = {{ setTimeout, clearTimeout }}
{_transport_source()}
{body}
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_latest_interruption_wins_when_transcripts_complete_out_of_order():
    result = _run_transport_scenario(
        """
let busy = true
const submitted = []
const states = []
const errors = []
const controller = {
  latestAssistant: () => ({ id: 'assistant-a', pending: false, text: 'A' }),
  interrupt: async () => undefined,
  submitText: text => {
    submitted.push(text)
    busy = true
    return true
  }
}
const transport = new DesktopTalkTransport(
  { voice: 'cedar' },
  controller,
  { onState: state => states.push(state), onError: error => errors.push(error) }
)

transport.handleEvent(JSON.stringify({
  type: 'input_audio_buffer.speech_started', item_id: 'input-b'
}))
transport.handleEvent(JSON.stringify({
  type: 'input_audio_buffer.speech_started', item_id: 'input-c'
}))
transport.handleEvent(JSON.stringify({
  type: 'conversation.item.input_audio_transcription.completed',
  item_id: 'input-b',
  transcript: 'older interruption B'
}))
transport.handleEvent(JSON.stringify({
  type: 'conversation.item.input_audio_transcription.completed',
  item_id: 'input-c',
  transcript: 'latest interruption C'
}))

busy = false
await new Promise(resolve => setTimeout(resolve, 250))
transport.stop()
console.log(JSON.stringify({ submitted, states, errors }))
"""
    )

    assert result["submitted"] == ["latest interruption C"]
    assert result["errors"] == []


def test_new_speech_invalidates_a_waiting_assistant_response():
    result = _run_transport_scenario(
        """
let currentAssistant = { id: 'assistant-a', pending: false, text: 'A' }
const sent = []
const controller = {
  latestAssistant: () => currentAssistant,
  interrupt: async () => undefined,
  submitText: () => true
}
const transport = new DesktopTalkTransport(
  { voice: 'cedar' },
  controller,
  { onState: () => undefined, onError: () => undefined }
)
transport.channel = {
  readyState: 'open',
  send: payload => sent.push(JSON.parse(payload)),
  close: () => undefined
}

await transport.submitText('turn B', 'input-b')
transport.handleEvent(JSON.stringify({
  type: 'input_audio_buffer.speech_started', item_id: 'input-c'
}))
transport.handleAssistantResponse({ id: 'assistant-b', pending: false, text: 'stale answer B' })

transport.stop()
console.log(JSON.stringify({
  responseCreates: sent.filter(event => event.type === 'response.create')
}))
"""
    )

    assert result["responseCreates"] == []


def test_idless_assistant_snapshot_is_rendered_once_for_the_current_turn():
    result = _run_transport_scenario(
        """
const sent = []
const controller = {
  latestAssistant: () => ({ pending: false }),
  interrupt: async () => undefined,
  submitText: () => true,
  subscribeAssistant: () => () => undefined
}
const transport = new DesktopTalkTransport(
  { voice: 'cedar' },
  controller,
  { onState: () => undefined, onError: error => { throw new Error(error) } }
)
transport.channel = {
  readyState: 'open',
  send: payload => sent.push(JSON.parse(payload)),
  close: () => undefined
}
await transport.submitText('without an id')
transport.handleAssistantResponse({ pending: false, text: 'assistant without an id' })
transport.stop()
console.log(JSON.stringify({
  responseCreates: sent.filter(event => event.type === 'response.create')
}))
"""
    )

    response_creates = cast(list[dict], result["responseCreates"])
    assert [event["response"]["input"][0]["content"][0]["text"] for event in response_creates] == [
        "assistant without an id"
    ]


def test_barge_in_cancels_the_named_renderer_response_and_audio_buffer():
    result = _run_transport_scenario(
        """
const sent = []
const controller = {
  latestAssistant: () => ({ id: 'assistant-a' }),
  interrupt: async () => undefined,
  submitText: () => true,
  subscribeAssistant: () => () => undefined
}
const transport = new DesktopTalkTransport(
  { voice: 'cedar' },
  controller,
  { onState: () => undefined, onError: () => undefined }
)
transport.channel = {
  readyState: 'open',
  send: payload => sent.push(JSON.parse(payload)),
  close: () => undefined
}
transport.handleEvent(JSON.stringify({
  type: 'response.created', response: { id: 'renderer-response-1' }
}))
transport.handleEvent(JSON.stringify({
  type: 'output_audio_buffer.started', response_id: 'renderer-response-1'
}))
transport.handleEvent(JSON.stringify({
  type: 'input_audio_buffer.speech_started', item_id: 'input-b'
}))
transport.stop()
console.log(JSON.stringify({ sent }))
"""
    )

    sent = cast(list[dict], result["sent"])
    assert {event["type"] for event in sent} == {
        "response.cancel",
        "output_audio_buffer.clear",
    }
    cancel = next(event for event in sent if event["type"] == "response.cancel")
    assert cancel["response_id"] == "renderer-response-1"


def test_rejected_submit_clears_pending_turn_and_allows_the_next_turn():
    result = _run_transport_scenario(
        """
const submitted = []
const sent = []
const controller = {
  latestAssistant: () => ({ id: 'assistant-a', pending: false, text: 'A' }),
  interrupt: async () => undefined,
  submitText: text => {
    submitted.push(text)
    return submitted.length > 1
  },
  subscribeAssistant: () => () => undefined
}
const transport = new DesktopTalkTransport(
  { voice: 'cedar' },
  controller,
  { onState: () => undefined, onError: error => { throw new Error(error) } }
)
transport.channel = {
  readyState: 'open',
  send: payload => sent.push(JSON.parse(payload)),
  close: () => undefined
}

await transport.submitText('rejected turn')
const afterReject = {
  waitingForAssistant: transport.waitingForAssistant,
  activeTurnGeneration: transport.activeTurnGeneration
}

await transport.submitText('accepted turn')
transport.handleAssistantResponse({ id: 'assistant-b', pending: false, text: 'accepted answer' })
transport.stop()
console.log(JSON.stringify({
  submitted,
  afterReject,
  responseCreates: sent.filter(event => event.type === 'response.create')
}))
"""
    )

    assert result["submitted"] == ["rejected turn", "accepted turn"]
    assert result["afterReject"] == {
        "waitingForAssistant": False,
        "activeTurnGeneration": None,
    }
    response_creates = cast(list[dict], result["responseCreates"])
    assert [event["response"]["input"][0]["content"][0]["text"] for event in response_creates] == [
        "accepted answer"
    ]
