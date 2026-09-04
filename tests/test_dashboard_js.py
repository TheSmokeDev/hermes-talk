"""Executable dashboard transport protocol regressions (Node, no browser)."""

from __future__ import annotations

from pathlib import Path
from subprocess import run

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_JS = ROOT / "dashboard" / "dist" / "index.js"

#: Outer guard on the `node` subprocess. Every script below bounds its own
#: waits at 1s (`waitFor`), so this only has to cover node's cold start — which
#: on a cold windows-latest runner has exceeded 10s and failed main for nothing.
NODE_TIMEOUT_S = 60


def test_dashboard_serializes_tool_calls_and_continues_once_after_response_done():
    script = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
let releaseFirst;
const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
const started = [];
const sent = [];
const window = {
  __HERMES_TALK_TEST_HOOK__: true,
  __HERMES_PLUGINS__: { register() {} },
  __HERMES_PLUGIN_SDK__: {
    React: { createElement() {} },
    hooks: { useState() {}, useEffect() {}, useRef() {}, useCallback() {} },
    components: {},
    async fetchJSON(_url, opts) {
      const name = JSON.parse(opts.body).name;
      started.push(name);
      if (name === "first") await firstGate;
      return { output: "result-" + name };
    },
  },
  sessionStorage: { getItem() { return ""; } },
  setTimeout,
  clearTimeout,
};
const context = { window, setTimeout, clearTimeout, AbortController, console };
vm.runInNewContext(source, context, { filename: "index.js" });
const Transport = window.__HERMES_TALK_TEST__.TalkTransport;
const transport = new Transport({}, { onStatus() {}, onError() {} });
transport.channel = {
  readyState: "open",
  send(payload) { sent.push(JSON.parse(payload)); },
};
const call = (id, name) => JSON.stringify({
  type: "response.function_call_arguments.done",
  call_id: id,
  name,
  arguments: "{}",
});
const waitFor = async (predicate, timeoutMs = 1000) => {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("timed out waiting for dashboard transport");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
};
(async () => {
  transport.handleEvent(call("call-1", "first"));
  transport.handleEvent(call("call-2", "second"));
  transport.handleEvent(JSON.stringify({ type: "response.done" }));
  await waitFor(() => started.length >= 1);
  if (JSON.stringify(started) !== JSON.stringify(["first"])) {
    throw new Error("calls launched concurrently: " + JSON.stringify(started));
  }
  if (sent.length !== 0) throw new Error("continued before tools resolved");
  releaseFirst();
  await waitFor(() => started.length === 2 && sent.some((m) => m.type === "response.create"));
  if (JSON.stringify(started) !== JSON.stringify(["first", "second"])) {
    throw new Error("wrong call order: " + JSON.stringify(started));
  }
  const ids = sent.slice(0, -1).map((m) => m.item.call_id);
  if (JSON.stringify(ids) !== JSON.stringify(["call-1", "call-2"])) {
    throw new Error("wrong output order: " + JSON.stringify(sent));
  }
  if (sent.filter((m) => m.type === "response.create").length !== 1) {
    throw new Error("continuation count: " + JSON.stringify(sent));
  }
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = run(
        ["node", "-e", script, str(DASHBOARD_JS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=NODE_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_dashboard_cascade_relays_text_and_plays_pcm_until_barge_in():
    """The cascade transport: NDJSON out, PCM onto the AudioContext, abort kills both."""

    script = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const posted = [];      // NDJSON lines the transport wrote into request bodies
const fetches = [];     // every cascade fetch: {signal, bodyLines}
const scheduled = [];   // AudioBufferSourceNodes started on the fake context
const stopped = [];     // sources stop()ed (barge-in / teardown)

class FakeAudioContext {
  // Honours a requested sampleRate, like Chrome and Edge do. The transport
  // asks for the PCM's own 24kHz so the browser resamples nothing.
  constructor(options) {
    this.sampleRate = (options && options.sampleRate) || 48000;
    this.currentTime = 0;
    this.destination = {};
  }
  createBuffer(channels, length, sampleRate) {
    if (channels !== 1 || sampleRate !== 24000) throw new Error("wrong PCM shape");
    const data = new Float32Array(length);
    return { duration: length / sampleRate, getChannelData: (i) => data };
  }
  createBufferSource() {
    const node = {
      buffer: null,
      startedAt: -1,
      connect() {},
      start(at) { this.startedAt = at; scheduled.push(this); },
      stop() { stopped.push(this); },
    };
    return node;
  }
  close() { return Promise.resolve(); }
}

// One scripted fetch per POST: capture the request stream, answer with a
// response body the test drives later.
const pendingResponses = [];
const fetchStub = (url, opts) => {
  const entry = { url, opts, aborted: false, lines: posted };
  opts.signal.addEventListener("abort", () => { entry.aborted = true; });
  fetches.push(entry);
  (async () => {
    const reader = opts.body.getReader();
    for (;;) {
      const step = await reader.read();
      if (step.done) break;
      const text = new TextDecoder().decode(step.value);
      text.split("\n").filter((line) => line).forEach((line) => {
        posted.push(JSON.parse(line));
      });
    }
  })().catch(() => {});
  return new Promise((resolve) => pendingResponses.push(resolve));
};

const window = {
  __HERMES_TALK_TEST_HOOK__: true,
  __HERMES_PLUGINS__: { register() {} },
  __HERMES_PLUGIN_SDK__: {
    React: { createElement() {} },
    hooks: { useState() {}, useEffect() {}, useRef() {}, useCallback() {} },
    components: {},
  },
  sessionStorage: { getItem() { return ""; } },
  setTimeout,
  clearTimeout,
  AudioContext: FakeAudioContext,
};
// An HTTP/2 page: the transport is allowed to stream its request body, which
// is the path this test exercises. The HTTP/1.1 fallback has its own test.
const context = {
  window, setTimeout, clearTimeout, AbortController, ReadableStream,
  TextDecoder, TextEncoder, Uint8Array, Int16Array, Float32Array, JSON, console,
  fetch: fetchStub, Promise, Math,
  performance: { getEntriesByType: () => [{ nextHopProtocol: "h2" }] },
  Request: class { constructor() {} },
};
vm.runInNewContext(source, context, { filename: "index.js" });
const Transport = window.__HERMES_TALK_TEST__.TalkTransport;
const waitFor = async (predicate, timeoutMs = 1000) => {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("timed out waiting for cascade transport");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
};
const respond = (pcmChunks) => {
  const stream = new ReadableStream({
    start(controller) {
      pcmChunks.forEach((pcm) => controller.enqueue(pcm));
      controller.close();
    },
  });
  pendingResponses.shift()({ ok: true, body: stream });
};
(async () => {
  const transcripts = [];
  const transport = new Transport(
    { voiceMode: "cascade" },
    {
      onStatus() {},
      onError() {},
      onTranscript: (role, text, final) => transcripts.push({ role, text, final }),
    },
  );
  transport.channel = { readyState: "open", send() {} };

  // Text deltas caption AND relay; the done line closes the request side.
  const send = (event) => transport.handleEvent(JSON.stringify(event));
  send({ type: "response.created", response: { id: "r1" } });
  send({ type: "response.output_text.delta", delta: "Hello " });
  send({ type: "response.output_text.delta", delta: "dashboard. " });
  send({ type: "response.output_text.done", text: "Hello dashboard." });
  await waitFor(() => fetches.length === 1);
  const relayUrl = "/api/plugins/hermes-talk/cascade-tts";
  if (fetches[0].url !== relayUrl) throw new Error("wrong relay url: " + fetches[0].url);
  if (fetches[0].opts.duplex !== "half") throw new Error("relay must stream duplex");
  await waitFor(() => posted.length === 3);
  if (JSON.stringify(posted) !== JSON.stringify([
    { delta: "Hello " }, { delta: "dashboard. " }, { done: "Hello dashboard." },
  ])) throw new Error("wrong NDJSON relay: " + JSON.stringify(posted));
  const finalCaption = transcripts.some(
    (t) => t.role === "assistant" && t.final && t.text === "Hello dashboard.",
  );
  if (!finalCaption) throw new Error("final caption missing: " + JSON.stringify(transcripts));

  // The PCM answer plays through the AudioContext at 24kHz mono.
  const pcm = new Uint8Array(960);  // one 20ms frame of 24k mono s16le
  pcm[0] = 7;
  // Split mid-SAMPLE (odd byte): carrying the straggler is the transport's job.
  respond([pcm.slice(0, 501), pcm.slice(501)]);
  await waitFor(() => scheduled.length === 2);
  const totalSamples = scheduled[0].buffer.getChannelData(0).length +
    scheduled[1].buffer.getChannelData(0).length;
  if (totalSamples !== 480) throw new Error("wrong sample count: " + totalSamples);
  // Gapless: the second buffer starts exactly where the first ends.
  const expectedStart = scheduled[0].startedAt + scheduled[0].buffer.duration;
  if (Math.abs(scheduled[1].startedAt - expectedStart) > 1e-9) {
    throw new Error("PCM playback is not gapless");
  }

  // Barge-in: the next fetch is aborted and every scheduled source stops.
  send({ type: "response.created", response: { id: "r2" } });
  send({ type: "response.output_text.delta", delta: "Second answer. " });
  await waitFor(() => fetches.length === 2);
  send({ type: "input_audio_buffer.speech_started" });
  if (!fetches[1].aborted) throw new Error("barge-in did not abort the relay fetch");
  if (stopped.length < 1) throw new Error("barge-in did not stop scheduled playback");

  // A native session never touches the relay.
  const native = new Transport(
    { voiceMode: "native" },
    { onStatus() {}, onError() {}, onTranscript() {} },
  );
  native.channel = { readyState: "open", send() {} };
  native.handleEvent(
    JSON.stringify({ type: "response.output_text.delta", delta: "ignored " }),
  );
  await new Promise((resolve) => setTimeout(resolve, 20));
  if (fetches.length !== 2) throw new Error("native mode dialed the cascade relay");
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = run(
        ["node", "-e", script, str(DASHBOARD_JS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=NODE_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_dashboard_buffers_the_answer_when_the_browser_cannot_stream_the_upload():
    """HTTP/1.1: audio still arrives, and the failure is never silent.

    Chrome only sends a streaming request body over HTTP/2 or HTTP/3. On a
    plain HTTP/1.1 origin — which a local dashboard almost always is — the
    `duplex: "half"` fetch rejects outright, and the relay's own `.catch()`
    swallowed it: text captioned fine, no audio ever played, and zero
    requests reached the server. The transport must notice it cannot stream
    and post the whole answer at `done` instead.
    """

    script = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const fetches = [];
const scheduled = [];
const errors = [];

class FakeAudioContext {
  constructor(options) {
    this.sampleRate = (options && options.sampleRate) || 48000;
    this.currentTime = 0;
    this.destination = {};
  }
  createBuffer(channels, length, sampleRate) {
    const data = new Float32Array(length);
    return { duration: length / sampleRate, getChannelData: (i) => data };
  }
  createBufferSource() {
    return {
      buffer: null, startedAt: -1, connect() {},
      start(at) { this.startedAt = at; scheduled.push(this); },
      stop() {},
    };
  }
  close() { return Promise.resolve(); }
}

const pendingResponses = [];
const fetchStub = (url, opts) => {
  // A streaming body on HTTP/1.1 is exactly what the browser refuses. If the
  // transport ever tries it here, that IS the bug — so fail the way Chrome
  // does rather than quietly accepting it.
  if (opts.duplex === "half") {
    return Promise.reject(new TypeError("Failed to fetch"));
  }
  fetches.push({ url, opts, body: opts.body });
  return new Promise((resolve) => pendingResponses.push(resolve));
};

const window = {
  __HERMES_TALK_TEST_HOOK__: true,
  __HERMES_PLUGINS__: { register() {} },
  __HERMES_PLUGIN_SDK__: {
    React: { createElement() {} },
    hooks: { useState() {}, useEffect() {}, useRef() {}, useCallback() {} },
    components: {},
  },
  sessionStorage: { getItem() { return ""; } },
  setTimeout,
  clearTimeout,
  AudioContext: FakeAudioContext,
};
// An HTTP/1.1 page — the local-dashboard default.
const context = {
  window, setTimeout, clearTimeout, AbortController, ReadableStream,
  TextDecoder, TextEncoder, Uint8Array, Int16Array, Float32Array, JSON, console,
  fetch: fetchStub, Promise, Math,
  performance: { getEntriesByType: () => [{ nextHopProtocol: "http/1.1" }] },
  Request: class { constructor() {} },
};
vm.runInNewContext(source, context, { filename: "index.js" });
const Transport = window.__HERMES_TALK_TEST__.TalkTransport;
const waitFor = async (predicate, timeoutMs = 1000) => {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("timed out waiting for cascade transport");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
};
(async () => {
  const transport = new Transport(
    { voiceMode: "cascade" },
    { onStatus() {}, onError: (m) => errors.push(m), onTranscript() {} },
  );
  transport.channel = { readyState: "open", send() {} };
  const send = (event) => transport.handleEvent(JSON.stringify(event));

  send({ type: "response.created", response: { id: "r1" } });
  send({ type: "response.output_text.delta", delta: "Hello " });
  send({ type: "response.output_text.delta", delta: "dashboard. " });
  // Nothing posts while the text is still arriving: there is no stream to
  // post it into, so the answer is held until it is complete.
  await new Promise((resolve) => setTimeout(resolve, 20));
  if (fetches.length !== 0) throw new Error("posted before the answer was done");

  send({ type: "response.output_text.done", text: "Hello dashboard." });
  await waitFor(() => fetches.length === 1);
  const post = fetches[0];
  if (post.url !== "/api/plugins/hermes-talk/cascade-tts") {
    throw new Error("wrong relay url: " + post.url);
  }
  if ("duplex" in post.opts) throw new Error("buffered post must not claim duplex");
  const body = new TextDecoder().decode(post.body);
  const lines = body.split("\n").filter((l) => l).map((l) => JSON.parse(l));
  if (JSON.stringify(lines) !== JSON.stringify([
    { delta: "Hello " }, { delta: "dashboard. " }, { done: "Hello dashboard." },
  ])) throw new Error("wrong buffered NDJSON: " + body);

  // And the PCM still plays.
  const pcm = new Uint8Array(960);
  const stream = new ReadableStream({
    start(controller) { controller.enqueue(pcm); controller.close(); },
  });
  pendingResponses.shift()({ ok: true, body: stream });
  await waitFor(() => scheduled.length === 1);

  if (errors.length !== 0) {
    throw new Error("a working buffered fallback must not report failure: " + errors);
  }
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = run(
        ["node", "-e", script, str(DASHBOARD_JS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=NODE_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_dashboard_reports_a_relay_failure_instead_of_swallowing_it():
    """"Silently" was half the bug: a dead relay must say so, once."""

    script = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const errors = [];
const warned = [];

class FakeAudioContext {
  constructor(options) {
    this.sampleRate = (options && options.sampleRate) || 48000;
    this.currentTime = 0;
    this.destination = {};
  }
  createBuffer(c, length, rate) {
    const data = new Float32Array(length);
    return { duration: length / rate, getChannelData: () => data };
  }
  createBufferSource() {
    return { buffer: null, connect() {}, start() {}, stop() {} };
  }
  close() { return Promise.resolve(); }
}

const window = {
  __HERMES_TALK_TEST_HOOK__: true,
  __HERMES_PLUGINS__: { register() {} },
  __HERMES_PLUGIN_SDK__: {
    React: { createElement() {} },
    hooks: { useState() {}, useEffect() {}, useRef() {}, useCallback() {} },
    components: {},
  },
  sessionStorage: { getItem() { return ""; } },
  setTimeout, clearTimeout,
  AudioContext: FakeAudioContext,
};
const context = {
  window, setTimeout, clearTimeout, AbortController, ReadableStream,
  TextDecoder, TextEncoder, Uint8Array, Int16Array, Float32Array, JSON,
  console: { warn: (m) => warned.push(m), error: () => {}, log: () => {} },
  fetch: () => Promise.reject(new TypeError("Failed to fetch")),
  Promise, Math,
  performance: { getEntriesByType: () => [{ nextHopProtocol: "h2" }] },
  Request: class { constructor() {} },
};
vm.runInNewContext(source, context, { filename: "index.js" });
const Transport = window.__HERMES_TALK_TEST__.TalkTransport;
const waitFor = async (predicate, timeoutMs = 1000) => {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
};
(async () => {
  const transport = new Transport(
    { voiceMode: "cascade" },
    { onStatus() {}, onError: (m) => errors.push(m), onTranscript() {} },
  );
  transport.channel = { readyState: "open", send() {} };
  const send = (event) => transport.handleEvent(JSON.stringify(event));

  send({ type: "response.created", response: { id: "r1" } });
  send({ type: "response.output_text.delta", delta: "First answer. " });
  send({ type: "response.output_text.done", text: "First answer." });
  await waitFor(() => errors.length === 1);
  if (!/text-only/.test(errors[0])) throw new Error("unhelpful receipt: " + errors[0]);
  if (warned.length !== 1) throw new Error("expected exactly one console warning");

  // A second failing answer must not spam: one receipt per session.
  send({ type: "response.created", response: { id: "r2" } });
  send({ type: "response.output_text.delta", delta: "Second answer. " });
  send({ type: "response.output_text.done", text: "Second answer." });
  await new Promise((resolve) => setTimeout(resolve, 40));
  if (errors.length !== 1) throw new Error("relay failure reported twice: " + errors.length);

  // A deliberate barge-in abort is NOT a failure and must stay quiet.
  const quiet = new Transport(
    { voiceMode: "cascade" },
    { onStatus() {}, onError: (m) => errors.push(m), onTranscript() {} },
  );
  quiet.channel = { readyState: "open", send() {} };
  quiet.handleEvent(JSON.stringify({ type: "response.created", response: { id: "r3" } }));
  quiet.handleEvent(JSON.stringify({
    type: "response.output_text.delta", delta: "Interrupted answer. ",
  }));
  quiet.handleEvent(JSON.stringify({ type: "input_audio_buffer.speech_started" }));
  await new Promise((resolve) => setTimeout(resolve, 40));
  if (errors.length !== 1) throw new Error("a barge-in was reported as a failure");
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = run(
        ["node", "-e", script, str(DASHBOARD_JS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=NODE_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_dashboard_pcm_context_asks_for_the_streams_own_sample_rate():
    """No resampling means no seam to drift.

    Web Audio resamples every AudioBuffer independently, so at the browser
    default (48kHz on Windows) two chunks resampled in isolation do not line
    up where they meet — an audible tick at every chunk boundary while the
    PCM leaving the server is provably clean. Asking for the PCM's own rate
    avoids the question entirely.
    """

    script = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const requested = [];
const buffers = [];

class FakeAudioContext {
  constructor(options) {
    requested.push(options ? options.sampleRate : undefined);
    this.sampleRate = (options && options.sampleRate) || 48000;
    this.currentTime = 0;
    this.destination = {};
  }
  createBuffer(channels, length, sampleRate) {
    const data = new Float32Array(length);
    const buffer = {
      duration: length / sampleRate, sampleRate, getChannelData: () => data,
    };
    buffers.push(buffer);
    return buffer;
  }
  createBufferSource() {
    return { buffer: null, connect() {}, start() {}, stop() {} };
  }
  close() { return Promise.resolve(); }
}

const window = {
  __HERMES_TALK_TEST_HOOK__: true,
  __HERMES_PLUGINS__: { register() {} },
  __HERMES_PLUGIN_SDK__: {
    React: { createElement() {} },
    hooks: { useState() {}, useEffect() {}, useRef() {}, useCallback() {} },
    components: {},
  },
  sessionStorage: { getItem() { return ""; } },
  setTimeout, clearTimeout,
  AudioContext: FakeAudioContext,
};
const context = {
  window, setTimeout, clearTimeout, AbortController, ReadableStream,
  TextDecoder, TextEncoder, Uint8Array, Int16Array, Float32Array, JSON, console,
  fetch: () => new Promise(() => {}), Promise, Math,
  performance: { getEntriesByType: () => [{ nextHopProtocol: "h2" }] },
  Request: class { constructor() {} },
};
vm.runInNewContext(source, context, { filename: "index.js" });
const Transport = window.__HERMES_TALK_TEST__.TalkTransport;
const transport = new Transport(
  { voiceMode: "cascade" },
  { onStatus() {}, onError() {}, onTranscript() {} },
);
transport.schedulePcm(new Uint8Array(960), transport.pcmGeneration);

if (requested[0] !== 24000) {
  throw new Error("context was not asked for the PCM rate: " + requested[0]);
}
if (buffers.length !== 1 || buffers[0].sampleRate !== 24000) {
  throw new Error("buffer was not created at the PCM rate");
}
if (transport.pcmContext.sampleRate !== 24000) {
  throw new Error("context is not running at the PCM rate");
}
process.exit(0);
"""
    completed = run(
        ["node", "-e", script, str(DASHBOARD_JS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=NODE_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_dashboard_resampler_is_continuous_across_chunk_seams():
    """The fallback for browsers that refuse a 24kHz context.

    Measured on a 440 Hz tone split into 40 uneven chunks: the per-chunk
    path jumped 0.105 between adjacent output samples where a smooth signal
    steps 0.035. A 3x step at a seam is the tick. Carrying the previous
    chunk's last sample and the fractional read position is what removes it
    — interpolating each chunk in isolation is the original bug in a
    different costume, so this drives BOTH paths and compares them.
    """

    script = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");

class FakeAudioContext {
  constructor() {
    // Refuse the requested rate, like the browsers this fallback exists for.
    this.sampleRate = 48000;
    this.currentTime = 0;
    this.destination = {};
  }
  createBuffer(channels, length, sampleRate) {
    const data = new Float32Array(length);
    return { duration: length / sampleRate, sampleRate, getChannelData: () => data };
  }
  createBufferSource() {
    return { buffer: null, connect() {}, start() {}, stop() {} };
  }
  close() { return Promise.resolve(); }
}

const window = {
  __HERMES_TALK_TEST_HOOK__: true,
  __HERMES_PLUGINS__: { register() {} },
  __HERMES_PLUGIN_SDK__: {
    React: { createElement() {} },
    hooks: { useState() {}, useEffect() {}, useRef() {}, useCallback() {} },
    components: {},
  },
  sessionStorage: { getItem() { return ""; } },
  setTimeout, clearTimeout,
  AudioContext: FakeAudioContext,
};
const context = {
  window, setTimeout, clearTimeout, AbortController, ReadableStream,
  TextDecoder, TextEncoder, Uint8Array, Int16Array, Float32Array, JSON, console,
  fetch: () => new Promise(() => {}), Promise, Math,
  performance: { getEntriesByType: () => [{ nextHopProtocol: "h2" }] },
  Request: class { constructor() {} },
};
vm.runInNewContext(source, context, { filename: "index.js" });
const Transport = window.__HERMES_TALK_TEST__.TalkTransport;
const make = () => new Transport(
  { voiceMode: "cascade" },
  { onStatus() {}, onError() {}, onTranscript() {} },
);

// A continuous 440 Hz tone at 24kHz, split into 40 UNEVEN chunks.
const TOTAL = 12000;
const tone = new Int16Array(TOTAL);
for (let i = 0; i < TOTAL; i++) {
  tone[i] = Math.round(Math.sin((2 * Math.PI * 440 * i) / 24000) * 20000);
}
const chunks = [];
let at = 0;
for (let c = 0; c < 40 && at < TOTAL; c++) {
  const size = 200 + ((c * 37) % 180);          // uneven on purpose
  chunks.push(tone.subarray(at, Math.min(at + size, TOTAL)));
  at += size;
}

const worstJump = (values) => {
  let worst = 0;
  for (let i = 1; i < values.length; i++) {
    worst = Math.max(worst, Math.abs(values[i] - values[i - 1]));
  }
  return worst;
};

// The IDEAL: one resample of the whole tone, no seams at all.
const ideal = make().resampleToContext(tone, 48000);

// The FIX: state carried across every chunk.
const fixed = make();
let carried = [];
chunks.forEach((chunk) => {
  carried = carried.concat(fixed.resampleToContext(chunk, 48000));
});

// The BUG: each chunk resampled in isolation, state reset every time.
const naive = make();
let isolated = [];
chunks.forEach((chunk) => {
  naive.pcmPrev = null;
  naive.pcmPos = 0;
  isolated = isolated.concat(naive.resampleToContext(chunk, 48000));
});

const idealJump = worstJump(ideal);
const fixedJump = worstJump(carried);
const naiveJump = worstJump(isolated);

// The fix must be indistinguishable from resampling the whole tone at once.
if (fixedJump > idealJump * 1.05) {
  throw new Error("state-carrying resampler has a seam: " + fixedJump + " vs " + idealJump);
}
// And the isolated path must be visibly worse, or this test proves nothing.
if (naiveJump < fixedJump * 2) {
  throw new Error(
    "per-chunk resampling was expected to be much worse: " +
    naiveJump + " vs " + fixedJump,
  );
}
// stopPcmPlayback must clear the carry-over, or a barge-in clicks the next answer.
fixed.stopPcmPlayback();
if (fixed.pcmPrev !== null || fixed.pcmPos !== 0) {
  throw new Error("barge-in left stale interpolation state behind");
}
process.exit(0);
"""
    completed = run(
        ["node", "-e", script, str(DASHBOARD_JS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=NODE_TIMEOUT_S,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
