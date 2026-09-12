"""Executable dashboard transport protocol regressions (Node, no browser)."""

from __future__ import annotations

from pathlib import Path
from subprocess import run

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_JS = ROOT / "dashboard" / "dist" / "index.js"

#: Outer guard on the `node` subprocess. Every script below bounds its own
#: waits at 1s (`waitFor`), so this only has to cover node's cold start — which
#: on a cold windows-latest runner has exceeded 10s and failed main for nothing.
NODE_TIMEOUT_S = 60


TASK_HARNESS = r"""
const fs = require("fs"), vm = require("vm"), assert = require("assert");
const requests = [], sent = [], errors = [], stages = [];
let sequence = 0, fetchOverride = null;
const callbacks = {
  onStatus() {}, onError(m) { errors.push(m); }, onTranscript() {},
  onTaskStage(row) { stages.push(row); }, onTaskState() {},
};
const window = {
  __HERMES_TALK_TEST_HOOK__: true,
  __HERMES_PLUGINS__: { register() {} },
  __HERMES_PLUGIN_SDK__: {
    React: { createElement() {} },
    hooks: { useState() {}, useEffect() {}, useRef() {}, useCallback() {} }, components: {},
    async fetchJSON(url, opts = {}) {
      const body = opts.body ? JSON.parse(opts.body) : null;
      if (url.endsWith("/event")) {
        assert(body && ["input.final", "response.started", "response.final", "response.done",
          "interaction.settle", "interaction.incomplete"].includes(body.kind),
          "invalid event kind");
        assert(!Object.hasOwn(body, "type"), "provider type leaked onto task-event wire");
      }
      requests.push({ url, body, signal: opts.signal });
      if (fetchOverride) {
        const result = fetchOverride(url, body, opts);
        if (result !== undefined) return result;
      }
      if (body && body.kind === "input.final") return {
        interaction_id: "interaction-" + body.input_id, input_id: body.input_id,
        canonical_state: "saved",
        origin_turn_id: "turn-" + body.input_id, event_id: "event-" + body.input_id,
          state: "staged",
      };
      if (url.endsWith("/state")) return { task: {}, history: { messages: [] }, interactions: [],
        jobs: [] };
      if (url.includes("/result?")) return { ok: true,
        output: "<script>full available result</script>", truncated: false };
      if (url.endsWith("/tool")) return { ok: true, output: "WORK_STARTED #7 kind=agent" };
      return { ok: true, state: "staged" };
    },
  },
  crypto: { randomUUID() { return "uuid-" + (++sequence); } },
  sessionStorage: { getItem() { return ""; }, setItem() {} }, setTimeout, clearTimeout,
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), {
  window, setTimeout, clearTimeout, AbortController, console,
}, { filename: "index.js" });
const Transport = window.__HERMES_TALK_TEST__.TalkTransport;
const make = (generation = 3) => {
  const t = new Transport({ task: { connection_id: "opaque-connection", generation } }, callbacks);
  t.channel = { readyState: "open", send(s) { sent.push(JSON.parse(s)); }, close() {} };
  return t;
};
const emit = (t, event) => t.handleEvent(JSON.stringify(event));
const waitFor = async (predicate) => {
  const deadline = Date.now() + 1500;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("Timed out; requests=" + JSON.stringify(
      requests) + "; errors=" + errors);
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
};
const drain = () => new Promise((resolve) => setTimeout(resolve, 20));
const events = (kind) => requests.filter((r) => r.body && r.body.kind === kind);
const creates = () => sent.filter((m) => m.type === "response.create");
const created = (t, id, request = creates().at(-1)) => {
  emit(t, { type: "response.created", response: { id, metadata: request.response.metadata } });
};
const done = (t, id, output = []) => emit(t, { type: "response.done", response: { id, output,
  status: "completed" } });
"""


def test_steering_receipt_labels_and_replay_are_inert():
    script = (
        TASK_HARNESS
        + r"""
const hooks = window.__HERMES_TALK_TEST__;
assert(hooks.controlLabel({status:'queued',source:'host_receipt',evidence:'backend_queue_ack'})
  .includes('delivery and application unconfirmed'));
assert(hooks.controlLabel({status:'queued',source:'client_observation'}).includes('unconfirmed'));
assert(hooks.controlLabel({status:'applied'}).includes('unconfirmed'));
assert(hooks.controlLabel({status:'unsupported'}).includes('unavailable'));
assert(hooks.steeringLabel({supported:true}).includes('available for this running job'));
assert(hooks.steeringLabel({supported:'true'}).includes('unavailable'));
assert(hooks.steeringLabel({supported:false,reason:'not_refreshed'}).includes('not_refreshed'));
assert.equal(requests.length,0); assert.equal(sent.length,0);
"""
    )
    result = run(
        ["node", "-e", script, str(DASHBOARD_JS)],
        capture_output=True,
        text=True,
        timeout=NODE_TIMEOUT_S,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "scenario",
    [
        r"""
let release;
fetchOverride = (url, body) => body && body.kind === "input.final"
  ? new Promise((resolve) => { release = () => resolve({ input_id: body.input_id,
    interaction_id: "typed-interaction", state: "staged" }); })
  : undefined;
const original = "  Original typed request\nwith a second line\t ";
const t = make(), pending = t.sendTyped(original);
await waitFor(() => release);
assert.equal(sent.length, 0, "provider input escaped stage barrier");
release(); assert.equal(await pending, true);
assert.equal(sent[0].item.content[0].text, original);
assert.equal(events("input.final")[0].body.text, original);
assert.equal(sent[0].item.id, events("input.final")[0].body.input_id);
assert.equal(events("input.final")[0].body.input_type, "typed");
assert.equal(creates()[0].response.metadata.talk_interaction_id, "typed-interaction");
assert.equal(creates()[0].response.input[0].id, sent[0].item.id);
created(t, "typed-response");
emit(t, { type: "response.output_text.done", response_id: "typed-response",
  item_id: "assistant-item", text: "Answer" });
done(t, "typed-response");
await waitFor(() => events("interaction.settle").length === 1);
assert.equal(events("response.final")[0].body.output_item_id, "assistant-item");
assert.equal(events("response.started")[0].body.response_id, "typed-response");
assert.equal(events("interaction.settle")[0].body.interaction_id, "typed-interaction");
t.stop();
""",
        r"""
let release;
fetchOverride = (url, body) => body && body.kind === "input.final"
  ? new Promise((resolve) => { release = () => resolve({ input_id: body.input_id,
    interaction_id: "voice-interaction", state: "staged" }); })
  : undefined;
const t = make();
emit(t, { type: "conversation.item.input_audio_transcription.completed", item_id: "audio-1",
  transcript: "Original utterance" });
await waitFor(() => release);
assert.equal(creates().length, 0);
release(); await drain();
assert.equal(creates().length, 0, "ASR final alone created a response");
assert.equal(events("interaction.settle").length, 0, "ASR final became passive persistence");
emit(t, { type: "input_audio_buffer.committed", item_id: "other-item" });
await drain(); assert.equal(creates().length, 0);
emit(t, { type: "input_audio_buffer.committed", item_id: "audio-1",
  previous_item_id: "other-item" });
await waitFor(() => creates().length === 1);
assert.equal(creates()[0].response.metadata.talk_input_id, "audio-1");
assert.equal(events("input.final")[0].body.text, "Original utterance");
assert.equal(events("input.final")[0].body.input_type, "voice");
t.stop();
""",
        r"""
const t = make();
emit(t, { type: "input_audio_buffer.committed", item_id: "audio-2" });
emit(t, { type: "conversation.item.input_audio_transcription.completed", item_id: "audio-2",
  transcript: "Original action" });
await waitFor(() => creates().length === 1);
emit(t, { type: "response.created", response: { id: "unlinked", metadata: {} } });
emit(t, { type: "response.function_call_arguments.done", response_id: "unlinked",
  call_id: "bad-call", name: "delegate_task",
  arguments: '{"goal":"Generated goal must never become input"}' });
done(t, "unlinked");
await drain();
assert.equal(requests.filter((r) => r.url.endsWith("/tool")).length, 0);
assert.equal(events("response.started").length, 0);
assert.equal(events("interaction.settle").length, 0);
assert(errors.some((m) => m.includes("unlinked")));
assert.equal(events("input.final").length, 1);
assert.equal(events("input.final")[0].body.text, "Original action");
created(t, "linked");
const altered = JSON.parse(JSON.stringify(creates()[0]));
altered.response.metadata.talk_input_id = "wrong-source";
created(t, "wrong", altered);
await drain(); assert.equal(events("response.started").length, 1);
t.stop();
""",
        r"""
let releaseFirst;
fetchOverride = (url, body) => url.endsWith("/tool") && body.call_id === "call-1"
  ? new Promise((resolve) => { releaseFirst = () => resolve({ ok: true, output: "first result" });
    }) : undefined;
const t = make(); await t.sendTyped("Do two things"); created(t, "r1");
done(t, "r1", [
  { type: "function_call", id: "fc1", call_id: "call-1" },
  { type: "function_call", id: "fc2", call_id: "call-2" },
]);
await drain(); assert.equal(events("interaction.settle").length, 0);
const call = (id) => emit(t, { type: "response.function_call_arguments.done", response_id: "r1",
  call_id: id, name: "delegate_task", arguments: '{"goal":"A generated child goal"}' });
call("call-1"); call("call-2"); call("call-2");
await waitFor(() => releaseFirst);
assert.equal(requests.filter((r) => r.url.endsWith("/tool")).length, 1, "tools did not serialize");
assert.equal(creates().length, 1, "continued before tool results");
releaseFirst(); await waitFor(() => creates().length === 2);
const tools = requests.filter((r) => r.url.endsWith("/tool"));
assert.equal(tools.length, 2);
assert.deepEqual(tools.map((r) => r.body.call_id), ["call-1", "call-2"]);
for (const tool of tools) {
  assert.equal(tool.body.connection_id, "opaque-connection"); assert.equal(tool.body.generation, 3);
  assert.equal(tool.body.interaction_id, events("input.final")[0].body.input_id.replace(/^/,
    "interaction-"));
  assert.equal(tool.body.response_id, "r1");
}
assert.deepEqual(events("response.done")[0].body.tool_call_ids, ["call-1", "call-2"]);
assert.equal(events("interaction.settle").length, 0, "settled while continuation pending");
assert.equal(creates()[1].response.metadata.talk_previous_response_id, "r1");
created(t, "r2");
emit(t, { type: "response.output_text.done", response_id: "r2", item_id: "final-answer",
  text: "Done" });
done(t, "r2");
await waitFor(() => events("interaction.settle").length === 1);
assert.equal(events("interaction.settle")[0].body.response_id, "r2");
assert.equal(events("response.started")[1].body.previous_response_id, "r1");
assert.equal(events("input.final").length, 1);
t.stop();
""",
        r"""
const t = make();
t.watchForRun("WORK_STARTED #7 kind=agent"); t.pollRun(7, "agent");
emit(t, { type: "conversation.item.created", item: { id: "synthetic-result", type: "message",
  role: "user", content: [{ type: "input_text", text: "Work run #7 finished" }] } });
await t.task.refresh();
const result = await t.task.result("run-7"); await drain();
assert.equal(result.output, "<script>full available result</script>");
assert.equal(requests.filter((r) => r.url.endsWith("/runs")).length, 0);
assert.equal(events("input.final").length, 0);
assert.equal(sent.length, 0, "replay/results caused automatic speech");
assert(requests.some((r) => r.url.includes(
  "connection_id=opaque-connection&generation=3&run_id=run-7")));
t.stop();
""",
        r"""
let release;
fetchOverride = (url, body) => body && body.kind === "input.final"
  ? new Promise((resolve) => { release = () => resolve({ input_id: body.input_id,
    interaction_id: "old-interaction" }); }) : undefined;
const old = make(3), pending = old.sendTyped("Old input");
await waitFor(() => release);
old.stop(); const stageCount = stages.length;
assert(events("input.final")[0].signal.aborted);
fetchOverride = null;
const replacement = make(4); await replacement.sendTyped("Replacement input");
const sentCount = sent.length; release(); assert.equal(await pending, false); await drain();
assert.equal(sent.length, sentCount, "old stage sent into replacement connection");
assert.equal(stages.filter((s) => s.text === "Old input").length, stageCount);
emit(old, { type: "conversation.item.input_audio_transcription.completed", item_id: "late",
  transcript: "Late" });
assert.equal(events("input.final").length, 2);
assert.equal(requests.filter((r) => r.url.endsWith("/close")).length, 1);
assert.deepEqual(requests.find((r) => r.url.endsWith("/close")).body, {
  connection_id: "opaque-connection", generation: 3 });
replacement.stop();
""",
        r"""
let release;
fetchOverride = (url) => url.endsWith("/tool") ? new Promise((resolve) => { release = (
  ) => resolve({ ok: true, output: "late result" }); }) : undefined;
const t = make(); await t.sendTyped("Action"); created(t, "r1");
emit(t, { type: "response.function_call_arguments.done", response_id: "r1", call_id: "call",
  name: "delegate_task", arguments: "{}" });
done(t, "r1", [{ type: "function_call", id: "fc", call_id: "call" }]);
await waitFor(() => release);
const before = sent.length; t.stop(); release(); await drain();
assert.equal(sent.length, before, "late tool result continued after stop");
assert.equal(events("interaction.settle").length, 0);
assert(requests.find((r) => r.url.endsWith("/tool")).signal.aborted);
assert.equal(errors.length, 0, "closed task reported a late error");
""",
        r"""
const t = make(); await t.sendTyped("Original"); created(t, "r1");
emit(t, { type: "response.function_call_arguments.done", response_id: "r1",
  call_id: "unannounced", name: "delegate_task", arguments: "{}" });
done(t, "r1", []);
await waitFor(() => events("interaction.incomplete").length === 1);
assert.equal(events("interaction.incomplete")[0].body.reason, "missing_tool_calls");
assert.equal(events("interaction.settle").length, 0);
assert.equal(creates().length, 1);
t.stop();
""",
        r"""
const t = make();
await t.sendTyped("First question");
const firstInput = events("input.final")[0].body.input_id;
created(t, "first-response");
done(t, "first-response", [{ id: "first-answer", type: "message",
  content: [{ type: "output_text", text: "First answer" }] }]);
await waitFor(() => t.task.completedGroups.length === 1);
emit(t, { type: "conversation.item.created", item: { id: "synthetic-result", type: "message",
  role: "user", content: [{ type: "input_text", text: "A background result" }] } });
emit(t, { type: "conversation.item.input_audio_transcription.completed", item_id: "other-unsettled",
  transcript: "Other speech without a committed item" });
emit(t, { type: "input_audio_buffer.committed", item_id: "failed-input" });
emit(t, { type: "conversation.item.input_audio_transcription.completed", item_id: "failed-input",
  transcript: "Interrupted speech" });
await waitFor(() => creates().length === 2);
created(t, "failed-response");
emit(t, { type: "response.done", response: { id: "failed-response", status: "cancelled",
  output: [{ id: "partial-answer", type: "message",
    content: [{ type: "output_text", text: "Incomplete answer" }] }] } });
await waitFor(() => events("interaction.incomplete").length === 1);
await t.sendTyped("Second question");
const secondRequest = creates().at(-1), currentInput = events("input.final").at(-1).body.input_id;
assert.deepEqual(secondRequest.response.input.map((item) => item.id),
  [firstInput, "first-answer", currentInput]);
assert.equal(secondRequest.response.metadata.talk_input_id, currentInput);
assert.equal(secondRequest.response.metadata.talk_interaction_id, "interaction-" + currentInput);
assert.equal(secondRequest.response.metadata.talk_previous_response_id, "");
assert.equal(events("input.final").length, 4, "synthetic result was staged as original input");
created(t, "second-response"); done(t, "second-response");
await waitFor(() => events("interaction.settle").length === 2);
t.stop();
""",
        r"""
const t = make(), inputIds = [];
for (let index = 0; index < 33; index++) {
  await t.sendTyped("Question " + index);
  inputIds.push(events("input.final").at(-1).body.input_id);
  created(t, "r" + index);
  done(t, "r" + index, [{ type: "message", id: "answer" + index,
    content: [{ type: "output_text", text: "Answer " + index }] }]);
  await waitFor(() => t.task.completedGroups.length &&
    t.task.completedGroups.at(-1).items.includes("answer" + index));
}
await t.sendTyped("Latest question");
const references = creates().at(-1).response.input.map((item) => item.id);
assert.equal(references.length, 65, "prior-reference bound exceeded");
assert.deepEqual(references.slice(0, 2), [inputIds[1], "answer1"]);
assert(!references.includes(inputIds[0]) && !references.includes("answer0"),
  "oldest interaction was only partially trimmed");
assert.equal(references.at(-1), events("input.final").at(-1).body.input_id);
t.stop();
""",
    ],
    ids=["typed-stage", "asr-before-commit", "explicit-linkage", "continuation-barrier",
      "inert-results", "stop-stage", "stop-tool", "missing-call", "completed-turn-context",
      "bounded-context-groups"],
)
def test_dashboard_bound_task_protocol(scenario):
    script = TASK_HARNESS + "\n(async () => {\n" + scenario + r"""
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


PAGE_HARNESS = TASK_HARNESS + r"""
const slots = [], listeners = {};
const transports = [];
let cursor = 0, effects = [], latestTransport, heldSession, sessionRelease, startOverride;
const sdk = window.__HERMES_PLUGIN_SDK__;
sdk.React.createElement = (tag, props, ...children) => ({ tag, props: props || {}, children });
sdk.components = { Button: "button", Input: "input" };
sdk.hooks = {
  useState(initial) {
    const index = cursor++;
    if (!(index in slots)) slots[index] = initial;
    return [slots[index], (value) => { slots[index] = typeof value === "function" ? value(
      slots[index]) : value; }];
  },
  useRef(initial) { const index = cursor++; if (!(index in slots)) slots[index] = {
    current: initial }; return slots[index]; },
  useCallback(callback) { cursor++; return callback; },
  useEffect(effect, deps) {
    const index = cursor++, previous = slots[index];
    if (!previous || deps.some((dep, at) => dep !== previous.deps[at])) {
      effects.push(() => {
        if (previous && previous.cleanup) previous.cleanup();
        slots[index] = { deps, cleanup: effect() };
      });
    }
  },
};
// React's useCallback preserves the callback when its dependencies are equal.
sdk.hooks.useCallback = (callback, deps) => {
  const index = cursor++, previous = slots[index];
  if (!previous || deps.some((dep, at) => dep !== previous.deps[at])) slots[index] = { deps,
    callback };
  return slots[index].callback;
};
window.location = { href: "https://dashboard.example/plugins/hermes-talk" };
window.addEventListener = (name, callback) => { listeners[name] = callback; };
window.removeEventListener = (name) => { delete listeners[name]; };
fetchOverride = (url, body) => {
  if (url.endsWith("/status")) return { configured: true, voices: ["marin"], voice: "marin",
    taskContinuity: { supported: true } };
  if (url.endsWith("/targets")) return { ok: true, targets: [{ target_id: "chosen-task",
    label: "Selected task", kind: "task", peer_id: "local", host_label: "Local host",
    profile: "default", session_id: "chosen-session" }], peers: [], unavailable: [],
    selection: { current: null, return_depth: 0 } };
  if (url.endsWith("/session")) {
    const session = { task: Object.assign({}, body.task, {
      connection_id: "page-connection", generation: 8, context: {
        workspace: "explicitly unavailable" },
      session_id: "chosen-session", profile: "default", peer_id: "local", kind: "task",
      label: "Selected task", host_label: "Local host", return_depth: 0,
      history: { messages: [{ id: "canonical-1", role: "user",
        content: "<img src=x onerror=bad()>Saved history" }] },
    }) };
    if (heldSession) return new Promise((resolve) => { sessionRelease = () => resolve(session); });
    return session;
  }
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), {
  window, setTimeout, clearTimeout, AbortController, console,
  document: { title: "Dashboard task page" }, navigator: { mediaDevices: {} },
    RTCPeerConnection: function () {},
}, { filename: "index.js" });
const Page = window.__HERMES_TALK_TEST__.TalkPage;
window.__HERMES_TALK_TEST__.TalkTransport.prototype.start = async function () {
  latestTransport = this;
  transports.push(this);
  this.channel = { readyState: "open", send(s) { sent.push(JSON.parse(s)); }, close() {} };
  if (startOverride) await startOverride(this);
};
const render = () => { cursor = 0; effects = []; const tree = Page(); effects.forEach((f) => f());
  return tree; };
const nodes = (tree) => !tree || typeof tree !== "object" ? []
  : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.children)];
const label = (tree) => !tree ? "" : typeof tree === "string" ? tree
  : Array.isArray(tree) ? tree.map(label).join(" ") : label(tree.children);
const button = (tree, text) => nodes(tree).find((node) => node.tag === "button" && label(
  node) === text);
"""


TARGET_PAGE_HARNESS = PAGE_HARNESS + r"""
const baseFetch = fetchOverride, connections = new Map();
const targetRows = [
  { target_id: "a", kind: "task", label: "Task A", peer_id: "local", host_label: "Local host",
    profile: "default", session_id: "task-a" },
  { target_id: "b", kind: "bot", label: "Bot Chat", peer_id: "local", host_label: "Local host",
    profile: "default", session_id: "bot-b" },
  { target_id: "remote-a", kind: "task", label: "Shared task", peer_id: "peer-a",
    host_label: "Shared host", profile: "default", session_id: "same-session" },
  { target_id: "remote-b", kind: "task", label: "Shared task", peer_id: "peer-b",
    host_label: "Shared host", profile: "default", session_id: "same-session" },
];
let serverCurrent = null, generation = 0, switchHandler, peerOffline = false;
const returnStack = [];
const descriptor = (targetId, tab) => {
  const target = targetRows.find((row) => row.target_id === targetId);
  assert(target, "unknown target fixture");
  const task = Object.assign({}, target, { tab_id: tab, connection_id: "connection-" + ++generation,
    generation, return_depth: returnStack.length, context: { workspace: "unavailable" },
    history: { messages: [{ id: targetId + "-message", role: "user",
      content: targetId + " history" }] },
  });
  connections.set(task.connection_id, task);
  serverCurrent = task;
  return { task, selection: { state: "activated", target_id: targetId,
    return_depth: returnStack.length, label: target.label, host_label: target.host_label } };
};
const activate = (body) => {
  const tab = serverCurrent.tab_id;
  let next = body.target_id;
  if (body.back) { assert(returnStack.length); next = returnStack.pop(); }
  else returnStack.push(serverCurrent.target_id);
  return descriptor(next, tab);
};
fetchOverride = (url, body) => {
  if (url.endsWith("/targets")) return { ok: true,
    targets: peerOffline && body.peer_id !== "local" ? [] : targetRows.filter((row) =>
      row.peer_id === body.peer_id).map((row) => Object.assign({}, row,
        { profile: body.profile || row.profile })),
    peers: [{ peer_id: "peer-a", label: "Shared peer" },
      { peer_id: "peer-b", label: "Shared peer" }],
    unavailable: peerOffline ? [{ peer_id: body.peer_id, profile: body.profile,
      reason: "offline" }] : [],
    selection: { current: serverCurrent, return_depth: returnStack.length },
  };
  if (url.endsWith("/session")) return descriptor(body.task.target_id, body.task.tab_id);
  if (url.endsWith("/switch")) {
    if (switchHandler) return switchHandler(body);
    return activate(body);
  }
  if (url.endsWith("/state")) {
    const task = connections.get(body.connection_id);
    return { task, history: task.history, interactions: [], jobs: task.target_id === "a" ? [
      { run_id: "a-job", action_id: "a-action", status: "running", goal: "Work owned by A",
        result_available: true, approval: { state: "unsupported" } },
    ] : [] };
  }
  if (url.includes("/result?")) return { ok: true, output: "A available result", truncated: false };
  return baseFetch(url, body);
};
const field = (tree, name) => nodes(tree).find((node) => node.props["aria-label"] === name);
const choose = (name, value) => {
  const tree = render(); field(tree, name).props.onChange({ target: { value } }); return render();
};
const click = (text) => {
  const tree = render(), control = button(tree, text);
  assert(control && !control.props.disabled, "missing/disabled button: " + text);
  control.props.onClick(); return render();
};
const readyPage = async () => { await drain(); render(); await drain(); return render(); };
const bootA = async () => {
  render(); await readyPage(); choose("Task or Bot target", "a"); click("Join / resume task");
  await readyPage(); await latestTransport.task.refresh(); return render();
};
"""


@pytest.mark.parametrize(
    "scenario",
    [
        r"""
let tree = await bootA(); const first = latestTransport;
assert(label(tree).includes("Work owned by A"));
click("View available result"); tree = await readyPage();
assert(label(tree).includes("A available result"));
choose("Task or Bot target", "b"); click("Switch target"); await readyPage();
assert(first.closed); assert.equal(latestTransport.session.task.target_id, "b");
await latestTransport.task.refresh(); tree = render();
assert(!label(tree).includes("Work owned by A") && !label(tree).includes("A available result"));
assert(label(tree).includes("Bot Chat · bot · Local host (local) / default"));
first.cb.onTaskState({ task: first.session.task, history: first.session.task.history,
  jobs: [{ run_id: "late-job", goal: "Late A job must stay on A" }] });
assert(!label(render()).includes("Late A job"));
const second = latestTransport; click("Return to previous (1)"); await readyPage();
assert(second.closed); assert.equal(latestTransport.session.task.target_id, "a");
await latestTransport.task.refresh(); tree = render();
assert(label(tree).includes("Work owned by A"));
assert(!label(tree).includes("A available result"), "cached result migrated across connections");
const switches = requests.filter((request) => request.url.endsWith("/switch"));
assert.equal(switches[0].body.target_id, "b");
assert.equal(switches[0].body.connection_id, first.task.context.connection_id);
assert.equal(switches[1].body.back, true);
assert.equal(switches[1].body.connection_id, second.task.context.connection_id);
assert.equal(sent.length, 0, "switch/return replay caused speech");
""",
        r"""
await bootA(); const original = latestTransport;
choose("Target source", "peer-a"); let tree = await readyPage();
assert(label(tree).includes("Shared task · task · Shared host (peer-a) / default"));
const firstCatalogId = field(tree, "Task or Bot target").children.flat().find((node) =>
  node && node.props && node.props.value === "remote-a").props.value;
choose("Target source", "peer-b"); tree = await readyPage();
assert(label(tree).includes("Shared task · task · Shared host (peer-b) / default"));
assert.notEqual(firstCatalogId, "remote-b");
choose("Remote profile", "research"); await readyPage();
const catalog = requests.filter((request) => request.url.endsWith("/targets")).at(-1);
assert.equal(catalog.body.peer_id, "peer-b"); assert.equal(catalog.body.profile, "research");
assert(!requests.some((request) => /profiles|discover/.test(request.url)));
switchHandler = () => ({ ok: false, state: "ambiguous", choices: targetRows.slice(2) });
choose("Target reference", "Shared task");
field(render(), "Target reference").props.onChange({ target: { value: "Shared task" } });
const form = nodes(render()).find((node) => node.tag === "form" &&
  label(node).includes("Find and switch"));
form.props.onSubmit({ preventDefault() {} }); tree = await readyPage();
assert.equal(latestTransport, original); assert(!original.closed);
assert(button(tree, "Shared task · task · Shared host (peer-a) / default"));
assert(button(tree, "Shared task · task · Shared host (peer-b) / default"));
switchHandler = () => Promise.reject(new Error("403: target revoked"));
click("Shared task · task · Shared host (peer-b) / default"); tree = await readyPage();
assert.equal(latestTransport, original); assert(!original.closed);
assert(label(tree).includes("target revoked"));
assert.equal(requests.filter((request) => request.url.endsWith("/close")).length, 0);
""",
        r"""
await bootA(); const original = latestTransport;
let release;
switchHandler = (body) => new Promise((resolve) => { release = () => resolve(activate(body)); });
choose("Task or Bot target", "b"); click("Switch target"); await waitFor(() => release);
assert.equal(latestTransport, original); assert(!original.closed);
click("Cancel switch"); release(); let tree = await readyPage();
assert.equal(latestTransport, original); assert(!original.closed);
assert.equal(transports.length, 1, "late minted descriptor installed a provider transport");
assert(label(tree).includes("reconcile or rejoin"));
assert(requests.find((request) => request.url.endsWith("/switch")).signal.aborted);
const close = requests.filter((request) => request.url.endsWith("/close")).at(-1);
assert.notEqual(close.body.connection_id, original.task.context.connection_id);
switchHandler = null;
click("Refresh targets / selection"); tree = await readyPage();
assert(original.closed, "reconciliation left superseded voice active");
assert(label(tree).includes("Server selection changed"));
click("Return to previous (1)"); await readyPage();
assert.equal(latestTransport.session.task.target_id, "a");
""",
        r"""
await bootA(); let release;
switchHandler = (body) => new Promise((resolve) => { release = () => resolve(activate(body)); });
choose("Task or Bot target", "b"); click("Switch target"); await waitFor(() => release);
click("Cancel switch"); switchHandler = null;
choose("Task or Bot target", "a"); click("Switch target"); await readyPage();
const replacement = latestTransport; release(); await readyPage();
assert.equal(latestTransport, replacement); assert(!replacement.closed);
assert.equal(transports.length, 2, "late switch superseded the newer connection");
""",
        r"""
await bootA(); let finishStart;
startOverride = (transport) => transport.session.task.target_id === "b"
  ? new Promise((resolve) => { finishStart = resolve; }) : undefined;
choose("Task or Bot target", "b"); click("Switch target");
await waitFor(() => finishStart); let tree = render();
const candidate = latestTransport; click("Cancel switch"); finishStart(); tree = await readyPage();
assert(candidate.closed, "cancelled provider setup stayed live");
assert(button(tree, "Join / resume task"));
assert(label(tree).includes("reconcile or rejoin"));
""",
        r"""
await bootA(); const original = latestTransport;
fetchOverride = ((base) => (url, body) => url.endsWith("/tool") ? {
  ok: true, output: "Target switch requested; awaiting activation", selection: { target_id: "b" },
} : base(url, body))(fetchOverride);
await original.sendTyped("Switch to Bot Chat"); created(original, "selector-response");
emit(original, { type: "response.function_call_arguments.done", response_id: "selector-response",
  call_id: "selector-call", name: "switch_target", arguments: '{"reference":"Bot Chat"}' });
await waitFor(() => requests.some((request) => request.url.endsWith("/tool")));
await drain();
assert(!requests.some((request) => request.url.endsWith("/switch")), "intent escaped done barrier");
done(original, "selector-response", [{ id: "selector-item", type: "function_call",
  call_id: "selector-call" }]);
await waitFor(() => latestTransport !== original); await readyPage();
assert(original.closed); assert.equal(latestTransport.session.task.target_id, "b");
const staged = events("input.final"); assert.equal(staged.length, 1);
assert.equal(staged[0].body.text, "Switch to Bot Chat");
assert.equal(staged[0].body.connection_id, original.task.context.connection_id);
const request = requests.find((item) => item.url.endsWith("/switch"));
assert.equal(request.body.target_id, "b");
assert.equal(request.body.connection_id, original.task.context.connection_id);
assert.equal(creates().length, 1, "new target automatically spoke old selector result");
""",
        r"""
await bootA(); const original = latestTransport;
fetchOverride = ((base) => (url, body) => url.endsWith("/tool") ? {
  ok: true, output: "Return requested", selection: { back: true },
} : base(url, body))(fetchOverride);
switchHandler = () => Promise.reject(new Error("403: previous target revoked"));
await original.sendTyped("Return to my previous task"); created(original, "return-response");
emit(original, { type: "response.function_call_arguments.done", response_id: "return-response",
  call_id: "return-call", name: "return_to_previous", arguments: "{}" });
done(original, "return-response", [{ id: "return-item", type: "function_call",
  call_id: "return-call" }]);
await waitFor(() => creates().length === 2);
assert.equal(latestTransport, original); assert(!original.closed);
const output = sent.find((message) => message.item && message.item.call_id === "return-call");
assert(output.item.output.includes("not confirmed"));
assert.equal(creates()[1].response.metadata.talk_previous_response_id, "return-response");
assert.equal(events("input.final").length, 1);
assert.equal(requests.find((item) => item.url.endsWith("/switch")).body.back, true);
""",
        r"""
await bootA();
choose("Target source", "peer-a"); await readyPage();
choose("Task or Bot target", "remote-a"); click("Switch target"); await readyPage();
assert.equal(latestTransport.session.task.peer_id, "peer-a");
click("Stop"); peerOffline = true; click("Refresh targets / selection");
let tree = await readyPage(); assert(label(tree).includes("offline"));
click("Return to previous (1)"); await readyPage();
assert.equal(latestTransport.session.task.target_id, "a");
const request = requests.filter((item) => item.url.endsWith("/switch")).at(-1);
assert.equal(request.body.back, true); assert(!Object.hasOwn(request.body, "peer_id"));
assert.equal(sent.length, 0);
""",
    ],
    ids=["local-switch-return", "peer-ambiguity-refusal", "cancel-late-switch",
         "stale-switch", "cancel-provider-start", "model-selection-intent", "model-refused-return",
         "offline-return"],
)
def test_dashboard_target_switching(scenario):
    script = TARGET_PAGE_HARNESS + "\n(async () => {\n" + scenario + r"""
listeners.pagehide();
process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""
    completed = run(
        ["node", "-e", script, str(DASHBOARD_JS)], cwd=ROOT, capture_output=True,
        text=True, timeout=NODE_TIMEOUT_S, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_dashboard_task_picker_rendering_and_page_generation_fences():
    script = PAGE_HARNESS + r"""
(async () => {
  render(); await drain(); let tree = render();
  assert(requests.some((r) => r.url.endsWith("/targets") && r.body.peer_id === "local" &&
    !Object.hasOwn(r.body, "profile") && r.body.tab_id.startsWith("tab_")));
  assert(label(tree).includes("Legacy unbound Talk (no task history)"));
  const picker = nodes(tree).find((node) => node.tag === "select" && label(node).includes(
    "Selected task"));
  picker.props.onChange({ target: { value: "chosen-task" } }); tree = render();
  button(tree, "Join / resume task").props.onClick(); await drain(); tree = render();
  const body = requests.find((r) => r.url.endsWith("/session")).body;
  assert.equal(body.task.target_id, "chosen-task");
  assert(!Object.hasOwn(body.task, "session_id") && !Object.hasOwn(body.task, "profile"));
  assert(body.task.tab_id.startsWith("tab_"));
  assert.deepEqual(body.task.page_reference, { url: window.location.href,
    title: "Dashboard task page" });
  assert(label(tree).includes("Canonical task history"));
  assert(label(tree).includes("<img src=x onerror=bad()>Saved history"));
  assert(!nodes(tree).some((
    node) => node.tag === "img" || node.props && node.props.dangerouslySetInnerHTML));
  latestTransport.cb.onTaskState({ task: latestTransport.session.task,
    history: latestTransport.session.task.history,
    interactions: [{ id: "staged-1", input_id: "input-1", text: "Not yet saved", state: "staged",
      canonical_state: "pending", origin_turn_id: "origin-1", responses: [], actions: [] }],
    jobs: [{ run_id: "job-1", action_id: "action-1", status: "done", goal: "Child goal",
      result_available: true, approval: { state: "unsupported" } }],
    events: { next_cursor: 12, retention_gap: true, snapshot_refetch_required: true, events: [
      { event_id: "run-event", kind: "run_state", observed_index: 12, source_seq: 1,
        source_epoch: "epoch-b", canonical_revision: null, state: "done",
        origin_turn_id: "origin-1", action_id: "action-1", label: "<script>Run observed</script>" },
      { event_id: "saved-event", kind: "history_saved", observed_index: 11, source_seq: 90,
        source_epoch: "epoch-a", canonical_revision: 4, state: "saved",
        origin_turn_id: "origin-1", action_id: "receipt-1", label: "History receipt observed" },
    ] },
  });
  tree = render();
  assert(label(tree).includes("staged · canonical: pending"));
  assert(label(tree).includes("Approval: unsupported"));
  const observationText = label(tree);
  assert(observationText.includes("Observation retention gap: earlier events are unavailable."));
  assert(observationText.includes("Snapshot refetch required: observations may be incomplete."));
  assert(observationText.indexOf("Observed index: 11") <
    observationText.indexOf("Observed index: 12"), "observed order followed source sequence");
  assert(observationText.includes("Source sequence: 90 · source epoch: epoch-a"));
  assert(observationText.includes("canonical revision: 4"));
  assert(observationText.includes("canonical revision: unavailable"));
  assert(nodes(tree).some((node) => node.tag === "a" &&
    node.props.href === "#ht-interaction-staged-1" && label(node) === "origin-1"));
  assert(nodes(tree).some((node) => node.tag === "a" &&
    node.props.href === "#ht-job-job-1" && label(node) === "action-1"));
  assert(observationText.includes("<script>Run observed</script>"));
  assert(!nodes(tree).some((node) => node.tag === "script"));
  assert.equal(sent.length, 0, "observation replay triggered provider speech");
  button(tree, "View available result").props.onClick(); await drain(); tree = render();
  assert(label(tree).includes("<script>full available result</script>"));
  assert(!nodes(tree).some((node) => node.tag === "script"));
  assert.equal(sent.length, 0, "task replay/result view spoke automatically");
  button(tree, "Stop").props.onClick(); tree = render();
  heldSession = true;
  button(tree, "Join / resume task").props.onClick(); await waitFor(() => sessionRelease);
    tree = render();
  button(tree, "Cancel connection").props.onClick(); sessionRelease(); await drain();
    tree = render();
  assert(button(tree, "Join / resume task"), "late session mint revived cancelled page");
  assert.equal(requests.filter((r) => r.url.endsWith("/close")).length, 2);
  heldSession = false;
  button(tree, "Join / resume task").props.onClick(); await drain(); tree = render();
  listeners.pagehide(); await drain();
  assert(latestTransport.closed, "pagehide did not close transport");
  assert.equal(requests.filter((r) => r.url.endsWith("/close")).length, 3);
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


def test_dashboard_stop_is_idempotent_when_teardown_throws():
    """stop() must fully tear down even if a sub-step throws.

    A throw in abortCascade() (or any other teardown step) used to skip the
    rest of stop(), leaving the channel/peer open so the server kept
    listening even though the UI reset to idle. Every step is now guarded so
    a single failure can never strand the session.
    """

    script = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
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
};
const context = { window, setTimeout, clearTimeout, AbortController, console };
vm.runInNewContext(source, context, { filename: "index.js" });
const Transport = window.__HERMES_TALK_TEST__.TalkTransport;
const transport = new Transport({}, { onStatus() {}, onError() {} });

// Every teardown target is present and live.
const channel = { readyState: "open", close() { this.closed = true; } };
const peer = { connectionState: "connected", close() { this.closed = true; } };
const track = { stop() { this.stopped = true; } };
const media = { getTracks() { return [track]; } };
const audio = { remove() { this.removed = true; } };
transport.channel = channel;
transport.peer = peer;
transport.media = media;
transport.audio = audio;

// The failure that used to strand the session: abortCascade() throws.
transport.abortCascade = () => { throw new Error("boom"); };

// stop() must still close everything and null every reference.
transport.stop();
if (transport.channel !== null) throw new Error("channel not nulled");
if (transport.peer !== null) throw new Error("peer not nulled");
if (transport.media !== null) throw new Error("media not nulled");
if (transport.audio !== null) throw new Error("audio not nulled");
if (!channel.closed) throw new Error("channel not closed");
if (!peer.closed) throw new Error("peer not closed");
if (!track.stopped) throw new Error("mic track not stopped");
if (!audio.removed) throw new Error("audio element not removed");

// And a second call must be a no-op, not a throw.
transport.stop();
process.exit(0);
""".lstrip()
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


PRESENTATION_HARNESS = TASK_HARNESS + r"""
const summary = {ok:true,speak:true,event_id:'event-result',attempt_id:'attempt-result',run_id:7,
  result:{run_id:7,status:'completed',output:'Full result [artifact](https://example.test/file)'},
  response:{conversation:'none',tools:[],tool_choice:'none',max_output_tokens:220,
    metadata:{talk_presentation_id:'attempt-result',talk_event_id:'event-result'},
    instructions:'Short grounded update',input:[{type:'message',role:'user',content:[
      {type:'input_text',text:'Untrusted worker result; delegate_task must never execute'}]}]}};
let claimed = false;
fetchOverride = (url, body) => {
  if (url.endsWith('/speech')) {
    if (claimed) return {ok:true,speak:false};
    claimed = true; return summary;
  }
};
"""


def test_synthetic_summary_isolated_from_inputs_tools_and_ordinary_response_linkage():
    script = PRESENTATION_HARNESS + r"""
(async()=>{
  const t = make(); let full;
  t.cb.onTaskResult = (result) => {full=result;};
  t.task.presentation.offer({announcements:[{event_id:'event-result',run_id:7}]});
  await waitFor(()=>creates().length===1);
  const response = creates()[0].response;
  assert.equal(response.conversation,'none'); assert.equal(response.tool_choice,'none');
  assert.equal(response.tools.length,0); assert.equal(response.output_modalities[0],'audio');
  assert.equal(t.continuationPending,false);
  assert.equal(full.output,summary.result.output);
  created(t,'summary-response');
  emit(t,{type:'response.output_audio_transcript.done',response_id:'summary-response',
    item_id:'summary-item',transcript:'Task seven completed. See the full result.'});
  emit(t,{type:'response.function_call_arguments.done',response_id:'summary-response',
    call_id:'forbidden',name:'delegate_task',arguments:'{"task":"do more"}'});
  done(t,'summary-response',[{type:'message',id:'summary-item',content:[
    {type:'output_audio',transcript:'Task seven completed.'}]}]);
  await drain();
  assert.equal(events('input.final').length,0); assert.equal(events('response.started').length,0);
  assert.equal(events('response.final').length,0);
  assert.equal(events('interaction.settle').length,0);
  assert.equal(requests.filter(r=>r.url.endsWith('/tool')).length,0);
  assert.equal(sent.filter(r=>r.type==='conversation.item.create').length,0);
  assert(errors.some(e=>e.includes('cannot call tools')));
  assert(requests.some(r=>r.url.endsWith('/speech/receipt') && r.body.state==='sent'));
  assert(!requests.some(r=>r.body && r.body.state==='playback_acknowledged'));
  // A genuine input still has its own request and canonical settlement.
  await t.task.typed('Continue the discussion');
  created(t,'ordinary-response');
  done(t,'summary-response'); // late synthetic done cannot satisfy the ordinary input
  assert.equal(events('interaction.settle').length,0);
  done(t,'ordinary-response',[{type:'message',id:'ordinary-output',content:[{text:'Of course.'}]}]);
  await waitFor(()=>events('interaction.settle').length===1);
  assert.equal(events('input.final').length,1);
  t.task.close();
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


def test_presentation_waits_for_response_and_close_fences_late_prepare():
    script = PRESENTATION_HARNESS + r"""
(async()=>{
  const t = make(); t.responseActive = true;
  t.task.presentation.offer({announcements:[{event_id:'event-result'}]});
  await drain(); assert.equal(creates().length,0);
  t.responseActive=false; t.continuationPending=true;
  await t.task.presentation.drain(); assert.equal(creates().length,0);
  t.continuationPending=false;
  let release;
  fetchOverride = (url)=>url.endsWith('/speech')
    ? new Promise(resolve=>{release=()=>resolve(summary);}) : undefined;
  const pending = t.task.presentation.drain();
  await waitFor(()=>release); t.task.close(); release(); await pending;
  assert.equal(creates().length,0);
  assert(requests.find(r=>r.url.endsWith('/speech')).signal.aborted);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("scenario", [
    r"""
  let now=1000; t.task.timing.clock=()=>now;
  emit(t,{type:'input_audio_buffer.speech_started',item_id:'operator-item'});
  t.task.presentation.offer({announcements:[{event_id:'event-result'}]});
  await drain(); assert.equal(creates().length,0);
  now=32000; t.task.timing.sample('input',true);
  await t.task.presentation.drain(); assert.equal(creates().length,0);
  now=32900; t.task.timing.sample('input',false);
  await t.task.presentation.drain();
  assert.equal(creates().length,1);
  assert(errors.some(e=>e.includes('measured silence')));
""",
    r"""
  t.task.timing.nativePlaying=true;
  t.task.presentation.offer({announcements:[{event_id:'event-result'}]});
  await drain(); assert.equal(creates().length,0);
  t.task.timing.nativePlaying=false;
  t.pcmContext={currentTime:1}; t.pcmNextTime=2;
  await t.task.presentation.drain(); assert.equal(creates().length,0);
  t.pcmContext.currentTime=3; t.cascadeReqs.add('draining-tts');
  await t.task.presentation.drain(); assert.equal(creates().length,0);
  t.cascadeReqs.clear(); await t.task.presentation.drain();
  assert.equal(creates().length,1);
""",
    r"""
  let release;
  fetchOverride=(url)=>url.endsWith('/speech')
    ? new Promise(resolve=>{release=()=>resolve(summary);}) : undefined;
  t.task.presentation.offer({announcements:[{event_id:'event-result'}]});
  await waitFor(()=>release);
  emit(t,{type:'input_audio_buffer.speech_started',item_id:'operator-item'});
  release(); await waitFor(()=>requests.some(r=>r.body && r.body.state==='deferred'));
  assert.equal(creates().length,0); assert.equal(t.task.presentation.pending.length,1);
  t.task.timing.speakingSince=null;
  fetchOverride=(url)=>url.endsWith('/speech') ? summary : undefined;
  await t.task.presentation.drain(); assert.equal(creates().length,1);
""",
    r"""
  t.task.presentation.offer({announcements:[{event_id:'event-result'}]});
  await waitFor(()=>creates().length===1);
  emit(t,{type:'input_audio_buffer.speech_started',item_id:'operator-item'});
  created(t,'late-summary');
  emit(t,{type:'response.function_call_arguments.done',response_id:'late-summary',
    call_id:'forbidden',name:'stop_work',arguments:'{"run_id":7}'});
  await drain();
  assert(sent.some(r=>r.type==='response.cancel' && r.response_id==='late-summary'));
  assert(sent.some(r=>r.type==='output_audio_buffer.clear'));
  assert(requests.some(r=>r.body && r.body.state==='unknown'));
  assert.equal(requests.filter(r=>r.url.endsWith('/tool')).length,0);
""",
    r"""
  t.task.presentation.offer({announcements:[{event_id:'event-result'}]});
  await waitFor(()=>creates().length===1); created(t,'summary-response');
  emit(t,{type:'output_audio_buffer.started',response_id:'summary-response'});
  done(t,'summary-response'); await drain();
  assert.equal(t.task.timing.snapshot().playback_active,true);
  emit(t,{type:'output_audio_buffer.stopped',response_id:'summary-response'});
  assert.equal(t.task.timing.snapshot().playback_active,false);
  assert(!requests.some(r=>r.body && r.body.state==='playback_acknowledged'));
""",
    r"""
  let now=1000; t.task.timing.clock=()=>now;
  await t.task.typed('Keep this real input');
  assert.equal(creates().length,1);
  t.task.presentation.offer({announcements:[{event_id:'event-result'}]});
  await drain(); assert.equal(creates().length,1);
  now=47000; await t.task.presentation.drain();
  assert.equal(creates().length,2);
  await waitFor(()=>events('interaction.incomplete').length===1);
  assert.equal(events('input.final').length,1);
""",
])
def test_speech_timing_blocks_recovers_and_preserves_worker_ownership(scenario):
    script = PRESENTATION_HARNESS + "\n(async()=>{const t=make();\n" + scenario + r"""
  t.task.close();
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr



def test_old_playback_stop_does_not_clear_the_current_response():
    script = TASK_HARNESS + r"""
const t=make();
emit(t,{type:'output_audio_buffer.started',response_id:'old-response'});
emit(t,{type:'output_audio_buffer.started',response_id:'current-response'});
emit(t,{type:'output_audio_buffer.stopped',response_id:'old-response'});
assert.equal(t.task.timing.snapshot().playback_active,true);
emit(t,{type:'output_audio_buffer.stopped',response_id:'current-response'});
assert.equal(t.task.timing.snapshot().playback_active,false);
t.task.close();
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


LIVE_HARNESS = TASK_HARNESS + r"""
const captions = [], fullResults = [], peers = [], tracks = [], audioElements = [];
let micRequests = 0, closedNotifications = 0, liveFetch;
class FakePeer {
  constructor() { this.listeners = {}; this.connectionState = 'new'; peers.push(this); }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  emit(name, event = {}) { for (const callback of this.listeners[name] || []) callback(event); }
  addTrack() {}
  createDataChannel(label) {
    assert.equal(label, 'oai-events');
    const channel = { readyState: 'connecting', listeners: {},
      addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
      emit(name, event = {}) {
        for (const callback of this.listeners[name] || []) callback(event);
      },
      send(data) { sent.push(JSON.parse(data)); },
      close() { this.readyState = 'closed'; this.emit('close'); },
    };
    return this.channel = channel;
  }
  async createOffer() { return { type: 'offer', sdp: 'browser-offer' }; }
  async setLocalDescription(offer) { this.localDescription = offer; }
  async setRemoteDescription(answer) {
    this.remoteDescription = answer; this.connectionState = 'connected';
    this.channel.readyState = 'open'; this.channel.emit('open');
  }
  close() { this.connectionState = 'closed'; this.emit('connectionstatechange'); }
}
const navigator = { mediaDevices: { async getUserMedia(options) {
  assert.equal(options.audio, true); micRequests++;
  const track = { stopped: false, stop() { this.stopped = true; } }; tracks.push(track);
  return { getTracks: () => [track], getAudioTracks: () => [track] };
} } };
const document = { body: { appendChild() {} }, createElement(name) {
  assert.equal(name, 'audio'); const audio = { style: {}, srcObject: null,
    pause() { this.paused = true; }, remove() { this.removed = true; } };
  audioElements.push(audio); return audio;
} };
fetchOverride = (url, body, opts) => {
  if (liveFetch) { const result = liveFetch(url, body, opts);
    if (result !== undefined) return result; }
  if (url.endsWith('/live/session')) return { ok: true,
    binding_id: 'opaque-live', sdp: 'server-answer' };
  if (url.endsWith('/live/events')) return { ok: true, events: [], cursor: body.after };
  if (url.endsWith('/live/input')) return { ok: true };
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  window, setTimeout, clearTimeout, AbortController, console, navigator, document,
  RTCPeerConnection: FakePeer,
  fetch() { throw new Error('Live must never send a credential or SDP directly to a provider'); },
}, { filename: 'index.js' });
const Live = window.__HERMES_TALK_TEST__.LiveTransport;
const makeLive = (authSource = 'subscription') => window.__HERMES_TALK_TEST__.makeTransport({
  voiceMode: 'live', authSource, live: { transport: 'webrtc' },
  task: { connection_id: 'bound-live-task', generation: 9 },
}, Object.assign({}, callbacks, {
  onTranscript(role, text, final) { captions.push({ role, text, final }); },
  onTaskResult(result) { fullResults.push(result); }, onClosed() { closedNotifications++; },
}));
const pollNow = async (t) => {
  await waitFor(() => !t.livePolling); clearTimeout(t.liveTimer); t.liveTimer = null;
  await t.pollEvents();
};
"""


@pytest.mark.parametrize("auth_source", ["subscription", "api"])
def test_live_browser_negotiates_server_sdp_and_never_relays_actions(auth_source):
    script = LIVE_HARNESS + f"\n(async()=>{{const t=makeLive('{auth_source}');\n" + r"""
assert(t instanceof Live); assert.equal(micRequests, 0, 'construction requested microphone');
await t.start(); await waitFor(() => !t.livePolling);
assert.equal(micRequests, 1);
assert.equal(peers[0].remoteDescription.sdp, 'server-answer');
const offer = requests.find(r => r.url.endsWith('/live/session'));
assert.deepEqual(offer.body, { sdp: 'browser-offer',
  connection_id: 'bound-live-task', generation: 9 });
t.channel.emit('message', { data: JSON.stringify({ type: 'response.function_call_arguments.done',
  call_id: 'evil-call', name: 'delegate_task', arguments: '{"goal":"Unexpected action"}' }) });
t.channel.emit('message', { data: JSON.stringify({
  type: 'delegation.created', delegation_id: 'evil' }) });
assert.equal(t.send({ type: 'response.create' }), false);
const original = '  Preserve this typed input\nexactly.\t ';
assert.equal(await t.sendTyped(original), true);
const input = requests.find(r => r.url.endsWith('/live/input')).body;
assert.equal(input.text, original); assert(input.input_id.startsWith('live_input_'));
assert.equal(input.binding_id, 'opaque-live'); assert.equal(input.generation, 9);
assert.equal(captions.length, 0, 'typed input must be echoed by the authoritative event stream');
liveFetch = (url) => url.endsWith('/state')
  ? { task: {}, announcements: [{event_id:'summary'}] } : undefined;
await t.task.refresh(); await drain();
assert.equal(requests.filter(r => r.url.endsWith('/speech') || r.url.endsWith('/tool') ||
  r.url.endsWith('/event')).length, 0);
assert.equal(sent.length, 0);
t.stop(); t.stop(); await drain();
assert(tracks[0].stopped && audioElements[0].paused && audioElements[0].removed);
assert.equal(peers[0].connectionState, 'closed'); assert.equal(t.liveTimer, null);
assert.equal(t.task.controllers.size, 0); assert.equal(closedNotifications, 1);
assert.equal(requests.filter(r => r.url.endsWith('/live/close')).length, 1);
assert.equal(requests.filter(r => r.url === '/api/plugins/hermes-talk/close').length, 1);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_server_events_deduplicate_without_promoting_partial_transcripts():
    script = LIVE_HARNESS + r"""
(async()=>{
const t=makeLive(); await t.start(); await waitFor(()=>!t.livePolling);
let batch = [
  {sequence:1,type:'transcript',role:'user',text:'Launch ',final:false},
  {sequence:2,type:'transcript',role:'user',text:'Codex',final:false},
  {sequence:3,type:'transcript',role:'assistant',text:'Starting ',final:false},
];
liveFetch=(url)=>url.endsWith('/live/events') ? {ok:true,events:batch,cursor:3} : undefined;
await pollNow(t); await pollNow(t);
assert.equal(captions.length,3); assert(captions.every(c=>c.final===false));
batch=[{sequence:3,type:'transcript',role:'assistant',text:'Starting ',final:false},
  {sequence:4,type:'transcript',role:'user',text:'Launch Codex.',final:true},
  {sequence:5,type:'result',run_id:7,output:'Complete result',action:{state:'completed'}},
];
liveFetch=(url)=>url.endsWith('/live/events') ? {ok:true,events:batch,cursor:5} : undefined;
await pollNow(t);
assert.equal(captions.length,4); assert.equal(captions[3].text,'Launch Codex.');
assert.equal(captions[3].final,true); assert.equal(fullResults.length,1);
assert.equal(fullResults[0].output,'Complete result'); assert.equal(t.liveCursor,5);
assert.equal(sent.length,0); assert.equal(events('input.final').length,0);
t.stop();
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("failure", [
    "throw new Error('403 stale task connection')",
    "return {ok:false,events:[],cursor:0}",
    "return {ok:true,events:[],cursor:-1}",
    "return {ok:true,events:[{sequence:2,type:'transcript',role:'user',"
    "text:'x',final:false}],cursor:1}",
    "return {ok:true,events:[{sequence:1,type:'transcript',role:'user',"
    "text:'x',final:'yes'}],cursor:1}",
    "return {ok:true,events:[{sequence:1,type:'error',"
    "message:'Live authentication expired'}],cursor:1}",
])
def test_live_event_failure_stops_microphone_without_rebilling_or_retry(failure):
    script = LIVE_HARNESS + "\n(async()=>{\n" + r"""
const t=makeLive(); await t.start(); await waitFor(()=>!t.livePolling);
liveFetch=(url)=>{if(url.endsWith('/live/events')) {
""" + failure + r"""
}};
await pollNow(t); await drain();
assert(t.closed && tracks[0].stopped); assert.equal(t.liveTimer,null);
assert.equal(requests.filter(r=>r.url.endsWith('/live/session')).length,1);
assert.equal(errors.length,1); assert.equal(sent.length,0);
assert.equal(captions.length,0); assert.equal(closedNotifications,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_cancelled_negotiation_and_stale_poll_cannot_reopen_audio_or_publish():
    script = LIVE_HARNESS + r"""
(async()=>{
let release;
liveFetch=(url)=>url.endsWith('/live/session') ? new Promise(resolve=>{release=()=>resolve({
  ok:true,binding_id:'late-binding',sdp:'late-answer'});}) : undefined;
const first=makeLive(), pending=first.start(); await waitFor(()=>release);
first.stop(); release(); await assert.rejects(pending,/cancelled/); await drain();
assert(tracks[0].stopped && audioElements[0].paused); assert(!peers[0].remoteDescription);
assert(requests.find(r=>r.url.endsWith('/live/session')).signal.aborted);
assert(requests.some(r=>r.url.endsWith('/live/close') && r.body.binding_id==='late-binding'));
liveFetch=null; const second=makeLive(); await second.start();
await waitFor(()=>!second.livePolling);
let finishPoll;
liveFetch=(url)=>url.endsWith('/live/events') ? new Promise(resolve=>{finishPoll=()=>resolve({
  ok:true,cursor:1,events:[{sequence:1,type:'transcript',role:'user',text:'stale',final:true}]
});}) : undefined;
const polling=pollNow(second); await waitFor(()=>finishPoll);
second.stop(); finishPoll(); await polling;
assert.equal(captions.length,0); assert.equal(second.liveTimer,null);
assert(tracks[1].stopped); assert.equal(sent.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["disconnected", "channel-close", "typed-input"])
def test_live_transport_and_input_failure_close_bound_audio(failure):
    script = LIVE_HARNESS + "\n(async()=>{const t=makeLive();await t.start();\n" + {
        "disconnected": "peers[0].connectionState='disconnected';"
        "peers[0].emit('connectionstatechange');",
        "channel-close": "t.channel.emit('close');",
        "typed-input": r"""
liveFetch=(url)=>{if(url.endsWith('/live/input'))throw new Error('401 expired authentication');};
await assert.rejects(t.sendTyped('Original input'),/401/);
""",
    }[failure] + r"""
await drain(); assert(t.closed && tracks[0].stopped); assert.equal(closedNotifications,1);
assert.equal(requests.filter(r=>r.url.endsWith('/live/session')).length,1);
assert.equal(sent.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_page_requires_operator_start_preserves_partials_and_switches_with_existing_flow():
    script = TARGET_PAGE_HARNESS + r"""
const liveBase=fetchOverride;
fetchOverride=(url,body,opts)=>{
  if(url.endsWith('/live/events')) return {ok:true,cursor:1,events:[
    {sequence:1,type:'result',output:'Switch requested',selection:{target_id:'b'}}]};
  const result=liveBase(url,body,opts);
  if(url.endsWith('/status')) return Object.assign({},result,{voiceMode:'live'});
  if(url.endsWith('/session') || url.endsWith('/switch')) return Object.assign({},result,{
    voiceMode:'live',authSource:'subscription',live:{transport:'webrtc'}});
  return result;
};
const liveClass=window.__HERMES_TALK_TEST__.LiveTransport;
liveClass.prototype.start=async function(){
  await window.__HERMES_TALK_TEST__.TalkTransport.prototype.start.call(this);
  this.bindingId='binding-'+this.task.context.generation;
};
(async()=>{
render(); let tree=await readyPage(); assert.equal(transports.length,0);
assert(button(tree,'Start GPT-Live').props.disabled);
assert(label(tree).includes('Choose a task for GPT-Live'));
choose('Task or Bot target','a'); assert.equal(transports.length,0);
click('Join / resume task'); tree=await readyPage(); const first=latestTransport;
assert(first instanceof liveClass); assert.equal(transports.length,1);
first.cb.onTranscript('user','Launch ',false); first.cb.onTranscript('user','Codex',false);
tree=render(); assert(label(tree).includes('Launch Codex'));
assert(nodes(tree).some(n=>n.props.className==='ht-role' && label(n)==='You …'));
first.cb.onTranscript('user','Launch Codex.',true); tree=render();
assert(!nodes(tree).some(n=>n.props.className==='ht-role' && label(n)==='You …'));
startOverride=()=>{assert(first.closed,'replacement started before old audio was drained');};
await first.pollEvents(); await readyPage();
assert(first.closed); assert(latestTransport instanceof liveClass);
assert.equal(latestTransport.session.task.target_id,'b');
const request=requests.find(r=>r.url.endsWith('/switch'));
assert.equal(request.body.target_id,'b');
assert.equal(request.body.connection_id,first.task.context.connection_id);
assert(requests.some(r=>r.url.endsWith('/live/close') && r.body.binding_id==='binding-1'));
first.cb.onTranscript('assistant','Stale voice text',true);
assert(!label(render()).includes('Stale voice text'));
assert.equal(sent.length,0); assert.equal(requests.filter(r=>r.url.endsWith('/tool')).length,0);
listeners.pagehide(); process.exit(0);
})().catch(e=>{console.error(e);process.exit(1);});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("response", [
    "throw new Error('401 subscription login expired')",
    "return {ok:true,binding_id:'bad-binding',sdp:''}",
])
def test_live_setup_failure_releases_capture_and_does_not_try_other_auth(response):
    script = LIVE_HARNESS + "\n(async()=>{\n" + r"""
liveFetch=(url)=>{if(url.endsWith('/live/session')) {
""" + response + r"""
}};
const t=makeLive(); await assert.rejects(t.start());
assert(t.closed && tracks[0].stopped && audioElements[0].paused);
assert.equal(peers[0].connectionState,'closed'); assert.equal(t.liveTimer,null);
assert.equal(requests.filter(r=>r.url.endsWith('/live/session')).length,1);
assert.equal(requests.filter(r=>r.url.endsWith('/live/events')).length,0);
assert.equal(sent.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_missing_task_is_rejected_before_microphone_request():
    script = LIVE_HARNESS + r"""
(async()=>{
const t=new Live({voiceMode:'live',live:{transport:'webrtc'}},callbacks);
await assert.rejects(t.start(),/authorized task/);
assert.equal(micRequests,0); assert.equal(requests.length,0); assert.equal(peers.length,0);
t.stop();
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr



def test_live_spoken_updates_require_recent_input_and_output_measurement():
    script = LIVE_HARNESS + r"""
const t=makeLive(); let now=5000; t.task.timing.clock=()=>now;
assert.equal(t.liveTiming(),null,'unmeasured audio must not be assumed quiet');
t.task.timing.sample('input',false); assert.equal(t.liveTiming(),null);
t.task.timing.sample('output',false);
const quiet=t.liveTiming(); assert.equal(quiet.operator_speaking,false);
assert.equal(quiet.playback_active,false);
t.task.timing.sample('input',true); assert.equal(t.liveTiming().operator_speaking,true);
now=7000; assert.equal(t.liveTiming(),null,'stale samples must not authorize speech');
t.stop();
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr



def test_live_large_result_uses_authorized_result_endpoint_without_speech_or_tools():
    script = LIVE_HARNESS + r"""
(async()=>{
const t=makeLive(); await t.start(); await waitFor(()=>!t.livePolling);
liveFetch=(url)=>url.endsWith('/live/events') ? {ok:true,cursor:1,events:[
  {sequence:1,type:'result',run_id:'large-job',result_available:true}]} : undefined;
await pollNow(t);
assert.equal(fullResults.length,1);
assert.equal(fullResults[0].output,'<script>full available result</script>');
assert(requests.some(r=>r.url.includes('/result?connection_id=bound-live-task&generation=9')));
assert.equal(sent.length,0); assert.equal(requests.filter(r=>r.url.endsWith('/tool')).length,0);
t.stop();
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_transcript_rows_preserve_exact_deltas_and_late_item_identity():
    script = TASK_HARNESS + r"""
const append = window.__HERMES_TALK_TEST__.appendTranscriptRows;
let rows = [];
const add = (event) => { rows = append(rows, Object.assign({role:'user',final:false},event),
  'row-'+event.event_id); };
add({event_id:'a',item_id:'one',finality:'delta',text:'go',start_ms:0,end_ms:100});
add({event_id:'b',item_id:'one',finality:'delta',text:' ',start_ms:100,end_ms:150});
add({event_id:'c',item_id:'one',finality:'delta',text:'go',start_ms:150,end_ms:200});
add({event_id:'d',item_id:'other',finality:'item',text:'Other turn',start_ms:1000,end_ms:1200});
add({event_id:'item',item_id:'one',finality:'item',text:'go go\n ',start_ms:0,end_ms:250});
assert.equal(rows[0].text,'go go\n '); assert.equal(rows[0].final,false);
assert.equal(rows[1].text,'Other turn');
add({event_id:'c',item_id:'one',finality:'delta',text:'go',start_ms:150,end_ms:200});
assert.equal(rows[0].fragments.length,4,'same event ID replay duplicated an observation');
add({event_id:'turn',item_id:'turn-one',finality:'turn',final:true,
  text:'go go.\n ',start_ms:0,end_ms:300});
assert.equal(rows.length,2); assert.equal(rows[0].text,'go go.\n '); assert(rows[0].final);
add({event_id:'late',item_id:'one',finality:'delta',text:' go',start_ms:150,end_ms:200});
assert.equal(rows[0].text,'go go.\n '); assert(rows[0].final);
assert.equal(rows[0].fragments.at(-1).text,' go','late evidence was dropped');
add({event_id:'new',finality:'delta',text:'Fresh words',start_ms:2000,end_ms:2200});
assert.equal(rows.at(-1).text,'Fresh words'); assert.equal(rows.at(-1).final,false);
assert(rows.every(row=>row.item_id !== 'new'),'event ID became item identity');
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_async_operation_receipts_do_not_claim_job_completion_or_stop_captions():
    script = LIVE_HARNESS + r"""
(async()=>{
const t=makeLive(); const statuses=[]; t.cb.onStatus=value=>statuses.push(value);
await t.start(); await waitFor(()=>!t.livePolling);
liveFetch=(url)=>url.endsWith('/live/input') ?
  {ok:true,operation_id:'operation-one',state:'admitted',pending:true} : undefined;
assert.equal(await t.sendTyped('  Exact input  '),true);
assert.equal(requests.filter(r=>r.url.endsWith('/live/input')).at(-1).body.admission,'async');
liveFetch=(url)=>url.endsWith('/live/events') ? {ok:true,cursor:3,events:[
  {sequence:1,type:'operation',operation_id:'operation-one',state:'deciding',pending:true},
  {sequence:2,type:'transcript',role:'user',text:' ',final:false,finality:'delta',event_id:'space'},
  {sequence:3,type:'operation',operation_id:'operation-one',state:'completed',pending:false},
]} : undefined;
await pollNow(t);
assert.equal(captions[0].text,' '); assert.equal(captions[0].final,false);
assert.equal(t.operations.get('operation-one').state,'completed');
assert(statuses.at(-1).includes('job status is shown separately'));
assert.equal(fullResults.length,0); assert.equal(sent.length,0);
t.stop();
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_close_stops_local_audio_before_waiting_for_capture_and_revokes_after_ack():
    script = LIVE_HARNESS + r"""
(async()=>{
const t=makeLive(); await t.start(); await waitFor(()=>!t.livePolling);
let release;
liveFetch=(url)=>url.endsWith('/live/close') ? new Promise(resolve=>{
  release=()=>resolve({ok:true});
}) : undefined;
t.stop();
assert(tracks[0].stopped && audioElements[0].paused && t.task.closed);
assert.equal(requests.filter(r=>r.url==='/api/plugins/hermes-talk/close').length,0,
  'task revoked before pending transcript flush');
t.stop(); assert.equal(requests.filter(r=>r.url.endsWith('/live/close')).length,1);
release(); await drain();
assert.equal(requests.filter(r=>r.url==='/api/plugins/hermes-talk/close').length,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_snapshot_waits_for_capture_ack_without_blocking_event_polling():
    script = LIVE_HARNESS + r"""
(async()=>{
const t=makeLive(); await t.start(); await waitFor(()=>!t.livePolling);
let release;
liveFetch=(url)=>url.endsWith('/live/flush') ? new Promise(resolve=>{
  release=()=>resolve({ok:true});
}) : undefined;
const before=requests.filter(r=>r.url.endsWith('/state')).length;
const pending=t.task.refresh(); await waitFor(()=>release);
assert.equal(requests.filter(r=>r.url.endsWith('/state')).length,before);
await pollNow(t); assert(!t.closed);
release(); await pending;
assert.equal(requests.filter(r=>r.url.endsWith('/state')).length,before+1);
t.stop();
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = run(["node", "-e", script, str(DASHBOARD_JS)], capture_output=True,
                 text=True, timeout=NODE_TIMEOUT_S)
    assert result.returncode == 0, result.stdout + result.stderr
