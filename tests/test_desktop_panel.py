"""Exercise panel interactions against its state-owner callback contract."""

from pathlib import Path
from subprocess import run

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const calls = [];
const React = {createElement(type, props, ...children) {return {type, props, children};}};
const context = vm.createContext({React,HermesSDK:{Button:'button',Input:'input'},
  navigator:{mediaDevices:{getUserMedia(){throw Error('Unexpected microphone capture');}}}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8')
  .replace(/export function /g,'function '),context);
const owner = {connectionId:'host-a',profile:'profile-a',sessionId:'pane-a',
  storedSessionId:'task-a'};
const readOnly = {recipient_id:'history-a',app:'codex_desktop',task_id:'task-b',host_id:'host-b',
  title:'Duplicate title',read_only:true,proven_control:'none',operations:['history','status']};
const writable = {recipient_id:'live-a',app:'claude_code',task_id:'task-c',host_id:'host-c',
  title:'Duplicate title',read_only:false,proven_control:'peer_ipc',send_agent_message:'direct',
  operations:['send_agent_message']};
let props = {status:{source:'subscription'},ready:true,active:false,voiceOwner:owner,
  tasks:[{target_id:'owner',label:'Owner task'}],selectedTask:'owner',typed:'',attachments:[],
  recipients:[readOnly,writable],selectedRecipient:'',recipientOperation:'message',
  canSendTyped:true,inputCapabilities:{operations:['message','start_worker','cancel','steer','approval']},
  setTyped(value){props.typed=value;calls.push(['draft',value]);},
  setRecipient(value){props.selectedRecipient=value;calls.push(['recipient',value]);},
  setRecipientQuery(value){props.recipientQuery=value;},
  setRecipientOperation(value){props.recipientOperation=value;calls.push(['operation',value]);},
  readRecipient(){calls.push(['read',props.selectedRecipient]);},
  startTalk(){calls.push(['start']);},stopTalk(){calls.push(['stop']);},
  switchTarget(value){calls.push(['owner',value]);},
  sendTyped(){calls.push(['send',props.typed,props.selectedRecipient,props.recipientOperation,
    props.attachments.slice()]);},
  refresh(){},refreshCatalog(){},setVoice(){},saveUpdatePreference(){},
};
function render(next={}) {props={...props,...next};return context.DesktopTalkView(props);}
function nodes(node) {
  if (!node || typeof node !== 'object') return [];
  return [node,...(node.children||[]).flat(Infinity).flatMap(nodes)];
}
function text(node) {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (node?.type==='style') return '';
  return (node?.children||[]).flat(Infinity).map(text).join(' ');
}
function byLabel(tree,label) {
  const node=nodes(tree).find(node=>node.props?.['aria-label']===label);
  assert(node,'Missing '+label);return node;
}
function button(tree,label) {
  const node=nodes(tree).find(node=>node.type==='button'&&text(node)===label);
  assert(node,'Missing button '+label);return node;
}
function submit(tree) {
  let stopped=false,prevented=false;
  byLabel(tree,'Typed message').props.onSubmit({preventDefault(){prevented=true;},
    stopPropagation(){stopped=true;}});
  assert(stopped&&prevented,'nested host composer must not submit');
}
"""


def run_panel(script):
    result = run(
        ["node", "-e", HARNESS + script, str(ROOT / "ui/desktop-view.js")],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_typed_edit_and_explicit_send_work_without_connecting_audio():
    run_panel(r"""
let tree=render();
assert(text(tree).includes('Microphone off'));
byLabel(tree,'Message Hermes').props.onChange({target:{value:'Keep this draft'}});
assert.deepEqual(calls,[['draft','Keep this draft']]);
tree=render();
assert.equal(button(tree,'Send').props.disabled,false);
submit(tree);
assert.deepEqual(calls[1],['send','Keep this draft','','message',[]]);
assert.equal(calls.filter(call=>call[0]==='start').length,0);
tree=render({sending:true});submit(tree);
assert.equal(calls.filter(call=>call[0]==='send').length,1);
assert.equal(byLabel(tree,'Message Hermes').props.value,'Keep this draft');
""")


def test_recipient_search_and_actions_preserve_exact_identity_and_voice_owner():
    run_panel(r"""
let tree=render();
const options=nodes(byLabel(tree,'Addressed recipient')).filter(node=>node.type==='option');
assert(text(options.find(node=>node.props.value==='history-a'))
  .includes('host-b · task-b · history-a'));
assert(text(options.find(node=>node.props.value==='live-a')).includes('host-c · task-c · live-a'));
byLabel(tree,'Find an app or task').props.onChange({target:{value:'host-c'}});
tree=render();
assert.equal(nodes(tree).filter(node=>node.type==='option'&&node.props.value==='history-a').length,0);
const picker=nodes(tree).find(node=>node.type==='select'&&
  node.props['aria-label']==='Addressed recipient');
picker.props.onChange({target:{value:'live-a'}});
tree=render({typed:'Message exact task'});submit(tree);
assert.deepEqual(calls.at(-1).slice(0,4),['send','Message exact task','live-a','message']);
assert(text(tree).includes('Voice owner:  Owner task'));
assert.equal(calls.filter(call=>call[0]==='owner'||call[0]==='start').length,0);
assert.equal(button(tree,'Send').props.disabled,false);
tree=render({selectedRecipient:'history-a',recipientQuery:''});
assert(text(tree).includes('Read-only history'));
assert.equal(button(tree,'Send').props.disabled,true);
submit(tree);
assert.equal(calls.filter(call=>call[0]==='send').length,1);
byLabel(tree,'Recipient action').props.onChange({target:{value:'read'}});
tree=render();button(tree,'Read conversation').props.onClick();
assert.deepEqual(calls.at(-1),['read','history-a']);
assert.equal(button(tree,'Send').props.disabled,true);
""")


def test_unavailable_recipient_and_operations_never_silently_fall_back():
    run_panel(r"""
let tree=render({selectedRecipient:'missing',typed:'Do this'});
assert(text(tree).includes('Recipient unavailable · missing'));
assert.equal(button(tree,'Send').props.disabled,true);submit(tree);assert.equal(calls.length,0);
tree=render({selectedRecipient:'live-a',recipients:[{...writable,available:false}]});
assert.equal(button(tree,'Send').props.disabled,true);
tree=render({recipients:[readOnly,writable],recipientOperation:'start_worker',
  inputCapabilities:{operations:[]}});
assert.equal(button(tree,'Send').props.disabled,true);
tree=render({inputCapabilities:{operations:['start_worker']}});
assert(text(tree).includes('Starts a new worker on this task.'));
assert.equal(nodes(tree).filter(node=>node.type==='p'&&
  text(node).includes('host-c · task-c · live-a')).length,0,
  'start_worker is not addressed to a recipient');
submit(tree);
assert.equal(calls.at(-1)[3],'start_worker');
tree=render({recipientOperation:'steer'});
assert.equal(button(tree,'Send').props.disabled,true);
assert.equal(calls.filter(call=>call[0]==='send').length,1);
""")


def test_history_requires_the_current_full_identity_and_does_not_grant_send():
    run_panel(r"""
const history={...readOnly,messages:[{id:'m',role:'assistant',text:'Exact history text'}],
  observed_at:'2026-09-14T10:00:00Z',source:{modified_at:'2026-09-14T09:59:00Z'},truncated:true};
let tree=render({selectedRecipient:'history-a',recipientHistory:history,typed:'No write grant'});
assert(text(byLabel(tree,'Recipient history')).includes('Exact history text'));
assert(text(tree).includes('2026-09-14T10:00:00Z'));
assert.equal(button(tree,'Send').props.disabled,true);
tree=render({recipientHistory:{...history,host_id:'other-host'}});
assert(!text(tree).includes('Exact history text'));
tree=render({recipientHistory:history,selectedRecipient:'live-a'});
assert(!text(tree).includes('Exact history text'));assert.equal(calls.length,0);
""")


def test_refresh_recipients_is_explicit_and_voice_owner_cannot_switch_in_panel():
    run_panel(r"""
let tree=render({active:true,returnDepth:1,
  refreshRecipients(){calls.push(['refresh-recipients']);}});
assert.equal(calls.length,0);
button(tree,'Refresh recipients').props.onClick();
assert.deepEqual(calls,[['refresh-recipients']]);
const ownerSelect=nodes(tree).find(node=>node.type==='select'&&node.props.value==='owner');
assert.equal(ownerSelect.props.disabled,true);
assert.equal(button(tree,'Return to previous conversation').props.disabled,true);
tree=render({recipientLoading:true});
assert.equal(button(tree,'Refreshing recipients…').props.disabled,true);
tree=render({voiceOwner:undefined});
assert.equal(Boolean(button(tree,'Return to previous conversation').props.disabled),false);
""")


def test_attachments_paste_drop_and_selection_stay_local_until_send():
    run_panel(r"""
const files=[{name:'diagram.png',size:12,type:'image/png'}];
const added=[];
let tree=render({attachmentsSupported:true,addAttachments(selected){
  added.push(...selected);props.attachments=selected.map((file,index)=>({...file,id:'file-'+index}));
},removeAttachment(id){props.attachments=props.attachments.filter(file=>file.id!==id);}});
let prevented=0;
byLabel(tree,'Message Hermes').props.onPaste({clipboardData:{files:[]},
  preventDefault(){prevented++;}});
assert.equal(prevented,0);
byLabel(tree,'Message Hermes').props.onPaste({clipboardData:{files},
  preventDefault(){prevented++;}});
assert.equal(prevented,1);assert.equal(added[0],files[0]);assert.equal(calls.length,0);
tree=render();assert(text(tree).includes('diagram.png'));
// Only the operation whose dispatch reaches a child can carry files; an owner
// message keeps them local and says so instead of offering a send that refuses.
assert.equal(button(tree,'Send').props.disabled,true);
assert(text(tree).includes('Only a worker start can carry attachments'));
tree=render({recipientOperation:'start_worker'});
assert.equal(button(tree,'Send').props.disabled,false);
assert(!text(tree).includes('Only a worker start can carry attachments'));
byLabel(tree,'Typed message').props.onDrop({
  dataTransfer:{files:[],getData(){throw Error('URL read');}},
  preventDefault(){prevented++;},stopPropagation(){}});
assert.equal(added.length,1);
byLabel(tree,'Typed message').props.onDrop({dataTransfer:{files},
  preventDefault(){},stopPropagation(){}});
assert.equal(added.length,2);assert.equal(calls.length,0);
const event={target:{files,value:'browser selection'}};
byLabel(tree,'Attach files').props.onChange(event);
assert.equal(event.target.value,'');assert.equal(added.length,3);
tree=render();submit(tree);
assert.equal(calls.at(-1)[0],'send');assert.equal(calls.at(-1)[4][0].name,'diagram.png');
tree=render({attachmentsSupported:false});
assert.equal(button(tree,'Send').props.disabled,true);
assert(text(tree).includes('Files remain local'));
button(tree,'Remove diagram.png').props.onClick();
assert.equal(props.attachments.length,0);
tree=render({attachments:[{...files[0],id:'bad-preview',previewUrl:'https://external.example/image'}]});
assert.equal(nodes(tree).filter(node=>node.type==='img').length,0);
""")


def test_collapse_mute_sleep_stop_and_appearance_are_independent():
    run_panel(r"""
const file={id:'attachment',name:'notes.txt',size:1,type:'text/plain'};
let tree=render({active:true,typed:'Retain me',attachments:[file],
  appearance:{skin:'system',animate:false},
  collapse(){calls.push(['collapse']);},setMuted(value){props.muted=value;calls.push(['mute',value]);},
  setSleeping(value){props.sleeping=value;calls.push(['sleep',value]);},
  setAppearance(value){props.appearance=value;}});
for(const label of ['Collapse','Mute microphone','Sleep','Stop talking'])
  assert.equal(button(tree,label).props.type,'button');
button(tree,'Collapse').props.onClick();tree=render();
assert.equal(byLabel(tree,'Message Hermes').props.value,'Retain me');
assert(text(tree).includes('notes.txt'));assert.deepEqual(calls,[['collapse']]);
button(tree,'Mute microphone').props.onClick();tree=render();
assert.equal(tree.props['data-audio'],'muted');
button(tree,'Unmute microphone').props.onClick();
button(tree,'Sleep').props.onClick();tree=render();
assert.equal(tree.props['data-audio'],'sleeping');
assert.equal(button(tree,'Mute microphone').props.disabled,true);
button(tree,'Wake').props.onClick();tree=render();
byLabel(tree,'Appearance').props.onChange({target:{value:'contrast'}});tree=render();
assert.equal(tree.props['data-skin'],'contrast');
nodes(tree).find(node=>node.type==='input'&&node.props.type==='checkbox')
  .props.onChange({target:{checked:true}});tree=render();
assert.equal(tree.props['data-animate'],'true');
button(tree,'Stop talking').props.onClick();
assert.equal(calls.filter(call=>call[0]==='stop').length,1);
assert.equal(calls.filter(call=>call[0]==='start'||call[0]==='send').length,0);
""")


def test_job_progress_is_bounded_and_controls_keep_exact_action_and_request():
    run_panel(r"""
const approval={request_id:'approval-a',description:'Allow current operation',
  choices:['once','deny']};
const job={run_id:42,action_id:'action-a',status:'running',goal:'Current job',
  approval:{actionable:true,approvals:[approval]},steering:{supported:true}};
const jobs=Array.from({length:9},(_,i)=>({run_id:i,action_id:'a'+i,
  status:'running',goal:'Other '+i}));
let tree=render({active:true,taskState:{jobs:[...jobs,job],events:{events:[
  {run_id:42,action_id:'foreign',observed_index:99,label:'Foreign progress'},
  {run_id:42,action_id:'action-a',observed_index:2,label:'Tests running'},
  {run_id:42,action_id:'action-a',observed_index:1,label:'Reading files'},
]}},cancelJob(id){calls.push(['cancel',id]);},steerJob(id){calls.push(['steer',id]);},
answerApproval(answer){calls.push(['approval',answer]);}});
const cards=nodes(tree).filter(node=>node.type==='article'&&
  node.props.className.includes('htd-job'));
assert.equal(cards.length,8);assert(text(tree).includes('Showing  8  of  10  jobs.'));
assert(text(cards[0]).includes('Current job'));
assert(text(tree).includes('Tests running'));assert(!text(tree).includes('Foreign progress'));
assert(!text(tree).includes('Reading files'));assert(!text(tree).includes('%'));
button(cards[0],'Steer owned job').props.onClick();assert.deepEqual(calls.at(-1),['steer',42]);
button(cards[0],'Cancel job').props.onClick();assert.deepEqual(calls.at(-1),['cancel',42]);
button(cards[0],'Allow once').props.onClick();
assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1))),['approval',
  {run_id:42,action_id:'action-a',request_id:'approval-a',choice:'once'}]);
tree=render({pendingActions:{'approval:approval-a':{pending:true}}});
assert.equal(button(tree,'Allow once').props.disabled,true);
assert(text(tree).includes('Submitting approval'));
tree=render({pendingActions:{'approval:approval-a':{error:'private-token'}}});
assert.equal(button(tree,'Allow once').props.disabled,false);
assert(text(tree).includes('The approval could not be submitted'));
assert(!text(tree).includes('private-token'));
tree=render({taskState:{jobs:[{...job,approval:{...job.approval,actionable:false}}]}});
assert.equal(button(tree,'Allow once').props.disabled,true);
""")


def test_full_result_open_is_silent_and_replay_never_reruns_work():
    run_panel(r"""
const job={run_id:7,action_id:'accepted-action',goal:'Finished job',status:'completed',
  result_available:true,presentation:{event_id:'event-result',state:'unknown',result_ready:true,
    context_submitted:true,playback_finished:false,replay_eligible:true}};
let tree=render({taskState:{jobs:[job]},showResult(id){calls.push(['result',id]);},
  replayResult(id){calls.push(['replay',id]);}});
assert.equal(button(tree,'Show result').props.disabled,false);
button(tree,'Show result').props.onClick();assert.deepEqual(calls,[['result',7]]);
assert(text(tree).includes('Summary audio state unknown'));
assert(!text(tree).includes('playback finished'));
assert.equal(button(tree,'Replay summary').props.disabled,true);
const output='Full output beginning. '+('detail '.repeat(1000))+'Full output ending.';
tree=render({active:true,results:{7:{output,status:'completed'}}});
assert(text(byLabel(tree,'Task results')).includes(output));
assert(text(byLabel(tree,'Task results')).includes('Job  7  ·  Finished job'));
button(tree,'Replay summary').props.onClick();
assert.deepEqual(calls,[['result',7],['replay','event-result']]);
tree=render({taskState:{jobs:[{...job,presentation:{...job.presentation,state:'playback_finished',
  playback_started:true,playback_finished:true}}]}});
assert(text(tree).includes('Summary playback finished'));
assert.equal(calls.filter(call=>call[0]==='send'||call[0]==='start').length,0);
tree=render({taskState:{jobs:[job]},results:{},resultsReadable:false});
assert.equal(button(tree,'Show result').props.disabled,true);
assert(text(tree).includes('Connect this conversation to open the stored result.'));
assert.deepEqual(calls,[['result',7],['replay','event-result']]);
""")


def test_bounded_results_keep_the_opened_visible_job_in_view():
    run_panel(r"""
const results=Object.fromEntries(Array.from({length:12},(_,i)=>[i,{output:'Output '+i}]));
const tree=render({taskState:{jobs:[{run_id:0,goal:'Visible oldest job',status:'completed',
  result_available:true}]},results});
const resultView=byLabel(tree,'Task results');
assert(text(resultView).includes('Output 0'));
assert(text(resultView).includes('Visible oldest job'));
assert.equal(nodes(resultView).filter(node=>node.type==='article').length,8);
""")


def test_job_controls_require_connection_capabilities_and_replay_support():
    run_panel(r"""
const job={run_id:8,action_id:'action-a',status:'running',steering:{supported:true},
  approval:{actionable:true,approvals:[{request_id:'request-a',choices:['once','deny']}]}};
const result={run_id:9,action_id:'action-b',status:'completed',result_available:true,
  presentation:{event_id:'result-a',state:'unknown',replay_eligible:true}};
let tree=render({active:true,selectedJob:8,recipientOperation:'steer',typed:'Correction',
  inputCapabilities:{operations:[]},replaySupported:false,taskState:{jobs:[job,result]},
  cancelJob(){},steerJob(){},answerApproval(){},replayResult(){}});
for(const label of ['Cancel job','Steer owned job','Allow once','Deny','Replay summary','Send'])
  assert.equal(button(tree,label).props.disabled,true,label+' must be unavailable');
assert(text(tree).includes('Summary replay is unavailable on this connection.'));
assert(text(tree).includes('Approval controls are unavailable for this request.'));
tree=render({inputCapabilities:{operations:['cancel','steer','approval']},replaySupported:true});
for(const label of ['Cancel job','Steer owned job','Allow once','Deny','Replay summary','Send'])
  assert.equal(Boolean(button(tree,label).props.disabled),false,label+' must be available');
assert(!text(tree).includes('Summary replay is unavailable on this connection.'));
""")


def test_audio_status_uses_measured_output_and_preserves_unknown_states():
    run_panel(r"""
function statusText(tree) {return text(nodes(tree).find(node=>node.props?.role==='status'));}
let tree=render({active:true,live:'Unknown transport observation'});
assert(statusText(tree).includes('Connected'));assert(!statusText(tree).includes('Listening'));
tree=render({live:'Listening…'});assert(statusText(tree).includes('Listening…'));
tree=render({live:'Thinking…'});assert(statusText(tree).includes('Thinking…'));
tree=render({audioActivity:{output:true}});assert(statusText(tree).includes('Audio playing'));
assert(!statusText(tree).includes('Thinking'));assert(!statusText(tree).includes('heard'));
tree=render({muted:true});assert(statusText(tree).includes('Microphone muted'));
assert(statusText(tree).includes('Audio playing'));
tree=render({audioActivity:{output:false},live:'Listening…'});
assert(!statusText(tree).includes('Listening'));assert(!statusText(tree).includes('Audio playing'));
tree=render({muted:false,audioActivity:{input:true}});
assert(statusText(tree).includes('Microphone audio detected'));
tree=render({sleeping:true,audioActivity:{output:true}});
assert(statusText(tree).includes('Sleeping'));assert(!statusText(tree).includes('Audio playing'));
""")
