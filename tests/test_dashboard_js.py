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
  constructor() { this.currentTime = 0; this.destination = {}; }
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
const context = {
  window, setTimeout, clearTimeout, AbortController, ReadableStream,
  TextDecoder, TextEncoder, Uint8Array, Int16Array, Float32Array, JSON, console,
  fetch: fetchStub, Promise,
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
