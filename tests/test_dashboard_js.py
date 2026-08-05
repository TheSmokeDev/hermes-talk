"""Executable dashboard transport protocol regressions (Node, no browser)."""

from __future__ import annotations

from pathlib import Path
from subprocess import run

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_JS = ROOT / "dashboard" / "dist" / "index.js"


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
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
