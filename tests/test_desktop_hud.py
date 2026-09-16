"""Native HUD behavior exercised against freshly rendered desktop sources."""

from __future__ import annotations

import runpy
from pathlib import Path
from subprocess import run

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = runpy.run_path(str(ROOT / "scripts/build_ui.py"))


@pytest.fixture
def desktop_source(tmp_path):
    source = tmp_path / "plugin.js"
    source.write_text(
        BUILDER["render_bundles"](ROOT)[ROOT / "desktop/plugin.js"], encoding="utf-8"
    )
    return source


HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const {File} = require('buffer');
const calls = [], registered = [], voiceRenders = [], opens = [], disposers = [];
let acquires = 0, releases = 0, stops = 0, captures = 0, prepared = 0;
let focused = null, hookIndex = 0;
const instances = new Map(), effects = [], mounts = new Map();
const same = (before, next) => before && next && before.length === next.length &&
  before.every((value, index) => value === next[index]);
const React = {
  Fragment: 'fragment',
  createElement(type, props, ...children) { return {type, props:props || {}, children}; },
  useState(initial) {
    const index = hookIndex++, state = focused.slots;
    if (!(index in state)) {
      state[index] = {value:typeof initial === 'function' ? initial() : initial};
    }
    return [state[index].value, value => {
      state[index].value = typeof value === 'function' ? value(state[index].value) : value;
    }];
  },
  useRef(value) {
    const index = hookIndex++;
    return focused.slots[index] || (focused.slots[index] = {current:value});
  },
  useMemo(factory, deps) {
    const index = hookIndex++, previous = focused.slots[index];
    if (!previous || !same(previous.deps, deps)) {
      focused.slots[index] = {value:factory(), deps};
    }
    return focused.slots[index].value;
  },
  useCallback(callback, deps) { return React.useMemo(() => callback, deps); },
  useEffect(callback, deps) {
    const index = hookIndex++, state = focused.slots, previous = state[index];
    if (!previous || !same(previous.deps, deps)) {
      const effect = {deps, cleanup:previous?.cleanup};
      state[index] = effect;
      effects.push(() => {
        effect.cleanup?.();
        effect.cleanup = callback();
      });
    }
  },
};
function render(root, name = 'root') {
  const visited = new Set();
  function visit(node, path) {
    if (Array.isArray(node)) return node.map((child, index) => visit(child, path + '/' + index));
    if (!node || typeof node !== 'object') return node;
    if (typeof node.type === 'function') {
      let instance = instances.get(path);
      if (!instance || instance.type !== node.type) {
        instance?.slots.forEach(slot => slot?.cleanup?.());
        instance = {type:node.type, props:node.props, slots:[]};
        instances.set(path, instance);
        mounts.set(node.type, (mounts.get(node.type) || 0) + 1);
      }
      instance.props = node.props;
      visited.add(path);
      focused = instance; hookIndex = 0;
      const child = node.type(node.props);
      focused = null;
      return visit(child, path + '/render');
    }
    return {...node, children:node.children.map((child, index) =>
      visit(child, path + '/' + index))};
  }
  const tree = visit(root, name);
  for (const [path, instance] of instances) {
    if ((path === name || path.startsWith(name + '/')) && !visited.has(path)) {
      instance.slots.forEach(slot => slot?.cleanup?.()); instances.delete(path);
    }
  }
  return tree;
}
function flushEffects() { while(effects.length) effects.shift()(); }
function nodes(node) {
  if (Array.isArray(node)) return node.flatMap(nodes);
  if (!node || typeof node !== 'object') return [];
  return [node,...node.children.flatMap(nodes)];
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join(' ');
  if (!node || typeof node !== 'object') return String(node || '');
  return node.children.map(text).join(' ');
}
const find = (tree, predicate) => nodes(tree).find(predicate);
const button = (tree, label) => find(tree, node => node.type === 'button' && text(node) === label);
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
async function waitFor(predicate) {
  for(let i=0;i<100 && !predicate();i++) await tick();
  assert(predicate(), 'Expected lifecycle operation did not occur');
}
const owner = {connectionId:'connection-a', profile:'profile-a', sessionId:'runtime-a',
  storedSessionId:'stored-a'};
const lifetime = new AbortController(), leaseLifetime = new AbortController();
let controller = {capabilities:{microphoneLease:1,pinnedRest:1,prepareSession:1}, owner,
  signal:lifetime.signal,
  async prepareSession() { prepared++; return this.owner; },
  async acquire() { acquires++; return {signal:leaseLifetime.signal, release(){releases++;}}; },
  stop() { stops++; lifetime.abort(); },
};
const HermesSDK = {Button:'button',Input:'input',Popover:'popover',
  PopoverTrigger:'popover-trigger',PopoverContent:'popover-content',
  useComposerVoiceController:()=>controller};
const storage = new Map();
const host = {
  voice: {available:true, register(render){voiceRenders.push(render);},
    async open(owner){opens.push(owner);}},
  async rest(path, options) {calls.push({path,options}); return {ok:true};},
  register(entry) {registered.push(entry);}, onDispose(dispose){disposers.push(dispose);},
};
const context = vm.createContext({React,HermesSDK,Headers,DOMException,AbortController,
  console, URL, File, document:{title:'Talk'},
  navigator:{mediaDevices:{async getUserMedia(){
    captures++; throw Error('Audio is not authorized');}}},
  window:{setTimeout,clearTimeout,setInterval,clearInterval,crypto:require('crypto').webcrypto,
    location:{href:'app://hermes'},addEventListener(){},removeEventListener(){},
    sessionStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},
    localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)}}});
const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace(/^import .* from .*\r?\n/gm, '')
  .replace(/export function /g, 'function ')
  .replace('export default {', 'globalThis.plugin = {');
vm.runInContext(source, context);
"""


def run_hud(desktop_source, script):
    result = run(
        ["node", "-e", HARNESS + "\n(async()=>{\n" + script + r"""
})().catch(error=>{console.error(error);process.exitCode=1;});
""", str(desktop_source)],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_composer_opens_registered_hud_before_any_audio(desktop_source):
    run_hud(desktop_source, r"""
context.plugin.register(host);
assert.equal(voiceRenders.length,1);
assert.equal(registered.length,2);
assert.equal(captures,0); assert.equal(acquires,0); assert.equal(calls.length,0);
let tree=render(registered[0].render(),'composer'); flushEffects();
const launch=button(tree,'Talk');
assert.equal(launch.props.type,'button');
launch.props.onClick(); launch.props.onClick();
await waitFor(()=>opens.length===1);
assert.equal(prepared,1,'a repeated click cannot open another pending runtime');
assert.deepEqual(JSON.parse(JSON.stringify(opens[0])),owner);
assert(Object.isFrozen(opens[0]));
assert.equal(acquires,0); assert.equal(captures,0); assert.equal(calls.length,0);
render(null,'composer');
assert.equal(stops,0,'navigating away from the launcher does not stop the native HUD');
""")


@pytest.mark.parametrize("changed", ["connectionId", "profile", "storedSessionId"])
def test_launcher_rejects_preparation_for_another_owner(desktop_source, changed):
    run_hud(desktop_source, r"""
context.plugin.register(host);
controller.prepareSession=async()=>({...owner,[FIELD]:'other'});
const tree=render(registered[0].render(),'composer'); flushEffects();
button(tree,'Talk').props.onClick(); await tick();
assert.equal(opens.length,0); assert.equal(acquires,0);
assert(find(render(registered[0].render(),'composer'), node=>node.props.role==='alert'));
""".replace("FIELD", repr(changed)))


def test_navigation_during_preparation_cannot_open_a_late_hud(desktop_source):
    run_hud(desktop_source, r"""
context.plugin.register(host);
let finish;
controller.prepareSession=()=>new Promise(resolve=>{finish=resolve;});
const tree=render(registered[0].render(),'composer'); flushEffects();
button(tree,'Talk').props.onClick(); await waitFor(()=>finish);
render(null,'composer'); finish(owner); await tick();
assert.equal(opens.length,0); assert.equal(acquires,0); assert.equal(stops,0);
""")


def test_topbar_matches_runtime_as_well_as_stored_owner(desktop_source):
    run_hud(desktop_source, r"""
context.plugin.register(host);
render(registered[0].render(),'composer-a'); flushEffects();
controller={...controller,owner:{...owner,sessionId:'runtime-b'}};
render(registered[0].render(),'composer-b'); flushEffects();
HermesSDK.host={state:{focusedSessionOwner:{get:()=>owner},
  focusedSessionId:{get:()=>owner.sessionId},
  focusedStoredSessionId:{get:()=>owner.storedSessionId}},notify(){assert.fail('owner is exact');}};
registered[1].data.onSelect();
await waitFor(()=>opens.length===1);
assert.equal(opens[0].sessionId,'runtime-a');
assert.equal(acquires,0); assert.equal(captures,0);
render(null,'composer-a'); render(null,'composer-b');
""")


def test_collapse_retains_actual_talk_page_draft_and_local_attachment(desktop_source):
    run_hud(desktop_source, r"""
context.plugin.register(host);
const runtime=voiceRenders[0]({controller});
let tree=render(runtime,'hud');
const page=[...instances.values()].find(instance=>instance.type.name==='TalkPage');
assert(page);
const initialPage=page.type;
find(tree,node=>node.type==='textarea').props.onChange({target:{value:'Keep my draft'}});
find(tree,node=>node.props.type==='file').props.onChange({target:{
  files:[new File(['local only'],'notes.txt',{type:'text/plain'})],value:'notes.txt'}});
tree=render(runtime,'hud');
assert.equal(find(tree,node=>node.type==='textarea').props.value,'Keep my draft');
assert(text(tree).includes('notes.txt'));
button(tree,'Collapse').props.onClick();
tree=render(runtime,'hud');
assert(!find(tree,node=>node.type==='textarea'));
const toggle=button(tree,'Talk');
assert.equal(toggle.props['aria-expanded'],false);
assert.equal(toggle.props.type,'button','native button supports keyboard and touch activation');
assert.equal(toggle.props['data-hud-drag'],'move','the host drags the window from this button');
assert(toggle.props['aria-describedby']);
assert(text(tree).includes('Microphone off'));
assert(!find(tree,node=>node.props.className==='ht-desktop-view'),
  'hover and focus status must not mount the expanded view');
controller={...controller,owner:{...owner}};
toggle.props.onClick({pointerType:'touch'});
tree=render(voiceRenders[0]({controller}),'hud');
assert.equal(find(tree,node=>node.type==='textarea').props.value,'Keep my draft');
assert(text(tree).includes('notes.txt'));
assert.equal([...instances.values()].find(instance=>instance.type.name==='TalkPage').type,initialPage);
assert.equal(mounts.get(initialPage),1);
assert.equal(acquires,0); assert.equal(captures,0); assert.equal(calls.length,0);
let prevented=0;
find(tree,node=>node.props.className==='ht-hud').props.onKeyDown({key:'Escape',preventDefault(){prevented++;}});
assert.equal(prevented,1);
assert(!find(render(runtime,'hud'),node=>node.type==='textarea'));
""")


def test_hover_status_uses_exact_recipient_and_observed_work(desktop_source):
    run_hud(desktop_source, r"""
const recipient={recipient_id:'codex:b',app:'codex_desktop',title:'Same title',
  host_id:'host-b',task_id:'task-b'};
let expanded=false;
const props={expanded,setExpanded:value=>{expanded=value;},active:true,
  selectedRecipient:recipient.recipient_id,recipients:[{...recipient,recipient_id:'codex:a',
    host_id:'host-a',task_id:'task-a'},recipient],taskState:{jobs:[
      {status:'running'},{status:'waiting_approval'},{status:'completed'},{status:'unrecognized'}]}};
let tree=render(React.createElement(context.TalkHudPresentation,props));
const toggle=button(tree,'Talk');
assert(toggle.props['aria-label'].includes('task-b'));
assert(toggle.props['aria-label'].includes('host-b'));
assert(!toggle.props['aria-label'].includes('task-a'));
assert(text(tree).includes('Active work: 2'));
assert(text(tree).includes('Connected'));
assert(!text(tree).includes('Listening'),'connection alone cannot establish detected listening');
tree=render(React.createElement(context.TalkHudPresentation,{...props,sleeping:true}));
assert(text(tree).includes('Sleeping · microphone off'));
assert.equal(captures,0); assert.equal(calls.length,0); assert.equal(expanded,false);
""")


@pytest.mark.parametrize("native_close", [False, True])
def test_active_hud_retains_one_lease_through_collapse_and_navigation(desktop_source, native_close):
    run_hud(desktop_source, r"""
let trackStops=0, offers=0;
const track={enabled:true,stop(){trackStops++;}};
const media={getTracks:()=>[track],getAudioTracks:()=>[track]};
context.navigator.mediaDevices.getUserMedia=async()=>{captures++; return media;};
context.document={title:'Talk',body:{appendChild(){}},
  createElement(){return {style:{},remove(){}};}};
context.fetch=async()=>{offers++; return {ok:true,async text(){return 'answer';}};};
context.RTCPeerConnection=class {
  constructor(){this.connectionState='new';}
  addEventListener(){} addTrack(){}
  createDataChannel(){return {readyState:'connecting',addEventListener(){},close(){}};}
  async createOffer(){return {type:'offer',sdp:'offer'};}
  async setLocalDescription(){} async setRemoteDescription(){}
  close(){this.connectionState='closed';}
};
const target={target_id:'target-a',peer_id:'local',profile:owner.profile,
  session_id:owner.storedSessionId};
host.rest=async(path,options)=>{
  calls.push({path,options});
  if(path==='/status') return {configured:true,voiceMode:'native',source:'api'};
  if(path==='/targets') return {ok:true,targets:[target]};
  if(path==='/session') return {voiceMode:'native',offerUrl:'https://fixture.invalid/offer',
    clientSecret:'fixture',task:{...target,tab_id:options.body.task.tab_id,
      connection_id:'talk-connection-a',generation:1}};
  if(path==='/runs') return {runs:[]};
  return {ok:true,jobs:[]};
};
context.plugin.register(host);
render(registered[0].render(),'composer'); flushEffects();
const nativeController=controller;
const runtime=voiceRenders[0]({controller:nativeController});
render(runtime,'hud'); flushEffects(); await tick();
let tree=render(runtime,'hud'); flushEffects();
assert.equal(captures,0); assert.equal(acquires,0);
assert.equal(button(tree,'Connect').props.disabled,false);
button(tree,'Connect').props.onClick();
await waitFor(()=>offers===1); await tick();
tree=render(runtime,'hud'); flushEffects();
tree=render(runtime,'hud');
assert.equal(button(tree,'Talk').props['aria-expanded'],false,
  'connecting shrinks the window to the Talk button');
button(tree,'Talk').props.onClick();
tree=render(runtime,'hud'); flushEffects();
assert(button(tree,'Stop talking'));
assert.equal(captures,1); assert.equal(acquires,1);
button(tree,'Mute microphone').props.onClick();
assert.equal(track.enabled,false);
tree=render(runtime,'hud');
button(tree,'Sleep').props.onClick();
tree=render(runtime,'hud');
assert(text(tree).includes('Sleeping'));
button(tree,'Wake').props.onClick();
assert.equal(track.enabled,false,'wake preserves the selected microphone mute');
tree=render(runtime,'hud');
button(tree,'Unmute microphone').props.onClick();
assert.equal(track.enabled,true);
button(tree,'Collapse').props.onClick();
tree=render(runtime,'hud');
controller={...nativeController,owner:{...owner,sessionId:'navigated-runtime',storedSessionId:'navigated-task'}};
render(registered[0].render(),'composer'); flushEffects();
render(null,'composer');
assert.equal(trackStops,0); assert.equal(releases,0); assert.equal(stops,0);
button(tree,'Talk').props.onClick({pointerType:'touch'});
tree=render(voiceRenders[0]({controller:nativeController}),'hud'); flushEffects();
assert(button(tree,'Stop talking'));
assert.equal(captures,1); assert.equal(acquires,1);
assert.equal(calls.filter(row=>row.path==='/session').length,1);
if(NATIVE_CLOSE) lifetime.abort();
else button(tree,'Stop talking').props.onClick();
await tick();
assert.equal(trackStops,1); assert.equal(releases,1);
assert.equal(stops,NATIVE_CLOSE?0:1);
assert(lifetime.signal.aborted);
render(null,'hud'); await tick();
assert.equal(trackStops,1); assert.equal(releases,1);
assert.equal(calls.filter(row=>row.path==='/close').length,1);
""".replace("NATIVE_CLOSE", str(native_close).lower()))


@pytest.mark.parametrize("field", ["connectionId", "profile", "sessionId", "storedSessionId"])
def test_hud_preparation_is_pinned_to_all_four_owner_fields(desktop_source, field):
    run_hud(desktop_source, r"""
controller.prepareSession=async()=>({...owner,[FIELD]:'changed'});
const sdk=context.createDesktopTalkSDK(host,controller);
assert.equal(sdk.lifetimeSignal,lifetime.signal);
await assert.rejects(sdk.prepareTask({tabId:'tab-a'}),/conversation changed/);
assert.equal(calls.length,0); assert.equal(acquires,0);
""".replace("FIELD", repr(field)))


@pytest.mark.parametrize("status", [401, 403, 503])
def test_plugin_authorization_loss_stops_native_owner(desktop_source, status):
    run_hud(desktop_source, r"""
host.rest=async()=>{
  throw Error("Error invoking remote method 'hermes:api': Error: STATUS: denied");};
const sdk=context.createDesktopTalkSDK(host,controller);
await assert.rejects(sdk.fetchJSON('/api/plugins/hermes-talk/status'),/^Error: STATUS: denied$/);
assert.equal(stops,STATUS===503?0:1);
assert.equal(sdk.lifetimeSignal.aborted,STATUS!==503);
assert.equal(acquires,0);
""".replace("STATUS", str(status)))


def test_stopped_hud_releases_late_microphone_lease(desktop_source):
    run_hud(desktop_source, r"""
let finish;
controller.acquire=()=>new Promise(resolve=>{finish=resolve;});
const sdk=context.createDesktopTalkSDK(host,controller), pending=sdk.acquireMicrophone();
await waitFor(()=>finish); sdk.stopHost();
finish({signal:leaseLifetime.signal,release(){releases++;}});
await assert.rejects(pending,{name:'AbortError'});
assert.equal(releases,1); assert.equal(stops,1); assert.equal(captures,0);
await assert.rejects(sdk.acquireMicrophone(),{name:'AbortError'});
""")


def test_older_host_keeps_explicit_composer_mode(desktop_source):
    run_hud(desktop_source, r"""
delete host.voice;
context.plugin.register(host);
let tree=render(registered[0].render(),'composer');
assert.equal(voiceRenders.length,0);
const popover=find(tree,node=>node.type==='popover');
assert(popover); assert.equal(popover.props.modal,false);
popover.props.onOpenChange(true);
tree=render(registered[0].render(),'composer');
assert(text(tree).includes('Composer mode'));
assert(text(tree).includes('update Hermes Desktop'));
assert.equal(acquires,0); assert.equal(captures,0);
""")


def test_hud_shrinks_to_the_button_when_the_session_connects(desktop_source):
    run_hud(desktop_source, r"""
context.plugin.register(host);
const expands=[];
const props={active:false, expanded:false, setExpanded:value=>expands.push(value),
  appearance:{skin:'system',animate:false}, recipients:[], taskState:{jobs:[]}};
const show=extra=>{
  render(React.createElement(context.TalkHudPresentation,{...props,...extra}),'hud');
  flushEffects();
};
show({});
assert.deepEqual(expands,[],'mounting collapsed does not touch the panel');
show({active:true});
assert.deepEqual(expands,[false],'connecting shrinks the window to the Talk button');
show({active:true});
assert.deepEqual(expands,[false],'staying connected does not collapse again');
""")


def test_hud_keeps_the_panel_open_on_connect_when_the_preference_is_off(desktop_source):
    run_hud(desktop_source, r"""
context.plugin.register(host);
const expands=[];
const props={active:false, expanded:false, setExpanded:value=>expands.push(value),
  appearance:{skin:'system',animate:false,collapseOnConnect:false},
  recipients:[], taskState:{jobs:[]}};
const show=extra=>{
  render(React.createElement(context.TalkHudPresentation,{...props,...extra}),'hud');
  flushEffects();
};
show({});
show({active:true});
assert.deepEqual(expands,[],'connecting leaves the panel alone');
const tree=render(React.createElement(context.TalkHudPresentation,{...props,active:true}),'hud');
button(tree,'Talk').props.onClick();
assert.deepEqual(expands,[true],'the button still opens the panel afterwards');
""")


def test_hud_expands_while_hovered_and_a_click_pins_it(desktop_source):
    run_hud(desktop_source, r"""
context.plugin.register(host);
const expands=[];
const props={active:true, expanded:false, setExpanded:value=>expands.push(value),
  appearance:{skin:'system',animate:false}, recipients:[], taskState:{jobs:[]}};
let tree=render(React.createElement(context.TalkHudPresentation, props),'hud'); flushEffects();
const section=find(tree,node=>node.type==='section');
section.props.onPointerEnter();
assert.deepEqual(expands,[true],'hovering the button opens the panel');
section.props.onPointerLeave({currentTarget:{contains:()=>false}});
assert.deepEqual(expands,[true,false],'leaving closes a hover-opened panel');
section.props.onPointerEnter();
context.document.activeElement={};
section.props.onPointerLeave({currentTarget:{contains:()=>true}});
assert.deepEqual(expands,[true,false,true],'a focused control keeps the panel open');
context.document.activeElement=null;
tree=render(React.createElement(context.TalkHudPresentation,{...props,expanded:true}),'hud');
flushEffects();
button(tree,'Talk').props.onClick();
assert.deepEqual(expands,[true,false,true],'clicking a hover-opened panel pins it');
find(tree,node=>node.type==='section').props.onPointerLeave({currentTarget:{contains:()=>false}});
assert.deepEqual(expands,[true,false,true],'a pinned panel survives the pointer leaving');
button(tree,'Talk').props.onClick();
assert.deepEqual(expands,[true,false,true,false],'a second click collapses a pinned panel');
""")


def test_hud_ignores_hover_when_the_preference_is_off(desktop_source):
    run_hud(desktop_source, r"""
context.plugin.register(host);
const expands=[];
const props={active:true, expanded:false, setExpanded:value=>expands.push(value),
  appearance:{skin:'system',animate:false,hoverExpand:false}, recipients:[], taskState:{jobs:[]}};
const tree=render(React.createElement(context.TalkHudPresentation, props),'hud'); flushEffects();
const section=find(tree,node=>node.type==='section');
section.props.onPointerEnter();
section.props.onPointerLeave({currentTarget:{contains:()=>false}});
assert.deepEqual(expands,[]);
button(tree,'Talk').props.onClick();
assert.deepEqual(expands,[true],'the button still opens the panel');
""")


def test_a_reconnect_keeps_a_pinned_panel_but_closes_a_hover_opened_one(desktop_source):
    run_hud(desktop_source, r"""
context.plugin.register(host);
const expands=[];
const props={active:true, expanded:false, setExpanded:value=>expands.push(value),
  appearance:{skin:'system',animate:false}, recipients:[], taskState:{jobs:[]}};
const show=extra=>{
  const tree=render(React.createElement(context.TalkHudPresentation,{...props,...extra}),'hud');
  flushEffects();
  return tree;
};
let tree=show({});
button(tree,'Talk').props.onClick();
assert.deepEqual(expands,[true],'the operator pins the panel open');
show({expanded:true, active:false});
show({expanded:true, active:true});
assert.deepEqual(expands,[true],'a reconnect leaves a pinned panel alone');
tree=show({expanded:true});
button(tree,'Talk').props.onClick();
assert.deepEqual(expands,[true,false],'a second click collapses and unpins');
tree=show({expanded:false});
find(tree,node=>node.type==='section').props.onPointerEnter();
assert.deepEqual(expands,[true,false,true],'hover opens without pinning');
show({expanded:true, active:false});
show({expanded:true, active:true});
assert.deepEqual(expands,[true,false,true,false],'a reconnect closes a hover-opened panel');
""")
