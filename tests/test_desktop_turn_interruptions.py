from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JS = ROOT / "desktop" / "plugin.js"


def _transport_source() -> str:
    source = PLUGIN_JS.read_text(encoding="utf-8")
    start = source.index("class DesktopRealtimeRelay")
    end = source.index("function stopDesktopRelay", start)
    return source[start:end]


def _run_transport_scenario(body: str) -> dict[str, object]:
    script = f"""
const DESKTOP_RELAY_TIMEOUT_MS = 30_000
const DESKTOP_RELAY_INSTRUCTIONS = 'render'
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
const submitted = []
const states = []
const errors = []
const controller = {
  latestAssistant: () => ({ id: 'assistant-a', pending: false, text: 'A' }),
  interrupt: async () => undefined,
  submitText: text => {
    submitted.push(text)
    return true
  }
}
const transport = new DesktopRealtimeRelay(
  { voice: 'cedar' },
  controller,
  { release() {} },
  { onStatus: state => states.push(state), onError: error => errors.push(error) }
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
const transport = new DesktopRealtimeRelay(
  { voice: 'cedar' },
  controller,
  { release() {} },
  { onStatus: () => undefined, onError: () => undefined }
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
const transport = new DesktopRealtimeRelay(
  { voice: 'cedar' },
  controller,
  { release() {} },
  { onStatus: () => undefined, onError: error => { throw new Error(error) } }
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
const transport = new DesktopRealtimeRelay(
  { voice: 'cedar' },
  controller,
  { release() {} },
  { onStatus: () => undefined, onError: () => undefined }
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


def test_stopping_before_event_channel_opens_finishes_start_and_releases_lease():
    result = _run_transport_scenario(
        """
let releases = 0
const channel = { addEventListener() {}, close() {} }
const peer = {
  connectionState: 'new', addEventListener() {}, addTrack() {},
  createDataChannel: () => channel,
  async createOffer() { return { sdp: 'offer' } },
  async setLocalDescription() {},
  async setRemoteDescription() {},
  close() { this.connectionState = 'closed' }
}
globalThis.RTCPeerConnection = function () { return peer }
Object.defineProperty(globalThis, 'navigator', { value: { mediaDevices: { async getUserMedia() {
  return { getTracks: () => [], getAudioTracks: () => [] }
} } } })
globalThis.document = {
  body: { appendChild() {} }, createElement: () => ({ style: {}, remove() {} })
}
globalThis.fetch = async () => ({ ok: true, text: async () => 'answer' })
const transport = new DesktopRealtimeRelay(
  { offerUrl: 'https://relay.invalid', clientSecret: 'ephemeral', voice: 'cedar' },
  { subscribeAssistant: () => () => {}, latestAssistant: () => null, submitText: () => true,
    interrupt: () => true },
  { release() { releases++ } },
  { onStatus() {}, onError(message) { throw new Error(message) } }
)
const starting = transport.start()
await new Promise(resolve => setTimeout(resolve, 0))
transport.stop()
await starting
await new Promise(resolve => setTimeout(resolve, 0))
console.log(JSON.stringify({
  closed: transport.closed, releases, timer: transport.channelOpenTimer
}))
"""
    )

    assert result == {"closed": True, "releases": 1, "timer": None}


def test_failed_offer_closes_the_unopened_channel_without_unhandled_rejection():
    result = _run_transport_scenario(
        """
let releases = 0
let unhandled = 0
process.on('unhandledRejection', () => { unhandled++ })
const channel = { addEventListener() {}, close() {} }
const peer = {
  connectionState: 'new', addEventListener() {}, addTrack() {},
  createDataChannel: () => channel,
  async createOffer() { throw new Error('offer failed') },
  close() { this.connectionState = 'closed' }
}
globalThis.RTCPeerConnection = function () { return peer }
Object.defineProperty(globalThis, 'navigator', { value: { mediaDevices: { async getUserMedia() {
  return { getTracks: () => [], getAudioTracks: () => [] }
} } } })
globalThis.document = {
  body: { appendChild() {} }, createElement: () => ({ style: {}, remove() {} })
}
const transport = new DesktopRealtimeRelay(
  { offerUrl: 'https://relay.invalid', clientSecret: 'ephemeral', voice: 'cedar' },
  { subscribeAssistant: () => () => {}, latestAssistant: () => null, submitText: () => true,
    interrupt: () => true },
  { release() { releases++ } },
  { onStatus() {}, onError() {} }
)
await assert.rejects(transport.start(), /offer failed/)
transport.stop()
await new Promise(resolve => setTimeout(resolve, 0))
console.log(JSON.stringify({ releases, unhandled, timer: transport.channelOpenTimer }))
"""
    )

    assert result == {"releases": 1, "unhandled": 0, "timer": None}


def test_rejected_submit_clears_pending_turn_and_allows_the_next_turn():
    result = _run_transport_scenario(
        """
const submitted = []
const sent = []
const errors = []
const controller = {
  latestAssistant: () => ({ id: 'assistant-a', pending: false, text: 'A' }),
  interrupt: async () => undefined,
  submitText: text => {
    submitted.push(text)
    return submitted.length > 1
  },
  subscribeAssistant: () => () => undefined
}
const transport = new DesktopRealtimeRelay(
  { voice: 'cedar' },
  controller,
  { release() {} },
  { onStatus: () => undefined, onError: error => errors.push(error) }
)
transport.channel = {
  readyState: 'open',
  send: payload => sent.push(JSON.parse(payload)),
  close: () => undefined
}

await transport.submitText('rejected turn')
const afterReject = {
  waitingForAssistant: transport.waitingForAssistant,
  turnGeneration: transport.turnGeneration
}

await transport.submitText('accepted turn')
transport.handleAssistantResponse({ id: 'assistant-b', pending: false, text: 'accepted answer' })
transport.stop()
console.log(JSON.stringify({
  submitted,
  afterReject,
  errors,
  responseCreates: sent.filter(event => event.type === 'response.create')
}))
"""
    )

    assert result["submitted"] == ["rejected turn", "accepted turn"]
    assert result["afterReject"] == {
        "waitingForAssistant": False,
        "turnGeneration": 1,
    }
    assert result["errors"] == ["Hermes did not accept the transcript. Try again."]
    response_creates = cast(list[dict], result["responseCreates"])
    assert [event["response"]["input"][0]["content"][0]["text"] for event in response_creates] == [
        "accepted answer"
    ]
