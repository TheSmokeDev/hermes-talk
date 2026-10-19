"""Shared UI generation and optional host lifecycle regressions (offline Node)."""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from subprocess import run

import pytest
from test_dashboard_js import LIVE_HARNESS, NODE_TIMEOUT_S, PAGE_HARNESS

ROOT = Path(__file__).resolve().parents[1]
BUILDER = runpy.run_path(str(ROOT / "scripts/build_ui.py"))
SHARED = ROOT / "ui/talk-surface.js"


def test_generated_ui_is_current():
    assert BUILDER["build"](ROOT, check=True) == 0


def test_presentation_controls_cannot_override_live_state_or_actions():
    script = PAGE_HARNESS + r"""
function NativePresentation() {}
let hidden = 0;
const hide = () => {hidden++;};
cursor = 0;
const tree = Page({presentation: NativePresentation, presentationProps: {
  open: true, hide, active: true, startTalk: 'not-an-action',
}});
assert.equal(tree.tag, NativePresentation);
assert.equal(tree.props.open, true);
assert.equal(tree.props.hide, hide);
assert.equal(tree.props.active, false);
assert.equal(typeof tree.props.startTalk, 'function');
assert.equal(requests.length, 0);
tree.props.hide(); assert.equal(hidden, 1);
process.exit(0);
"""
    completed = run(
        ["node", "-e", script, str(ROOT / "dashboard/dist/index.js")],
        cwd=ROOT, capture_output=True, text=True, timeout=NODE_TIMEOUT_S,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("desktop", [False, True])
def test_build_is_deterministic_and_check_never_writes(tmp_path, desktop):
    (tmp_path / "ui").mkdir()
    (tmp_path / "dashboard/dist").mkdir(parents=True)
    for name in ("talk-surface.js", "dashboard-entry.js"):
        (tmp_path / "ui" / name).write_bytes((ROOT / "ui" / name).read_bytes())
    css = '/* ${literal} \"quoted\" 雪 */\n.ht-page { color: red; }\n'
    (tmp_path / "dashboard/dist/style.css").write_text(css, encoding="utf-8", newline="\n")
    if desktop:
        (tmp_path / "ui/desktop-entry.js").write_text(
            "const TALK_CSS = __HERMES_TALK_CSS__;\nexport { createTalkSurface, TALK_CSS };\n",
            encoding="utf-8",
        )
    assert BUILDER["build"](tmp_path) == 0
    outputs = list(BUILDER["render_bundles"](tmp_path))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in outputs}
    assert len(outputs) == (2 if desktop else 1)
    assert BUILDER["build"](tmp_path) == 0
    assert BUILDER["build"](tmp_path, check=True) == 0
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in outputs}
    if desktop:
        generated = (tmp_path / "desktop/plugin.js").read_text(encoding="utf-8")
        assert "const TALK_CSS = " + json.dumps(css, ensure_ascii=True) + ";" in generated
        assert generated.count("function createTalkSurface(SDK)") == 1
        assert "__HERMES_TALK_CSS__" not in generated
    with (tmp_path / "ui/talk-surface.js").open("a", encoding="utf-8") as stream:
        stream.write("\n// changed source\n")
    assert BUILDER["build"](tmp_path, check=True) == 1
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in outputs}
    assert BUILDER["build"](tmp_path) == 0
    assert BUILDER["build"](tmp_path, check=True) == 0


@pytest.mark.parametrize("token_count", [0, 2])
def test_desktop_requires_one_css_placeholder_before_writing(tmp_path, token_count):
    (tmp_path / "ui").mkdir()
    for name in ("talk-surface.js", "dashboard-entry.js"):
        (tmp_path / "ui" / name).write_text("// source\n", encoding="utf-8")
    (tmp_path / "ui/desktop-entry.js").write_text(
        "__HERMES_TALK_CSS__" * token_count, encoding="utf-8"
    )
    with pytest.raises(ValueError, match="exactly one"):
        BUILDER["build"](tmp_path)
    assert not (tmp_path / "dashboard/dist/index.js").exists()


TRANSPORT_HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
let captures = 0, peers = 0, offers = 0, trackStops = 0, releases = 0, closed = 0;
let captureOverride, offerOverride;
const requests = [], peerEvents = {};
const media = { getTracks() { return [{stop() { trackStops++; }}]; },
  getAudioTracks() { return []; } };
class Peer {
  constructor() { peers++; this.connectionState = 'new'; }
  addEventListener(name, callback) { peerEvents[name] = callback; }
  addTrack() {}
  createDataChannel() { return { readyState:'connecting', addEventListener() {}, close() {} }; }
  async createOffer() { return {type:'offer', sdp:'offer'}; }
  async setLocalDescription() {}
  async setRemoteDescription() {}
  close() { this.connectionState = 'closed'; }
}
const window = { setTimeout, clearTimeout, sessionStorage:{getItem(){return '';}} };
const sdk = { React:{createElement(){}}, hooks:{}, components:{},
  async fetchJSON(url, opts, timeoutMs) {
    requests.push({url, opts, timeoutMs}); return {output:'ok'}; }
};
const context = vm.createContext({window, console, AbortController, RTCPeerConnection:Peer,
  navigator:{mediaDevices:{async getUserMedia() {
    captures++; return captureOverride ? captureOverride() : media;
  }}}, document:{body:{appendChild(){}}, createElement(){return {style:{},remove(){}};}},
  async fetch() { offers++; if (offerOverride) return offerOverride();
    return {ok:true, async text(){return 'answer';}}; }
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const surface = context.createTalkSurface(sdk);
const make = (session={}) => new surface.TalkTransport(session, {
  onStatus(){}, onError(){}, onClosed(){closed++;}
});
const tick = () => new Promise(resolve=>setTimeout(resolve,0));
const waitFor = async predicate => {
  for(let i=0;i<100 && !predicate();i++) await tick();
  assert(predicate(), 'awaited lifecycle stage did not occur');
};
const leaseController = new AbortController();
const lease = {signal:leaseController.signal,release(){releases++;}};
"""


def run_node(script, source=SHARED):
    result = run(
        ["node", "-e", script, str(source)],
        capture_output=True,
        text=True,
        timeout=NODE_TIMEOUT_S,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_factory_has_no_dashboard_bootstrap_and_pins_its_sdk():
    run_node(TRANSPORT_HARNESS + r"""
(async()=>{
assert.deepEqual(Object.keys(surface).sort(), ['TalkPage','TalkTransport','LiveTransport',
  'makeTransport','appendTranscriptRows','controlLabel','steeringLabel'].sort());
assert.equal(window.__HERMES_TALK_TEST__, undefined);
window.__HERMES_PLUGIN_SDK__ = {fetchJSON(){throw Error('wrong owner');}};
const t=make();
await t.handleFunctionCall({call_id:'call',name:'status',arguments:'{}'});
assert.equal(requests.length,1);
assert.equal(requests[0].timeoutMs,6500);
assert(requests[0].opts.signal instanceof AbortSignal);
t.stop();
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


@pytest.mark.parametrize("scenario", [
    r"""
const t=make(); await t.start(); assert.equal(captures,1); assert.equal(offers,1);
t.stop(); t.stop(); assert.equal(trackStops,1); assert.equal(releases,0);
""",
    r"""
sdk.acquireMicrophone=async()=>lease;
const t=make(); await t.start(); t.stop(); t.stop(); leaseController.abort();
assert.equal(releases,1); assert.equal(trackStops,1); assert.equal(closed,0);
""",
    r"""
sdk.acquireMicrophone=async()=>lease;
const t=make(); await t.start(); leaseController.abort(); t.stop();
assert(t.closed); assert.equal(releases,1); assert.equal(trackStops,1); assert.equal(closed,1);
""",
    r"""
let resolveLease, acquisitionSignal;
sdk.acquireMicrophone=({signal})=>{acquisitionSignal=signal;
  return new Promise(resolve=>{resolveLease=resolve;});};
const t=make(), starting=t.start(); await waitFor(()=>resolveLease);
t.stop(); assert(acquisitionSignal.aborted); resolveLease(lease); await starting;
assert.equal(captures,0); assert.equal(peers,0); assert.equal(releases,1);
""",
    r"""
leaseController.abort(); sdk.acquireMicrophone=async()=>lease;
const t=make(); await t.start(); assert(t.closed);
assert.equal(captures,0); assert.equal(peers,0); assert.equal(releases,1);
""",
    r"""
let resolveCapture; captureOverride=()=>new Promise(resolve=>{resolveCapture=resolve;});
sdk.acquireMicrophone=async()=>lease;
const t=make(), starting=t.start(); await waitFor(()=>resolveCapture);
leaseController.abort(); assert(t.closed); assert.equal(releases,1);
resolveCapture(media); await starting; assert.equal(trackStops,1); assert.equal(offers,0);
""",
    r"""
sdk.acquireMicrophone=async()=>lease; captureOverride=()=>{throw Error('capture denied');};
const t=make(); await assert.rejects(t.start(),/capture denied/); t.stop();
assert(t.closed); assert.equal(releases,1); assert.equal(offers,0);
""",
    r"""
sdk.acquireMicrophone=async()=>lease; offerOverride=()=>{throw Error('offer refused');};
const t=make(); await assert.rejects(t.start(),/offer refused/); t.stop();
assert(t.closed); assert.equal(releases,1); assert.equal(trackStops,1);
""",
    r"""
sdk.validateVoiceMode=async value=>{assert.equal(value.voiceMode,'cascade');
  throw Error('unsupported mode');};
sdk.acquireMicrophone=async()=>{throw Error('lease must not be acquired');};
const t=make({voiceMode:'cascade'}); await assert.rejects(t.start(),/unsupported mode/);
assert.equal(captures,0); assert.equal(peers,0); assert.equal(offers,0);
""",
    r"""
let resolveValidation; sdk.validateVoiceMode=()=>new Promise(resolve=>{resolveValidation=resolve;});
sdk.acquireMicrophone=async()=>{throw Error('cancelled validation acquired a lease');};
const t=make(), starting=t.start(); await waitFor(()=>resolveValidation);
t.stop(); resolveValidation(); await starting; assert.equal(captures,0); assert.equal(peers,0);
""",
])
def test_transport_microphone_lifecycle(scenario):
    run_node(TRANSPORT_HARNESS + "\n(async()=>{\n" + scenario + r"""
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


@pytest.mark.parametrize("cancel", [False, True])
def test_page_validates_before_mint_and_drops_late_validation(cancel):
    script = PAGE_HARNESS + "\n(async()=>{\n" + r"""
render(); await drain(); let tree=render(), finish;
sdk.validateVoiceMode=()=>new Promise((resolve,reject)=>{finish={resolve,reject};});
button(tree,'Start legacy Talk').props.onClick(); await waitFor(()=>finish);
assert.equal(requests.filter(row=>row.url.endsWith('/session')).length,0);
""" + (r"""
listeners.pagehide(); finish.resolve(); await drain();
""" if cancel else r"""
finish.reject(Error('Cascade is unavailable in Desktop.')); await drain();
assert(label(render()).includes('Cascade is unavailable in Desktop.'));
""") + r"""
assert.equal(requests.filter(row=>row.url.endsWith('/session')).length,0);
assert.equal(transports.length,0);
for(const slot of slots) if(slot && slot.cleanup) slot.cleanup();
process.exit(0);
})().catch(e=>{console.error(e);process.exit(1);});
"""
    run_node(script, ROOT / "dashboard/dist/index.js")


def test_task_request_keeps_abort_signal_and_original_sdk_on_close():
    run_node(TRANSPORT_HARNESS + r"""
(async()=>{
let finish;
sdk.fetchJSON=(url,opts,timeoutMs)=>{
  requests.push({url,opts,timeoutMs});
  if(url.endsWith('/state')) return new Promise(resolve=>{finish=resolve;});
  return Promise.resolve({ok:true});
};
const t=make({task:{connection_id:'original-connection',generation:7}});
const pending=t.task.request('/state',{});
await waitFor(()=>finish);
const request=requests.find(row=>row.url.endsWith('/state'));
assert(request.opts.signal instanceof AbortSignal);
window.__HERMES_PLUGIN_SDK__={fetchJSON(){throw Error('new owner must not receive close');}};
t.stop(); assert(request.opts.signal.aborted); finish({ok:true});
await assert.rejects(pending,/closed or request cancelled/);
assert.equal(requests.filter(row=>row.url.endsWith('/close')).length,1);
assert.equal(JSON.parse(requests.at(-1).opts.body).connection_id,'original-connection');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_live_microphone_revocation_keeps_original_close_order():
    run_node(LIVE_HARNESS + r"""
(async()=>{
let released=0;
const owner=new AbortController();
window.__HERMES_PLUGIN_SDK__.acquireMicrophone=async()=>({
  signal:owner.signal,release(){released++;}
});
const t=makeLive(); await t.start(); await waitFor(()=>!t.livePolling);
owner.abort(); t.stop(); await drain();
assert.equal(released,1); assert.equal(closedNotifications,1);
assert(tracks.every(track=>track.stopped));
const closes=requests.filter(row=>row.url.endsWith('/close'));
assert.deepEqual(closes.map(row=>row.url),[
  '/api/plugins/hermes-talk/live/close','/api/plugins/hermes-talk/close'
]);
assert(closes.every(row=>row.body.connection_id==='bound-live-task'));
assert.equal(closes[0].body.binding_id,'opaque-live');
})().catch(e=>{console.error(e);process.exitCode=1;});
""", ROOT / "dashboard/dist/index.js")
