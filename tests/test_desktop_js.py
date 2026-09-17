"""Exercise the bundled Desktop entry against the host SDK boundary."""

import json
from pathlib import Path
from subprocess import run

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const calls = [], registered = [], disposers = [];
const React = {createElement(type, props, ...children) {return {type, props, children};}};
const atom = value => {
  const listeners = new Set();
  return {get() {return value;}, set(next) {value = next; listeners.forEach(fn=>fn(next));},
    subscribe(fn) {listeners.add(fn); return ()=>listeners.delete(fn);}};
};
const hostState = {
  focusedSessionOwner:atom({connectionId:'connection-a',profile:'profile-a'}),
  focusedSessionId:atom('pane-a'), focusedStoredSessionId:atom('stored-a'),
  focusedSessionProfile:atom('profile-a'),
  connectionId:atom('connection-a'), profile:atom('profile-a')};
const requests = [];
const HermesSDK = {Button:'button',Input:'input',
  host:{state:hostState, notify(){},
    request(method, params) {
      requests.push({method, params});
      return Promise.resolve({title:'Fixture conversation',
        session_key:hostState.focusedStoredSessionId.get()});
    }}};
const stockHooks = () => {
  React.useState = initial => [typeof initial === 'function' ? initial() : initial, ()=>{}];
  React.useEffect = () => {};
  React.useRef = value => ({current:value});
};
const context = vm.createContext({React,HermesSDK,Headers,DOMException,AbortController,
  console, window:{setTimeout,clearTimeout,sessionStorage:{getItem(){return '';}}}});
const source = fs.readFileSync(process.argv[1],'utf8')
  .replace(/^import .* from .*\r?\n/gm,'')
  .replace(/export function /g,'function ')
  .replace('export default {','globalThis.plugin = {');
vm.runInContext(source,context);
const owner = {connectionId:'connection-a',profile:'profile-a',sessionId:'pane-a',
  storedSessionId:'stored-a'};
const leaseController = new AbortController();
let acquires = 0, releases = 0;
const controller = {capabilities:{microphoneLease:1,pinnedRest:1,prepareSession:1},owner,
  async acquire() {acquires++;return {signal:leaseController.signal,release(){releases++;}};}};
const host = {rest(path,options) {calls.push({path,options});return Promise.resolve({ok:true});},
  register(entry) {registered.push(entry);},onDispose(fn){disposers.push(fn);}};
const createSDK=()=>context.createDesktopTalkSDK(host,controller);
"""


def run_node(script, *, source="desktop/plugin.js"):
    result = run(
        ["node", "-e", HARNESS + script, str(ROOT / source)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_desktop_pins_route_token_and_late_receipt():
    run_node(r"""
(async()=>{
const sdk=createSDK(); let finish;
host.rest=(path,options)=>{calls.push({path,options});return new Promise(r=>{finish=r;});};
const abort = new AbortController();
const pending=sdk.fetchJSON('/api/plugins/hermes-talk/session',{
  method:'POST',body:JSON.stringify({taskId:'task-a'}),signal:abort.signal,
  headers:{'x-talk-token':'talk-gate','Authorization':'Bearer unrelated-host-token'}},17500);
controller.owner={...owner,connectionId:'connection-b',profile:'profile-b'};
abort.abort(); finish({receipt:'session-a'});
assert.equal((await pending).receipt,'session-a');
assert.equal(calls[0].options.scope.connectionId,'connection-a');
assert.equal(calls[0].options.scope.profile,'profile-a');
assert.equal(calls[0].options.pluginToken,'talk-gate');
assert.equal(calls[0].options.timeoutMs,17500);
assert.equal(calls[0].options.body.taskId,'task-a');
assert.equal(calls[0].options.headers,undefined);
host.rest=async(path,options)=>{calls.push({path,options});return {};};
await sdk.fetchJSON('/api/plugins/hermes-talk/close',{
  method:'POST',body:JSON.stringify({sessionId:'session-a'})});
assert.equal(calls[1].options.scope.connectionId,'connection-a');
assert.equal(calls[1].options.scope.profile,'profile-a');
await assert.rejects(sdk.acquireMicrophone(),/conversation changed/);
assert.equal(acquires,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


@pytest.mark.parametrize("scenario", [
    r"""
controller.capabilities={microphoneLease:1};
assert.throws(createSDK,/incomplete Talk contract/);
""",
    r"""
controller.owner=null; assert.throws(createSDK,/connected Hermes conversation/);
""",
    r"""
const sdk=createSDK(); const abort=new AbortController();abort.abort();
await assert.rejects(sdk.fetchJSON('/api/plugins/hermes-talk/status',{
  signal:abort.signal}),{name:'AbortError'});assert.equal(calls.length,0);
""",
    r"""
const sdk=createSDK();
await assert.rejects(sdk.fetchJSON('/api/plugins/another-plugin/status'),/different plugin/);
await assert.rejects(sdk.fetchJSON('/api/plugins/hermes-talk/session',{body:'{broken'}));
assert.equal(calls.length,0);
""",
    r"""
const sdk=createSDK();
sdk.validateVoiceMode({voiceMode:'live'});sdk.validateVoiceMode({voiceMode:'native'});
assert.throws(()=>sdk.validateVoiceMode({voiceMode:'cascade'}),/supports GPT-Live/);
assert.throws(()=>sdk.validateVoiceMode({voiceMode:'unrecognized'}));
assert.equal(acquires,0);assert.equal(calls.length,0);
""",
    r"""
controller.acquire=async()=>null;
await assert.rejects(createSDK().acquireMicrophone(),/microphone ownership/);
""",
    r"""
controller.acquire=async()=>({release(){releases++;}});
await assert.rejects(createSDK().acquireMicrophone(),/microphone ownership/);
assert.equal(releases,1);
""",
    r"""
const lease=await createSDK().acquireMicrophone();assert.equal(acquires,1);
assert.equal(lease.signal,leaseController.signal);lease.release();assert.equal(releases,1);
""",
])
def test_desktop_rejects_unavailable_or_invalid_operations(scenario):
    run_node("\n(async()=>{\n" + scenario + r"""
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_desktop_registers_inside_the_composer_without_starting_audio():
    run_node(r"""
context.plugin.register(host);
assert.equal(context.plugin.id,'hermes-talk');assert.equal(registered.length,2);
assert.equal(registered[0].area,'composer.actions');
assert.equal(registered[1].area,'titleBar.tools.right');
assert.equal(typeof registered[1].data.onSelect,'function');
assert.equal(typeof registered[0].render().type,'function');
assert.equal(acquires,0);assert.equal(calls.length,0);
assert.equal(disposers.length,1);disposers[0]();
""")


@pytest.mark.parametrize("status", [401, 403, 503])
def test_desktop_exposes_http_status_inside_electron_errors(status):
    run_node(r"""
(async()=>{
const message = STATUS + ': {"detail":"fixture refusal"}';
host.rest=async()=>{
  throw new Error("Error invoking remote method 'hermes:api': Error: " + message);
};
await assert.rejects(createSDK().fetchJSON('/api/plugins/hermes-talk/status'),
  error=>error.message===message);
const unchanged = new Error('Temporary outage while retrieving item 401');
host.rest=async()=>{throw unchanged;};
await assert.rejects(createSDK().fetchJSON('/api/plugins/hermes-talk/status'),
  error=>error===unchanged);
assert.equal(acquires,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
""".replace("STATUS", str(status)))


def test_same_owner_controller_refresh_preserves_page_and_updates_acquisition():
    run_node(r"""
(async()=>{
let ref, memo, deps;
React.useRef=value=>ref||(ref={current:value});
React.useMemo=(factory,next)=>{
  if(!deps || deps.some((value,index)=>value!==next[index])){memo=factory();deps=next;}
  return memo;
};
const first=context.DesktopTalkPanel({context:host,controller});
let latest=controller, refreshedAcquires=0;
const sdk=context.createDesktopTalkSDK(host,()=>latest);
const refreshed={...controller,owner:{...owner},async acquire(){
  refreshedAcquires++;return {signal:leaseController.signal,release(){}};}};
latest=refreshed;
const second=context.DesktopTalkPanel({context:host,controller:refreshed});
assert.equal(first.children[1].type,second.children[1].type,
  'same-owner controller refresh must retain the mounted TalkPage');
await sdk.acquireMicrophone();assert.equal(refreshedAcquires,1);assert.equal(acquires,0);
const third=context.DesktopTalkPanel({context:host,
  controller:{...refreshed,owner:{...owner,storedSessionId:'different-task'}}});
assert.equal(second.children[1].type,third.children[1].type,
  'the action owns remount/close; preparation must preserve the in-flight surface');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_opening_talk_cannot_submit_the_composer_draft():
    run_node(r"""
React.useState=()=>[null,()=>{}];React.useEffect=()=>{};
React.useRef=value=>({current:value});
HermesSDK.useComposerVoiceController=()=>controller;
HermesSDK.Popover='popover';HermesSDK.PopoverTrigger='popover-trigger';
HermesSDK.PopoverContent='popover-content';
const tree=context.DesktopTalkAction();
const popover=tree.children[0];
assert.equal(popover.type,'popover');assert.equal(popover.props.modal,false);
assert.equal(popover.children[0].children[0].props.type,'button');
popover.props.onOpenChange(true);assert.equal(calls.length,0);assert.equal(acquires,0);
const presentation=context.DesktopTalkPresentation({popoverOpen:true,
  onPopoverOpenChange(){},stopTalk(){}});
let stopped=0;
presentation.children[1].props.onSubmit({stopPropagation(){stopped++;}});
assert.equal(stopped,1,'Talk form submits must stop before the host composer');
""")


def test_desktop_prepares_exact_conversation_before_catalog_and_microphone():
    run_node(r"""
(async()=>{
for (const draft of [false,true]) {
  controller.owner={...owner,storedSessionId:draft?null:owner.storedSessionId};
  controller.capabilities.prepareSession=1;
  let prepared=false, pendingPublication;
  const next={...owner,sessionId:'prepared-runtime'};
  controller.prepareSession=async()=>{
    prepared=true;
    pendingPublication=setTimeout(()=>{controller.owner=next;},5);
    return next;
  };
  host.rest=async(path,options)=>{
    assert(prepared);assert.equal(path,'/targets');
    assert.equal(options.scope.connectionId,owner.connectionId);
    assert.equal(options.body.session_id,owner.storedSessionId);
    assert.equal(options.body.profile,owner.profile);
    assert.equal(options.pluginToken,undefined);
    return {ok:true,targets:[{target_id:'target-current',peer_id:'local',
      profile:owner.profile,session_id:owner.storedSessionId}]};
  };
  const notices=[];
  const sdk=context.createDesktopTalkSDK(host,()=>controller,(...args)=>notices.push(args));
  const result=await sdk.prepareTask({tabId:'tab-a'});
  clearTimeout(pendingPublication);
  assert.equal(result.target_id,'target-current');
  assert.equal(sdk.desktopOwner.storedSessionId,owner.storedSessionId);
  assert.equal(notices[0][0],true);assert.equal(notices.at(-1)[0],false);
  assert.equal(acquires,0,'preparing does not open the microphone');
}
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_desktop_refuses_wrong_or_cancelled_preparation_without_fallback():
    run_node(r"""
(async()=>{
for (const scenario of ['other-task','other-host','resume-failed','cancel','wrong-catalog']) {
  controller.owner={...owner};controller.capabilities.prepareSession=1;
  const abort=new AbortController();let catalog=0;
  controller.prepareSession=async()=>{
    if(scenario==='resume-failed') throw Error('Resume failed');
    if(scenario==='cancel') abort.abort();
    return {...owner,...(scenario==='other-task'?{storedSessionId:'other'}:{}),
      ...(scenario==='other-host'?{connectionId:'other'}:{})};
  };
  host.rest=async()=>{catalog++;return {ok:true,targets:[{target_id:'wrong',
    peer_id:'local',profile:owner.profile,session_id:'somewhere-else'}]};};
  await assert.rejects(createSDK().prepareTask({tabId:'tab-a',signal:abort.signal}));
  assert.equal(catalog,scenario==='wrong-catalog'?1:0);
  assert.equal(acquires,0);
}
})().catch(e=>{console.error(e);process.exitCode=1;});
""")

@pytest.mark.parametrize(("message", "expected", "retry"), [
    (
        '400: {"error":"invalid_event","detail":"file://fixture/private?token=fixture-private-token"}',
        "Talk could not complete this request. Try again.",
        True,
    ),
    (
        '401: {"detail":"fixture-private-token"}',
        "Reconnect to this Hermes connection and try again.",
        False,
    ),
    (
        '403: {"detail":"fixture-private-token"}',
        "Reconnect to this Hermes connection and try again.",
        False,
    ),
    (
        "Temporary failure retrieving item 401: fixture-private-token",
        "Talk could not complete this request. Try again.",
        True,
    ),
    (
        '503: {"detail":"fixture-private-token"}',
        "Talk is temporarily unavailable. Try again.",
        True,
    ),
    (
        "NotAllowedError: Permission denied; fixture-private-token",
        "Allow microphone access for Hermes in your system settings, then try again.",
        True,
    ),
])
def test_desktop_notice_distinguishes_request_failure_from_auth(message, expected, retry):
    run_node(r"""
const message = __MESSAGE__, expected = __EXPECTED__, retry = __RETRY__;
let starts = 0, refreshes = 0;
const tree = context.DesktopTalkView({
  status:{configured:true,source:'subscription'},ready:true,error:new Error(message),
  tasks:[],transcript:[],results:{},startTalk(){starts++;},refresh(){refreshes++;},
});
function nodes(node) {
  if (!node || typeof node !== 'object') return [];
  return [node,...(node.children||[]).flat(Infinity).flatMap(nodes)];
}
function text(node) {
  if (typeof node === 'string') return node;
  return (node?.children||[]).flat(Infinity).map(text).join(' ');
}
const alert = nodes(tree).find(node=>node.props?.role==='alert');
assert(alert,'the failure must produce an actionable notice');
assert(text(alert).includes(expected));
const action = nodes(alert).find(node=>node.type==='button');
assert.equal(text(action),retry?'Try again':'Check connection');
assert.equal(action.props.type,'button');
action.props.onClick();
assert.equal(starts,retry?1:0);assert.equal(refreshes,retry?0:1);
for (const privateDetail of ['fixture-private-token','invalid_event','file://']) {
  assert(!text(tree).includes(privateDetail),'raw server error details must stay out of the view');
}
assert.equal(calls.length,0);assert.equal(acquires,0);
""".replace("__MESSAGE__", json.dumps(message))
        .replace("__EXPECTED__", json.dumps(expected))
        .replace("__RETRY__", json.dumps(retry)), source="ui/desktop-view.js")


def test_stock_lane_prepares_the_focused_conversation_before_the_catalog():
    run_node(r"""
(async()=>{
stockHooks();
const stock=context.useStockVoiceController();
assert.equal(stock.capabilities.lane,'stock');
assert.equal(stock.capabilities.microphoneLease,0);
assert.equal(stock.capabilities.pinnedRest,0);
assert.deepEqual(JSON.parse(JSON.stringify(stock.owner)),{connectionId:'connection-a',
  profile:'profile-a',sessionId:'pane-a',storedSessionId:'stored-a'});
host.rest=async(path,options)=>{
  assert.equal(requests.length,1,'the stored conversation is confirmed before the catalog');
  calls.push({path,options});
  return {ok:true,targets:[{target_id:'target-a',peer_id:'local',profile:'profile-a',
    session_id:'stored-a'}]};
};
const sdk=context.createDesktopTalkSDK(host,()=>stock);
const target=await sdk.prepareTask({tabId:'tab-a'});
assert.equal(target.target_id,'target-a');
assert.equal(requests[0].method,'session.title');
assert.equal(requests[0].params.session_id,'pane-a');
assert.equal(requests[0].params.title,undefined,'a title read must not rename the conversation');
assert.equal(calls.length,1);assert.equal(calls[0].path,'/targets');
assert.equal(calls[0].options.body.session_id,'stored-a');
assert.equal(calls[0].options.body.profile,'profile-a');
assert.equal(sdk.desktopOwner.storedSessionId,'stored-a');
assert.equal(acquires,0,'preparing does not open the microphone');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_stock_lane_refuses_a_stored_session_mismatch():
    run_node(r"""
(async()=>{
stockHooks();
const stock=context.useStockVoiceController();
HermesSDK.host.request=async(method,params)=>{
  requests.push({method,params});
  return {title:'Fixture conversation',session_key:'stored-elsewhere'};
};
const sdk=context.createDesktopTalkSDK(host,()=>stock);
await assert.rejects(sdk.prepareTask({tabId:'tab-a'}),/conversation changed/);
assert.equal(requests.length,1);
assert.equal(calls.length,0,'a mismatched conversation never reaches the catalog');
assert.equal(acquires,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_stock_lane_refuses_when_the_active_profile_moved():
    run_node(r"""
(async()=>{
stockHooks();
const stock=context.useStockVoiceController();
const sdk=context.createDesktopTalkSDK(host,()=>stock);
hostState.profile.set('profile-b');
await assert.rejects(sdk.prepareTask({tabId:'tab-a'}),/follows the active profile/);
assert.equal(requests.length,0,'a moved profile is refused before the host is asked');
assert.equal(calls.length,0);assert.equal(acquires,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_stock_lane_rest_refuses_after_the_active_profile_moves():
    run_node(r"""
(async()=>{
stockHooks();
const stock=context.useStockVoiceController();
const sdk=context.createDesktopTalkSDK(host,()=>stock);
await sdk.fetchJSON('/api/plugins/hermes-talk/status');
assert.equal(calls.length,1);
assert.equal(calls[0].options.scope.profile,'profile-a');
hostState.connectionId.set('connection-b');
await assert.rejects(sdk.fetchJSON('/api/plugins/hermes-talk/session',
  {method:'POST',body:JSON.stringify({taskId:'task-a'})}),/follows the active profile/);
assert.equal(calls.length,1,'no request leaves for the moved connection');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


@pytest.mark.parametrize(("atom_name", "expected"), [
    ("focusedStoredSessionId", "Send one message in this conversation first"),
    ("focusedSessionId", "Send one message in this conversation first"),
])
def test_stock_lane_asks_for_a_first_message_when_the_conversation_is_unsaved(atom_name, expected):
    run_node(r"""
(async()=>{
stockHooks();
hostState.__ATOM__.set(null);
const stock=context.useStockVoiceController();
const sdk=context.createDesktopTalkSDK(host,()=>stock);
await assert.rejects(sdk.prepareTask({tabId:'tab-a'}),/__EXPECTED__/);
assert.equal(requests.length,0);
assert.equal(calls.length,0);assert.equal(acquires,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
""".replace("__ATOM__", atom_name).replace("__EXPECTED__", expected))


def test_stock_lane_grants_a_release_only_microphone_lease():
    run_node(r"""
(async()=>{
stockHooks();
let prompts=0;
context.window.hermesDesktop={requestMicrophoneAccess(){
  prompts++;return Promise.reject(new Error('Hermes has no microphone bridge'));}};
const stock=context.useStockVoiceController();
const sdk=context.createDesktopTalkSDK(host,()=>stock);
const lease=await sdk.acquireMicrophone();
assert.equal(prompts,1,'a stock host is still asked for system microphone access');
assert.equal(lease.signal,stock.signal);
lease.release();
assert.equal(stock.signal.aborted,false,'releasing a stock lease cannot end the session');
sdk.stopHost();
assert.equal(stock.signal.aborted,true);
await assert.rejects(sdk.acquireMicrophone(),{name:'AbortError'});
assert.equal(prompts,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


@pytest.mark.parametrize(("message", "lane", "expected", "retry"), [
    (
        '404: {"detail":"target_missing"}',
        "stock",
        "Send one message in this conversation first, then Connect.",
        True,
    ),
    (
        '404: {"detail":"target_missing"}',
        None,
        "Send one message in this conversation first, then Connect.",
        True,
    ),
    (
        '404: {"detail":"unknown_route"}',
        "stock",
        "Talk could not complete this request. Try again.",
        True,
    ),
    (
        '401: {"detail":"fixture-private-token"}',
        "stock",
        "This Hermes Desktop cannot send TALK_DASHBOARD_TOKEN.",
        False,
    ),
    (
        '403: {"detail":"fixture-private-token"}',
        "stock",
        "This Hermes Desktop cannot send TALK_DASHBOARD_TOKEN.",
        False,
    ),
    (
        '401: {"detail":"fixture-private-token"}',
        None,
        "Reconnect to this Hermes connection and try again.",
        False,
    ),
])
def test_stock_lane_notice_explains_the_first_message_and_token_rules(
    message, lane, expected, retry
):
    run_node(r"""
const message = __MESSAGE__, expected = __EXPECTED__, retry = __RETRY__;
let starts = 0, refreshes = 0;
const tree = context.DesktopTalkView({
  status:{configured:true,source:'subscription'},ready:true,error:new Error(message),
  lane:__LANE__,tasks:[],transcript:[],results:{},
  startTalk(){starts++;},refresh(){refreshes++;},
});
function nodes(node) {
  if (!node || typeof node !== 'object') return [];
  return [node,...(node.children||[]).flat(Infinity).flatMap(nodes)];
}
function text(node) {
  if (typeof node === 'string') return node;
  return (node?.children||[]).flat(Infinity).map(text).join(' ');
}
const alert = nodes(tree).find(node=>node.props?.role==='alert');
assert(alert,'the failure must produce an actionable notice');
assert(text(alert).includes(expected));
const action = nodes(alert).find(node=>node.type==='button');
assert.equal(text(action),retry?'Try again':'Check connection');
action.props.onClick();
assert.equal(starts,retry?1:0);assert.equal(refreshes,retry?0:1);
assert(!text(tree).includes('fixture-private-token'),
  'raw server error details must stay out of the view');
assert.equal(calls.length,0);assert.equal(acquires,0);
""".replace("__MESSAGE__", json.dumps(message))
        .replace("__EXPECTED__", json.dumps(expected))
        .replace("__LANE__", json.dumps(lane))
        .replace("__RETRY__", json.dumps(retry)), source="ui/desktop-view.js")


def test_stock_lane_token_refusal_keeps_the_stock_lifetime():
    run_node(r"""
(async()=>{
stockHooks();
const stock=context.useStockVoiceController();
const sdk=context.createDesktopTalkSDK(host,()=>stock);
host.rest=async()=>{throw new Error(
  "Error invoking remote method 'hermes:api': Error: 401: {\"detail\":\"token required\"}");};
await assert.rejects(sdk.fetchJSON('/api/plugins/hermes-talk/status'),/^Error: 401:/);
assert.equal(stock.signal.aborted,false,
  'a stock host cannot present the token: the refusal stays a notice and the panel renders');
host.rest=async(path,options)=>{calls.push({path,options}); return {ok:true};};
await sdk.fetchJSON('/api/plugins/hermes-talk/status');
assert.equal(calls.length,1,'the panel still reaches the host after a token refusal');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_stock_lane_refuses_a_conversation_with_no_profile():
    run_node(r"""
(async()=>{
stockHooks();
hostState.focusedSessionOwner.set({connectionId:'connection-a'});
hostState.focusedSessionProfile.set(null);
hostState.profile.set(null);
const stock=context.useStockVoiceController();
assert.equal(stock.owner.connectionId,'connection-a');
assert.equal(stock.owner.profile,null,'an absent profile is never invented');
assert.throws(()=>context.createDesktopTalkSDK(host,()=>stock),
  /Open a connected Hermes conversation before starting Talk/);
assert.equal(requests.length,0,'no host request carries a fabricated profile');
assert.equal(calls.length,0,'no plugin request carries a fabricated profile');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_stock_lane_does_not_read_an_absent_profile_as_a_profile_named_local():
    run_node(r"""
(async()=>{
stockHooks();
hostState.focusedSessionOwner.set({connectionId:'connection-a',profile:'local'});
hostState.profile.set(null);
const stock=context.useStockVoiceController();
assert.equal(stock.owner.profile,'local','a conversation may genuinely use this name');
const sdk=context.createDesktopTalkSDK(host,()=>stock);
await assert.rejects(sdk.prepareTask({tabId:'tab-a'}),/follows the active profile/);
assert.equal(requests.length,0,'an unreported active profile is not that conversation');
assert.equal(calls.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
