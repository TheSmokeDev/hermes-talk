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


DESKTOP_TOOLS = r"""
const rpc = [];
let serial = 0;
const gateway = {async request(method,params) {
  rpc.push({method,params});
  if(method==='session.create') return {session_id:'new-'+(++serial)};
  if(method==='prompt.submit') return {admission_id:'admit-1',status:'queued',
    ref:{session_id:params.session_id}};
  if(method==='session.list') return {scope:params.scope,
    sessions:[{session_id:'stored-a',title:'A',secret:'hidden'}]};
  if(method==='slash.exec') return {type:'exec',output:'No scheduled jobs.'};
  return {ok:true};
}};
HermesSDK.host.getGateway=()=>gateway;
HermesSDK.host.toolsets={list:async scope=>[{name:'terminal',enabled:true,api_key:'sk-secret'}]};
HermesSDK.host.skills={list:async scope=>[{name:'example',enabled:true}]};
"""


def test_desktop_voice_preserves_server_tools_and_uses_scoped_catalog():
    run_node(DESKTOP_TOOLS + r"""
(async()=>{
const sdk=createSDK();
const patch=sdk.realtimeSessionPatch({instructions:'Existing rules',
 tools:[{name:'delegate_task'}]});
assert.equal(patch.tools.length,9);
assert(patch.instructions.startsWith('Existing rules'));
assert.equal(patch.tools[0].name,'delegate_task');
let seen;
HermesSDK.host.toolsets.list=async scope=>{seen=scope;
 return [{name:'terminal',enabled:true,api_key:'sk-secret'}];};
const output=await sdk.executeLocalTool('desktop_capabilities',{});
assert.deepEqual(JSON.parse(JSON.stringify(seen)),{connectionId:'connection-a',profile:'profile-a'});
assert.equal(output.toolsets[0].name,'terminal');
assert(!JSON.stringify(output).includes('secret'));
assert.equal(rpc.length,0);
})();
""")


def test_desktop_create_uses_new_session_and_reports_admission_not_completion():
    run_node(DESKTOP_TOOLS + r"""
(async()=>{
const sdk=createSDK();
const result=await sdk.executeLocalTool('desktop_create_session',{
 title:'Same title',initial_message:'Wait for my next message.'},{callId:'call-1'});
assert.equal(result.ok,true); assert.equal(result.session_id,'new-1');
assert.equal(result.admission.status,'queued');
assert.deepEqual(rpc.map(r=>r.method),
 ['session.list','session.create','session.title','prompt.submit']);
assert(!Object.hasOwn(rpc[1].params,'title'));
assert(!Object.hasOwn(rpc[1].params,'model'));
assert.equal(rpc[1].params.service_tier,'normal');
assert(rpc.every(r=>r.params.profile==='profile-a'));
assert.equal(rpc[3].params.submission_id,'talk-opening-call-1');
assert.equal(hostState.focusedSessionId.get(),'pane-a');
})();
""")


def test_desktop_tools_refuse_scope_drift_and_cancelled_calls():
    run_node(DESKTOP_TOOLS + r"""
(async()=>{
const sdk=createSDK(), abort=new AbortController(); abort.abort();
await assert.rejects(sdk.executeLocalTool('desktop_list_sessions',{},
 {signal:abort.signal}),/cancelled/);
hostState.connectionId.set('connection-b');
await assert.rejects(sdk.executeLocalTool('desktop_create_session',{
 title:'No',initial_message:'No'},{callId:'call-2'}),/active profile/);
assert.equal(rpc.length,0);
})();
""")


def test_desktop_partial_creation_retains_id_without_retrying():
    run_node(DESKTOP_TOOLS + r"""
(async()=>{
const sdk=createSDK(), original=gateway.request;
gateway.request=async (method,params)=>method==='prompt.submit'
 ? {admission_id:'uncertain',status:'unknown',ref:{session_id:params.session_id}}
 : original(method,params);
const result=await sdk.executeLocalTool('desktop_create_session',{
 title:'Partial',initial_message:'Wait'},{callId:'call-3'});
assert.equal(result.ok,false); assert.equal(result.created,true);
assert.equal(result.session_id,'new-1'); assert.equal(serial,1);
})();
""")


def test_desktop_inventory_uses_only_fixed_read_commands():
    run_node(DESKTOP_TOOLS + r"""
(async()=>{
const sdk=createSDK();
await sdk.executeLocalTool('desktop_scheduled_jobs',{command:'/cron remove all'});
await sdk.executeLocalTool('desktop_messaging',{});
assert.deepEqual(rpc.map(r=>r.params.command),['/cron list --all','/platforms']);
assert(rpc.every(r=>r.params.session_id==='pane-a'));
gateway.request=async()=>({type:'send',message:'Do work'});
await assert.rejects(sdk.executeLocalTool('desktop_messaging',{}),/read-only/);
})();
""")


def test_desktop_session_inventory_includes_closed_conversations():
    run_node(DESKTOP_TOOLS + r"""
(async()=>{
const sdk=createSDK();
gateway.request=async(method,params)=>({scope:params.scope,sessions: params.scope==='live'
 ? [{id:'live-1',title:'Live',started_at:2}]
 : [{id:'old-1',title:'Historical',started_at:1},{id:'live-1',title:'Live',started_at:2}]});
const result=await sdk.executeLocalTool('desktop_list_sessions',{});
assert.equal(result.sessions.length,2);
assert.equal(result.sessions[1].title,'Historical');
})();
""")


def test_ssh_legacy_protocol_uses_advertised_shapes_without_retrying_writes():
    run_node(DESKTOP_TOOLS + r"""
(async()=>{
const sdk=createSDK();
gateway.request=async(method,params)=>{
 rpc.push({method,params});
 if(method==='session.list' && params.scope) {
  const error=new Error('invalid params for session.list: scope: Extra inputs are not permitted');
  error.code=4000;throw error;
 }
 if(method==='session.list') return {sessions:[{id:'old-1',title:'Saved'}]};
 if(method==='session.create') return {session_id:'runtime-1',stored_session_id:'stored-1'};
 if(method==='prompt.submit') return {status:'streaming',user_row_id:12};
 if(method==='slash.exec') return {output:'No jobs'};
 return {ok:true};
};
const listed=await sdk.executeLocalTool('desktop_list_sessions',{});
assert.equal(listed.sessions[0].title,'Saved');
const created=await sdk.executeLocalTool('desktop_create_session',{
 title:'SSH',initial_message:'Wait for my next message.'},{callId:'ssh-1'});
assert.equal(created.ok,true); assert.equal(created.session_id,'stored-1');
const create=rpc.find(row=>row.method==='session.create').params;
assert.equal(create.idempotency_key,'talk-ssh-1');assert(!('request_id' in create));
const submit=rpc.find(row=>row.method==='prompt.submit').params;
assert(!('submission_id' in submit));
assert.equal((await sdk.executeLocalTool('desktop_scheduled_jobs',{})).report,'No jobs');
})();
""")


def test_project_directory_must_come_from_the_scoped_inventory():
    run_node(DESKTOP_TOOLS + r"""
(async()=>{
const sdk=createSDK(), base=gateway.request;
gateway.request=async(method,params)=>{
 if(method==='session.list' && params.scope) {
  const e=new Error('invalid params for session.list: scope: Extra inputs');e.code=4000;throw e;
 }
 if(method==='projects.list') return {projects:[{name:'Canary',primary_path:'/srv/canary',
  folders:[{path:'/srv/canary'}]}]};
 if(method==='prompt.submit') return {status:'streaming',user_row_id:1};
 return base(method,params);
};
const args={title:'Canary',initial_message:'Wait.',project_directory:'/srv/canary'};
await assert.rejects(sdk.executeLocalTool('desktop_create_session',args,{callId:'p1'}),
 /desktop_projects/);
assert.equal(rpc.length,0);
const listed=await sdk.executeLocalTool('desktop_projects',{});
assert.equal(listed.projects[0].primary_path,'/srv/canary');
await sdk.executeLocalTool('desktop_create_session',args,{callId:'p2'});
const create=rpc.find(row=>row.method==='session.create');
assert.equal(create.params.cwd,'/srv/canary');assert.equal(create.params.cwd_explicit,true);
})();
""")


def test_session_progress_and_messages_stay_on_verified_target():
    run_node(DESKTOP_TOOLS + r"""
(async()=>{
const sdk=createSDK(), base=gateway.request;
gateway.request=async(method,params)=>{
 if(method==='session.resume') {rpc.push({method,params});return {
  session_id:'stored-a',stored_session_id:'stored-a',running:false,execution_generation:7,
  info:{cwd:'/srv/project',provider:'openai-codex'}};}
 if(method==='session.history') return {messages:[{role:'user',content:'Question'},
  {role:'tool',content:'Do not expose this tool dump'},
  {role:'assistant',content:'DONE'}]};
 if(method==='session.steer') {rpc.push({method,params});return {status:'queued'};}
 return base(method,params);
};
await assert.rejects(sdk.executeLocalTool('desktop_message_session',{
 session_id:'foreign-profile',text:'bad'},{callId:'foreign'}),/current catalog/);
assert(!rpc.some(row=>row.method==='session.resume'));
const status=await sdk.executeLocalTool('desktop_session_status',{session_id:'stored-a'});
assert.equal(status.running,false);assert.equal(status.messages.length,2);
assert.equal(status.messages[1].text,'DONE');
const queued=await sdk.executeLocalTool('desktop_message_session',{
 session_id:'stored-a',text:'Next task'},{callId:'message-1'});
assert.equal(queued.ok,true);
const submit=rpc.find(row=>row.method==='prompt.submit').params;
assert.equal(submit.queued,true);assert.equal(submit.session_id,'stored-a');
assert.equal(submit.submission_id,'talk-message-message-1');
await sdk.executeLocalTool('desktop_message_session',{
 session_id:'stored-a',text:'Change direction',mode:'steer'},{callId:'steer-1'});
assert.equal(rpc.find(row=>row.method==='session.steer').params.execution_generation,7);
assert.equal(hostState.focusedSessionId.get(),'pane-a');
})();
""")


def test_session_identity_mismatch_refuses_message_and_failed_probe_never_creates():
    run_node(DESKTOP_TOOLS + r"""
(async()=>{
const sdk=createSDK(), base=gateway.request;
gateway.request=async(method,params)=>method==='session.resume'
 ? {session_id:'wrong',stored_session_id:'wrong'} : base(method,params);
await assert.rejects(sdk.executeLocalTool('desktop_message_session',{
 session_id:'stored-a',text:'Do not send'},{callId:'bad-id'}),/identity/);
assert(!rpc.some(row=>row.method==='prompt.submit'));
gateway.request=async()=>{throw new Error('Disconnected');};
await assert.rejects(sdk.executeLocalTool('desktop_create_session',{
 title:'No duplicate',initial_message:'Wait'},{callId:'no-create'}),/Disconnected/);
assert.equal(serial,0);
})();
""")


def test_old_exact_title_target_reports_inflight_progress():
    run_node(DESKTOP_TOOLS + r"""
(async()=>{
const sdk=createSDK();
gateway.request=async(method,params)=>{
 if(method==='session.list') return {scope:params.scope,sessions: params.title==='Old project'
  ? [{id:'old-250',title:'Old project'}] : []};
 if(method==='session.resume') return {session_id:'old-250',stored_session_id:'old-250',
  running:true,inflight:{assistant:'Running the checks now',streaming:true},
  todo_state:{todos:[{content:'Check tests',status:'in_progress'}]},
  pending:[{id:'pending'}],prompts:[{kind:'approval'}]};
 if(method==='session.history') return {messages:[{role:'assistant',content:'Previous turn'}]};
 throw new Error(method);
};
await sdk.executeLocalTool('desktop_list_sessions',{exact_title:'Old project'});
const result=await sdk.executeLocalTool('desktop_session_status',{session_id:'old-250'});
assert.equal(result.running,true);
assert.equal(result.in_progress.assistant,'Running the checks now');
assert.equal(result.todos[0].status,'in_progress');
assert.equal(result.queued_count,1); assert.equal(result.waiting_for_approval,true);
})();
""")


@pytest.mark.parametrize("method", ["session.create", "prompt.submit", "session.steer"])
def test_lost_write_reply_reports_uncertainty_without_dispatching_again(method):
    run_node(DESKTOP_TOOLS + "\nconst lostMethod=" + json.dumps(method) + r""";
(async()=>{
const sdk=createSDK(), base=gateway.request;
let writes=0;
gateway.request=async(method,params)=>{
 if(method===lostMethod) {writes++;throw new Error('Reply lost after acceptance');}
 if(method==='session.resume') return {session_id:'stored-a',stored_session_id:'stored-a',
  execution_generation:4};
 return base(method,params);
};
const result=lostMethod==='session.create'
 ? await sdk.executeLocalTool('desktop_create_session',{
    title:'Uncertain',initial_message:'Wait'},{callId:'lost-1'})
 : await sdk.executeLocalTool('desktop_message_session',{
    session_id:'stored-a',text:'Next instruction',
    mode:lostMethod==='session.steer'?'steer':'queue'},{callId:'lost-1'});
assert.equal(writes,1);assert.equal(result.ok,false);
assert.equal(lostMethod==='session.create'
 ? result.creation_uncertain : result.delivery_uncertain,true);
assert(result.note.includes('unconfirmed'));
assert(result.note.includes('Do not automatically repeat'));
if(lostMethod!=='session.create') assert.equal(result.session_id,'stored-a');
})();
""")


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
    (
        '503: {"detail":{"code":"context_unavailable","message":"fixture-private-token"}}',
        "Error code: 503 context_unavailable",
        True,
    ),
    (
        '503: {"detail":{"code":"Not A Code fixture-private-token","message":"x"}}',
        "Talk is temporarily unavailable. Try again.",
        True,
    ),
    (
        "GPT-Live needs a Hermes host with task context support, which this host does not "
        "report. Use OpenAI Realtime here (TALK_VOICE_MODE=native).",
        "GPT-Live needs a Hermes host with task context support",
        False,
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


def test_stock_lane_reconnects_after_stop_without_changing_conversation():
    run_node(r"""
(async()=>{
stockHooks();
let prompts=0;
context.window.hermesDesktop={requestMicrophoneAccess:async()=>{prompts++;}};
const stock=context.useStockVoiceController();
const sdk=context.createDesktopTalkSDK(host,()=>stock);
const first=await sdk.acquireMicrophone();
sdk.stopHost();
assert.equal(first.signal.aborted,true,'Stop invalidates the old microphone lease');
assert.equal(stock.signal.aborted,false,'Stop must not dispose the conversation controller');
const second=await sdk.acquireMicrophone();
assert.equal(second.signal.aborted,false);
assert.notEqual(second.signal,first.signal);
assert.equal(prompts,2);
second.release();
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_stock_lane_stop_cancels_pending_acquisition_but_allows_retry():
    run_node(r"""
(async()=>{
stockHooks();
let finishPrompt;
context.window.hermesDesktop={requestMicrophoneAccess:()=>new Promise(resolve=>{
  finishPrompt=resolve;
})};
const stock=context.useStockVoiceController();
const sdk=context.createDesktopTalkSDK(host,()=>stock);
const pending=sdk.acquireMicrophone();
sdk.stopHost();
finishPrompt();
await assert.rejects(pending,{name:'AbortError'});
context.window.hermesDesktop.requestMicrophoneAccess=async()=>{};
const retry=await sdk.acquireMicrophone();
assert.equal(retry.signal.aborted,false);
retry.release();
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


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
assert.notEqual(lease.signal,stock.signal);
lease.release();
assert.equal(lease.signal.aborted,true);
assert.equal(stock.signal.aborted,false,'releasing a stock lease cannot end the session');
sdk.stopHost();
assert.equal(stock.signal.aborted,false,'manual Stop preserves the mounted conversation');
stock.dispose();
assert.equal(stock.signal.aborted,true);
await assert.rejects(sdk.acquireMicrophone(),{name:'AbortError'});
assert.equal(prompts,1);
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_stock_lane_unmount_disposes_controller_and_active_lease():
    run_node(r"""
(async()=>{
stockHooks();
const effects=[];
React.useEffect=effect=>effects.push(effect);
const stock=context.useStockVoiceController();
const cleanup=effects[1]();
const sdk=context.createDesktopTalkSDK(host,()=>stock);
const lease=await sdk.acquireMicrophone();
cleanup();
assert.equal(stock.signal.aborted,true);
assert.equal(lease.signal.aborted,true);
await assert.rejects(sdk.acquireMicrophone(),{name:'AbortError'});
cleanup();
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


def test_popover_content_receives_focus_outside_guard():
    """PopoverContent must receive onFocusOutside; outside-click and Escape still dismiss."""
    run_node(r"""
React.useState=()=>[null,()=>{}];React.useEffect=()=>{};
React.useRef=value=>({current:value});
HermesSDK.Popover='popover';HermesSDK.PopoverTrigger='popover-trigger';
HermesSDK.PopoverContent='popover-content';
const popoverClosed = [];
const onPopoverOpenChange = (open) => { if (!open) popoverClosed.push(true); };
const tree = context.DesktopTalkPresentation({
  popoverOpen: true, onPopoverOpenChange, stopTalk() {},
});
const popoverContent = (tree.children || []).filter(Boolean)
  .find(c => c.type === 'popover-content');
assert(popoverContent, 'PopoverContent must be rendered when popoverOpen is true');
assert.equal(typeof popoverContent.props.onFocusOutside, 'function',
  'onFocusOutside must be wired');
const fakeEvent = { preventDefault() { this.defaultPrevented = true; } };
popoverContent.props.onFocusOutside(fakeEvent);
assert.equal(fakeEvent.defaultPrevented, true,
  'onFocusOutside must call preventDefault to block Radix focus-outside dismissal');
assert.equal(popoverClosed.length, 0,
  'onFocusOutside must not close the popover');
// Only focus-outside is blocked. Radix's pointer and keyboard dismissal paths
// stay unwired, so outside click and Escape still close the popover.
for (const blocked of ['onInteractOutside','onPointerDownOutside','onEscapeKeyDown']) {
  assert.equal(popoverContent.props[blocked], undefined,
    blocked + ' must stay unwired so outside click and Escape still dismiss');
}
assert.equal(calls.length, 0);
assert.equal(acquires, 0);
""")


def test_refused_plugin_request_names_route_and_code_in_the_console():
    """Plugin REST rides IPC, so the console is the only trace devtools can show."""
    run_node(r"""
(async()=>{
const warnings = [];
context.console = {warn(line){warnings.push(line);}, error(){}, log(){}};
const sdk=createSDK();
host.rest=async()=>{throw new Error("Error invoking remote method 'hermes:api': Error: 503: " +
  JSON.stringify({detail:{code:'context_unavailable',message:'fixture-private-token'}}));};
await assert.rejects(sdk.fetchJSON('/api/plugins/hermes-talk/targets',{method:'POST',body:'{}'}),
  /^Error: 503:/);
assert.deepEqual(warnings,
  ['[hermes-talk] POST /targets failed: 503 context_unavailable']);
host.rest=async()=>{throw new Error("Error invoking remote method 'hermes:api': Error: " +
  "401: fixture-private-token");};
await assert.rejects(sdk.fetchJSON('/api/plugins/hermes-talk/status'));
assert.equal(warnings[1],'[hermes-talk] GET /status failed: 401');
assert(!warnings.join(' ').includes('fixture-private-token'),
  'the refusal free text stays out of the console');
})().catch(e=>{console.error(e);process.exitCode=1;});
""")


def test_recipients_explain_a_host_without_task_context_instead_of_failing():
    run_node(r"""
let refreshes = 0;
const view = (supported, recipientError) => context.DesktopTalkView({
  status:{configured:true,source:'subscription',taskContinuity:{supported}},ready:true,
  tasks:[],transcript:[],results:{},startTalk(){},refresh(){},
  setRecipient(){},refreshRecipients(){refreshes++;},recipientError,
});
function nodes(node) {
  if (!node || typeof node !== 'object') return [];
  return [node,...(node.children||[]).flat(Infinity).flatMap(nodes)];
}
function text(node) {
  if (typeof node === 'string') return node;
  return (node?.children||[]).flat(Infinity).map(text).join(' ');
}
const refused = '503: ' + JSON.stringify({detail:{code:'context_unavailable',message:'x'}});
const stock = view(false, refused);
assert(text(stock).includes('Other recipients need task context support on this Hermes host.'));
assert(!text(stock).includes('The recipient could not be loaded'),
  'an expected refusal on this host is not an error');
const refresh = nodes(stock).find(node=>node.type==='button' && text(node)==='Refresh recipients');
assert.equal(refresh.props.disabled,true);
const bound = view(true, refused);
assert(!text(bound).includes('Other recipients need task context support'));
assert(text(bound).includes('The recipient could not be loaded. Refresh the list and try ' +
  'again. Error code: 503 context_unavailable'));
""", source="ui/desktop-view.js")
