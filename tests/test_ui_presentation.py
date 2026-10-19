"""Run presentation and local PCM lifecycle behavior against the authored UI source."""

from __future__ import annotations

from pathlib import Path
from subprocess import run

import pytest

ROOT = Path(__file__).resolve().parents[1]

HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const requests = [], sent = [], errors = [], sources = [], relays = [];
let override, attempt = 0;
class AudioContext {
  constructor() { this.sampleRate = 24000; this.currentTime = 0; this.state = 'running'; }
  createBuffer(channels, length, rate) {
    return { duration: length / rate, getChannelData: () => new Float32Array(length) };
  }
  createBufferSource() {
    const source = { connect() {}, start(at) { this.at = at; },
      stop() { if (this.onended) this.onended(); } };
    sources.push(source); return source;
  }
  close() { return Promise.resolve(); }
}
const prepared = (body) => ({ ok: true, speak: true, event_id: body.event_id,
  attempt_id: 'attempt-' + (++attempt), run_id: 7,
  result: { run_id: 7, status: 'completed', output: 'Existing full result' },
  response: { conversation: 'none', tools: [], tool_choice: 'none', input: [],
    metadata: { talk_event_id: body.event_id, talk_presentation_id: 'attempt-' + attempt } } });
const window = {
  __HERMES_TALK_TEST_HOOK__: true, __HERMES_PLUGINS__: { register() {} },
  __HERMES_PLUGIN_SDK__: {
    React: { createElement() {} }, hooks: {}, components: {},
    async fetchJSON(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : undefined;
      requests.push({ url, body, options });
      if (override) { const reply = override(url, body); if (reply !== undefined) return reply; }
      if (url.endsWith('/speech')) return prepared(body);
      if (url.endsWith('/state')) return { jobs: [], announcements: [] };
      if (url.includes('/result?')) return { ok: true, output: 'Existing full result' };
      return { ok: true };
    }
  },
  AudioContext, setTimeout, clearTimeout,
  sessionStorage: { getItem() { return ''; } },
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  window, AbortController, setTimeout, clearTimeout, TextEncoder, console,
  Uint8Array, Int16Array, Float32Array,
  fetch(url, options) {
    return new Promise((resolve, reject) => relays.push({resolve, reject, options}));
  }
});
const Transport = window.__HERMES_TALK_TEST__.TalkTransport;
const make = (cascade = false) => {
  const transport = new Transport({ voiceMode: cascade ? 'cascade' : 'realtime',
    task: { connection_id: 'owner-A', generation: 7 } }, {
      onError(message) { errors.push(message); },
      onTranscript() {}, onStatus() {}, onTaskState() {},
    });
  transport.channel = { readyState: 'open',
    send(value) { sent.push(JSON.parse(value)); }, close() {} };
  return transport;
};
const emit = (t, event) => t.handleEvent(JSON.stringify(event));
const creates = () => sent.filter((item) => item.type === 'response.create');
const receipts = () => requests.filter((item) => item.url.endsWith('/speech/receipt'))
  .map((item) => item.body);
const states = () => receipts().map((item) => item.state);
const pause = () => new Promise((resolve) => setTimeout(resolve, 5));
const wait = async (condition) => {
  const deadline = Date.now() + 1500;
  while (!condition()) {
    if (Date.now() > deadline) {
      throw new Error('Timed out: ' + JSON.stringify({ requests, errors }));
    }
    await pause();
  }
};
const start = async (t) => {
  t.task.presentation.offer({ announcements: [{ event_id: 'result-A' }] });
  await wait(() => creates().length === 1 && states().includes('context_submitted'));
  emit(t, { type: 'response.created',
    response: { id: 'response-A', metadata: creates()[0].response.metadata } });
};
const done = (t) => emit(t, { type: 'response.done',
  response: { id: 'response-A', status: 'completed' } });
const textDone = (t) => emit(t, { type: 'response.output_text.done',
  response_id: 'response-A', text: 'Summary' });
const feed = async (t, chunks = [new Uint8Array(960)]) => {
  await wait(() => relays.length === 1);
  relays[0].resolve({ ok: true, body: { getReader() { return {
    async read() { return chunks.length ? { value: chunks.shift(), done: false } : { done: true }; }
  }; } } });
  await wait(() => t.cascadeReqs.size === 0);
};
"""


@pytest.fixture
def source_bundle(tmp_path):
    path = tmp_path / "talk.js"
    path.write_text(
        (ROOT / "ui/talk-surface.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "ui/dashboard-entry.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return path


def execute(source_bundle, scenario):
    result = run(
        ["node", "-e", HARNESS + "\n(async () => {\n" + scenario + r"""
})().catch(error => { console.error(error); process.exitCode = 1; });
""", str(source_bundle)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("accepted", [False, True])
def test_dispatch_waits_for_persisted_submitting(source_bundle, accepted):
    execute(source_bundle, "const accepted = " + str(accepted).lower() + r""";
const t = make(); let release;
override = (url, body) => body && body.state === 'submitting'
  ? new Promise(resolve => { release = () => resolve({ ok: accepted }); }) : undefined;
t.task.presentation.offer({ announcements: [{ event_id: 'result-A' }] });
await wait(() => release);
assert.equal(creates().length, 0);
release();
await wait(() => !t.task.presentation.preparing);
assert.equal(creates().length, accepted ? 1 : 0);
const prepare = requests.find(item => item.url.endsWith('/speech')).body;
assert.equal(prepare.presentation_protocol, 1);
assert.equal(prepare.playback_supported, false);
assert.equal(prepare.connection_id, 'owner-A'); assert.equal(prepare.generation, 7);
t.stop();
""")


@pytest.mark.parametrize("retire", ["t.stop()", "t.task.presentation.interrupt()"])
def test_retirement_fences_a_late_dispatch_receipt(source_bundle, retire):
    execute(source_bundle, r"""
const t = make(); let release;
override = (url, body) => body && body.state === 'submitting'
  ? new Promise(resolve => { release = () => resolve({ ok: true }); }) : undefined;
t.task.presentation.offer({ announcements: [{ event_id: 'result-A' }] });
await wait(() => release);
""" + retire + r""";
release(); await wait(() => !t.task.presentation.preparing);
assert.equal(creates().length, 0);
await wait(() => states().includes('deferred'));
assert(!states().includes('context_submitted'));
t.stop();
""")


def test_webrtc_remains_unknown_and_replay_only_presents_existing_result(source_bundle):
    execute(source_bundle, r"""
const t = make(); await start(t);
emit(t, { type: 'output_audio_buffer.started', response_id: 'response-A' });
done(t);
emit(t, { type: 'output_audio_buffer.stopped', response_id: 'response-A' });
await wait(() => states().includes('unknown'));
assert(!states().includes('playback_started')); assert(!states().includes('playback_finished'));
await t.task.result(7);
assert.equal(creates().length, 1);
assert.equal(await t.task.presentation.replay('result-A'), true);
assert.equal(creates().length, 2);
const prepares = requests.filter(item => item.url.endsWith('/speech'));
assert.equal(prepares.length, 2); assert.equal(prepares[1].body.replay, true);
assert.notEqual(creates()[0].event_id, creates()[1].event_id);
assert(!requests.some(item => item.url.endsWith('/tool') || item.url.endsWith('/event')));
assert(creates().every(item => item.response.conversation === 'none' &&
  item.response.tool_choice === 'none' && item.response.tools.length === 0));
t.stop();
""")


def test_foreign_preparation_is_rejected_before_any_receipt_or_dispatch(source_bundle):
    execute(source_bundle, r"""
const t = make();
override = url => url.endsWith('/speech') ? prepared({event_id: 'foreign-result'}) : undefined;
t.task.presentation.offer({ announcements: [{ event_id: 'result-A' }] });
await wait(() => !t.task.presentation.preparing);
assert.equal(creates().length, 0); assert.equal(receipts().length, 0);
assert(errors.some(message => message.includes('requested event')));
t.stop();
""")


@pytest.mark.parametrize("generation_first", [False, True])
def test_pcm_completion_requires_response_end_and_correlated_local_drain(
    source_bundle, generation_first
):
    execute(source_bundle, "const generationFirst = " + str(generation_first).lower() + r""";
const t = make(true); await start(t); textDone(t);
await feed(t);
assert.equal(requests.find(item => item.url.endsWith('/speech')).body.playback_supported, true);
assert.equal(sources.length, 1);
assert(!states().includes('playback_started'), 'Scheduling is not playback');
if (generationFirst) done(t);
assert(!states().includes('playback_finished'));
t.task.presentation.playback('foreign-response', 'started');
assert(!states().includes('playback_started'));
t.pcmContext.currentTime = 0.01;
await wait(() => states().includes('playback_started'));
assert(!states().includes('playback_finished'));
t.pcmContext.currentTime = sources[0].at + sources[0].buffer.duration;
sources[0].onended();
if (!generationFirst) {
  await pause(); assert(!states().includes('playback_finished')); done(t);
}
await wait(() => states().includes('playback_finished'));
sources[0].onended(); done(t); await pause();
assert.equal(states().filter(state => state === 'playback_started').length, 1);
assert.equal(states().filter(state => state === 'playback_finished').length, 1);
assert(receipts().filter(item => item.state.startsWith('playback_')).every(item =>
  item.response_id === 'response-A' && item.attempt_id === 'attempt-1' && item.generation === 7));
t.stop();
""")


def test_pcm_interruption_and_late_drain_never_finish_or_execute_tools(source_bundle):
    execute(source_bundle, r"""
const t = make(true); await start(t); textDone(t); await feed(t);
t.pcmContext.currentTime = 0.01; await wait(() => states().includes('playback_started'));
t.task.presentation.interrupt();
await wait(() => states().includes('interrupted'));
t.pcmContext.currentTime = 1; sources[0].onended(); done(t);
emit(t, { type: 'response.function_call_arguments.done', response_id: 'response-A',
  call_id: 'forbidden', name: 'delegate_task', arguments: '{"task":"rerun"}' });
await pause(); assert(!states().includes('playback_finished'));
assert(!requests.some(item => item.url.endsWith('/tool')));
t.stop();
""")


def test_pcm_drain_waits_for_relay_eof(source_bundle):
    execute(source_bundle, r"""
const t = make(true); await start(t); textDone(t); done(t);
await wait(() => relays.length === 1);
let reads = 0, endStream;
relays[0].resolve({ ok: true, body: { getReader() { return {
  read() {
    if (reads++ === 0) return Promise.resolve({ done: false, value: new Uint8Array(960) });
    return new Promise(resolve => { endStream = () => resolve({ done: true }); });
  }
}; } } });
await wait(() => sources.length === 1 && endStream);
t.pcmContext.currentTime = sources[0].buffer.duration;
sources[0].onended(); await pause();
assert(states().includes('playback_started')); assert(!states().includes('playback_finished'));
endStream(); await wait(() => states().includes('playback_finished'));
t.stop();
""")


def test_uncertain_provider_dispatch_is_not_retried_from_state(source_bundle):
    execute(source_bundle, r"""
const t = make(); let dispatches = 0;
t.channel.send = () => { dispatches += 1; throw new Error('connection lost after write'); };
t.task.presentation.offer({ announcements: [{ event_id: 'result-A' }] });
await wait(() => states().includes('unknown'));
t.task.presentation.offer({ announcements: [{ event_id: 'result-A' }] });
await pause();
assert.equal(dispatches, 1);
assert.equal(requests.filter(item => item.url.endsWith('/speech')).length, 1);
assert(!requests.some(item => item.url.endsWith('/tool')));
t.task.close();
""")


def test_pcm_truncated_stream_cannot_confirm_completion(source_bundle):
    execute(source_bundle, r"""
const t = make(true); await start(t); textDone(t); await feed(t, [new Uint8Array(961)]);
done(t); await wait(() => states().includes('unknown'));
t.pcmContext.currentTime = 1; sources[0].onended(); await pause();
assert(!states().includes('playback_finished'));
t.stop();
""")


def test_shutdown_preserves_exact_attempt_retirement(source_bundle):
    execute(source_bundle, r"""
const t = make(); await start(t); t.stop();
await wait(() => states().includes('unknown'));
const retirement = requests.find(item => item.body && item.body.state === 'unknown');
assert.equal(retirement.options.keepalive, true);
assert.equal(retirement.body.connection_id, 'owner-A'); assert.equal(retirement.body.generation, 7);
assert.equal(retirement.body.event_id, 'result-A');
assert.equal(retirement.body.attempt_id, 'attempt-1');
""")
